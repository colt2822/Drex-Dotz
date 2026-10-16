"""Adapter over the existing Revenue Hunter CRM (the single authoritative store).

Original tables are only written through the CRM's own functions (crm.py) so its
invariants hold. Additive `sales_*` tables in the same database hold what the CRM
lacks: recipient addresses, send intents, address-level suppression, worker state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

CRM_DIR = Path(os.environ.get("DOTZ_CRM_DIR", Path.home() / "elyraon-revenue-hunter-20261015"))
if str(CRM_DIR) not in sys.path:
    sys.path.insert(0, str(CRM_DIR))
import crm as _crm  # noqa: E402  (the existing CRM module)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ORIGINAL_TABLES = ("prospects", "drafts", "followups", "events", "replies")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales_contacts(prospect_id TEXT PRIMARY KEY REFERENCES prospects(id), email TEXT NOT NULL,
  source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_contacts_email ON sales_contacts(email);
CREATE TABLE IF NOT EXISTS sales_suppressions(email TEXT PRIMARY KEY, reason TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sales_send_intents(send_id TEXT PRIMARY KEY, prospect_id TEXT NOT NULL REFERENCES prospects(id),
  recipient TEXT NOT NULL, kind TEXT NOT NULL, subject_hash TEXT NOT NULL, body_hash TEXT NOT NULL, status TEXT NOT NULL,
  provider_message_id TEXT, thread_id TEXT, policy_hash TEXT, created_at TEXT NOT NULL, sent_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_one_initial ON sales_send_intents(recipient) WHERE kind='INITIAL_MESSAGE' AND status IN ('INTENT','SENT');
CREATE TABLE IF NOT EXISTS sales_inbound(provider_message_id TEXT PRIMARY KEY, kind TEXT NOT NULL, prospect_id TEXT, detail TEXT, processed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sales_domain_blocklist(domain TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sales_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CrmStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else _crm.DB
        self.c = _crm.connect(self.path)  # existing CRM connect (WAL, row_factory)
        self.c.execute("PRAGMA busy_timeout=10000")
        self.c.executescript(SCHEMA)
        self.c.commit()

    # ---- safety ------------------------------------------------------------
    def backup(self, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"crm-{time.strftime('%Y%m%dT%H%M%S')}.sqlite3"
        out = sqlite3.connect(dest)
        self.c.backup(out)
        out.close()
        return dest

    def original_counts(self) -> dict[str, int]:
        return {t: self.c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ORIGINAL_TABLES}

    def original_fingerprint(self) -> dict[str, str]:
        """Hash of every original row (id-ordered). Appends change it; the prefix check below proves preservation."""
        fp = {}
        for t in ORIGINAL_TABLES:
            h = hashlib.sha256()
            for r in self.c.execute(f"SELECT * FROM {t} ORDER BY 1"):
                h.update(json.dumps(tuple(r), default=str).encode())
            fp[t] = h.hexdigest()
        return fp

    @contextmanager
    def txn(self):
        self.c.execute("BEGIN IMMEDIATE")
        try:
            yield self.c
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise

    # ---- state / kill switch ----------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        r = self.c.execute("SELECT value FROM sales_state WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_state(self, key: str, value: str) -> None:
        self.c.execute("INSERT INTO sales_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                       (key, value, _crm.now()))
        self.c.commit()

    def stopped(self) -> str | None:
        return self.get_state("emergency_stop")

    # ---- contacts / suppression / dedupe ----------------------------------
    @staticmethod
    def normalize(email: str) -> str:
        e = email.strip().casefold()
        local, _, domain = e.partition("@")
        if domain in ("gmail.com", "googlemail.com"):
            local = local.split("+")[0].replace(".", "")
            domain = "gmail.com"
        else:
            local = local.split("+")[0]
        return f"{local}@{domain}"

    def backfill_contacts(self) -> int:
        n = 0
        for r in self.c.execute("SELECT id, channel FROM prospects").fetchall():
            m = EMAIL_RE.search(r["channel"] or "")
            if not m or self.c.execute("SELECT 1 FROM sales_contacts WHERE prospect_id=?", (r["id"],)).fetchone():
                continue
            e = self.normalize(m.group(0))
            try:
                self.c.execute("INSERT INTO sales_contacts VALUES(?,?,?,?)", (r["id"], e, "crm.channel", _crm.now()))
                n += 1
            except sqlite3.IntegrityError:
                pass  # duplicate address across prospects: first one wins
        self.c.commit()
        return n

    def import_historic_suppressions(self) -> int:
        """Suppress addresses of prospects already suppressed or hard-bounced in the CRM history."""
        n = 0
        rows = self.c.execute("""SELECT DISTINCT c.email, COALESCE(p.suppression_reason,'HARD_BOUNCE') reason FROM sales_contacts c
            JOIN prospects p ON p.id=c.prospect_id
            WHERE p.suppressed=1 OR EXISTS(SELECT 1 FROM events e WHERE e.prospect_id=p.id AND e.event_type='HARD_BOUNCE')""").fetchall()
        for r in rows:
            n += self.add_suppression(r["email"], r["reason"][:200], "crm.history")
        return n

    def backfill_history_sends(self) -> int:
        """Mirror historical CRM SENT events into the send ledger so dedupe, rate and bounce math see them."""
        n = 0
        from datetime import datetime, timezone
        for e in self.c.execute("SELECT prospect_id, evidence, created_at FROM events WHERE event_type='SENT'").fetchall():
            email = mid = iso = None
            if e["evidence"].lstrip().startswith("{"):
                try:
                    j = json.loads(e["evidence"])
                except ValueError:
                    continue
                mid, email = j.get("gmail_message_id"), j.get("recipient")
                iso = datetime.fromisoformat(e["created_at"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            elif re.search(r"; to=", e["evidence"]):
                m = re.search(r"message_id=(\w+)", e["evidence"])
                t = re.search(r"; to=([^;\s]+)", e["evidence"])
                mid, email = (m.group(1) if m else None), (t.group(1) if t else None)
                iso = datetime.fromisoformat(e["created_at"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            else:
                m = re.search(r"message_id=(\w+)", e["evidence"])
                ts = re.search(r"sent_at=(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d) ([+-]\d{4})", e["evidence"])
                email = self.contact_for(e["prospect_id"])
                if m and ts:
                    mid = m.group(1)
                    iso = datetime.strptime(f"{ts.group(1)} {ts.group(2)} {ts.group(3)}", "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            if not (mid and iso and email):
                continue
            email = self.normalize(email)
            self.c.execute("INSERT OR IGNORE INTO sales_contacts VALUES(?,?,?,?)", (e["prospect_id"], email, "crm.sent_event", _crm.now()))
            cur = self.c.execute("INSERT OR IGNORE INTO sales_send_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (f"hist-{mid}", e["prospect_id"], email, "INITIAL_MESSAGE", "historic", "historic", "SENT",
                                  mid, None, "historic", iso, iso))
            n += cur.rowcount
        self.c.commit()
        return n

    def add_suppression(self, email: str, reason: str, source: str) -> int:
        cur = self.c.execute("INSERT OR IGNORE INTO sales_suppressions VALUES(?,?,?,?)",
                             (self.normalize(email), reason, source, _crm.now()))
        self.c.commit()
        return cur.rowcount

    def is_suppressed(self, email: str) -> bool:
        e = self.normalize(email)
        if self.c.execute("SELECT 1 FROM sales_suppressions WHERE email=?", (e,)).fetchone():
            return True
        domain = e.split("@")[1]
        return bool(self.c.execute("SELECT 1 FROM sales_suppressions WHERE email=?", ("@" + domain,)).fetchone())

    def block_domain(self, domain: str, reason: str) -> int:
        cur = self.c.execute("INSERT OR IGNORE INTO sales_domain_blocklist VALUES(?,?,?)", (domain.lower(), reason, _crm.now()))
        self.c.commit()
        return cur.rowcount

    def domain_blocked(self, email: str) -> str | None:
        """Qualification blocklist (aggregators/portals found after the fact). Registrable-domain match."""
        d = email.split("@")[-1].lower()
        parts = d.split(".")
        for i in range(len(parts) - 1):
            r = self.c.execute("SELECT reason FROM sales_domain_blocklist WHERE domain=?", (".".join(parts[i:]),)).fetchone()
            if r:
                return r[0]
        return None

    def contact_for(self, pid: str) -> str | None:
        r = self.c.execute("SELECT email FROM sales_contacts WHERE prospect_id=?", (pid,)).fetchone()
        return r[0] if r else None

    def prospect_for_email(self, email: str) -> str | None:
        r = self.c.execute("SELECT prospect_id FROM sales_contacts WHERE email=?", (self.normalize(email),)).fetchone()
        return r[0] if r else None

    def add_prospect(self, *, pid: str, name: str, company: str, website: str, email: str, source_url: str,
                     signal: str, proposal: str, fit: int, deal_low=None, deal_high=None) -> bool:
        """Insert a discovered prospect with dedupe on source_url, address, and CRM company/website."""
        e = self.normalize(email)
        if self.c.execute("SELECT 1 FROM sales_contacts WHERE email=?", (e,)).fetchone():
            return False
        if self.c.execute("SELECT 1 FROM prospects WHERE source_url=? OR (website IS NOT NULL AND website=?)", (source_url, website)).fetchone():
            return False
        ts = _crm.now()
        with self.txn() as c:
            c.execute("""INSERT INTO prospects(id,name,company,website,channel,source_url,source_date,observed_signal,proposal,fit_score,
                         deal_low,deal_high,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'PROSPECT_FOUND',?,?)""",
                      (pid, name, company, website, f"Public business email: {e} (published on official page)", source_url, ts[:10],
                       signal, proposal, fit, deal_low, deal_high, ts, ts))
            c.execute("INSERT INTO sales_contacts VALUES(?,?,?,?)", (pid, e, "luna.discovery", ts))
            c.execute("INSERT INTO events(prospect_id,event_type,evidence,created_at) VALUES(?,?,?,?)",
                      (pid, "PROSPECT_ADDED", f"Autonomous discovery; source {source_url}", ts))
        return True

    # ---- thin pass-throughs to the CRM's own invariant-preserving writers ---
    def set_stage(self, pid, stage, evidence=""): _crm.set_stage(self.c, pid, stage, evidence)
    def save_draft(self, pid, kind, subject, body): _crm.save_draft(self.c, pid, kind, subject, body)
    def approve(self, pid, operator, kind="INITIAL_MESSAGE"): _crm.approve(self.c, pid, operator, kind)
    def suppress_prospect(self, pid, reason): _crm.suppress(self.c, pid, reason)
    def classify_reply(self, pid, cls, evidence): _crm.classify_reply(self.c, pid, cls, evidence)
    def event(self, pid, etype, evidence):
        self.c.execute("INSERT INTO events(prospect_id,event_type,evidence,created_at) VALUES(?,?,?,?)", (pid, etype, evidence, _crm.now()))
        self.c.commit()

    def draft(self, pid, kind="INITIAL_MESSAGE"):
        return self.c.execute("SELECT * FROM drafts WHERE prospect_id=? AND kind=?", (pid, kind)).fetchone()

    def prospect(self, pid):
        return self.c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()

    # ---- send ledger -------------------------------------------------------
    def sent_count_since(self, seconds: int) -> int:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - seconds))
        return self.c.execute("SELECT count(*) FROM sales_send_intents WHERE status IN ('INTENT','SENT') AND created_at>=?", (cutoff,)).fetchone()[0]

    def last_sends(self, n: int):
        return self.c.execute("SELECT * FROM sales_send_intents WHERE status='SENT' ORDER BY created_at DESC LIMIT ?", (n,)).fetchall()

    def open_intents(self):
        return self.c.execute("SELECT * FROM sales_send_intents WHERE status='INTENT'").fetchall()
