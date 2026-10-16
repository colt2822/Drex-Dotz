"""Discovery engine: independent per-source job queues, enrichment pool, qualification, ranked promotion.

Never sends. Never constructs a Gmail client. Promotion only creates a CRM prospect + a grounded draft, which the
existing SalesWorker then gates and (inside the approved window/policy) sends.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from dotz.sales.disco import qualify
from dotz.sales.disco.enrich import enrich_site
from dotz.sales.disco.net import Fetcher
from dotz.sales.disco.sources import DirectorySource, OsmSource, SearchSource, SourceBlocked, SourceError
from dotz.sales.disco.store import DiscoStore
from dotz.sales.disco.util import slug

STATE_NAMES = {"TX": "Texas", "FL": "Florida", "GA": "Georgia", "NC": "North Carolina", "TN": "Tennessee", "AZ": "Arizona", "CO": "Colorado", "OH": "Ohio",
               "IL": "Illinois", "PA": "Pennsylvania", "MI": "Michigan", "VA": "Virginia", "SC": "South Carolina", "AL": "Alabama", "MO": "Missouri",
               "IN": "Indiana", "WA": "Washington", "OR": "Oregon", "NV": "Nevada", "UT": "Utah", "OK": "Oklahoma", "LA": "Louisiana", "KY": "Kentucky",
               "MN": "Minnesota", "WI": "Wisconsin", "MD": "Maryland", "NJ": "New Jersey", "CA": "California", "NY": "New York", "MA": "Massachusetts"}
TERMINAL_REJECT_FOR_VERIFIED = {"duplicate_existing_prospect", "domain_or_contact_suppressed", "mx_failed", "named_person_only", "no_site_published_email",
                                "dead_site", "parked_or_placeholder_site", "robots_denied_or_non_html", "aggregator_or_portal", "agency_or_vendor_competitor",
                                "chain_or_franchise", "no_operating_evidence"}


class Engine:
    def __init__(self, db_path, cfg: dict, fetcher: Fetcher | None = None, now=time.time, sleep=time.sleep, mx=None, sources: dict | None = None):
        self.db_path, self.cfg, self.now, self.sleep = db_path, cfg, now, sleep
        self.f = fetcher or Fetcher.from_cfg(cfg)
        self._tl = threading.local()
        self.mx = mx or qualify.mx_ok
        self.sources = sources or {"osm": OsmSource(cfg, self.f), "search": SearchSource(cfg, self.f), "directory": DirectorySource(cfg, self.f)}
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()

    @property
    def store(self) -> DiscoStore:
        """One SQLite connection per thread (sqlite3 objects are thread-bound)."""
        st = getattr(self._tl, "s", None)
        if st is None:
            st = self._tl.s = DiscoStore(self.db_path, self.now)
        return st

    # ------------------------------------------------------------------ jobs
    def seed_jobs(self, regions_per_cat: int = 3) -> int:
        """Initial + expansion seeding. A category/source gets its next region only once earlier ones are done (saturation-driven)."""
        n = 0
        cats, regions = self.cfg["categories"], self.cfg["regions"]
        for cat, cc in cats.items():
            for src, build in (("osm", lambda r: {"tags": cc["osm"], "state": r, "category": cat} if cc.get("osm") else None),
                               ("search", lambda r: {"query": f"{cc['keywords'][0]} company {STATE_NAMES.get(r, r)} contact us request a quote", "category": cat, "region": r, "location": STATE_NAMES.get(r, r)})):
                if src == "osm" and not cc.get("osm"):
                    continue
                if src == "search" and not self.cfg["sources"]["search"]["enabled"]:
                    continue
                if src == "osm" and not self.cfg["sources"]["osm"]["enabled"]:
                    continue
                open_n = self.store.c.execute("SELECT count(*) FROM disco_jobs WHERE source=? AND category=? AND status IN ('pending','running','failed')", (src, cat)).fetchone()[0]
                have = {r[0] for r in self.store.c.execute("SELECT region FROM disco_jobs WHERE source=? AND category=?", (src, cat))}
                if open_n >= 1:
                    continue
                added = 0
                for i, r in enumerate(regions):
                    if r in have:
                        continue
                    params = build(r)
                    if params and self.store.add_job(f"{src}:{cat}:{r}", src, cat, r, params, priority=round(cc["weight"] * (1 - 0.6 * i / len(regions)), 3)):
                        n += 1; added += 1
                    if added >= regions_per_cat:
                        break
        dcfg = self.cfg["sources"]["directory"]
        if dcfg["enabled"]:
            for p in dcfg.get("pages", []):
                if self.store.add_job(f"directory:{slug(p['url'], 80)}", "directory", p.get("category"), p.get("region"), p, priority=1.0):
                    n += 1
        return n

    def performance_multiplier(self, category: str) -> float:
        """Outcome feedback: positive-reply rate by category vs global, only once sends are sufficient. Prior = 1.0."""
        c = self.store.c
        try:
            rows = c.execute("""SELECT d.business_category cat, count(DISTINCT i.send_id) sends,
                 count(DISTINCT CASE WHEN r.prospect_id IS NOT NULL THEN i.send_id END) replies
                 FROM disco_candidates d JOIN sales_send_intents i ON i.prospect_id=d.crm_pid AND i.status='SENT'
                 LEFT JOIN sales_inbound r ON r.prospect_id=d.crm_pid AND r.kind='REPLY' GROUP BY d.business_category""").fetchall()
        except Exception:
            return 1.0
        tot_s = sum(r["sends"] for r in rows)
        tot_r = sum(r["replies"] for r in rows)
        mine = next((r for r in rows if r["cat"] == category), None)
        if tot_s < 100 or not mine or mine["sends"] < 30:
            return 1.0
        g = (tot_r + 1) / (tot_s + 20)
        m = (mine["replies"] + 1) / (mine["sends"] + 20)
        return max(0.5, min(2.0, m / g))

    def reprioritize(self) -> None:
        for cat, cc in self.cfg["categories"].items():
            mult = self.performance_multiplier(cat)
            self.store.c.execute("UPDATE disco_jobs SET priority=ROUND(priority*?,3) WHERE category=? AND status='pending' AND ?<>1.0", (mult, cat, mult))

    # ---------------------------------------------------------------- sources
    def _ingest(self, job, recs) -> tuple[int, int]:
        raw = new = 0
        for r in recs:
            if not r.domain:
                continue
            if self.store.record_raw(r.source, r.source_ref, job["job_id"], r.domain, r.name):
                raw += 1
            if self.store.upsert_candidate({"domain": r.domain, "name": r.name, "website": r.website, "category": r.category, "location": r.location,
                                            "region": r.region, "source": r.source, "source_url": r.source_url, "source_ref": r.source_ref,
                                            "phone": r.phone, "brand": r.brand, "email_hint": r.email_hint}):
                new += 1
        return raw, new

    def run_source_job(self, source: str) -> dict | None:
        until = self.store.blocked_until(source)
        if until > self.now():
            return {"source": source, "skipped": f"blocked_until {until:.0f}"}
        job = self.store.claim_job(source)
        if not job:
            return None
        src, cd = self.sources[source], self.cfg["sources"][source]
        try:
            res = src.run(dict(job))
            recs, cursor = (res if isinstance(res, tuple) else (res, None))
            raw, new = self._ingest(job, recs)
            if cursor and source == "directory" and job["attempts"] < cd.get("max_pages_per_job", 8) + 1 and cursor != job["cursor"]:
                self.store.finish_job(job["job_id"], "pending", raw, new, cursor=cursor)
            else:
                self.store.finish_job(job["job_id"], "done" if raw or new or True else "exhausted", raw, new)
            self.store.note_source(source, True, f"{job['job_id']} raw={raw} new={new}")
            return {"source": source, "job": job["job_id"], "raw": raw, "new": new}
        except SourceBlocked as e:
            cool = cd.get("block_cooldown_s", 900)
            self.store.note_source(source, False, str(e), block_for=cool)
            self.store.finish_job(job["job_id"], "pending", error=str(e)[:200], retry_in=cool)
            return {"source": source, "job": job["job_id"], "blocked": str(e)}
        except SourceError as e:
            self.store.note_source(source, False, str(e))
            self.store.finish_job(job["job_id"], "failed", error=str(e)[:200], retry_in=min(3600, 60 * 2 ** job["attempts"]))
            return {"source": source, "job": job["job_id"], "error": str(e)}

    # ------------------------------------------------------------ enrichment
    def enrich_batch(self, n: int | None = None) -> dict:
        ec = self.cfg["enrich"]
        rows = self.store.claim_for_enrichment(n or ec["batch"])
        if not rows:
            return {"enriched": 0}
        idx = qualify.CrmIndex(self.store.c)
        stats = {"enriched": 0, "qualified": 0, "rejected": 0}

        def work(r):
            if idx.suppressed(r["domain"], None):
                return r, ({"stage": "REJECTED", "reject_reason": "domain_or_contact_suppressed"}, [])
            if idx.duplicate(r["domain"], None):
                return r, ({"stage": "REJECTED", "reject_reason": "duplicate_existing_prospect"}, [])
            try:
                return r, enrich_site(self.f, r, ec["pages_per_site"])
            except Exception as e:  # one bad site must never kill the batch
                return r, ({"stage": "RAW", "reject_reason": None}, [])  # leave retryable; counted below
        with ThreadPoolExecutor(max_workers=ec["workers"]) as pool:
            for r, (fields, ev) in pool.map(work, rows):
                if fields.get("stage") == "RAW":
                    self.store.save_enrichment(r["domain"], {"stage": "RAW"}, [])
                    continue
                self.store.save_enrichment(r["domain"], fields, ev)
                stats["enriched"] += 1
                self.qualify_one(r["domain"], idx, stats)
        return stats

    def qualify_one(self, domain: str, idx=None, stats=None) -> dict:
        idx = idx or qualify.CrmIndex(self.store.c)
        cand = self.store.candidate(domain)
        if cand["stage"] == "REJECTED" and cand["reject_reason"]:
            res = {"qualified": False, "reject": cand["reject_reason"]}
        else:
            res = qualify.gate(cand, idx, self.mx, self.cfg, self.performance_multiplier(cand["business_category"]), self.cfg["categories"])
        if res["qualified"]:
            self.store.c.execute("UPDATE disco_candidates SET stage='QUALIFIED',reject_reason=NULL,fit=?,buying_signal=?,evidence_quality=?,contact_confidence=?,confidence_score=?,"
                                 "rank=?,expected_commercial_value_estimate=?,fit_reason=?,updated_at=? WHERE domain=?",
                                 (res["fit"], res["buying_signal"], res["evidence_quality"], res["contact_confidence"], res["confidence_score"], res["rank"],
                                  res["expected_commercial_value_estimate"], res["fit_reason"], self.now(), domain))
            if stats is not None: stats["qualified"] += 1
        else:
            self.store.c.execute("UPDATE disco_candidates SET stage='REJECTED',reject_reason=?,fit=COALESCE(?,fit),updated_at=? WHERE domain=?", (res["reject"], res.get("fit"), self.now(), domain))
            if stats is not None: stats["rejected"] += 1
        return res

    # -------------------------------------------------------------- promotion
    def promote(self, worker, limit: int | None = None, domains: list[str] | None = None, dry_run: bool = False) -> list[dict]:
        """Create CRM prospect + grounded draft for top-ranked QUALIFIED candidates (never sends)."""
        pc = self.cfg["promotion"]
        policy_cap = worker.p.cfg["discovery"]["max_new_prospects_per_day"]
        made = worker.s.c.execute("SELECT count(*) FROM events WHERE event_type='PROSPECT_ADDED' AND evidence LIKE 'Autonomous discovery%' AND created_at>=?",
                                  (time.strftime("%Y-%m-%dT00:00:00", time.gmtime(self.now())),)).fetchone()[0]
        budget = max(0, min(pc["max_per_day"], policy_cap - made) if limit is None else min(limit, policy_cap - made))
        q = "SELECT * FROM disco_candidates WHERE stage='QUALIFIED' AND rank>=?" + (" AND domain IN (%s)" % ",".join("?" * len(domains)) if domains else "") + " ORDER BY rank DESC LIMIT ?"
        rows = self.store.c.execute(q, (pc["min_rank"], *(domains or []), budget)).fetchall()
        out = []
        for c in rows:
            found = {"company": c["company_name"], "email": c["contact_email"], "signal": c["signal_quote"], "fit": int(c["fit"]), "proposal":
                     "Workflow automation for quote, intake and scheduling.", "website": c["website"].rstrip("/"), "source_url": c["contact_url"]}
            rec = {"domain": c["domain"], "rank": c["rank"], "email": found["email"]}
            if dry_run:
                out.append({**rec, "dry_run": True}); continue
            pid = f"dot-{time.strftime('%Y%m%d', time.gmtime(self.now()))}-{slug(c['domain'], 50)}"
            try:
                if worker.s.add_prospect(pid=pid, name="Business contact (public page)", company=found["company"], website=found["website"], email=found["email"],
                                         source_url=found["source_url"], signal=found["signal"], proposal=found["proposal"], fit=found["fit"]):
                    drafted = worker.draft_for(pid, found)
                    self.store.c.execute("UPDATE disco_candidates SET stage='PROMOTED',crm_pid=?,updated_at=? WHERE domain=?", (pid, self.now(), c["domain"]))
                    out.append({**rec, "pid": pid, "drafted": drafted})
                else:
                    self.store.c.execute("UPDATE disco_candidates SET stage='REJECTED',reject_reason='duplicate_existing_prospect',updated_at=? WHERE domain=?", (self.now(), c["domain"]))
                    out.append({**rec, "skipped": "duplicate"})
            except Exception as e:  # Luna down etc: leave QUALIFIED, retry later
                out.append({**rec, "error": f"{type(e).__name__}: {str(e)[:100]}"})
                if type(e).__name__ == "LunaUnavailable":
                    break
        return out

    # ---------------------------------------------------------------- metrics
    def metrics(self) -> dict:
        c = self.store.c
        one = lambda q, *a: c.execute(q, a).fetchone()[0]
        promoted = "SELECT crm_pid FROM disco_candidates WHERE stage='PROMOTED'"
        m = {
            "RAW_DISCOVERED": one("SELECT count(*) FROM disco_raw"),
            "UNIQUE_COMPANIES": one("SELECT count(*) FROM disco_candidates"),
            "CONTACT_VERIFIED": one("SELECT count(*) FROM disco_candidates WHERE contact_email IS NOT NULL AND contact_type='role' AND (reject_reason IS NULL OR reject_reason NOT IN "
                                    "('mx_failed','duplicate_existing_prospect','domain_or_contact_suppressed','named_person_only','aggregator_or_portal','agency_or_vendor_competitor','chain_or_franchise','no_operating_evidence'))"),
            "QUALIFIED": one("SELECT count(*) FROM disco_candidates WHERE stage IN ('QUALIFIED','PROMOTED')"),
            "ELIGIBLE_TO_DRAFT": one("SELECT count(*) FROM disco_candidates WHERE stage='QUALIFIED'"),
            "DRAFTED": one("SELECT count(*) FROM disco_candidates WHERE stage='PROMOTED'"),
            "SENT": one(f"SELECT count(*) FROM sales_send_intents WHERE status='SENT' AND prospect_id IN ({promoted})"),
            "BOUNCED": one(f"SELECT count(*) FROM sales_inbound WHERE kind='BOUNCE_HARD' AND prospect_id IN ({promoted})"),
            "REPLIED": one(f"SELECT count(DISTINCT prospect_id) FROM sales_inbound WHERE kind='REPLY' AND prospect_id IN ({promoted})"),
            "POSITIVE_REPLIES": one(f"SELECT count(*) FROM replies WHERE classification IN ('INTERESTED','PRICING') AND prospect_id IN ({promoted})") if self._has("replies", "classification") else None,
            "MEETINGS": None, "CONVERSIONS": None,  # no observed source wired yet: unknown, not zero
            "PENDING_ENRICHMENT": one("SELECT count(*) FROM disco_candidates WHERE stage IN ('RAW','ENRICHING')"),
        }
        m["REJECTION_BREAKDOWN"] = {r[0]: r[1] for r in c.execute("SELECT reject_reason, count(*) FROM disco_candidates WHERE stage='REJECTED' GROUP BY 1 ORDER BY 2 DESC")}
        m["SOURCES"] = {r["source"]: dict(r) for r in c.execute("SELECT * FROM disco_source_health")}
        m["JOBS"] = {r[0]: r[1] for r in c.execute("SELECT status, count(*) FROM disco_jobs GROUP BY 1")}
        return m

    def _has(self, table, col) -> bool:
        return any(r[1] == col for r in self.store.c.execute(f"PRAGMA table_info({table})"))

    # ------------------------------------------------------------- run loops
    def _loop(self, name, fn, idle=30):
        while not self.stop_ev.is_set():
            try:
                busy = fn()
            except Exception as e:  # never die
                self.store.c.execute("INSERT OR REPLACE INTO disco_source_health(source,status,detail,updated_at) VALUES(?,?,?,?)",
                                     (f"loop:{name}", "error", f"{type(e).__name__}: {str(e)[:150]}", self.now()))
                busy = False
            self.stop_ev.wait(0.5 if busy else idle)

    def run_forever(self):
        self.store.reset_running()
        threads = []
        for s in self.sources:
            if not self.cfg["sources"][s]["enabled"]:
                continue
            gap = self.cfg["sources"][s]["min_interval_s"]
            threads.append(threading.Thread(target=self._loop, args=(s, lambda s=s: bool(self.run_source_job(s)), gap), daemon=True, name=f"src-{s}"))
        threads.append(threading.Thread(target=self._loop, args=("enrich", lambda: self.enrich_batch().get("enriched", 0) > 0, 20), daemon=True, name="enrich"))
        threads.append(threading.Thread(target=self._loop, args=("seed", lambda: bool(self.seed_jobs()), 300), daemon=True, name="seed"))
        for t in threads:
            t.start()
        last = 0
        while not self.stop_ev.is_set():
            self.store.c.execute("INSERT INTO disco_source_health(source,status,detail,updated_at) VALUES('engine','ok','heartbeat',?) ON CONFLICT(source) DO UPDATE SET updated_at=excluded.updated_at,status='ok'", (self.now(),))
            if self.now() - last > 900:
                self.reprioritize(); last = self.now()
            self.stop_ev.wait(15)
        for t in threads:
            t.join(timeout=20)
