"""Qualification gates + scoring. Pure functions over an enriched candidate row; no network, no sending.

Ceiling on contact verification (stated honestly): address is printed on the company's own site, domain matches the
site's registrable domain, role-type local part, MX/A exists. No SMTP probing and no pattern guessing.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

REJECT_ORDER = ["site_blocks_automated_access", "dead_site", "parked_or_placeholder_site", "robots_denied_or_non_html", "aggregator_or_portal", "agency_or_vendor_competitor",
                "chain_or_franchise", "no_operating_evidence", "no_site_published_email", "named_person_only", "mx_failed", "domain_or_contact_suppressed",
                "duplicate_existing_prospect", "no_grounded_signal", "low_fit", "low_contact_confidence"]

_mx_cache: dict[str, tuple[float, bool]] = {}
_mx_lock = threading.Lock()


def mx_ok(domain: str, ttl: float = 604800) -> bool:
    with _mx_lock:
        v = _mx_cache.get(domain)
    if v and time.time() - v[0] < ttl:
        return v[1]
    try:
        out = subprocess.run(["dig", "+short", "+time=3", "+tries=1", "MX", domain], capture_output=True, text=True, timeout=10).stdout.split()
        ok = bool(out) and out != ["0", "."]
        if not ok:
            ok = bool(subprocess.run(["dig", "+short", "+time=3", "+tries=1", "A", domain], capture_output=True, text=True, timeout=10).stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False  # fail closed
    with _mx_lock:
        _mx_cache[domain] = (time.time(), ok)
    return ok


class CrmIndex:
    """Snapshot of what the CRM already knows, for dedupe/suppression. Read-only."""

    def __init__(self, conn):
        from dotz.sales.disco.util import canonical_domain, registrable
        self.reg = registrable
        self.sites, self.email_domains, self.supp_emails, self.supp_domains = set(), set(), set(), set()
        for r in conn.execute("SELECT website FROM prospects WHERE website IS NOT NULL"):
            d = canonical_domain(r[0])
            if d:
                self.sites.add(registrable(d))
        for r in conn.execute("SELECT email FROM sales_contacts"):
            self.email_domains.add(registrable(r[0].split("@")[1]))
        for r in conn.execute("SELECT email FROM sales_suppressions"):
            e = r[0]
            (self.supp_domains.add(registrable(e[1:])) if e.startswith("@") else self.supp_emails.add(e.lower()))

    def suppressed(self, domain: str, email: str | None) -> bool:
        return self.reg(domain) in self.supp_domains or bool(email and email.lower() in self.supp_emails)

    def duplicate(self, domain: str, email: str | None) -> bool:
        r = self.reg(domain)
        return r in self.sites or r in self.email_domains


def gate(cand, idx: CrmIndex | None, mx=mx_ok, cfg: dict | None = None, perf_mult: float = 1.0, categories: dict | None = None) -> dict:
    """Return {'qualified': bool, 'reject': reason|None, scores..., 'fit_reason': str}. Order = REJECT_ORDER precedence."""
    cfg = cfg or {"ranking": {"assumed_deal_value_usd": 1500, "assumed_close_rate": 0.01, "min_fit_to_qualify": 60, "min_contact_confidence": 60}}
    rk = cfg["ranking"]
    s = json.loads(cand["signals"] or "{}")
    present = set(s.get("present", []))
    out = {"qualified": False, "reject": None}

    def rej(r):
        out["reject"] = r
        return out

    if cand["reject_reason"]:
        return rej(cand["reject_reason"])
    ah = s.get("aggregator_hits", [])
    if ah:
        return rej("aggregator_or_portal")
    if len(s.get("agency_hits", [])) >= 2:
        return rej("agency_or_vendor_competitor")
    if s.get("chain"):
        return rej("chain_or_franchise")
    if cand["operating_status"] != "operating_site_live_with_contact_details":
        return rej("no_operating_evidence")
    email, ctype = cand["contact_email"], cand["contact_type"]
    if not email:
        return rej("no_site_published_email")
    if ctype != "role":
        return rej("named_person_only")
    dom = email.split("@")[1]
    if idx and idx.suppressed(cand["domain"], email):
        return rej("domain_or_contact_suppressed")
    if idx and idx.duplicate(cand["domain"], email):
        return rej("duplicate_existing_prospect")
    if not mx(dom):
        return rej("mx_failed")
    if not cand["signal_quote"] or len(cand["signal_quote"]) < 20:
        return rej("no_grounded_signal")
    # ---- scores
    cat_w = (categories or {}).get(cand["business_category"], {}).get("weight", 0.7)
    fit = 40 * cat_w + 15 * (cand["locations"] == 1 and not cand["brand"]) + 15 + 10 * bool(present & {"after_hours", "appointment_dispatch", "manual_intake", "phone_dependency"}) \
        + 10 * (not s.get("automation_present")) + 10
    buying = min(100, 30 * ("hiring" in present) + 30 * ("expansion" in present) + 15 * ("after_hours" in present) + 10 * ("manual_intake" in present)
                 + 10 * ("appointment_dispatch" in present) + 5 * ("phone_dependency" in present) + 5 * ("repetitive_comms" in present))
    kinds = len(present) + (1 if cand["source"] == "osm" else 0)
    evq = min(100, 20 * min(kinds, 3) + 20 * (cand["pages_fetched"] >= 2) + 20)
    cc = 70 + 10 * ("contact_page" in (cand["contact_method"] or "")) + 10 * bool(cand["contact_url"]) + 5
    cc = min(95, cc)  # ceiling: no deliverability probe
    fit = min(100.0, fit)
    if fit < rk["min_fit_to_qualify"]:
        out.update(fit=fit)
        return rej("low_fit")
    if cc < rk["min_contact_confidence"]:
        return rej("low_contact_confidence")
    rank = min(100.0, (0.45 * fit + 0.25 * buying + 0.15 * evq + 0.15 * cc) * perf_mult)
    ecv = rk["assumed_deal_value_usd"] * rk["assumed_close_rate"] * (fit / 100) * (0.5 + buying / 200)
    reason = ("Public site shows " + ", ".join(sorted(present)) + ("; no online booking/chat advertised" if not s.get("automation_present") else "")) if present else "Public site shows an operating service business"
    out.update(qualified=True, reject=None, fit=round(fit, 1), buying_signal=float(buying), evidence_quality=float(evq), contact_confidence=float(cc),
               confidence_score=round(0.5 * evq + 0.5 * cc, 1), rank=round(rank, 1), expected_commercial_value_estimate=round(ecv, 2), fit_reason=reason)
    return out
