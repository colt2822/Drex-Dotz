"""Fleet roster. Operational dots have real adapters; the rest stay NOT_OPERATIONAL (no mock completions)."""
from __future__ import annotations

NO_ADAPTER = "no verified adapter wired yet; tasks for this dot are refused rather than faked"

DOTS: list[dict] = [
    {"name": "sentinel", "role": "Sentinel", "operational": True,
     "mission": "Watch service, host and cluster (N1/N2/N3/N4) health and raise alerts. Read-only.",
     "capabilities": {"allow": ["host.read", "systemd.read", "cluster.read"], "deny": ["secrets.*", "systemd.write", "crm.*", "net.send"]},
     "budget": {"compute_daily_usd": 0.0, "tasks_per_hour": 120}},
    {"name": "analyst", "role": "Analyst", "operational": True,
     "mission": "Measure the business funnel from the CRM and discovery tables. Read-only; unknown stays unknown.",
     "capabilities": {"allow": ["crm.read", "disco.read"], "deny": ["secrets.*", "crm.write", "net.send"]},
     "budget": {"compute_daily_usd": 0.0, "tasks_per_hour": 60}},
    {"name": "scout", "role": "Scout", "operational": True,
     "mission": "Observe the single discovery-v2 service (heartbeat, sources, throughput). Does not run discovery itself.",
     "capabilities": {"allow": ["disco.read"], "deny": ["secrets.*", "crm.write", "net.send"]},
     "budget": {"compute_daily_usd": 0.0, "tasks_per_hour": 60}},
    {"name": "sales", "role": "Sales Dot", "operational": True,
     "mission": ("Observe the one authoritative Sales Dot sender; check deliverability DNS; reverify and promote staged "
                 "prospects through the existing CRM adapter. Never builds Gmail, never sends, never edits policy."),
     "capabilities": {"allow": ["crm.read", "disco.read", "dns.read", "net.fetch.public", "crm.promote"],
                      "deny": ["secrets.*", "net.send", "gmail.*", "policy.write"]},
     "budget": {"compute_daily_usd": 0.0, "tasks_per_hour": 60, "promotions_per_day": 25}},
    {"name": "director", "role": "Director", "operational": False, "reason": NO_ADAPTER + " (scheduling currently done by the supervisor)",
     "mission": "Assign and sequence work across the fleet, escalate to the owner.",
     "capabilities": {"allow": ["fleet.enqueue"], "deny": ["secrets.*", "net.send", "money.*"]}, "budget": {"compute_daily_usd": 1.0, "tasks_per_hour": 30}},
    {"name": "research", "role": "Research", "operational": False, "reason": NO_ADAPTER,
     "mission": "Deep-dive prospects and markets from public sources.",
     "capabilities": {"allow": ["net.fetch.public", "crm.read"], "deny": ["secrets.*", "net.send", "money.*"]}, "budget": {"compute_daily_usd": 1.0, "tasks_per_hour": 20}},
    {"name": "growth", "role": "Growth", "operational": False, "reason": NO_ADAPTER,
     "mission": "Propose outbound experiments and channel mix from measured outcomes.",
     "capabilities": {"allow": ["crm.read"], "deny": ["secrets.*", "net.send", "money.*"]}, "budget": {"compute_daily_usd": 1.0, "tasks_per_hour": 20}},
    {"name": "commerce", "role": "Commerce", "operational": False, "reason": NO_ADAPTER,
     "mission": "Track storefront listings, orders and revenue.",
     "capabilities": {"allow": ["commerce.read"], "deny": ["secrets.*", "money.*", "net.send"]}, "budget": {"compute_daily_usd": 0.5, "tasks_per_hour": 20}},
    {"name": "support", "role": "Support", "operational": False, "reason": NO_ADAPTER,
     "mission": "Triage inbound replies and customer questions for the owner.",
     "capabilities": {"allow": ["crm.read"], "deny": ["secrets.*", "net.send", "money.*"]}, "budget": {"compute_daily_usd": 0.5, "tasks_per_hour": 20}},
]

# dot, kind, every_s
SCHEDULES = [
    ("sentinel", "sentinel.host_health", 60),
    ("sentinel", "sentinel.cluster_health", 600),
    ("analyst", "analyst.funnel", 300),
    ("scout", "scout.discovery_observe", 120),
    ("sales", "sales.sender_observe", 120),
    ("sales", "sales.deliverability", 21600),
]
