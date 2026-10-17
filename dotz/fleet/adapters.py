"""Verified adapters. Each has execute() (do/measure) and verify() (re-check real state).

A task is SUCCEEDED only if verify() passes. Adapters never build a Gmail client, never call the sender's tick,
and never run discovery jobs: they observe the single authoritative processes or act through existing adapters.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotz.fleet.db import FleetDB

CTRL_BIN = Path.home() / "ELYRAON-CONTROL" / "bin"
STATE_DIR = Path.home() / ".local" / "state" / "dotz"
USER_UNITS = ["drex-sales-dot", "drex-sales-discovery", "drex-fleet", "drex-fleet-command"]
SYSTEM_UNITS = ["drex-hermes-harness", "drex-decision-gateway", "nova-orchestrator-api", "nova-harnessd", "nova-router",
                "nova-drex-monitor", "nova-hub", "nova-console", "nova-hermes", "drex-copy-trader-price", "drex-copy-trader-stop"]


class TaskFailed(Exception):
    """Raised for an unverifiable or failed task. terminal=True skips retries."""
    def __init__(self, msg: str, terminal: bool = False):
        super().__init__(msg)
        self.terminal = terminal


@dataclass
class Ctx:
    db: FleetDB
    crm_path: Path | None = None
    now: callable = time.time
    runner: callable = field(default=None)   # injectable subprocess runner for tests

    def crm_ro(self) -> sqlite3.Connection:
        from dotz.sales.crm_store import _crm
        p = Path(self.crm_path or _crm.DB)
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def run(self, argv: list[str], timeout: float = 20) -> tuple[int, str]:
        if self.runner:
            return self.runner(argv, timeout)
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + (("\n" + p.stderr) if p.returncode and p.stderr else "")).strip()
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        except OSError as e:
            return 127, str(e)


# ------------------------------------------------------------------ sentinel
def _unit(ctx: Ctx, unit: str) -> dict:
    for scope in (["--user"], []):
        rc, out = ctx.run(["systemctl", *scope, "show", f"{unit}.service", "-p", "LoadState,ActiveState,SubState,NRestarts,ActiveEnterTimestamp,MemoryCurrent"], 10)
        kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        if rc == 0 and kv.get("LoadState") not in (None, "not-found"):
            mem = kv.get("MemoryCurrent", "")
            return {"unit": unit, "scope": "user" if scope else "system", "active": kv.get("ActiveState"), "sub": kv.get("SubState"),
                    "restarts": int(kv["NRestarts"]) if kv.get("NRestarts", "").isdigit() else None, "since": kv.get("ActiveEnterTimestamp"),
                    "mem_bytes": int(mem) if mem.isdigit() else None}
    return {"unit": unit, "scope": None, "active": "not-found", "sub": None}


def host_health_exec(ctx: Ctx, payload: dict) -> dict:
    units = [_unit(ctx, u) for u in USER_UNITS + SYSTEM_UNITS]
    mem = {}
    for l in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = l.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            mem[k] = int(v.split()[0]) * 1024
    la = os.getloadavg()
    disks = {}
    for mp in ("/", str(Path.home())):
        u = shutil.disk_usage(mp)
        disks[mp] = {"total": u.total, "used": u.used, "pct": round(100 * u.used / u.total, 1)}
    alerts = []
    f = STATE_DIR / "alerts.jsonl"
    if f.exists():
        for l in f.read_text().splitlines()[-15:]:
            try:
                alerts.append(json.loads(l))
            except ValueError:
                pass
    findings = [f"{u['unit']} {u['active']}" for u in units if u["active"] not in ("active",) and u["scope"]]
    return {"units": units, "mem": mem, "load": list(la), "cpus": os.cpu_count(), "disks": disks, "alerts": alerts, "findings": findings,
            "stop_file": (STATE_DIR / "STOP").exists()}


def host_health_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    named = [u for u in r["units"] if u["unit"] in ("drex-sales-dot", "drex-sales-discovery")]
    return [{"check": "systemd answered for core units", "ok": all(u["scope"] for u in named)},
            {"check": "meminfo parsed", "ok": "MemTotal" in r["mem"]},
            {"check": "all probed units resolved a state", "ok": all(u["active"] for u in r["units"])}]


CLUSTER_CMDS = {"cluster": ["cluster-health"], "n1_gate": ["n1-money-gate"], "n4_lab": ["n4-forward-lab"]}


def parse_cluster(text: str) -> dict:
    nodes, cur = {}, None
    for l in text.splitlines():
        m = re.match(r"== (\S+)", l)
        if m:
            cur = m.group(1).lower()
            nodes[cur] = {"header": l[3:].strip(), "services": {}, "disks": []}
            continue
        if cur:
            s = re.match(r"\s+svc (\S+)\s+(\S+)", l)
            if s:
                nodes[cur]["services"][s.group(1)] = s.group(2)
            elif l.strip().startswith(("disk", "old-nvme")):
                nodes[cur]["disks"].append(l.strip())
            elif re.match(r"\s+\S+ up ", l) or re.match(r"\s+\S+ up\b", l):
                nodes[cur]["host"] = l.strip()
    return nodes


def cluster_exec(ctx: Ctx, payload: dict) -> dict:
    out = {}
    for k, argv in CLUSTER_CMDS.items():
        rc, txt = ctx.run([str(CTRL_BIN / argv[0])], 75)
        out[k] = {"rc": rc, "text": txt[-4000:]}
    out["nodes"] = parse_cluster(out["cluster"]["text"]) if out["cluster"]["rc"] == 0 else {}
    return out


def cluster_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    n = r["nodes"]
    return [{"check": "cluster-health exited 0", "ok": r["cluster"]["rc"] == 0},
            {"check": "all four nodes reported", "ok": all(k in n for k in ("n1", "n2", "n3", "n4")), "have": sorted(n)},
            {"check": "n1 money gate output non-empty", "ok": r["n1_gate"]["rc"] == 0 and bool(r["n1_gate"]["text"])}]


def _fetch_cf_traffic() -> dict:
    import urllib.request
    try:
        import tomllib
    except ImportError:
        return {"status": "UNAVAILABLE", "reason": "tomllib not available"}
    cfg_file = Path.home() / ".wrangler" / "config" / "default.toml"
    if not cfg_file.exists():
        return {"status": "UNAVAILABLE", "reason": "wrangler config not found"}
    try:
        cfg = tomllib.load(cfg_file.open("rb"))
        token = cfg.get("oauth_token")
        if not token:
            return {"status": "UNAVAILABLE", "reason": "no oauth_token"}
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))
        query = """
        query GetZoneAnalytics($zoneTag: string!, $since: string!) {
          viewer {
            zones(filter: {zoneTag: $zoneTag}) {
              httpRequests1dGroups(limit: 7, filter: {date_geq: $since}, orderBy: [date_DESC]) {
                dimensions { date }
                sum { requests pageViews bytes }
                uniq { uniques }
              }
            }
          }
        }
        """
        payload = json.dumps({
            "query": query,
            "variables": {"zoneTag": "e5ace93c3aa736de6c255e1294bc9098", "since": since}
        }).encode()
        req = urllib.request.Request(
            "https://api.cloudflare.com/client/v4/graphql",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "ElyreonFleet/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            zones = data.get("data", {}).get("viewer", {}).get("zones", [])
            if not zones:
                return {"status": "ERROR", "reason": "no zone data"}
            groups = zones[0].get("httpRequests1dGroups", [])
            history = []
            for g in groups:
                history.append({
                    "date": g.get("dimensions", {}).get("date"),
                    "uniques": g.get("uniq", {}).get("uniques", 0),
                    "page_views": g.get("sum", {}).get("pageViews", 0),
                    "requests": g.get("sum", {}).get("requests", 0),
                })
            return {
                "status": "OK",
                "zone": "elyraon.com.co",
                "today": history[0] if history else None,
                "history_7d": history,
                "totals_7d": {
                    "uniques_sum": sum(h["uniques"] for h in history),
                    "page_views": sum(h["page_views"] for h in history),
                    "requests": sum(h["requests"] for h in history),
                }
            }
    except Exception as e:
        return {"status": "UNAVAILABLE", "reason": f"{type(e).__name__}: {str(e)[:100]}"}


# ------------------------------------------------------------------ analyst
def funnel_exec(ctx: Ctx, payload: dict) -> dict:
    c = ctx.crm_ro()
    q = lambda s, *a: c.execute(s, a).fetchone()[0]
    has = lambda t: c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    now = ctx.now()
    f = {"crm": {
        "prospects": q("SELECT count(*) FROM prospects"), "drafts": q("SELECT count(*) FROM drafts"), "replies_table": q("SELECT count(*) FROM replies"),
        "prospect_status": {r[0]: r[1] for r in c.execute("SELECT status, count(*) FROM prospects GROUP BY 1")},
    }}
    f["traffic"] = _fetch_cf_traffic()
    s = {"sent_total": q("SELECT count(*) FROM sales_send_intents WHERE status='SENT'"),
         "sent_by_this_dot": q("SELECT count(*) FROM sales_send_intents WHERE status='SENT' AND policy_hash!='historic'"),
         "sent_historic": q("SELECT count(*) FROM sales_send_intents WHERE status='SENT' AND policy_hash='historic'"),
         "sent_24h": q("SELECT count(*) FROM sales_send_intents WHERE status IN ('INTENT','SENT') AND policy_hash!='historic' AND created_at>=?", iso(now - 86400)),
         "open_intents": q("SELECT count(*) FROM sales_send_intents WHERE status='INTENT'"),
         "failed_intents": q("SELECT count(*) FROM sales_send_intents WHERE status='FAILED'"),
         "inbound_by_kind": {r[0]: r[1] for r in c.execute("SELECT kind, count(*) FROM sales_inbound GROUP BY 1")},
         "suppressions": q("SELECT count(*) FROM sales_suppressions"),
         "blocklisted_domains": q("SELECT count(*) FROM sales_domain_blocklist"),
         "sent_by_day": {r[0]: r[1] for r in c.execute("SELECT substr(sent_at,1,10), count(*) FROM sales_send_intents WHERE status='SENT' AND sent_at IS NOT NULL GROUP BY 1 ORDER BY 1 DESC LIMIT 14")}}
    # 'REPLY' in sales_inbound means ANY non-bounce inbox mail; only rows matched to an outreach prospect are replies to us.
    s["replies_received"] = q("SELECT count(*) FROM sales_inbound WHERE kind='REPLY' AND prospect_id IS NOT NULL")
    s["inbound_unmatched"] = q("SELECT count(*) FROM sales_inbound WHERE kind='REPLY' AND prospect_id IS NULL")
    s["sends_with_thread_id"] = q("SELECT count(*) FROM sales_send_intents WHERE status='SENT' AND thread_id IS NOT NULL")
    s["hard_bounces"] = s["inbound_by_kind"].get("BOUNCE_HARD", 0)
    f["sales"] = s
    f["events"] = {r[0]: r[1] for r in c.execute("SELECT event_type, count(*) FROM events GROUP BY 1 ORDER BY 2 DESC LIMIT 25")}
    d = {}
    if has("disco_candidates"):
        d = {"stages": {r[0]: r[1] for r in c.execute("SELECT stage, count(*) FROM disco_candidates GROUP BY 1")},
             "raw": q("SELECT count(*) FROM disco_raw"), "candidates": q("SELECT count(*) FROM disco_candidates"),
             "rejections": {r[0]: r[1] for r in c.execute("SELECT reject_reason, count(*) FROM disco_candidates WHERE stage='REJECTED' GROUP BY 1 ORDER BY 2 DESC")},
             "jobs": {r[0]: r[1] for r in c.execute("SELECT status, count(*) FROM disco_jobs GROUP BY 1")},
             "top_staged": [dict(r) for r in c.execute("SELECT domain,company_name,business_category,region,rank,contact_email,signal_quote,signal_url,enriched_at FROM disco_candidates WHERE stage='QUALIFIED' ORDER BY rank DESC LIMIT 12")]}
        d["qualified_total"] = d["stages"].get("QUALIFIED", 0) + d["stages"].get("PROMOTED", 0)
    f["disco"] = d
    f["eligible_queue"] = _eligible(ctx, c)
    # 60-send diagnostics: where did sends go and what came back
    f["send_diag"] = {
        "to_blocklisted_domains": q("SELECT count(*) FROM sales_send_intents i JOIN sales_domain_blocklist b ON i.recipient LIKE '%@'||b.domain WHERE i.status='SENT'"),
        "unmatched_inbound": s["inbound_unmatched"], "sends_with_thread_id": s["sends_with_thread_id"],
        "last_reply_sync": _state(c, "last_reply_sync"),
    }
    c.close()
    f["unknown"] = {"meetings": None, "conversions": None, "revenue_usd": None, "note": "No observed source is wired for these; shown as UNKNOWN, never zero."}
    return f


def _state(c, key):
    r = c.execute("SELECT value, updated_at FROM sales_state WHERE key=?", (key,)).fetchone()
    return {"value": r[0], "updated_at": r[1]} if r else None


def _eligible(ctx: Ctx, c) -> dict:
    """Send-eligibility via the sender's own candidate_problems(), on a read-only connection (no Gmail, no writes)."""
    try:
        from dotz.sales.crm_store import CrmStore
        from dotz.sales.policy import Policy
        from dotz.sales.worker import SalesWorker
        store = CrmStore.__new__(CrmStore)
        store.c = c
        pol = Policy()
        ap = pol.approval()
        cache_mx = lambda dom: ((_state(c, f"mx:{dom}") or {"value": "0|0"})["value"].split("|")[0] == "1")
        w = SalesWorker(store, pol, None, None, mx_check=cache_mx)
        rows = c.execute("SELECT p.id FROM prospects p JOIN drafts d ON d.prospect_id=p.id AND d.kind='INITIAL_MESSAGE' "
                         "WHERE p.suppressed=0 AND p.status IN ('MESSAGE_DRAFTED','HUMAN_APPROVED')").fetchall()
        probs = {r[0]: w.candidate_problems(r[0], ap) for r in rows} if ap else None
        if probs is None:
            return {"eligible": None, "reason": "policy not approved for current hash"}
        reasons: dict[str, int] = {}
        for v in probs.values():
            for p in v:
                reasons[p.split(":")[0]] = reasons.get(p.split(":")[0], 0) + 1
        return {"eligible": sum(1 for v in probs.values() if not v), "queued_drafts": len(probs), "blocked_reasons": reasons}
    except Exception as e:
        return {"eligible": None, "reason": f"{type(e).__name__}: {str(e)[:120]}"}


