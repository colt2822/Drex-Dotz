"""Additive disco_* tables in the existing CRM database. One connection per thread, short transactions."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS disco_jobs(
  job_id TEXT PRIMARY KEY, source TEXT NOT NULL, category TEXT, region TEXT, params TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',            -- pending|running|done|failed|exhausted
  attempts INTEGER NOT NULL DEFAULT 0, next_run_at REAL NOT NULL DEFAULT 0, cursor TEXT,
  raw_count INTEGER DEFAULT 0, new_count INTEGER DEFAULT 0, error TEXT, priority REAL DEFAULT 1.0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_disco_jobs_q ON disco_jobs(source, status, next_run_at);
CREATE TABLE IF NOT EXISTS disco_raw(
  source TEXT NOT NULL, source_ref TEXT NOT NULL, job_id TEXT, domain TEXT, name TEXT, seen_at REAL NOT NULL,
  PRIMARY KEY(source, source_ref));
CREATE TABLE IF NOT EXISTS disco_candidates(
  domain TEXT PRIMARY KEY, company_name TEXT, website TEXT, business_category TEXT, location TEXT, region TEXT,
  source TEXT, source_url TEXT, source_ref TEXT, evidence_timestamp REAL, phone TEXT, brand TEXT, osm_email_hint TEXT,
  locations INTEGER DEFAULT 1,
  stage TEXT NOT NULL DEFAULT 'RAW',                 -- RAW|ENRICHING|ENRICHED|REJECTED|QUALIFIED|PROMOTED
  reject_reason TEXT, operating_status TEXT, fit_reason TEXT,
  contact_email TEXT, contact_type TEXT, contact_url TEXT, contact_checked_at REAL, contact_method TEXT,
  confidence_score REAL, fit REAL, buying_signal REAL, evidence_quality REAL, contact_confidence REAL,
  expected_commercial_value_estimate REAL, rank REAL, signals TEXT, signal_quote TEXT, signal_url TEXT,
  pages_fetched INTEGER DEFAULT 0, enriched_at REAL, crm_pid TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_disco_cand_stage ON disco_candidates(stage, rank);
CREATE TABLE IF NOT EXISTS disco_evidence(
  id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, kind TEXT NOT NULL, quote TEXT, url TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_disco_ev ON disco_evidence(domain);
CREATE TABLE IF NOT EXISTS disco_source_health(source TEXT PRIMARY KEY, status TEXT, detail TEXT, blocked_until REAL DEFAULT 0,
  ok_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0, consecutive_fail INTEGER DEFAULT 0, updated_at REAL);
"""


