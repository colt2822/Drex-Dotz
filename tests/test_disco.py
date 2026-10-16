"""Offline tests for Sales Dot discovery v2 (fixtures only; no network, no Gmail)."""
import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from dotz.sales import worker as W
from dotz.sales.crm_store import CrmStore
from dotz.sales.disco import config, qualify
from dotz.sales.disco.engine import Engine
from dotz.sales.disco.enrich import enrich_site, extract_emails, quote_for, norm_ws
from dotz.sales.disco.net import Fetcher, Page
from dotz.sales.disco.sources import (SourceBlocked, SourceError, OsmSource, SearchSource, parse_directory_page, parse_overpass, overpass_query)
from dotz.sales.disco.store import DiscoStore
from dotz.sales.disco.util import canonical_domain, registrable
from dotz.sales.policy import Policy

FIX = Path(__file__).parent / "fixtures"
LIVE = Path.home() / "elyraon-revenue-hunter-20261015/data/crm.sqlite3"
pytestmark = pytest.mark.skipif(not LIVE.exists(), reason="needs CRM schema source")
CFG = config.load()


class FakeFetcher:
    """Serves fixture pages; records fetch order."""
    def __init__(self, pages: dict[str, str | int]):
        self.pages, self.log = pages, []

    def get(self, url):
        self.log.append(url)
        key = url.rstrip("/")
        v = self.pages.get(key)
        if v is None:
            return Page(url, 404, "", 0.0, "text/html")
        if isinstance(v, int):
            return Page(url, v, "", 0.0)
        return Page(url, 200, v, 1.0e9, "text/html")


def cand(domain="lonestarair.example", name="Lone Star Air", **kw):
    base = dict(domain=domain, website=f"https://{domain}/", company_name=name, brand=None, locations=1, business_category="hvac", source="osm")
    base.update(kw)
    return base


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "crm.sqlite3"
    shutil.copy(LIVE, p)
    c = sqlite3.connect(p)
    for t in ("disco_jobs", "disco_raw", "disco_candidates", "disco_evidence", "disco_source_health"):   # live run data must not leak into tests
        c.execute(f"DROP TABLE IF EXISTS {t}")
    c.commit(); c.close()
    CrmStore(p)
    return p


# ---------------- source extraction
def test_overpass_parse_filters_and_normalizes():
    recs = parse_overpass(json.loads((FIX / "overpass_tx.json").read_text()), "hvac", "TX")
    names = {r.name for r in recs}
    assert names == {"Lone Star Air", "Lone Star Air (shop 2)", "Big Chain HVAC"}   # no-site, facebook-only, wrong state dropped
    a = next(r for r in recs if r.name == "Lone Star Air")
    assert a.domain == "lonestarair.example" and a.source_url == "https://www.openstreetmap.org/node/1" and a.email_hint == "info@lonestarair.example"
    assert next(r for r in recs if r.name.startswith("Big")).brand == "Q1"


def test_overpass_query_scoped_to_state():
    q = overpass_query(["craft=hvac"], "TX")
    assert 'ISO3166-2"="US-TX' in q and '"craft"="hvac"' in q


def test_directory_page_extracts_outbound_businesses_only():
    html = '<a href="https://acmeplumb.example/">Acme Plumbing</a><a href="/about">About</a><a href="https://facebook.com/x">Facebook</a><a href="/p2">Next</a>'
    recs, nxt = parse_directory_page(html, "https://chamber.example/members", "plumbing", "TX")
    assert [r.domain for r in recs] == ["acmeplumb.example"] and nxt == "https://chamber.example/p2"


# ---------------- enrichment, false positives, contact verification
def test_enrich_good_site_signals_quotes_and_contact():
    f = FakeFetcher({"https://lonestarair.example": (FIX / "site_good.html").read_text(), "https://lonestarair.example/contact": (FIX / "site_contact.html").read_text()})
    fields, ev = enrich_site(f, cand())
    s = json.loads(fields["signals"])
    assert fields["operating_status"] == "operating_site_live_with_contact_details"
    assert {"after_hours", "manual_intake", "hiring"} <= set(s["present"])
    assert fields["contact_email"] == "info@lonestarair.example" and fields["contact_type"] == "role"
    kinds = {k for k, *_ in ev}
    assert "contact_email" in kinds
    # every quote is verbatim in the fetched page text and carries a URL
    from dotz.sales.luna import html_to_text
    corpus = norm_ws(html_to_text((FIX / "site_good.html").read_text()) + html_to_text((FIX / "site_contact.html").read_text()))
    for kind, quote, url, _ in ev:
        if kind != "contact_email":
            assert norm_ws(quote) in corpus and url.startswith("https://lonestarair.example")


