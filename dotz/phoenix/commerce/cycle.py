"""Deterministic distribution maintenance in PHOENIX's existing controller timer.

No sixth seat, model invocation, sender, scheduler or financial ledger. Remote events
are an untrusted observation buffer; they cannot attest revenue or external identity.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from dotz.phoenix import lifecycle
from dotz.phoenix.executor import guard

ORIGIN = "https://elyraon.com.co"
ROLES = {
    "PHOENIX-01": "conversion_and_usage",
    "PHOENIX-02": "framework_partnerships",
    "PHOENIX-03": "verifiable_task_demand",
    "PHOENIX-04": "distribution_and_discovery",
    "PHOENIX-05": "unmet_agent_demand",
}
STAGES = ("DISCOVERED", "INSTALLED", "CONNECTED", "FREE_CALL", "PAID_CALL", "REPEAT_BUYER", "REVENUE_COLLECTED")
SCHEMA = """
CREATE TABLE IF NOT EXISTS phoenix_agent_usage(
 event_id TEXT PRIMARY KEY, client_id TEXT, stage TEXT NOT NULL, source TEXT NOT NULL,
 campaign TEXT NOT NULL, tool TEXT, success INTEGER NOT NULL, synthetic INTEGER NOT NULL,
 evidence_class TEXT NOT NULL, elapsed_ms REAL, provider_cost_usd REAL,
 payment_status TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_phoenix_agent_client ON phoenix_agent_usage(client_id,at);
CREATE TABLE IF NOT EXISTS phoenix_agent_identity(
 client_id TEXT PRIMARY KEY, external_evidence TEXT NOT NULL, verified_by TEXT NOT NULL,
 installation_evidence TEXT, discovery_evidence TEXT, verified_source TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS phoenix_agent_outcome_link(
 event_id TEXT PRIMARY KEY, outcome_id TEXT NOT NULL UNIQUE, client_id TEXT NOT NULL,
 provider_receipt TEXT NOT NULL UNIQUE);
"""


def setup(s):
    s.c.executescript(SCHEMA)


def ingest(s, rows):
    """Only permit non-financial observation stages from the transport buffer."""
    setup(s)
    n = 0
    with s.tx():
        for r in rows:
            if r.get("stage") not in ("DISCOVERED", "TOKEN_ISSUED", "CONNECTED", "FREE_CALL", "CALL_FAILED"):
                continue
            synthetic = int(bool(r.get("synthetic")))
            n += s.c.execute("INSERT OR IGNORE INTO phoenix_agent_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r["event_id"], r.get("client_id"), r["stage"], r.get("source", "other"), r.get("campaign", ""),
                r.get("tool"), int(bool(r.get("success"))), synthetic,
                "CONTROLLED_TEST" if synthetic else "UNVERIFIED_PUBLIC", r.get("elapsed_ms"),
                r.get("provider_cost_usd"), "NOT_PAID", r["at"])).rowcount
    return n


def summary(s):
    setup(s)
    observations = [dict(r) for r in s.c.execute(
        "SELECT stage,evidence_class,source,count(*) events,count(DISTINCT client_id) clients,"
        "sum(success) successes,avg(elapsed_ms) mean_elapsed_ms FROM phoenix_agent_usage GROUP BY stage,evidence_class,source")]
    # Installation, buyer identity, payments and settlement require independent evidence.
    # Existing owner-attested phoenix_outcome is the only revenue authority.
    funnel = {k: 0 for k in STAGES}
    for stage, col in (("DISCOVERED", "discovery_evidence"), ("INSTALLED", "installation_evidence")):
        funnel[stage] = s.c.execute(f"SELECT count(*) FROM phoenix_agent_identity WHERE length({col})>=8").fetchone()[0]
    for stage in ("CONNECTED", "FREE_CALL"):
        funnel[stage] = s.c.execute("SELECT count(DISTINCT u.client_id) FROM phoenix_agent_usage u JOIN phoenix_agent_identity i USING(client_id) WHERE u.stage=? AND u.success=1 AND u.synthetic=0", (stage,)).fetchone()[0]
    paid = s.c.execute("SELECT l.client_id,o.funded_cents,o.collected_cents FROM phoenix_agent_outcome_link l JOIN phoenix_outcome o USING(outcome_id) JOIN phoenix_agent_identity i ON i.client_id=l.client_id WHERE o.synthetic=0").fetchall()
    funnel["PAID_CALL"] = sum(r["funded_cents"] > 0 for r in paid)
    from collections import Counter
    funnel["REPEAT_BUYER"] = sum(n > 1 for n in Counter(r["client_id"] for r in paid if r["funded_cents"] > 0).values())
    funnel["REVENUE_COLLECTED"] = sum(r["collected_cents"] > 0 for r in paid)
    return {"verified_external_repeat_users": s.c.execute("SELECT count(*) FROM (SELECT u.client_id FROM phoenix_agent_usage u JOIN phoenix_agent_identity i USING(client_id) WHERE u.synthetic=0 AND u.stage='FREE_CALL' AND u.success=1 GROUP BY u.client_id HAVING count(*)>1)").fetchone()[0],
            "actual_external_installations": None, "actual_external_users": None,
            "observability": "Verified counts cover evidence admitted by the owner; unobserved installations and anonymous identity are UNKNOWN.",
            "observations": observations, "verified_external_funnel": funnel,
            "verified_collected_cents": sum(r["collected_cents"] for r in paid),
            "revenue_authority": "phoenix_outcome (existing owner/provider attestation)",
            "source_attribution": "client-claimed unless independently verified",
            "provider_cost_usd": 0, "hosting_cost_usd": None, "net_operating_contribution_usd": None,
            "paid_enabled": False, "last_cycle": s.kv_get("agent_commerce:last_tick")}


def fetch(path, token=None):
    req = urllib.request.Request(ORIGIN + path, headers={"User-Agent": "ELYREON-PHOENIX-maintenance/1", **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read(1_000_000))


def verify_external(s, client_id, *, evidence, by, installation=None, discovery=None, source=None):
    """Operator-only evidence admission. Never callable from the public service."""
    setup(s)
    rows = s.c.execute("SELECT synthetic FROM phoenix_agent_usage WHERE client_id=?", (client_id,)).fetchall()
    if not rows or any(r[0] for r in rows) or len(evidence) < 8 or not by.startswith("owner:"):
        raise ValueError("Independent external identity evidence and owner attestation required; controlled clients excluded")
    s.c.execute("INSERT INTO phoenix_agent_identity VALUES(?,?,?,?,?,?,?)", (client_id,evidence,by,installation,discovery,source,s.now()))


def tick(s, *, transport=fetch, config=None):
    setup(s)
    if config is None:
        path = Path(os.environ.get("PHOENIX_COMMERCE_CONFIG", Path.home() / ".config/elyreon/phoenix-commerce.json"))
        config = json.loads(path.read_text()) if path.exists() else {}
    if not config.get("enabled"):
        return {"enabled": False}
    result = {}
    from .acquisition import run as acquisition_run
    if transport is fetch:
        result["acquisition"] = acquisition_run(s)
    for slot, role in ROLES.items():
        inst = lifecycle.live(s, slot)
        if inst is None:
            continue
        key = "agent_commerce:due:" + slot
        if s.now() < s.kv_get(key, 0):
            continue
        try:
            guard(s, inst)
            if role == "conversion_and_usage":
                cursor = s.kv_get("agent_commerce:cursor", 0)
                rows = transport(f"/agents/events?after={cursor}", config.get("sync_token"))["events"]
                detail = {"imported": ingest(s, rows), "attribution": "unverified observations only"}
                if rows:
                    s.kv_set("agent_commerce:cursor", max(r["seq"] for r in rows))
                # Retention is maintained by the same existing controller, after ingestion.
                if transport is fetch:
                    req = urllib.request.Request(ORIGIN + "/agents/maintenance", data=b"{}", method="POST", headers={"Authorization": "Bearer " + config["sync_token"], "Content-Type": "application/json", "User-Agent": "ELYREON-PHOENIX-maintenance/1"})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        r.read(1000)
                s.c.execute("DELETE FROM phoenix_agent_usage WHERE at < ?", (s.now()-30*86400,))
            elif role == "distribution_and_discovery":
                manifest = transport("/.well-known/ard.json")
                health = transport("/agents/health")
                listing = transport("/agents/server.json")
                # Official Registry readback; no duplicate submission when version exists.
                try:
                    req = urllib.request.Request("https://registry.modelcontextprotocol.io/v0.1/servers?search=" + urllib.parse.quote(listing["name"]), headers={"User-Agent": "ELYREON-PHOENIX-maintenance/1"})
                    if transport is fetch:
                        with urllib.request.urlopen(req, timeout=8) as r:
                            registry = json.loads(r.read(1_000_000))
                        verified = any(x["server"]["name"] == listing["name"] and x["server"]["version"] == listing["version"] for x in registry.get("servers", []))
                    else:
                        verified = False
                except Exception:
                    verified = False
                publication = "already verified" if verified else "pending"
                if not verified and transport is fetch and config.get("registry_maintenance_approved"):
                    # Maintain the already-authorized integration only. No new namespace,
                    # URL, paid capability, registry account or package publication.
                    import subprocess
                    manifest_path = Path(config["registry_manifest"])
                    candidate = json.loads(manifest_path.read_text())
                    if (candidate["name"] != "co.com.elyraon/workflow-rescue" or candidate.get("packages")
                            or [x.get("url") for x in candidate.get("remotes", [])] != [ORIGIN + "/agents/mcp"]):
                        raise ValueError("Registry maintenance scope changed; leave pending for normal integration review")
                    command = [config["registry_publisher"], "publish", str(manifest_path)]
                    p = subprocess.run(command, cwd=config["registry_auth_dir"], capture_output=True, text=True, timeout=20)
                    publication = "submitted; readback pending" if p.returncode == 0 else "held: provider rejected maintenance"
                detail = {"health": health.get("ok"), "resources": len(manifest.get("entries", [])), "mcp_registry_verified": verified,
                          "publication": publication,
                          "next_action": "improve installation conversion" if verified else "repair or publish registry listing"}
                s.kv_set("agent_commerce:discovery", detail)
            else:
                observed = summary(s)["observations"]
                calls = sum(r["events"] for r in observed if r["stage"] == "FREE_CALL" and r["evidence_class"] != "CONTROLLED_TEST")
                failures = sum(r["events"] for r in observed if r["stage"] == "CALL_FAILED" and r["evidence_class"] != "CONTROLLED_TEST")
                # Evidence-triggered work stays with its existing seat; fulfillment remains Floor-owned.
                detail = {"public_unverified_calls": calls, "public_failures": failures,
                          "priority": "repair onboarding or validation failures" if failures else "find first independent use" if not calls else "validate buyer demand before paid activation",
                          "fulfillment_owner": "existing Agent Floor workers",
                          "prepared_connectors": ["MCP Streamable HTTP", "Agent Skills", "A2A 0.3 JSON-RPC", "HTTP/OpenAPI"],
                          "outreach": "held; no unsolicited messages or upstream PRs"}
                # Avoid repeated unchanged-market research; permit a new research brief once a week.
                research_key = "agent_commerce:research:" + role
                detail["ecosystem_research_due"] = s.now() >= s.kv_get(research_key, 0)
                if detail["ecosystem_research_due"]:
                    if transport is fetch and role == "unmet_agent_demand":
                        # One cached weekly public marketplace read; never purchase or call a paid resource.
                        url = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?" + urllib.parse.urlencode({"query": "n8n workflow validation", "limit": 5})
                        req = urllib.request.Request(url, headers={"User-Agent": "ELYREON-PHOENIX-maintenance/1"})
                        with urllib.request.urlopen(req, timeout=8) as r:
                            market = json.loads(r.read(1_000_000))
                        detail["market_observations"] = [{"resource": x.get("resource"), "quality": x.get("quality"), "price_options": [{"amount": a.get("amount"), "asset": a.get("asset"), "network": a.get("network")} for a in x.get("accepts", [])]} for x in market.get("resources", [])]
                        detail["market_evidence_limit"] = "facilitator-reported activity; independent buyer identities unverified"
                    s.kv_set(research_key, s.now() + 7*86400)
            s.kv_set("agent_commerce:work:" + slot, {"owner_instance": inst["instance_id"], "role": role, "at": s.now(), **detail})
            s.event(inst["instance_id"], slot, "AGENT_DISTRIBUTION", role, json.dumps(detail))
            s.kv_set(key, s.now() + (900 if role == "conversion_and_usage" else 6*3600))
            result[slot] = detail
        except Exception as e:
            s.event(inst["instance_id"], slot, "AGENT_DISTRIBUTION_ERROR", f"{role}: {type(e).__name__}: {e}"[:500])
            s.kv_set(key, s.now() + 900)
            result[slot] = {"error": type(e).__name__}
    s.kv_set("agent_commerce:last_tick", {"at": s.now(), "roles_run": list(result)})
    return result
