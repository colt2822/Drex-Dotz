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
    {"name": "director", "role": "Director", "operational": True,
     "mission": "Assign and sequence work across the fleet, coordinate dot outputs, escalate to the owner.",
     "capabilities": {"allow": ["fleet.enqueue", "fleet.read"], "deny": ["secrets.*", "net.send", "money.*"]}, "budget": {"compute_daily_usd": 1.0, "tasks_per_hour": 30}},
    {"name": "research", "role": "Research", "operational": True,
     "mission": "Deep-dive prospects, market intelligence, and canonical Grok research handoffs across fleet consumers.",
     "capabilities": {"allow": ["net.fetch.public", "crm.read"], "deny": ["secrets.*", "net.send", "money.*"]}, "budget": {"compute_daily_usd": 1.0, "tasks_per_hour": 20}},
    {"name": "growth", "role": "Growth", "operational": True,
     "mission": "Observe content pipeline, validate claims firewall, propose channel mix from measured outcomes.",
     "capabilities": {"allow": ["crm.read"], "deny": ["secrets.*", "net.send", "money.*"]}, "budget": {"compute_daily_usd": 1.0, "tasks_per_hour": 20}},
    {"name": "commerce", "role": "Commerce", "operational": True,
     "mission": "Observe verified storefront revenue (Stripe-reconciled gross/refunds/fees/net, D1 fulfillment) through the authenticated read-only Worker feed. Read-only; unknown stays unknown.",
     "capabilities": {"allow": ["commerce.read"], "deny": ["secrets.*", "money.*", "net.send"]}, "budget": {"compute_daily_usd": 0.5, "tasks_per_hour": 20}},
    {"name": "linkedin", "role": "LinkedIn Observer", "operational": True,
     "mission": "Observe local bridge campaign and action aggregates. Read-only; transport remains disabled.",
     "capabilities": {"allow": ["linkedin.read"], "deny": ["secrets.*", "net.send", "linkedin.write"]},
     "budget": {"compute_daily_usd": 0.0, "tasks_per_hour": 12}},
    {"name": "support", "role": "Support", "operational": True,
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
    ("commerce", "commerce.revenue_observe", 300),
    ("support", "support.triage_observe", 300),
    ("linkedin", "linkedin.observe_local", 300),
    ("research", "research.grok_digest", 600),
    ("growth", "growth.pipeline_observe", 600),
    ("director", "director.coordinate", 600),
]