def test_contact_rejects_agency_credit_billing_privacy_named_free_and_images():
    t = "a@agencystudio.example billing@x.example privacy@lonestarair.example jane.smith@lonestarair.example bob@gmail.com logo@2x.png info@lonestarair.example"
    got = {c["email"]: c for c in extract_emails(t, "lonestarair.example")}
    assert got["info@lonestarair.example"]["ok"] and got["info@lonestarair.example"]["type"] == "role"
    assert got["jane.smith@lonestarair.example"]["type"] == "named_person"
    assert not got["privacy@lonestarair.example"]["ok"] and not got["bob@gmail.com"]["ok"] and not got["a@agencystudio.example"]["ok"]
    assert "logo@2x.png" not in got


def test_agency_credit_email_is_not_used_when_only_option():
    f = FakeFetcher({"https://x.example": "<p>HVAC service. 24/7 emergency service available. Call (512) 555-0100 at 5 Oak Street</p><a href='mailto:hello@agencystudio.example'>site by</a>"})
    fields, _ = enrich_site(f, cand("x.example", "X Air"))
    assert fields["contact_email"] is None


def test_dead_and_parked_and_robots_denied_sites_rejected():
    assert enrich_site(FakeFetcher({"https://d.example": 503}), cand("d.example"))[0]["reject_reason"] == "dead_site"
    assert enrich_site(FakeFetcher({"https://p.example": "<p>This domain is for sale</p>"}), cand("p.example"))[0]["reject_reason"] == "parked_or_placeholder_site"

    class Denied(FakeFetcher):
        def get(self, url): return None
    assert enrich_site(Denied({}), cand("r.example"))[0]["reject_reason"] == "robots_denied_or_non_html"


def test_aggregator_false_positive_detected_even_with_real_looking_email():
    f = FakeFetcher({"https://quotemyroof.example": (FIX / "site_aggregator.html").read_text()})
    fields, _ = enrich_site(f, cand("quotemyroof.example", "QuoteMyRoof", business_category="roofing"))
    r = qualify.gate({**fields, **cand("quotemyroof.example", "QuoteMyRoof"), "reject_reason": fields.get("reject_reason"), "signal_quote": fields.get("signal_quote"), "pages_fetched": 1},
                     None, mx=lambda d: True)
    assert r["qualified"] is False and r["reject"] == "aggregator_or_portal"


def test_chain_and_agency_rejected():
    base = dict(cand(), reject_reason=None, operating_status="operating_site_live_with_contact_details", contact_email="info@lonestarair.example", contact_type="role",
                signal_quote="x" * 30, pages_fetched=2, contact_method="published_on_official_site_page(contact_page)", contact_url="u")
    assert qualify.gate({**base, "signals": json.dumps({"present": [], "chain": True})}, None, mx=lambda d: True)["reject"] == "chain_or_franchise"
    assert qualify.gate({**base, "signals": json.dumps({"present": [], "agency_hits": ["a", "b"]})}, None, mx=lambda d: True)["reject"] == "agency_or_vendor_competitor"


def test_mx_alone_is_not_enough_and_mx_failure_blocks():
    base = dict(cand(), reject_reason=None, operating_status="operating_site_live_with_contact_details", contact_email=None, contact_type=None, signals=json.dumps({"present": ["after_hours"]}),
                signal_quote="x" * 30, pages_fetched=2, contact_method=None, contact_url=None)
    assert qualify.gate(base, None, mx=lambda d: True)["reject"] == "no_site_published_email"      # MX ok but nothing published
    with_email = {**base, "contact_email": "info@lonestarair.example", "contact_type": "role", "contact_url": "https://lonestarair.example/contact"}
    assert qualify.gate(with_email, None, mx=lambda d: False)["reject"] == "mx_failed"


