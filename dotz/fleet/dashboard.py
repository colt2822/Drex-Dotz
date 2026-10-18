"""DREX FLEET COMMAND: dashboard. Read-only except the Director owner actions; bound to 127.0.0.1, real data only.

Every figure carries its source and timestamp; the UI marks it STALE past `stale_after_s` and UNKNOWN when no
real source exists. Nothing here writes to the CRM, the fleet queue or any service.
"""
from __future__ import annotations

import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotz.fleet.db import DB_PATH, STATE_DIR

OK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def ro(path: Path) -> sqlite3.Connection | None:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def crm_path() -> Path:
    from dotz.sales.crm_store import _crm
    return Path(_crm.DB)


def snap(f: sqlite3.Connection, kind: str) -> dict:
    r = f.execute("SELECT ts,data FROM fleet_snapshots WHERE kind=? ORDER BY seq DESC LIMIT 1", (kind,)).fetchone()
    return {"as_of": r["ts"], "data": json.loads(r["data"])} if r else {"as_of": None, "data": None}


def rows(f, sql, *a):
    return [dict(r) for r in f.execute(sql, a).fetchall()]


def fleet_core(f, now) -> dict:
    counts = {r[0]: r[1] for r in f.execute("SELECT status, count(*) FROM fleet_tasks GROUP BY 1")}
    d24 = {r[0]: r[1] for r in f.execute("SELECT status, count(*) FROM fleet_tasks WHERE updated_at>? GROUP BY 1", (now - 86400,))}
    workers = rows(f, "SELECT w.*, t.kind current_kind FROM fleet_workers w LEFT JOIN fleet_tasks t ON t.task_id=w.current_task ORDER BY dot")
    latest: dict = {}
    for w in workers:  # one row per dot: the newest incarnation (older pids are history, not "down workers")
        if w["dot"] not in latest or w["heartbeat_at"] > latest[w["dot"]]["heartbeat_at"]:
            latest[w["dot"]] = w
    workers = list(latest.values())
    for w in workers:
        w["hb_age_s"] = now - w["heartbeat_at"]
        w["alive"] = w["status"] != "STOPPED" and w["hb_age_s"] < 30
    dots = rows(f, "SELECT * FROM fleet_dots ORDER BY operational DESC, name")
    return {"counts": counts, "last24h": d24, "workers": workers, "dots": dots}


def model_cost() -> dict:
    c = ro(STATE_DIR / "fleet-runtime" / "dotz.db")
    if not c:
        return {"fleet_model_usd": None, "calls": None}
    try:
        r = c.execute("SELECT count(*), sum(cost_usd) FROM model_calls").fetchone()
        return {"fleet_model_usd": r[1] or 0.0, "calls": r[0]}
    except sqlite3.Error:
        return {"fleet_model_usd": None, "calls": None}
    finally:
        c.close()


def live_sales(now) -> dict:
    p = crm_path()
    c = ro(p)
    if not c:
        return {"error": "CRM unreadable"}
    try:
        q = lambda s, *a: c.execute(s, a).fetchone()[0]
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
        return {"as_of": now, "sent_total": q("SELECT count(*) FROM sales_send_intents WHERE status='SENT'"),
                "sent_24h": q("SELECT count(*) FROM sales_send_intents WHERE status IN ('INTENT','SENT') AND policy_hash!='historic' AND created_at>=?", iso(now - 86400)),
                "replies": q("SELECT count(*) FROM sales_inbound WHERE kind='REPLY' AND prospect_id IS NOT NULL"),
                "inbound_unmatched": q("SELECT count(*) FROM sales_inbound WHERE kind='REPLY' AND prospect_id IS NULL"),
                "bounces": q("SELECT count(*) FROM sales_inbound WHERE kind LIKE 'BOUNCE%'"),
                "opt_outs": q("SELECT count(*) FROM sales_inbound WHERE kind='OPT_OUT'"),
                "prospects": q("SELECT count(*) FROM prospects"),
                "drafted_queue": q("SELECT count(*) FROM prospects WHERE status='MESSAGE_DRAFTED' AND suppressed=0")}
    except sqlite3.Error as e:
        return {"error": str(e)[:100]}
    finally:
        c.close()