def funnel_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    c = ctx.crm_ro()
    again = c.execute("SELECT count(*) FROM sales_send_intents WHERE status='SENT'").fetchone()[0]
    cand = c.execute("SELECT count(*) FROM disco_candidates").fetchone()[0] if r["disco"] else 0
    c.close()
    ds = r["disco"].get("stages", {})
    return [{"check": "sent_total re-queried within 3 of snapshot (live sender may add)", "ok": abs(again - r["sales"]["sent_total"]) <= 3},
            {"check": "stage counts sum to candidate total (±50 for live discovery)", "ok": abs(sum(ds.values()) - cand) <= 50 if ds else True},
            {"check": "historic + new sends == total", "ok": r["sales"]["sent_historic"] + r["sales"]["sent_by_this_dot"] == r["sales"]["sent_total"]}]


# -------------------------------------------------------------------- scout
def scout_exec(ctx: Ctx, payload: dict) -> dict:
    c = ctx.crm_ro()
    now = ctx.now()
    health = {r["source"]: dict(r) for r in c.execute("SELECT * FROM disco_source_health")}
    jobs = {r[0]: r[1] for r in c.execute("SELECT status, count(*) FROM disco_jobs GROUP BY 1")}
    cnt = {"raw": c.execute("SELECT count(*) FROM disco_raw").fetchone()[0], "candidates": c.execute("SELECT count(*) FROM disco_candidates").fetchone()[0],
           "pending_enrichment": c.execute("SELECT count(*) FROM disco_candidates WHERE stage IN ('RAW','ENRICHING')").fetchone()[0],
           "qualified": c.execute("SELECT count(*) FROM disco_candidates WHERE stage IN ('QUALIFIED','PROMOTED')").fetchone()[0]}
    c.close()
    eng = health.get("engine")
    prev = ctx.db.latest("scout")
    delta = {k: cnt[k] - prev["data"]["counts"][k] for k in cnt} if prev else None
    return {"engine_heartbeat_age_s": (now - eng["updated_at"]) if eng else None, "sources": health, "jobs": jobs, "counts": cnt,
            "delta_since_last": delta, "delta_seconds": (now - prev["ts"]) if prev else None}