def test_qualified_scores_and_estimate_labeled():
    base = dict(cand(), reject_reason=None, operating_status="operating_site_live_with_contact_details", contact_email="info@lonestarair.example", contact_type="role",
                signals=json.dumps({"present": ["after_hours", "manual_intake", "hiring"], "automation_present": False}), signal_quote="Request a free estimate using the form below.",
                pages_fetched=3, contact_method="published_on_official_site_page(contact_page)", contact_url="https://lonestarair.example/contact")
    r = qualify.gate(base, None, mx=lambda d: True, categories=CFG["categories"])
    assert r["qualified"] and 60 <= r["fit"] <= 100 and r["buying_signal"] >= 55 and r["contact_confidence"] <= 95
    assert r["expected_commercial_value_estimate"] > 0 and "hiring" in r["fit_reason"]


# ---------------- dedupe + suppression against the real CRM schema
def test_dedupe_and_suppression_via_crm_index(db):
    s = CrmStore(db)
    s.add_suppression("@badco.example", "optout", "test")
    s.add_suppression("owner@supp.example", "optout", "test")
    s.add_prospect(pid="p1", name="n", company="Known", website="https://www.known.example", email="info@known.example", source_url="https://known.example/c", signal="s" * 25, proposal="p", fit=80)
    idx = qualify.CrmIndex(s.c)
    assert idx.suppressed("badco.example", None) and idx.suppressed("x.example", "owner@supp.example") and not idx.suppressed("fine.example", "a@fine.example")
    assert idx.duplicate("known.example", None) and idx.duplicate("shop.known.example", None) and not idx.duplicate("new.example", None)


def test_blocklist_stops_send_gate(db):
    s = CrmStore(db)
    s.block_domain("portal.example", "aggregator")
    assert s.domain_blocked("hello@www.portal.example") == "aggregator" and s.domain_blocked("a@ok.example") is None


# ---------------- engine: jobs, resumability, dedupe, rate limits, failures
def mk_engine(db, osm_payload=None, **kw):
    cfg = json.loads(json.dumps(CFG))
    e = Engine(db, cfg, fetcher=kw.pop("fetcher", None) or FakeFetcher({}), mx=lambda d: True, **kw)
    return e


def test_osm_job_ingest_dedupes_domains_and_counts_locations(db):
    e = mk_engine(db)
    e.sources["osm"] = type("S", (), {"run": lambda self, job: parse_overpass(json.loads((FIX / "overpass_tx.json").read_text()), "hvac", "TX")})()
    e.seed_jobs()
    r = e.run_source_job("osm")
    assert r["raw"] == 3 and r["new"] == 2   # two nodes share lonestarair.example -> one company
    assert e.store.candidate("lonestarair.example")["locations"] == 2
    m = e.metrics()
    assert m["RAW_DISCOVERED"] == 3 and m["UNIQUE_COMPANIES"] == 2
    e.sources["osm"].run = lambda job: parse_overpass(json.loads((FIX / "overpass_tx.json").read_text()), "hvac", "TX")
    e.store.c.execute("UPDATE disco_jobs SET status='pending' WHERE job_id='osm:hvac:TX'")
    again = e.run_source_job("osm")
    assert again["raw"] == 0 and again["new"] == 0   # replay is idempotent