def build(view: str) -> dict:
    now = time.time()
    f = ro(DB_PATH)
    if not f:
        return {"error": "fleet.db not found: supervisor has never run", "now": now}
    try:
        core = fleet_core(f, now)
        funnel, sender, scout = snap(f, "funnel"), snap(f, "sender"), snap(f, "scout")
        linkedin = snap(f, "linkedin")
        out = {"now": now, "view": view, "core": {k: core[k] for k in ("counts", "last24h")}}
        if view == "overview":
            ls = live_sales(now)
            fu = funnel["data"] or {}
            ds = fu.get("disco", {})
            out |= {"workers": core["workers"], "live_sales": ls, "funnel": funnel, "scout": scout, "sender": sender,
                    "cost": model_cost(), "operational": [d["name"] for d in core["dots"] if d["operational"]],
                    "not_operational": [d["name"] for d in core["dots"] if not d["operational"]],
                    "linkedin": linkedin, "revenue": snap(f, "revenue"), "research": snap(f, "research"), "growth": snap(f, "growth"),
                    "director": snap(f, "director"), "support": snap(f, "triage"),
                    "unknown": {"verified_sales": "no source wired", "revenue_usd": "no source wired",
                                "meetings": "no source wired", "conversions": "no source wired", "luna_subscription_cost": "usage not metered"},
                    "recent": rows(f, "SELECT task_id,dot,kind,outcome,detail,at FROM fleet_attempts ORDER BY seq DESC LIMIT 14"),
                    "disco_qualified": ds.get("qualified_total")}
        elif view == "dots":
            dots = core["dots"]
            wmap = {w["dot"]: w for w in core["workers"]}
            for d in dots:
                d["capabilities"], d["budget"] = json.loads(d["capabilities"]), json.loads(d["budget"])
                d["worker"] = wmap.get(d["name"])
                d["task_counts"] = {r[0]: r[1] for r in f.execute("SELECT status, count(*) FROM fleet_tasks WHERE dot=? GROUP BY 1", (d["name"],))}
                d["history"] = rows(f, "SELECT task_id,kind,attempt,outcome,detail,receipt_id,at FROM fleet_attempts WHERE dot=? ORDER BY seq DESC LIMIT 8", d["name"])
                d["last_error"] = (f.execute("SELECT last_error,updated_at FROM fleet_tasks WHERE dot=? AND last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1", (d["name"],)).fetchone() or [None])[0]
            rc = ro(STATE_DIR / "fleet-runtime" / "dotz.db")
            receipts = {}
            if rc:
                receipts = {r["dot"]: dict(r) for r in rc.execute("SELECT dot, count(*) n, sum(decision='BLOCK') blocked, max(ts) last_ts FROM receipts GROUP BY dot")}
                rc.close()
            for d in dots:
                d["receipts"] = receipts.get(d["name"], {"n": 0, "blocked": 0, "last_ts": None})
            out["dots"] = dots
        elif view == "sales":
            out |= {"live_sales": live_sales(now), "funnel": funnel, "sender": sender, "scout": scout, "deliverability": snap(f, "deliverability"),
                    "promotion": snap(f, "promotion"), "dry_promotion": None}
        elif view == "linkedin":
            schedule_path = Path(__file__).with_name("linkedin_manual_schedule.json")
            try:
                plan = json.loads(schedule_path.read_text(encoding="utf-8"))
                out["linkedin_schedule"] = {"status": "READY", "source": "local_manual_schedule_file",
                                             "reporting_mode": "MANUAL_REPORTS_ONLY", "plan": plan}
            except (OSError, json.JSONDecodeError) as e:
                out["linkedin_schedule"] = {"status": "UNKNOWN", "reason": type(e).__name__,
                                             "source": str(schedule_path), "reporting_mode": "MANUAL_REPORTS_ONLY"}
            out["linkedin"] = snap(f, "linkedin")
        elif view == "revenue":
            rv = snap(f, "revenue")
            last = f.execute("SELECT outcome,detail,at FROM fleet_attempts WHERE kind='commerce.revenue_observe' ORDER BY seq DESC LIMIT 1").fetchone()
            ok_at = rv["as_of"]
            out |= {"revenue": {"as_of": ok_at, "feed": (rv["data"] or {}).get("feed"),
                                "last_attempt": dict(last) if last else None,
                                "unreachable": bool(last and last["outcome"] != "SUCCEEDED" and (ok_at is None or last["at"] > ok_at))}}
        elif view == "research":
            out |= {"research": snap(f, "research"), "canonical_file": "/home/colton-mcclain/elyraon.com.co/docs/x-research/canonical-fleet-research.json"}
        elif view == "growth":
            out |= {"growth": snap(f, "growth")}
        elif view == "director":
            from dotz.fleet import director
            out |= {"director": snap(f, "director"), **director.snapshot()}
        elif view == "system":
            out |= {"host": snap(f, "host"), "cluster": snap(f, "cluster"), "workers": core["workers"],
                    "dead": rows(f, "SELECT task_id,dot,kind,attempts,last_error,updated_at FROM fleet_tasks WHERE status IN ('DEAD','FAILED') ORDER BY updated_at DESC LIMIT 25"),
                    "retrying": rows(f, "SELECT task_id,dot,kind,attempts,last_error,run_after FROM fleet_tasks WHERE status='QUEUED' AND attempts>0 ORDER BY updated_at DESC LIMIT 10"),
                    "recent": rows(f, "SELECT task_id,dot,kind,outcome,detail,at FROM fleet_attempts WHERE outcome!='SUCCEEDED' ORDER BY seq DESC LIMIT 20")}
        elif view in ("model_usage", "models"):
            from dotz.fleet.model_usage import get_model_usage_snapshot
            mu = get_model_usage_snapshot(now=now)
            out |= mu
            out["model_usage"] = mu
        else:
            return {"error": "unknown view"}
        return out
    finally:
        f.close()