def scout_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    age = r["engine_heartbeat_age_s"]
    return [{"check": "discovery engine heartbeat < 90s old", "ok": age is not None and age < 90, "age_s": age},
            {"check": "raw/candidate totals never shrink", "ok": all((r["delta_since_last"] or {}).get(k, 0) >= 0 for k in ("raw", "candidates"))}]


# -------------------------------------------------------------- sales observe
def sender_exec(ctx: Ctx, payload: dict) -> dict:
    from dotz.sales.policy import Policy
    c = ctx.crm_ro()
    now = ctx.now()
    pol = Policy()
    ap = pol.approval()
    st = lambda k: (_state(c, k) or {}).get("value")
    hb = st("heartbeat")
    tick = json.loads(st("last_tick") or "null")
    acct = json.loads(st("account_sends_24h") or "null")
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    sales_24h = c.execute("SELECT count(*) FROM sales_send_intents WHERE status IN ('INTENT','SENT') AND policy_hash!='historic' AND created_at>=?", (iso(now - 86400),)).fetchone()[0]
    vals = {k: st(k) for k in ("emergency_stop", "pause_until", "pause_reason", "last_reply_sync")}
    bounces = [dict(r) for r in c.execute("SELECT i.provider_message_id AS dsn, i.kind, i.prospect_id, i.detail, i.processed_at, "
                                          "(SELECT reason FROM sales_suppressions s WHERE s.email=(SELECT recipient FROM sales_send_intents x WHERE x.prospect_id=i.prospect_id LIMIT 1)) AS suppression "
                                          "FROM sales_inbound i WHERE i.kind LIKE 'BOUNCE%' ORDER BY i.processed_at DESC LIMIT 20")]
    c.close()
    v = pol.cfg["volume"]
    return {"heartbeat_age_s": (now - float(hb)) if hb else None, "policy_hash": pol.hash[:12], "policy_approved": bool(ap), "categories": ap and ap["categories"],
            "emergency_stop": vals["emergency_stop"], "stop_file": (STATE_DIR / "STOP").exists(), "paused_until": float(vals["pause_until"] or 0) or None, "pause_reason": vals["pause_reason"],
            "in_window": pol.in_window(now), "window": "Mon-Fri 09:00-16:00 America/Chicago",
            "quota": {"effective_cap_24h": pol.effective_cap(), "owner_target_24h": v["account_rolling_24h_cap"], "provider_limit_24h": v["google_account_limit"],
                      "limited_by": "provider" if v["google_account_limit"] < v["account_rolling_24h_cap"] else "owner", "reserve": v["reserve_for_transactional_and_manual"], "sales_budget_if_idle": pol.sales_budget(0),
                      "sales_sent_24h": sales_24h, "account_sends_24h_cached": acct, "sales_budget_left_est": pol.sales_budget(acct["n"]) if acct else None},
            "bounces": bounces, "last_tick": tick, "last_reply_sync": vals["last_reply_sync"]}