def test_resumable_after_crash_and_expansion_only_when_saturated(db):
    e = mk_engine(db)
    n = e.seed_jobs()
    assert n > 0 and e.seed_jobs() == 0           # no duplicate jobs / no endless re-seeding while work is pending
    job = e.store.claim_job("osm")
    assert job["status"] == "pending"            # row fetched before update
    assert e.store.c.execute("SELECT status FROM disco_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0] == "running"
    e2 = mk_engine(db)                            # simulated restart
    assert e2.store.reset_running() >= 1
    assert e2.store.c.execute("SELECT status FROM disco_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0] == "pending"
    for r in e2.store.c.execute("SELECT job_id FROM disco_jobs WHERE source='osm' AND category='hvac'").fetchall():
        e2.store.finish_job(r[0], "done")
    assert e2.seed_jobs() > 0                     # saturated -> expands to next regions
    regions = {r[0] for r in e2.store.c.execute("SELECT region FROM disco_jobs WHERE category='hvac' AND source='osm'")}
    assert len(regions) == 6


def test_source_blocked_sets_cooldown_and_failure_is_isolated(db):
    e = mk_engine(db)
    e.seed_jobs()
    def blocked(job): raise SourceBlocked("challenge")
    def boom(job): raise SourceError("down")
    e.sources["search"].run = blocked
    r = e.run_source_job("search")
    assert "blocked" in r and e.store.blocked_until("search") > time.time()
    assert e.run_source_job("search")["skipped"].startswith("blocked_until")        # not hammered
    e.sources["osm"].run = boom
    assert "error" in e.run_source_job("osm")
    assert e.store.c.execute("SELECT status,attempts FROM disco_jobs WHERE status='failed'").fetchone()["attempts"] == 1
    assert e.store.blocked_until("osm") == 0                                         # osm failure doesn't block others


def test_fetcher_throttle_retry_and_robots(tmp_path):
    import httpx
    calls = []
    def handler(req):
        calls.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        if req.url.path == "/flaky":
            return httpx.Response(503) if calls.count(str(req.url)) < 2 else httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"})
    clock = [1000.0]; sleeps = []
    f = Fetcher("ua", per_host_min_interval_s=3, client=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: clock[0], sleep=lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
    assert f.get("https://h.example/private/x") is None and f.stats["robots_denied"] == 1
    assert f.get("https://h.example/a").status == 200
    assert f.get("https://h.example/flaky").status == 200 and f.stats["retries"] == 1
    assert sleeps and all(s >= 0 for s in sleeps) and min(sleeps) > 0                  # per-host spacing enforced
    assert sum(1 for c in calls if c.endswith("robots.txt")) == 1                      # robots cached


def test_robots_unreachable_fails_closed():
    import httpx
    f = Fetcher("ua", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))), retries=0, clock=lambda: 0.0, sleep=lambda s: None)
    assert f.get("https://z.example/") is None


def test_enrich_batch_end_to_end_and_ranking_order(db):
    e = mk_engine(db, fetcher=FakeFetcher({"https://lonestarair.example": (FIX / "site_good.html").read_text(), "https://lonestarair.example/contact": (FIX / "site_contact.html").read_text(),
                                          "https://quotemyroof.example": (FIX / "site_aggregator.html").read_text(), "https://dead.example": 500}))
    for d, n, c in (("lonestarair.example", "Lone Star Air", "hvac"), ("quotemyroof.example", "QuoteMyRoof", "roofing"), ("dead.example", "Dead Co", "hvac")):
        e.store.upsert_candidate({"domain": d, "name": n, "website": f"https://{d}/", "category": c, "source": "osm", "source_url": "u", "source_ref": d})
    st = e.enrich_batch()
    assert st["qualified"] == 1 and st["rejected"] == 2
    rb = e.metrics()["REJECTION_BREAKDOWN"]
    assert rb == {"aggregator_or_portal": 1, "dead_site": 1}
    q = e.store.candidate("lonestarair.example")
    assert q["stage"] == "QUALIFIED" and q["contact_email"] == "info@lonestarair.example" and q["rank"] > 0 and q["signal_url"].startswith("https://lonestarair.example")
    ev = e.store.c.execute("SELECT count(*) FROM disco_evidence WHERE domain='lonestarair.example' AND url LIKE 'https://lonestarair.example%'").fetchone()[0]
    assert ev >= 3


def test_existing_crm_prospect_and_suppressed_never_qualify(db):
    s = CrmStore(db)
    s.add_prospect(pid="p9", name="n", company="Known", website="https://known.example", email="info@known.example", source_url="https://known.example/c", signal="s" * 25, proposal="p", fit=80)
    s.add_suppression("@sup.example", "optout", "t")
    e = mk_engine(db, fetcher=FakeFetcher({"https://known.example": "<p>x</p>", "https://sup.example": "<p>x</p>"}))
    for d in ("known.example", "sup.example"):
        e.store.upsert_candidate({"domain": d, "name": d, "website": f"https://{d}/", "category": "hvac", "source": "osm", "source_url": "u", "source_ref": d})
    e.enrich_batch()
    assert e.store.candidate("known.example")["reject_reason"] == "duplicate_existing_prospect"
    assert e.store.candidate("sup.example")["reject_reason"] == "domain_or_contact_suppressed"
    assert e.metrics()["QUALIFIED"] == 0


