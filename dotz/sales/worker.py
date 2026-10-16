"""The one Sales Dot worker: discover, research, qualify, draft, send (policy-gated), sync replies.

Single canonical state: the existing Revenue Hunter CRM database (via CrmStore).
Sending is idempotent: a send intent is committed before Gmail is called and reconciled by
Message-ID after any crash; an intent is never resent.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotz.sales.crm_store import CrmStore, EMAIL_RE
from dotz.sales.gmail import ACCOUNT, GmailError, classify_inbound
from dotz.sales.luna import Luna, LunaUnavailable, html_to_text
from dotz.sales.policy import Policy

STATE_DIR = Path.home() / ".local" / "state" / "dotz"
STOP_FILE = STATE_DIR / "STOP"
ALERTS = STATE_DIR / "alerts.jsonl"
SENT_BY_DOT = "standing-policy"
FORBIDDEN = re.compile(r"(\$\s?\d|\bguarantee|\bfree trial\b|\bsave \d|\b\d+ ?% |\bact now\b|\blimited time\b|\burgent|\bfinal notice\b|^re:|^fwd:)", re.I | re.M)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def alert(kind: str, **detail) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(ALERTS, "a") as fh:
        fh.write(json.dumps({"ts": time.time(), "kind": kind, **detail}) + "\n")


def content_problems(subject: str, body: str, footer: str, max_words: int) -> list[str]:
    p = []
    core = body.split(footer)[0] if footer in body else body
    if footer not in body:
        p.append("missing_footer")
    if "opt out" not in body.lower() or "unsubscribe" not in body.lower():
        p.append("missing_opt_out")
    if len(core.split()) > max_words:
        p.append("too_long")
    if FORBIDDEN.search(subject + "\n" + core):
        p.append("forbidden_claim")
    if not subject.strip() or len(subject) > 80 or "\n" in subject:
        p.append("bad_subject")
    if "http" in core and "elyraon.com.co" not in core:
        p.append("foreign_link")
    return p


class SalesWorker:
    def __init__(self, store: CrmStore, policy: Policy, gmail, luna: Luna | None, now=time.time, sleep=time.sleep, mx_check=None):
        self.s, self.p, self.g, self.luna, self.now, self.sleep = store, policy, gmail, luna, now, sleep
        self.mx_check = mx_check or self._mx_check
        self._acct_cache: tuple[float, int] | None = None

    # ------------------------------------------------------------------ stop
    def stop(self, reason: str) -> None:
        self.s.set_state("emergency_stop", reason)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text(reason)
        alert("EMERGENCY_STOP", reason=reason)

    def stopped(self) -> str | None:
        if STOP_FILE.exists():
            return "STOP file: " + STOP_FILE.read_text()[:100]
        return self.s.stopped()

    def resume(self) -> None:
        STOP_FILE.unlink(missing_ok=True)
        self.s.c.execute("DELETE FROM sales_state WHERE key='emergency_stop'")
        self.s.c.commit()

    # ---------------------------------------------------------------- footer
    def footer(self) -> str:
        v = self.s.get_state("postal_footer")
        if v:
            return v
        r = self.s.c.execute("SELECT body FROM drafts WHERE body LIKE '%Advertisement / business solicitation%' ORDER BY approved DESC, id LIMIT 1").fetchone()
        if not r or "\nColt McClain\n" not in r[0]:
            raise RuntimeError("postal footer not recoverable from approved CRM drafts")
        v = "Colt McClain" + r[0].rsplit("\nColt McClain", 1)[1].rstrip()
        self.s.set_state("postal_footer", v)
        return v

    # ------------------------------------------------------- send gating
    def _utc_iso(self, t: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))

    def stop_rule_violation(self) -> str | None:
        sr = self.p.cfg["stop_conditions"]
        last = self.s.last_sends(int(sr.get("bounce_window_sends", 100)))
        if last:
            bounced = 0
            for r in last:
                if self.s.c.execute("SELECT 1 FROM sales_inbound WHERE kind='BOUNCE_HARD' AND prospect_id=?", (r["prospect_id"],)).fetchone() or \
                   self.s.c.execute("SELECT 1 FROM events WHERE prospect_id=? AND event_type='HARD_BOUNCE'", (r["prospect_id"],)).fetchone():
                    bounced += 1
            if bounced / len(last) >= sr["hard_bounce_rate_at_or_above"]:
                return f"hard bounce rate {bounced}/{len(last)} over last {len(last)} sends"
        if self.s.c.execute("SELECT 1 FROM sales_inbound WHERE kind='COMPLAINT'").fetchone():
            return "complaint recorded"
        pc = self.p.cfg["pause_conditions"]
        rep = self.s.c.execute("SELECT count(*) FROM sales_inbound WHERE kind='BOUNCE_REPUTATION' AND processed_at>=?",
                               (self._utc_iso(self.now() - 86400),)).fetchone()[0]
        if rep >= pc["reputation_dsn_stop_count_24h"]:
            return f"{rep} provider reputation/restriction DSNs in 24h"
        old = self.s.c.execute("SELECT 1 FROM sales_send_intents WHERE status='INTENT' AND created_at < ?",
                               (self._utc_iso(self.now() - sr["unresolved_send_intent_older_than_seconds"]),)).fetchone()
        if old:
            return "unresolved send intent"
        return None

    # ---------------------------------------------------- pauses (time-boxed, automatic)
    def paused_until(self) -> float:
        return float(self.s.get_state("pause_until", "0"))

    def pause(self, seconds: float, reason: str) -> None:
        self.s.set_state("pause_until", str(self.now() + seconds))
        self.s.set_state("pause_reason", reason)
        alert("SENDING_PAUSED", seconds=seconds, reason=reason)

    def on_gmail_error(self, e: GmailError, context: str) -> str:
        k = e.kind()
        if k == "throttle":
            steps = self.p.cfg["pause_conditions"]["provider_throttle_backoff_seconds"]
            n = min(int(self.s.get_state("throttle_strikes", "0")), len(steps) - 1)
            self.s.set_state("throttle_strikes", str(n + 1))
            self.pause(steps[n], f"Google throttling during {context}: {str(e)[:100]}")
        elif k in ("restricted", "auth"):
            self.stop(f"Gmail {k} signal during {context}: {str(e)[:120]}")
        return k

    # -------------------------------------------------------------- quota accounting
    def account_sends_24h(self, force: bool = False) -> int:
        """ALL messages sent from the Gmail account in the rolling 24h (Gmail truth + local sends since the count)."""
        t = self.now()
        if force or not self._acct_cache or t - self._acct_cache[0] > 300:
            n = self.g.count_sent_since(t - 86400)
            self._acct_cache = (t, n)
            self.s.set_state("account_sends_24h", json.dumps({"at": t, "n": n}))
            return n
        since = self._utc_iso(self._acct_cache[0])
        local = self.s.c.execute("SELECT count(*) FROM sales_send_intents WHERE status IN ('INTENT','SENT') AND policy_hash!='historic' AND created_at>?", (since,)).fetchone()[0]
        return self._acct_cache[1] + local

    def sales_sent_24h(self) -> int:
        return self.s.c.execute("SELECT count(*) FROM sales_send_intents WHERE status IN ('INTENT','SENT') AND policy_hash!='historic' AND created_at>=?",
                                (self._utc_iso(self.now() - 86400),)).fetchone()[0]

    def spacing(self, budget_left: int) -> float:
        pc = self.p.cfg["volume"]["pacing"]
        left = self.p.window_seconds_left(self.now())
        base = left / max(1, budget_left)
        base = min(max(base, pc["min_spacing_seconds"]), pc["max_spacing_seconds"])
        import random
        return max(pc["min_spacing_seconds"], base * (1 + random.uniform(-pc["jitter"], pc["jitter"])))

    def send_budget(self, approval: dict) -> tuple[bool, str, int]:
        if not self.p.in_window(self.now()):
            return False, "outside_window", 0
        if self.paused_until() > self.now():
            return False, "paused: " + (self.s.get_state("pause_reason", "") or "")[:100], 0
        try:
            used = self.account_sends_24h()
        except GmailError as e:
            self.on_gmail_error(e, "account quota count")
            return False, "account quota unknown (fail closed)", 0
        left = self.p.sales_budget(used)
        if left <= 0:
            return False, f"account quota {used}/{self.p.effective_cap()} (reserve kept for transactional/manual)", 0
        return True, "ok", left

    # --------------------------------------------------------------- verification
    def _mx_check(self, domain: str) -> bool:
        import subprocess
        key, ttl = f"mx:{domain}", self.p.cfg["verification"]["mx_cache_seconds"]
        v = self.s.get_state(key)
        if v:
            ok, at = v.split("|")
            if self.now() - float(at) < ttl:
                return ok == "1"
        try:
            out = subprocess.run(["dig", "+short", "+time=3", "+tries=1", "MX", domain], capture_output=True, text=True, timeout=10).stdout.split()
            ok = bool(out) and out != ["0", "."]
            if not ok:
                ok = bool(subprocess.run(["dig", "+short", "+time=3", "+tries=1", "A", domain], capture_output=True, text=True, timeout=10).stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            return False  # fail closed
        self.s.set_state(key, f"{int(ok)}|{self.now()}")
        return ok

    def candidate_problems(self, pid: str, approval: dict) -> list[str]:
        pr = self.s.prospect(pid)
        d = self.s.draft(pid)
        email = self.s.contact_for(pid)
        probs: list[str] = []
        if not pr or not d or not email:
            return ["no_prospect_draft_or_address"]
        if pr["suppressed"] or self.s.is_suppressed(email):
            return ["suppressed"]
        blocked = self.s.domain_blocked(email)
        if blocked:
            return ["domain_blocklisted:" + blocked[:60]]
        legacy = bool(d["approved"]) and pr["status"] == "HUMAN_APPROVED"  # owner approved this exact message
        if not legacy:
            ev = self.s.c.execute("SELECT evidence FROM events WHERE prospect_id=? AND event_type='DOT_DRAFTED' ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
            if not ev or json.loads(ev[0]).get("body_sha") != sha(d["body"]):
                probs.append("not_dot_drafted_or_edited")
            if "public_business_role_address" not in approval["categories"]:
                probs.append("category_not_approved")
            if self.p.is_free_mail(email):
                probs.append("free_mail")
            if pr["fit_score"] < self.p.cfg["discovery"]["min_fit_score"]:
                probs.append("low_fit")
        if self.s.c.execute("SELECT 1 FROM sales_send_intents WHERE recipient=? AND status IN ('INTENT','SENT')", (email,)).fetchone():
            probs.append("already_contacted")
        domain = email.split("@")[1]
        if not self.p.is_free_mail(email) and self.s.c.execute(
                "SELECT 1 FROM sales_send_intents WHERE status IN ('INTENT','SENT') AND recipient LIKE ? AND created_at>=?",
                ("%@" + domain, self._utc_iso(self.now() - 90 * 86400))).fetchone():
            probs.append("domain_contacted_90d")
        if self.p.cfg["verification"]["require_mx"] and not self.mx_check(email.split("@")[1]):
            probs.append("recipient_domain_failed_mx_verification")
        probs += content_problems(d["subject"], d["body"], self.footer(), self.p.cfg["content_rules"]["max_words"])
        return probs

    # --------------------------------------------------------------- send
    def send_one(self, pid: str, approval: dict) -> dict:
        """Idempotent send of one prospect's INITIAL_MESSAGE. Returns {sent, reason}."""
        probs = self.candidate_problems(pid, approval)
        if probs:
            return {"sent": False, "reason": ",".join(probs)}
        d, email = self.s.draft(pid), self.s.contact_for(pid)
        send_id = f"dot-{uuid.uuid4().hex[:20]}"
        if not d["approved"]:
            self.s.approve(pid, f"{SENT_BY_DOT}:{self.p.hash[:12]}")  # CRM invariant: SENT requires an approved draft
        d = self.s.draft(pid)
        with self.s.txn() as c:  # commit the intent BEFORE calling Gmail
            c.execute("INSERT INTO sales_send_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (send_id, pid, email, "INITIAL_MESSAGE", sha(d["subject"]), sha(d["body"]), "INTENT", None, None, self.p.hash, self._utc_iso(self.now()), None))
        self._crash_hook("after_intent")
        try:
            res = self.g.send(email, d["subject"], d["body"], send_id)
        except GmailError as e:
            kind = self.on_gmail_error(e, "send")
            if e.status and 400 <= e.status < 500:  # definitive refusal: nothing was sent, free the intent
                with self.s.txn() as c:
                    c.execute("UPDATE sales_send_intents SET status='FAILED' WHERE send_id=?", (send_id,))
                return {"sent": False, "reason": f"gmail_refused_{kind}", "send_id": send_id}
            alert("SEND_UNKNOWN", send_id=send_id, error=str(e)[:150])
            return {"sent": False, "reason": "send_error_pending_reconcile", "send_id": send_id}
        self._crash_hook("after_gmail_before_record")
        self._record_sent(send_id, pid, email, res, d)
        return {"sent": True, "send_id": send_id, "provider_message_id": res["provider_message_id"]}

    def _crash_hook(self, point: str) -> None:  # overridden in crash tests
        pass

    def _record_sent(self, send_id, pid, email, res, d) -> None:
        with self.s.txn() as c:
            c.execute("UPDATE sales_send_intents SET status='SENT',provider_message_id=?,thread_id=?,sent_at=? WHERE send_id=?",
                      (res["provider_message_id"], res["thread_id"], self._utc_iso(self.now()), send_id))
        ev = json.dumps({"delivery": "UNKNOWN", "gmail_message_id": res["provider_message_id"], "thread_id": res["thread_id"],
                         "recipient": email, "sender": res["sender"], "send_id": send_id, "status": "GMAIL_SENT_STANDING_POLICY"})
        if self.s.prospect(pid)["status"] != "SENT":
            self.s.set_stage(pid, "SENT", ev)

    def recover_intents(self) -> dict:
        """After a crash: reconcile every open intent against Gmail by Message-ID. Never resend."""
        out = {"reconciled": 0, "failed": 0, "pending": 0}
        for r in self.s.open_intents():
            age = self.now() - datetime.fromisoformat(r["created_at"]).replace(tzinfo=ZoneInfo("UTC")).timestamp()
            try:
                found = self.g.find_sent(r["send_id"], r["recipient"])
            except GmailError:
                out["pending"] += 1
                continue
            if found:
                self._record_sent(r["send_id"], r["prospect_id"], r["recipient"], found, self.s.draft(r["prospect_id"]))
                out["reconciled"] += 1
            elif age > 900:
                with self.s.txn() as c:
                    c.execute("UPDATE sales_send_intents SET status='FAILED' WHERE send_id=?", (r["send_id"],))
                out["failed"] += 1
            else:
                out["pending"] += 1
        return out

    # ---------------------------------------------------------- reply sync
    def sync_replies(self) -> dict:
        stats = {"new": 0, "bounce": 0, "opt_out": 0, "complaint": 0, "reply": 0, "auto": 0, "unmatched": 0}
        ids = self.g.list_ids("in:inbox -from:me newer_than:14d", limit=200)
        for mid in ids:
            if self.s.c.execute("SELECT 1 FROM sales_inbound WHERE provider_message_id=?", (mid,)).fetchone():
                continue
            m = self.g.get_parsed(mid)
            k = classify_inbound(m)
            stats["new"] += 1
            sender_pid = self.s.prospect_for_email(k["recipient"]) if k.get("recipient") else None
            if not sender_pid and m.get("thread_id"):
                r = self.s.c.execute("SELECT prospect_id FROM sales_send_intents WHERE thread_id=?", (m["thread_id"],)).fetchone()
                sender_pid = r[0] if r else None
            if not sender_pid and k["kind"] in ("OPT_OUT", "COMPLAINT", "REPLY") and m["from"] and not self.p.is_free_mail(m["from"][0]):
                dom = m["from"][0].split("@")[-1]
                r = self.s.c.execute("SELECT prospect_id FROM sales_contacts WHERE email LIKE ?", ("%@" + dom,)).fetchone()
                sender_pid = r[0] if r else None
            kind = k["kind"]
            detail = k["detail"]
            if kind == "BOUNCE":
                stats["bounce"] += 1
                rec = k.get("recipient")
                pid = self.s.prospect_for_email(rec) if rec else None
                if k["hard"] and rec:
                    self.s.add_suppression(rec, f"hard bounce {k['detail']}", "gmail.dsn")
                    if pid:
                        self.s.event(pid, "HARD_BOUNCE", f"Hard bounce from Gmail DSN: recipient {rec}; status {k['detail']}; message {mid}")
                        self.s.suppress_prospect(pid, f"Hard bounce {k['detail']} (DSN {mid})")
                kind = "BOUNCE_HARD" if k["hard"] else "BOUNCE_SOFT"
                if k.get("reputation"):
                    kind = "BOUNCE_REPUTATION"
                    self.pause(self.p.cfg["pause_conditions"]["reputation_dsn_pause_seconds"], f"reputation/restriction DSN {mid}")
                sender_pid = pid
            elif kind in ("OPT_OUT", "COMPLAINT"):
                stats["opt_out" if kind == "OPT_OUT" else "complaint"] += 1
                for a in m["from"][:1]:
                    self.s.add_suppression(a, f"{kind} reply {mid}", "gmail.reply")
                    self.s.add_suppression("@" + a.split("@")[1], f"{kind} reply {mid}", "gmail.reply") if not self.p.is_free_mail(a) else None
                if sender_pid:
                    self.s.suppress_prospect(sender_pid, f"{kind} reply via Gmail {mid}")
                if kind == "COMPLAINT":
                    self.stop(f"complaint language in reply {mid}")
            elif kind == "AUTO":
                stats["auto"] += 1
            else:
                stats["reply"] += 1
                if sender_pid:
                    try:
                        cls = self.luna.classify_reply(m["text"]) if self.luna else "OTHER"
                    except LunaUnavailable:
                        cls = "OTHER"
                    self.s.classify_reply(sender_pid, cls, m["text"][:500].strip() or "(empty)")
                    if cls in ("INTERESTED", "PRICING"):
                        alert("OPPORTUNITY", prospect=sender_pid, classification=cls, gmail_message=mid)
                else:
                    stats["unmatched"] += 1
            self.s.c.execute("INSERT OR IGNORE INTO sales_inbound VALUES(?,?,?,?,?)", (mid, kind, sender_pid, detail, self._utc_iso(self.now())))
            self.s.c.commit()
        self.s.set_state("last_reply_sync", str(self.now()))
        return stats

    # ---------------------------------------------------- discovery/drafting
    def discover(self) -> dict:
        cfg = self.p.cfg["discovery"]
        n = int(self.s.get_state("vertical_idx", "0"))
        vertical = cfg["verticals"][n % len(cfg["verticals"])]
        self.s.set_state("vertical_idx", str(n + 1))
        added = tried = 0
        urls = self.luna.search(f"{vertical} contact us request a quote United States")
        for url in urls:
            if tried >= cfg["per_tick_pages"]:
                break
            tried += 1
            text = self.luna.fetch_text(url)
            if not text or not EMAIL_RE.search(text):
                parsed = urllib.parse.urlparse(url)
                text = self.luna.fetch_text(f"{parsed.scheme}://{parsed.netloc}/contact") if parsed.path in ("", "/") else text
                if text:
                    url = f"{parsed.scheme}://{parsed.netloc}/contact"
            if not text:
                continue
            found = self.luna.extract_prospect(url, text, "estimate, quote, intake and scheduling workflow tools")
            if not found or found["fit"] < cfg["min_fit_score"] or self.p.is_free_mail(found["email"]):
                continue
            pid = "dot-" + time.strftime("%Y%m%d", time.gmtime(self.now())) + "-" + re.sub(r"[^a-z0-9]+", "-", found["company"].casefold()).strip("-")[:40]
            if self.s.add_prospect(pid=pid, name="Business contact (public page)", company=found["company"], website=found["website"],
                                   email=found["email"], source_url=found["source_url"], signal=found["signal"],
                                   proposal=found["proposal"], fit=found["fit"]):
                added += 1
                self.draft_for(pid, found)
        return {"vertical": vertical, "pages_tried": tried, "added": added}

    def draft_for(self, pid: str, found: dict) -> bool:
        d = self.luna.draft_message({"company": found["company"], "website": found["website"], "signal": found["signal"]}, self.p.cfg["discovery"]["offer_url"])
        body = d["body"].rstrip() + "\n\nELYRAON services: " + self.p.cfg["discovery"]["offer_url"] + "\n\n" + self.footer() if "elyraon.com.co" not in d["body"] \
            else d["body"].rstrip() + "\n\n" + self.footer()
        probs = content_problems(d["subject"], body, self.footer(), self.p.cfg["content_rules"]["max_words"])
        self.s.set_stage(pid, "QUALIFIED", f"Qualified by Luna with grounded page evidence (fit {found['fit']}); source {found['source_url']}")
        if probs:
            self.s.event(pid, "DOT_DRAFT_REJECTED", ",".join(probs))
            return False
        self.s.save_draft(pid, "INITIAL_MESSAGE", d["subject"], body)
        self.s.event(pid, "DOT_DRAFTED", json.dumps({"body_sha": sha(body), "model": "luna/codex_1"}))
        return True

    # ----------------------------------------------------------------- tick
    def tick(self) -> dict:
        r: dict = {"ts": self.now()}
        why = self.stopped()
        if why:
            return {**r, "stopped": why}
        try:
            self.g.assert_identity()
            r["identity"] = ACCOUNT
        except GmailError as e:
            if "mismatch" in str(e):
                self.stop(str(e))
                return {**r, "stopped": str(e)}
            r["gmail"] = "unavailable: " + str(e)[:100]
        if "gmail" not in r:
            r["recover"] = self.recover_intents()
            try:
                r["sync"] = self.sync_replies()
            except GmailError as e:
                r["sync_error"] = str(e)[:100]
        viol = self.stop_rule_violation()
        if viol:
            self.stop(viol)
            return {**r, "stopped": viol}
        r["sending"] = self._send_loop(r)
        self._discover_step(r)  # after sending, so research never starves paced sends
        return r

    def _discover_step(self, r: dict) -> None:
        dcfg = self.p.cfg["discovery"]
        if not self.luna or self.stopped() or not self._legacy_discovery_enabled() or self.now() - float(self.s.get_state("last_discovery", "0")) < dcfg["min_seconds_between_runs"]:
            return
        made = self.s.c.execute("SELECT count(*) FROM events WHERE event_type='PROSPECT_ADDED' AND evidence LIKE 'Autonomous discovery%' AND created_at>=?",
                                (time.strftime("%Y-%m-%dT00:00:00", time.gmtime(self.now())),)).fetchone()[0]
        if made >= dcfg["max_new_prospects_per_day"]:
            return
        self.s.set_state("last_discovery", str(self.now()))
        try:
            r["discovery"] = self.discover()
        except LunaUnavailable as e:
            r["discovery_error"] = str(e)[:120]

    @staticmethod
    def _legacy_discovery_enabled() -> bool:
        """v2 (dotz.sales.disco, separate process) replaces the single-thread Luna search that leaked aggregators."""
        try:
            from dotz.sales.disco import config
            return bool(config.load().get("legacy_discovery", True))
        except Exception:
            return True

    def _send_loop(self, r: dict):
        approval = self.p.approval()
        if not approval:
            return "disabled: policy not owner-approved"
        sync_age = self.now() - float(self.s.get_state("last_reply_sync", "0"))
        if sync_age > self.p.cfg["stop_conditions"]["reply_sync_stale_seconds"] or "gmail" in r:
            return "paused: reply sync stale or Gmail unavailable"
        tick_budget = self.p.cfg["volume"]["pacing"]["tick_budget_seconds"]
        t0, sent, last_reason = self.now(), [], "no eligible drafts"
        while not self.stopped():
            ok, why, left = self.send_budget(approval)
            if not ok:
                last_reason = "paused: " + why
                break
            gap = self.spacing(left)
            last = self.s.c.execute("SELECT max(created_at) FROM sales_send_intents WHERE status IN ('INTENT','SENT') AND policy_hash!='historic'").fetchone()[0]
            wait = gap - (self.now() - datetime.fromisoformat(last).replace(tzinfo=ZoneInfo("UTC")).timestamp()) if last else 0
            if wait > 0:
                if self.now() - t0 + wait > tick_budget:
                    last_reason = f"pacing: next send in {int(wait)}s"
                    break
                self.sleep(wait)
            row = next((x[0] for x in self.s.c.execute("""SELECT p.id FROM prospects p JOIN drafts d ON d.prospect_id=p.id AND d.kind='INITIAL_MESSAGE'
                       WHERE p.suppressed=0 AND p.status IN ('MESSAGE_DRAFTED','HUMAN_APPROVED') ORDER BY p.fit_score DESC""").fetchall()
                        if not self.candidate_problems(x[0], approval)), None)
            if row is None:
                break
            res = self.send_one(row, approval)
            if res["sent"]:
                sent.append(res["send_id"])
                self.s.set_state("throttle_strikes", "0")  # healthy send resets backoff
                last_reason = "sent"
            else:
                last_reason = res["reason"]
                break
            if self.now() - t0 >= tick_budget:
                break
        return {"sent": len(sent), "status": last_reason, "account_sends_24h": self._acct_cache[1] if self._acct_cache else None}


def run_loop(worker_factory, interval: int = 60) -> None:
    """Single-instance supervisor loop. flock guarantees one process; tick errors never kill the loop."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "sales.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another Sales Dot worker holds the lock")
    w = worker_factory()
    while True:
        try:
            res = w.tick()
        except Exception as e:  # never die; surface and retry next tick
            res = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            alert("TICK_ERROR", error=res["error"])
        w.s.set_state("last_tick", json.dumps({k: v for k, v in res.items()}, default=str)[:2000])
        w.s.set_state("heartbeat", str(time.time()))
        time.sleep(interval)