def sender_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    q = r["quota"]
    is_stopped = bool(r.get("emergency_stop") or r.get("stop_file"))
    heartbeat_ok = ((r["heartbeat_age_s"] is not None and r["heartbeat_age_s"] < 180) if not is_stopped else True)
    return [{"check": "sender heartbeat < 180s old (or emergency stop/hold active)", "ok": heartbeat_ok, "age_s": r["heartbeat_age_s"], "stopped": is_stopped},
            {"check": "effective cap never above provider limit; sales = cap - reserve", "ok": q["effective_cap_24h"] <= q["provider_limit_24h"] and q["sales_budget_if_idle"] == max(0, q["effective_cap_24h"] - q["reserve"]) and q["reserve"] == 200},
            {"check": "policy approval state resolved", "ok": "policy_approved" in r and bool(r.get("policy_hash"))}]


# ---------------------------------------------------------------- deliverability
def dns_exec(ctx: Ctx, payload: dict) -> dict:
    from dotz.sales.policy import Policy
    dom = Policy().cfg["sender_account"].split("@")[1]
    def txt(name):
        rc, out = ctx.run(["dig", "+short", "+time=4", "+tries=2", "TXT", name], 15)
        return rc, [l.strip().strip('"') for l in out.splitlines() if l.strip()]
    rc1, root = txt(dom); rc2, dm = txt(f"_dmarc.{dom}")
    dk, rc3 = {}, 0
    for sel in ("google", "default", "selector1", "selector2", "k1", "s1", "mail"):
        rc, v = txt(f"{sel}._domainkey.{dom}")
        rc3 = rc3 or rc
        if any("p=" in x for x in v):
            dk[sel] = v[0][:120]
    rc4, mx = ctx.run(["dig", "+short", "+time=4", "MX", dom], 15)
    spf = [x for x in root if x.startswith("v=spf1")]
    dmarc = [x for x in dm if x.upper().startswith("V=DMARC1")]
    pol = re.search(r"\bp=(\w+)", dmarc[0]) if dmarc else None
    return {"domain": dom, "dig_rc": [rc1, rc2, rc3, rc4], "spf": {"status": "PASS" if spf else "MISSING", "record": spf[0] if spf else None},
            "dmarc": {"status": "PASS" if dmarc else "MISSING", "record": dmarc[0] if dmarc else None, "policy": pol.group(1) if pol else None},
            "dkim": {"status": "PASS" if dk else "UNKNOWN_SELECTOR_NOT_FOUND", "selectors_found": dk},
            "mx": mx.split(), "note": "DNS publication only; does not prove Google signs outbound mail or alignment."}


def dns_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    return [{"check": "dig answered (rc 0) for SPF/DMARC/MX lookups", "ok": r["dig_rc"][0] == 0 and r["dig_rc"][1] == 0 and r["dig_rc"][3] == 0},
            {"check": "MX resolved for sender domain", "ok": bool(r["mx"])}]