# ---------------- promotion: no unauthorized sends, gates still apply
class NoGmail:
    def __getattr__(self, n): raise AssertionError(f"discovery touched Gmail.{n}")


class FakeLuna:
    def draft_message(self, prospect, offer_url):
        return {"subject": "Quick question about estimates", "body": f"Hello {prospect['company']} team,\n\nI noticed: \"{prospect['signal']}\" Would a simple intake workflow help? Details: {offer_url}"}


def test_promotion_creates_draft_only_and_sender_gates_still_apply(db, tmp_path, monkeypatch):
    monkeypatch.setattr(W, "STATE_DIR", tmp_path); monkeypatch.setattr(W, "STOP_FILE", tmp_path / "STOP"); monkeypatch.setattr(W, "ALERTS", tmp_path / "a.jsonl")
    e = mk_engine(db, fetcher=FakeFetcher({"https://lonestarair.example": (FIX / "site_good.html").read_text(), "https://lonestarair.example/contact": (FIX / "site_contact.html").read_text()}))
    e.store.upsert_candidate({"domain": "lonestarair.example", "name": "Lone Star Air", "website": "https://lonestarair.example/", "category": "hvac", "source": "osm", "source_url": "u", "source_ref": "r"})
    e.enrich_batch()
    pol = Policy(approval_file=tmp_path / "approval.json")
    w = W.SalesWorker(CrmStore(db), pol, NoGmail(), FakeLuna(), mx_check=lambda d: True)
    before = w.s.c.execute("SELECT count(*) FROM sales_send_intents").fetchone()[0]
    out = e.promote(w, limit=5, dry_run=False)
    assert out and out[0]["drafted"] is True
    assert w.s.c.execute("SELECT count(*) FROM sales_send_intents").fetchone()[0] == before          # no send, no intent
    pid = out[0]["pid"]
    assert w.s.prospect(pid)["status"] == "MESSAGE_DRAFTED" and w.s.contact_for(pid) == "info@lonestarair.example"
    assert e.store.candidate("lonestarair.example")["stage"] == "PROMOTED"
    assert e.promote(w, limit=5) == []                                                            # idempotent
    # unapproved policy: sender refuses even with a perfect draft
    assert w._send_loop({}) == "disabled: policy not owner-approved"


def test_promotion_respects_policy_daily_cap(db, tmp_path):
    e = mk_engine(db)
    pol = Policy(approval_file=tmp_path / "approval.json")
    pol.cfg["discovery"]["max_new_prospects_per_day"] = 0
    w = W.SalesWorker(CrmStore(db), pol, NoGmail(), FakeLuna())
    e.store.c.execute("INSERT INTO disco_candidates(domain,stage,rank,created_at,updated_at,website,company_name,contact_email,signal_quote,fit,contact_url) VALUES('q.example','QUALIFIED',90,0,0,'https://q.example/','Q','info@q.example','x'||'y'*30,80,'https://q.example/c')")
    assert e.promote(w, limit=5) == []


def test_blocklisted_domain_not_eligible_to_send(db, tmp_path):
    pol = Policy(approval_file=tmp_path / "approval.json"); pol.approve(["public_business_role_address"], "t")
    s = CrmStore(db)
    s.add_prospect(pid="pb", name="n", company="Portal", website="https://portal.example", email="hello@portal.example", source_url="https://portal.example/c", signal="s" * 25, proposal="p", fit=90)
    s.block_domain("portal.example", "aggregator")
    w = W.SalesWorker(s, pol, NoGmail(), None, mx_check=lambda d: True)
    s.save_draft("pb", "INITIAL_MESSAGE", "subj", "body")
    assert w.candidate_problems("pb", pol.approval())[0].startswith("domain_blocklisted")


def test_discovery_package_never_constructs_gmail():
    import dotz.sales.disco.engine as eng, inspect
    src = "".join(inspect.getsource(m) for m in (eng,))
    assert "Gmail(" not in src and ".send(" not in src


def test_performance_multiplier_neutral_without_data(db):
    assert mk_engine(db).performance_multiplier("hvac") == 1.0


def test_util_domains():
    assert canonical_domain("HTTPS://WWW.Foo.com/x?y") == "foo.com" and registrable("a.b.foo.co.uk") == "foo.co.uk"