class DiscoStore:
    def __init__(self, path: Path | str, now=time.time):
        self.c = sqlite3.connect(str(path), timeout=30, isolation_level=None)  # autocommit; explicit BEGIN for multi-stmt
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA busy_timeout=15000")
        self.now = now
        self.c.executescript(SCHEMA)

    def close(self):
        self.c.close()

    # ---- jobs -----------------------------------------------------------------
    def add_job(self, job_id, source, category, region, params, priority=1.0) -> bool:
        t = self.now()
        cur = self.c.execute("INSERT OR IGNORE INTO disco_jobs(job_id,source,category,region,params,priority,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                             (job_id, source, category, region, json.dumps(params), priority, t, t))
        return cur.rowcount == 1

    def reset_running(self) -> int:
        """Crash recovery: the single-process lock guarantees nothing is genuinely running at startup."""
        n = self.c.execute("UPDATE disco_jobs SET status='pending' WHERE status='running'").rowcount
        n += self.c.execute("UPDATE disco_candidates SET stage='RAW' WHERE stage='ENRICHING'").rowcount
        return n

    def claim_job(self, source: str):
        t = self.now()
        self.c.execute("BEGIN IMMEDIATE")
        try:
            r = self.c.execute("SELECT * FROM disco_jobs WHERE source=? AND status IN ('pending','failed') AND next_run_at<=? AND attempts<5 "
                               "ORDER BY priority DESC, created_at LIMIT 1", (source, t)).fetchone()
            if r:
                self.c.execute("UPDATE disco_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE job_id=?", (t, r["job_id"]))
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise
        return r

    def finish_job(self, job_id, status, raw=0, new=0, error=None, retry_in=0.0, cursor=None):
        t = self.now()
        self.c.execute("UPDATE disco_jobs SET status=?,raw_count=raw_count+?,new_count=new_count+?,error=?,next_run_at=?,cursor=?,updated_at=? WHERE job_id=?",
                       (status, raw, new, error, t + retry_in, cursor, t, job_id))

    # ---- source health ---------------------------------------------------------
    def health(self, source):
        return self.c.execute("SELECT * FROM disco_source_health WHERE source=?", (source,)).fetchone()

    def note_source(self, source, ok: bool, detail="", block_for=0.0):
        t = self.now()
        self.c.execute("INSERT OR IGNORE INTO disco_source_health(source,status,detail,updated_at) VALUES(?,?,?,?)", (source, "new", "", t))
        if ok:
            self.c.execute("UPDATE disco_source_health SET status='ok',detail=?,ok_count=ok_count+1,consecutive_fail=0,blocked_until=0,updated_at=? WHERE source=?", (detail[:200], t, source))
        else:
            self.c.execute("UPDATE disco_source_health SET status=?,detail=?,fail_count=fail_count+1,consecutive_fail=consecutive_fail+1,blocked_until=?,updated_at=? WHERE source=?",
                           ("blocked" if block_for else "failing", detail[:200], t + block_for if block_for else 0, t, source))

    def blocked_until(self, source) -> float:
        r = self.health(source)
        return r["blocked_until"] if r else 0.0

    # ---- candidates ------------------------------------------------------------
    def record_raw(self, source, source_ref, job_id, domain, name) -> bool:
        return self.c.execute("INSERT OR IGNORE INTO disco_raw VALUES(?,?,?,?,?,?)", (source, source_ref, job_id, domain, name, self.now())).rowcount == 1

    def upsert_candidate(self, rec: dict) -> bool:
        """Insert a new unique company; an existing domain only gets its location count bumped. True when new."""
        t = self.now()
        cur = self.c.execute(
            "INSERT OR IGNORE INTO disco_candidates(domain,company_name,website,business_category,location,region,source,source_url,source_ref,evidence_timestamp,"
            "phone,brand,osm_email_hint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["domain"], rec["name"], rec["website"], rec["category"], rec.get("location"), rec.get("region"), rec["source"], rec["source_url"],
             rec["source_ref"], t, rec.get("phone"), rec.get("brand"), rec.get("email_hint"), t, t))
        if cur.rowcount == 0:
            self.c.execute("UPDATE disco_candidates SET locations=locations+1,updated_at=? WHERE domain=?", (t, rec["domain"]))
        return cur.rowcount == 1

    def claim_for_enrichment(self, n: int):
        self.c.execute("BEGIN IMMEDIATE")
        try:
            rows = self.c.execute("SELECT * FROM disco_candidates WHERE stage='RAW' ORDER BY (brand IS NOT NULL), locations, created_at LIMIT ?", (n,)).fetchall()
            for r in rows:
                self.c.execute("UPDATE disco_candidates SET stage='ENRICHING',updated_at=? WHERE domain=?", (self.now(), r["domain"]))
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise
        return rows

    def save_enrichment(self, domain: str, fields: dict, evidence: list[tuple]) -> None:
        cols = ",".join(f"{k}=?" for k in fields)
        self.c.execute("BEGIN IMMEDIATE")
        try:
            self.c.execute(f"UPDATE disco_candidates SET {cols},updated_at=? WHERE domain=?", (*fields.values(), self.now(), domain))
            self.c.execute("DELETE FROM disco_evidence WHERE domain=?", (domain,))
            self.c.executemany("INSERT INTO disco_evidence(domain,kind,quote,url,fetched_at) VALUES(?,?,?,?,?)", [(domain, *e) for e in evidence])
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise

    def candidate(self, domain):
        return self.c.execute("SELECT * FROM disco_candidates WHERE domain=?", (domain,)).fetchone()