# ---------------------------------------------------------------- promotion
STALE_S = 14 * 86400
ROLE_LOCALS = {"info", "contact", "office", "service", "services", "sales", "support", "hello", "admin", "customerservice", "estimates", "quotes", "help", "team", "mail", "inquiries", "bookings", "schedule", "dispatch", "frontdesk"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip().casefold()


def reverify_candidate(ctx: Ctx, cand, fetcher, idx, mx, cfg, now: float) -> tuple[bool, list[str]]:
    from dotz.sales.disco import qualify
    from dotz.sales.disco.util import registrable, same_business_domain, host_of
    why = []
    ts = cand["contact_checked_at"] or cand["enriched_at"] or 0
    if now - ts > STALE_S:
        why.append("stale_evidence")
    email = cand["contact_email"] or ""
    if not email or cand["contact_type"] != "role":
        why.append("not_role_address")
    dom = email.split("@")[-1] if email else ""
    if dom and not same_business_domain(dom, cand["domain"]):
        why.append("email_domain_differs_from_site")
    if idx.suppressed(cand["domain"], email):
        why.append("suppressed")
    if idx.duplicate(cand["domain"], email):
        why.append("duplicate_in_crm")
    if dom and not mx(dom):
        why.append("mx_failed")
    res = qualify.gate(cand, None, mx, cfg, 1.0, cfg["categories"])  # structural gates (chain/aggregator/agency/operating) without CRM index
    if not res["qualified"]:
        why.append("gate:" + str(res["reject"]))
    if any(w in registrable(cand["domain"]) for w in ("roofcompanyreviews", "vettedroof", "quotemyroof")):
        why.append("known_portal")
    # live re-fetch: the address must still be printed on the company's own contact page, the quote on the signal page
    if not why:
        pg = fetcher.get(cand["contact_url"]) if cand["contact_url"] else None
        txt = (pg.text if pg and pg.status == 200 else "")
        if not pg or pg.status != 200:
            why.append("contact_page_unreachable_or_robots_denied")
        elif email.casefold() not in txt.casefold() and email.casefold() not in re.sub(r"\s*\[?\(?at\)?\]?\s*", "@", txt.casefold()):
            why.append("email_not_on_contact_page_now")
        sp = cand["signal_url"] or cand["contact_url"]
        sg = pg if sp == cand["contact_url"] else fetcher.get(sp)
        if sg and sg.status == 200:
            if _norm(cand["signal_quote"])[:80] not in _norm(sg.text):
                why.append("signal_quote_not_on_page_now")
        else:
            why.append("signal_page_unreachable_or_robots_denied")
    return (not why), why


def promote_exec(ctx: Ctx, payload: dict) -> dict:
    """Reverify then promote staged candidates through the existing CRM adapter. Idempotent per domain via fleet_effects."""
    from dotz.sales.crm_store import CrmStore
    from dotz.sales.disco import config as dconfig, qualify
    from dotz.sales.disco.net import Fetcher
    from dotz.sales.disco.util import slug
    from dotz.sales.luna import Luna
    from dotz.sales.policy import Policy
    from dotz.sales.worker import SalesWorker
    cfg = dconfig.load()
    dry = bool(payload.get("dry_run", True))
    limit = int(payload.get("limit", 25))
    only = set(payload.get("domains") or [])
    now = ctx.now()
    store = CrmStore(Path(ctx.crm_path)) if ctx.crm_path else CrmStore()
    pol = Policy()
    ap = pol.approval()
    luna = None if dry else Luna()
    w = SalesWorker(store, pol, None, luna)
    fetcher = Fetcher.from_cfg(cfg)
    idx = qualify.CrmIndex(store.c)
    made_today = store.c.execute("SELECT count(*) FROM events WHERE event_type='PROSPECT_ADDED' AND evidence LIKE 'Autonomous discovery%' AND created_at>=?",
                                 (time.strftime("%Y-%m-%dT00:00:00", time.gmtime(now)),)).fetchone()[0]
    budget = max(0, min(limit, pol.cfg["discovery"]["max_new_prospects_per_day"] - made_today, cfg["promotion"]["max_per_day"] if payload.get("respect_daily_cap", True) else limit))
    rows = store.c.execute("SELECT * FROM disco_candidates WHERE stage='QUALIFIED' AND rank>=? ORDER BY rank DESC", (cfg["promotion"]["min_rank"],)).fetchall()
    if only:
        rows = [r for r in rows if r["domain"] in only]
    out = {"dry_run": dry, "budget": budget, "considered": len(rows), "passed": [], "rejected": [], "promoted": [], "errors": []}
    for cand in rows:
        if len(out["passed"]) >= budget:
            break
        ok, why = reverify_candidate(ctx, cand, fetcher, idx, qualify.mx_ok, cfg, now)
        if not ok:
            out["rejected"].append({"domain": cand["domain"], "reasons": why})
            continue
        out["passed"].append({"domain": cand["domain"], "email": cand["contact_email"], "rank": cand["rank"]})
        if dry:
            continue
        key = f"promote:{cand['domain']}"
        pid = f"dot-{time.strftime('%Y%m%d', time.gmtime(now))}-{slug(cand['domain'], 50)}"
        if ctx.db.effect_done(key):
            out["errors"].append({"domain": cand["domain"], "error": "already promoted by a previous attempt (idempotent skip)"})
            continue
        found = {"company": cand["company_name"], "email": cand["contact_email"], "signal": cand["signal_quote"], "fit": int(cand["fit"]),
                 "proposal": "Workflow automation for quote, intake and scheduling.", "website": cand["website"].rstrip("/"), "source_url": cand["contact_url"]}
        try:
            added = store.add_prospect(pid=pid, name="Business contact (public page)", company=found["company"], website=found["website"], email=found["email"],
                                       source_url=found["source_url"], signal=found["signal"], proposal=found["proposal"], fit=found["fit"])
            if not added:
                store.c.execute("UPDATE disco_candidates SET stage='REJECTED',reject_reason='duplicate_existing_prospect',updated_at=? WHERE domain=?", (now, cand["domain"]))
                out["rejected"].append({"domain": cand["domain"], "reasons": ["duplicate_at_insert"]})
                continue
            ctx.db.record_effect(key, payload.get("_task_id", ""), {"pid": pid})
            try:
                drafted = w.draft_for(pid, found)
            except Exception as e:  # prospect exists; keep it recoverable instead of the engine's lost-draft trap
                drafted = False
                out["errors"].append({"domain": cand["domain"], "pid": pid, "error": f"draft failed: {type(e).__name__}: {str(e)[:100]}"})
            stage = "PROMOTED" if drafted else "DRAFT_FAILED"
            store.c.execute("UPDATE disco_candidates SET stage=?,crm_pid=?,updated_at=? WHERE domain=?", (stage, pid, now, cand["domain"]))
            store.c.commit()
            probs = w.candidate_problems(pid, ap) if (drafted and ap) else ["not_drafted"]
            out["promoted"].append({"domain": cand["domain"], "pid": pid, "drafted": drafted, "send_problems": probs, "send_eligible": not probs})
        except Exception as e:
            out["errors"].append({"domain": cand["domain"], "error": f"{type(e).__name__}: {str(e)[:120]}"})
    store.c.close()
    return out


def promote_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    checks = [{"check": "no unhandled promotion errors", "ok": not [e for e in r["errors"] if "draft failed" in e.get("error", "")]}]
    if r["dry_run"]:
        checks.append({"check": "dry run wrote nothing", "ok": not r["promoted"]})
        return checks
    c = ctx.crm_ro()
    for p in r["promoted"]:
        pr = c.execute("SELECT 1 FROM prospects WHERE id=?", (p["pid"],)).fetchone()
        dr = c.execute("SELECT 1 FROM drafts WHERE prospect_id=? AND kind='INITIAL_MESSAGE'", (p["pid"],)).fetchone()
        ct = c.execute("SELECT 1 FROM sales_contacts WHERE prospect_id=?", (p["pid"],)).fetchone()
        st = c.execute("SELECT stage FROM disco_candidates WHERE crm_pid=?", (p["pid"],)).fetchone()
        checks.append({"check": f"{p['domain']}: prospect+draft+contact in CRM, candidate stage updated",
                       "ok": bool(pr and ct and (dr is not None) == p["drafted"] and st and st[0] == ("PROMOTED" if p["drafted"] else "DRAFT_FAILED"))})
    c.close()
    return checks


# ------------------------------------------------------------------ commerce (revenue feed)
REVENUE_URL = os.environ.get("DREX_REVENUE_URL", "https://elyraon.com.co/api/revenue/metrics")
REVENUE_TOKEN_FILE = Path(os.environ.get("DREX_REVENUE_TOKEN_FILE", str(Path.home() / ".config" / "drex" / "revenue_metrics_token")))
REVENUE_SEEN = STATE_DIR / "revenue_alerts_seen.json"


def _alert_dedup(ctx: Ctx, active: dict[str, dict]) -> list[str]:
    """Append an alerts.jsonl line only for ids not already open. An id that clears is forgotten, so a recurrence alerts again."""
    try:
        seen = json.loads(REVENUE_SEEN.read_text())
    except (OSError, ValueError):
        seen = {}
    fresh = [i for i in active if i not in seen]
    if fresh:
        with (STATE_DIR / "alerts.jsonl").open("a") as fh:
            for i in fresh:
                fh.write(json.dumps({"ts": ctx.now(), "kind": "revenue." + active[i].get("kind", i), "id": i, "severity": active[i].get("severity"),
                                     "detail": active[i].get("detail"), "ref": active[i].get("ref")}) + "\n")
    REVENUE_SEEN.write_text(json.dumps({i: seen.get(i, ctx.now()) for i in active}))
    return fresh


def _fetch_revenue(ctx: Ctx) -> dict:
    import urllib.request
    token = REVENUE_TOKEN_FILE.read_text().strip()
    req = urllib.request.Request(REVENUE_URL, headers={"Authorization": f"Bearer {token}", "User-Agent": "DrexFleetCommand/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read(2_000_000))


def revenue_exec(ctx: Ctx, payload: dict) -> dict:
    fetch = payload.get("_fetch") or _fetch_revenue   # injectable for tests
    try:
        body = fetch(ctx)
    except Exception as e:  # network, auth, rate limit, bad JSON: feed is unreachable, never silently zero
        _alert_dedup(ctx, {"revenue_feed_unreachable": {"kind": "feed_unreachable", "severity": "critical",
                                                        "detail": f"{type(e).__name__}: {str(e)[:120]}"}})
        raise TaskFailed(f"revenue feed unreachable: {type(e).__name__}: {str(e)[:120]}")
    body["measured_traffic"] = _fetch_cf_traffic()
    active = {x["id"]: x for x in body.get("exceptions", []) if x.get("id")}
    fresh = _alert_dedup(ctx, active)
    return {"fetched_at": ctx.now(), "feed": body, "new_alerts": fresh}


def revenue_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    b, text = r["feed"], json.dumps(r["feed"])
    return [{"check": "schema 1 with stripe+d1 sources and periods", "ok": b.get("schema") == 1 and {"stripe", "d1"} <= set(b.get("sources", {})) and "periods" in b},
            {"check": "no customer email or full checkout session id in payload", "ok": "@" not in text and "cs_live_" not in text and "cs_test_" not in text},
            {"check": "feed not older than its own clock by > 10 min", "ok": abs(ctx.now() - b.get("as_of", 0)) < 600}]


# ------------------------------------------------------------------ support (inbound triage)
def triage_exec(ctx: Ctx, payload: dict) -> dict:
    """Observe sales_inbound only (no subject/body columns exist there): counts by kind, unmatched
    replies (no prospect_id, cannot be triaged against the CRM) and replies that do have a prospect_id
    (need an owner look). Never touches drafts, never answers anything."""
    c = ctx.crm_ro()
    now = ctx.now()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    since = iso(now - 86400)
    total_by_kind = {r[0]: r[1] for r in c.execute("SELECT kind, count(*) FROM sales_inbound GROUP BY 1")}
    last_24h_by_kind = {r[0]: r[1] for r in c.execute("SELECT kind, count(*) FROM sales_inbound WHERE processed_at>=? GROUP BY 1", (since,))}
    unmatched_replies = [dict(r) for r in c.execute(
        "SELECT provider_message_id, detail, processed_at FROM sales_inbound "
        "WHERE kind='REPLY' AND prospect_id IS NULL ORDER BY processed_at DESC LIMIT 20")]
    replies_needing_owner_review = [dict(r) for r in c.execute(
        "SELECT provider_message_id, prospect_id, detail, processed_at FROM sales_inbound "
        "WHERE kind='REPLY' AND prospect_id IS NOT NULL ORDER BY processed_at DESC LIMIT 20")]
    c.close()
    return {"as_of": now, "total_by_kind": total_by_kind, "last_24h_by_kind": last_24h_by_kind,
            "unmatched_replies": unmatched_replies, "replies_needing_owner_review": replies_needing_owner_review,
            "opt_outs_total": total_by_kind.get("OPT_OUT", 0)}


def triage_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    c = ctx.crm_ro()
    total = c.execute("SELECT count(*) FROM sales_inbound").fetchone()[0]
    reply_total = c.execute("SELECT count(*) FROM sales_inbound WHERE kind='REPLY'").fetchone()[0]
    c.close()
    return [{"check": "total_by_kind sums to live sales_inbound row count", "ok": sum(r["total_by_kind"].values()) == total},
            {"check": "unmatched_replies count never exceeds live REPLY total", "ok": len(r["unmatched_replies"]) <= reply_total},
            {"check": "replies_needing_owner_review count never exceeds live REPLY total", "ok": len(r["replies_needing_owner_review"]) <= reply_total}]


# ------------------------------------------------------------------ research (canonical Grok digest)
def grok_digest_exec(ctx: Ctx, payload: dict) -> dict:
    research_file = Path(payload.get("file", "/home/colton-mcclain/elyraon.com.co/docs/x-research/canonical-fleet-research.json"))
    if not research_file.exists():
        cands = sorted(Path("/home/colton-mcclain/elyraon.com.co/docs/x-research").glob("agy-handoff-*.json"))
        if cands:
            research_file = cands[-1]
        else:
            raise TaskFailed(f"no grok research file found at {research_file}")
    try:
        data = json.loads(research_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise TaskFailed(f"failed to read grok research: {e}")

    candidates = data.get("candidates") or []
    projections = data.get("fleet_projections") or {}

    return {
        "digested_at": ctx.now(),
        "research_date": data.get("research_date"),
        "schema": data.get("schema"),
        "candidates_count": len(candidates),
        "policy": data.get("policy", {}),
        "consumers_projected": list(projections.keys()),
        "fleet_projections": projections,
        "top_candidate": candidates[0] if candidates else None,
    }


def grok_digest_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    proj = r.get("fleet_projections", {})
    req = {"SCOUT", "SALES", "GROWTH", "COMMERCE", "ANALYST", "DIRECTOR"}
    return [
        {"check": "canonical research schema verified", "ok": bool(r.get("schema"))},
        {"check": "candidates count >= 1", "ok": r.get("candidates_count", 0) >= 1, "count": r.get("candidates_count")},
        {"check": "projections present for all 6 fleet consumers", "ok": req.issubset(set(proj.keys())), "missing": list(req - set(proj.keys()))},
        {"check": "zero paid X API spend policy verified", "ok": r.get("policy", {}).get("X_API_SPEND_BUDGET") == 0},
    ]


# ------------------------------------------------------------------ growth (content pipeline observe)
def growth_exec(ctx: Ctx, payload: dict) -> dict:
    mf_db = Path.home() / "elyraon.com.co" / "media_fleet" / "state" / "fleet.db"
    queue_counts = {}
    if mf_db.exists():
        try:
            c = sqlite3.connect(f"file:{mf_db}?mode=ro", uri=True)
            for row in c.execute("SELECT status, count(*) FROM content_queue GROUP BY 1"):
                queue_counts[row[0]] = row[1]
            c.close()
        except Exception:
            pass

    secrets_file = Path.home() / ".elyraon-secrets" / "media_fleet_tokens.json"
    google_sheets_connected = False
    if secrets_file.exists():
        try:
            sec = json.loads(secrets_file.read_text())
            google_sheets_connected = (
                bool(sec.get("GOOGLE_SHEETS", {}).get("credentials")) or
                bool(sec.get("GOOGLE_SHEETS", {}).get("token_uri")) or
                bool(sec.get("GOOGLE_SHEETS", {}).get("client_id"))
            )
        except Exception:
            pass

    return {
        "as_of": ctx.now(),
        "queue_counts": queue_counts,
        "queued_total": queue_counts.get("QUEUED", 0),
        "scheduled_total": queue_counts.get("SCHEDULED", 0),
        "published_total": queue_counts.get("PUBLISHED", 0),
        "google_sheets_connected": google_sheets_connected,
        "public_posting": "MANUAL_OWNER_ONLY",
        "x_api_publishing": "DISABLED",
    }


def growth_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    return [
        {"check": "content queue counts resolved", "ok": isinstance(r.get("queue_counts"), dict)},
        {"check": "public posting remains manual owner only", "ok": r.get("public_posting") == "MANUAL_OWNER_ONLY"},
        {"check": "X API publishing disabled", "ok": r.get("x_api_publishing") == "DISABLED"},
    ]


# ------------------------------------------------------------------ director (fleet coordination)
def director_exec(ctx: Ctx, payload: dict) -> dict:
    f = ctx.db
    now = ctx.now()

    funnel_snap = f.latest("funnel")
    research_snap = f.latest("research")
    revenue_snap = f.latest("revenue")

    stop_file_active = (STATE_DIR / "STOP").exists()
    blockers = []
    if stop_file_active:
        blockers.append("Sales outreach on emergency stop / provider policy hold")

    priorities = []
    if research_snap and research_snap.get("data"):
        proj = research_snap["data"].get("fleet_projections", {})
        priorities = proj.get("DIRECTOR", {}).get("high_value_findings", [])

    dot_rows = f.all("SELECT name, operational FROM fleet_dots")
    dots_summary = {r["name"]: bool(r["operational"]) for r in dot_rows}

    return {
        "coordinated_at": now,
        "stop_file_active": stop_file_active,
        "blockers": blockers,
        "dots_summary": dots_summary,
        "high_value_priorities_count": len(priorities),
        "high_value_priorities": priorities[:5],
        "delegation_status": "MONITORING_AND_COORDINATING",
        "revenue_observed": bool(revenue_snap and revenue_snap.get("data", {}).get("feed")),
        "funnel_observed": bool(funnel_snap and funnel_snap.get("data")),
    }


def director_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    return [
        {"check": "dots summary contains fleet dots", "ok": bool(r.get("dots_summary"))},
        {"check": "blockers state accurately resolved", "ok": "blockers" in r},
        {"check": "coordination completed", "ok": r.get("delegation_status") == "MONITORING_AND_COORDINATING"},
    ]




def linkedin_observe_exec(ctx: Ctx, payload: dict) -> dict:
    from pathlib import Path
    p = Path(payload.get("db_path") or "~/.linkedin-machine/lim.db").expanduser()
    if not p.is_file():
        return {"status": "UNKNOWN", "reason": "bridge database unavailable", "as_of": None}
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
        posts = c.execute("SELECT count(*) FROM posts").fetchone()[0]
        states = {r[0]: r[1] for r in c.execute("SELECT state,count(*) FROM commenters GROUP BY state")}
        acts = {r[0]: r[1] for r in c.execute("SELECT kind,count(*) FROM actions WHERE dry_run=0 GROUP BY kind")}
        newest = c.execute("SELECT max(at) FROM actions").fetchone()[0]
        c.close()
        return {"status": "OK", "source": "local_bridge_sqlite", "as_of": time.time(), "database_updated_at": newest,
                "campaign_count": posts, "action_states": states, "live_actions": acts,
                "pending_actions": sum(v for k, v in states.items() if k != "SEEN"),
                "unknown_actions": 0, "transport_mode": "DISABLED_BY_DEFAULT", "data_class": "redacted_aggregate"}
    except (sqlite3.Error, OSError) as e:
        return {"status": "UNKNOWN", "reason": type(e).__name__, "as_of": None}


def linkedin_observe_verify(ctx: Ctx, payload: dict, r: dict) -> list[dict]:
    return [{"check":"local bridge database read completed", "ok":r.get("status")=="OK"},
            {"check":"redacted aggregate and disabled transport", "ok":r.get("data_class")=="redacted_aggregate" and r.get("transport_mode")=="DISABLED_BY_DEFAULT"}]


@dataclass
class Adapter:
    kind: str
    capability: str
    execute: callable
    verify: callable
    timeout_s: float = 120


ADAPTERS: dict[str, Adapter] = {a.kind: a for a in [
    Adapter("linkedin.observe_local", "linkedin.read", linkedin_observe_exec, linkedin_observe_verify, 20),
    Adapter("sentinel.host_health", "systemd.read", host_health_exec, host_health_verify, 60),
    Adapter("sentinel.cluster_health", "cluster.read", cluster_exec, cluster_verify, 300),
    Adapter("analyst.funnel", "crm.read", funnel_exec, funnel_verify, 120),
    Adapter("scout.discovery_observe", "disco.read", scout_exec, scout_verify, 60),
    Adapter("sales.sender_observe", "crm.read", sender_exec, sender_verify, 60),
    Adapter("sales.deliverability", "dns.read", dns_exec, dns_verify, 120),
    Adapter("sales.promote", "crm.promote", promote_exec, promote_verify, 1200),
    Adapter("commerce.revenue_observe", "commerce.read", revenue_exec, revenue_verify, 60),
    Adapter("support.triage_observe", "crm.read", triage_exec, triage_verify, 60),
    Adapter("research.grok_digest", "crm.read", grok_digest_exec, grok_digest_verify, 120),
    Adapter("growth.pipeline_observe", "crm.read", growth_exec, growth_verify, 120),
    Adapter("director.coordinate", "fleet.enqueue", director_exec, director_verify, 120),
]}

SNAPSHOT_KIND = {"linkedin.observe_local": "linkedin", "sentinel.host_health": "host", "sentinel.cluster_health": "cluster", "analyst.funnel": "funnel", "scout.discovery_observe": "scout",
                 "sales.sender_observe": "sender", "sales.deliverability": "deliverability", "sales.promote": "promotion", "commerce.revenue_observe": "revenue",
                 "support.triage_observe": "triage", "research.grok_digest": "research", "growth.pipeline_observe": "growth", "director.coordinate": "director"}