class H(BaseHTTPRequestHandler):
    server_version = "DrexFleetCommand"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") else "[::1]"
        if host not in OK_HOSTS:
            return self._send(403, b'{"error":"host not allowed"}')
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html_path = Path(__file__).with_name("dashboard.html")
            page_content = html_path.read_text(encoding="utf-8") if html_path.exists() else PAGE
            return self._send(200, page_content.encode("utf-8"), "text/html")
        if path == "/healthz":
            return self._send(200, b'{"ok":true}')
        if path.startswith("/api/director/goals/"):
            from dotz.fleet import director
            try:
                return self._send(200, json.dumps({"ok": True, "goal": director.get_goal(path.rsplit("/", 1)[1])}).encode())
            except director.DirectorError as e:
                return self._send(e.http, json.dumps({"ok": False, "error": str(e)}).encode())
        if path.startswith("/api/"):
            try:
                return self._send(200, json.dumps(build(path[5:]), default=str).encode())
            except Exception as e:
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {str(e)[:160]}"}).encode())
        self._send(404, b'{"error":"not found"}')

    def _deny(self):
        self._send(405, b'{"error":"read-only dashboard"}')

    def do_POST(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") else "[::1]"
        if host not in OK_HOSTS:
            return self._send(403, b'{"error":"host not allowed"}')
        path = self.path.split("?")[0]
        if path.startswith("/api/director/"):
            return self._director_post(path)
        self._deny()

    def _director_post(self, path):
        """Owner actions. JSON content type forces a CORS preflight (never granted) and a foreign Origin is refused."""
        from dotz.fleet import director
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1].rsplit(":", 1)[0] not in OK_HOSTS:
            return self._send(403, b'{"ok":false,"error":"origin not allowed"}')
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, b'{"ok":false,"error":"application/json required"}')
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 20000)) or b"{}")
            parts = path.strip("/").split("/")          # api director goals [id [action]]
            if parts == ["api", "director", "goals"]:
                g = director.create_goal(body.get("goal"), body.get("project"), body.get("client_key"))
            elif len(parts) == 5 and parts[3] and parts[4] in ("pause", "resume", "cancel"):
                g = director.control(parts[3], parts[4])
            else:
                return self._send(404, b'{"ok":false,"error":"unknown director action"}')
            return self._send(200, json.dumps({"ok": True, "goal": g}, default=str).encode())
        except director.DirectorError as e:
            return self._send(e.http, json.dumps({"ok": False, "error": str(e)}).encode())
        except (ValueError, AttributeError):
            return self._send(400, b'{"ok":false,"error":"invalid request"}')
        except Exception as e:
            return self._send(500, json.dumps({"ok": False, "error": type(e).__name__}).encode())

    do_PUT = do_DELETE = do_PATCH = _deny


def main(port: int = 8795) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"DREX FLEET COMMAND on http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()


PAGE = (Path(__file__).with_name("dashboard.html")).read_text() if Path(__file__).with_name("dashboard.html").exists() else "<h1>missing dashboard.html</h1>"
