"""Company website enrichment: contact pages, hiring pages, deterministic opportunity signals with verbatim quotes.

Pure w.r.t. storage: returns (fields, evidence) for the engine to persist. Contact verification is a separate
step (verify_contact) that needs only the fetched page text, so it is testable offline from saved HTML.
"""
from __future__ import annotations

import re
import urllib.parse

from dotz.sales.disco.sources import extract_links
from dotz.sales.disco.util import canonical_domain, same_business_domain
from html.parser import HTMLParser


class _Text(HTMLParser):
    """Block-aware text: sentence boundaries survive (the legacy html_to_text glues adjacent blocks together)."""
    BLOCK = {"p", "div", "li", "ul", "ol", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "tr", "td", "th", "table", "form", "label", "button", "footer", "header", "nav", "span", "a"}
    SKIP = {"script", "style", "noscript", "svg", "nav", "select", "option"}

    def __init__(self):
        super().__init__(); self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v and v.startswith("mailto:"):
                    self.out.append(" " + v[7:].split("?")[0] + " ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_text(src: str) -> str:
    p = _Text()
    try:
        p.feed(src)
    except Exception:
        pass
    t = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    return re.sub(r"\n\s*\n+", "\n", t).strip()

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_TLD = {"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "woff", "woff2", "ico"}
FREE = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "live.com", "msn.com",
        "proton.me", "protonmail.com", "me.com", "comcast.net", "att.net", "sbcglobal.net"}
BLOCKED_LOCAL = re.compile(r"^(privacy|legal|abuse|dmca|noreply|no-reply|donotreply|do-not-reply|careers?|jobs?|hr|humanresources|recruit\w*|hiring|press|media|"
                           r"webmaster|postmaster|security|compliance|billing|accounts?|accounting|ap|ar|payroll|invoices?|unsubscribe|support-?legal|admin-?legal|"
                           r"mailer-daemon|sentry|wordpress|example|test|your-?name|name|email|user|username|johndoe)$", re.I)
ROLE_LOCAL = re.compile(r"^(info|contact|contactus|office|service|services|sales|hello|hi|support|estimates?|quotes?|dispatch|schedule|scheduling|"
                        r"appointments?|admin|customerservice|customercare|team|mail|inquiries|inquiry|request|bookings?|frontdesk|reception|help)$", re.I)

SIGNALS = {
    "after_hours": r"24/7|24[- ]hour|after[- ]hours|emergency (service|repair|call|plumb|hvac|roof)|nights and weekends",
    "appointment_dispatch": r"schedule (a |your )?(service|appointment|visit|repair)|book (an? |your )?(appointment|service|visit)|request (an? )?(appointment|service call)|dispatch",
    "manual_intake": r"request (a |your )?(free )?(quote|estimate)|get (a |your )?(free )?(quote|estimate)|free (quotes?|estimates?)|tell us about (your|the)|submit (the|this|your) form",
    "phone_dependency": r"call (us|now|today)|give us a call|call for (a |an )?(quote|estimate|service|appointment)|speak (with|to) (a|an|our)",
    "hiring": r"now hiring|we.?re hiring|join our team|career opportunities|job openings|apply (now|today)|open positions",
    "expansion": r"now serving|new location|we.?ve expanded|grand opening|expanding|additional service areas|opening a new",
    "repetitive_comms": r"appointment reminders?|we will (call|text|email) you (back|to confirm)|(call|contact) you (back|within)",
}
AUTOMATION_PRESENT = r"book online|schedule online|online (scheduling|booking)|book now|servicetitan|housecall ?pro|jobber|calendly|live chat|chat with us|chatbot|text-to-pay|customer portal"
AGG_STRONG = [r"compare (quotes|prices|contractors)", r"get matched", r"find (a|the best|top) (local )?(pro|contractor|plumber|roofer|electrician|mechanic)s?",
              r"our network of (contractors|pros|roofers|plumbers|professionals)", r"connects? you with", r"lead gen", r"not (a|an) (roofing|plumbing|hvac|electrical|contracting) (company|contractor)",
              r"we are not a", r"advertise with us", r"a local (specialist|pro|expert|professional)s? will (contact|call)", r"nationwide (plumbing|hvac|roofing|electrical|network)"]
AGG_WEAK = [r"\btop \d+\b", r"\bbest [a-z ]{3,40} in [A-Z]", r"write a review", r"reviews? (of|for) ", r"sponsored", r"directory"]  # common on real contractor sites: never decisive alone
AGENCY = [r"digital marketing", r"\bseo\b", r"web design", r"lead generation", r"marketing agency", r"we help (contractors|home service|local business)",
          r"book a demo", r"request a demo", r"pricing plans?", r"\bsaas\b", r"software (for|platform)", r"franchise opportunit", r"white[- ]label"]
AGG_NAME = re.compile(r"review|quote|vetted|compar|director|finder|near ?me|\btop\b|\bbest\b|\bpros?\b$|listing|leads?\b", re.I)
PARKED = re.compile(r"domain (is )?for sale|buy this domain|this domain (may be|is) for sale|parked (free|domain)|coming soon|under construction|account suspended|site not found|default web site page", re.I)
PHONE = re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")
ADDRESS = re.compile(r"\b\d{2,5}\s+[A-Za-z0-9.\s]{3,30}\s(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Highway|Hwy|Way|Court|Ct|Parkway|Pkwy)\b", re.I)
PAGE_HINT = re.compile(r"contact|about|career|job|hiring|team|schedule|appointment|quote|estimate|service-area|book", re.I)
CHAIN_TEXT = re.compile(r"\b(over|more than|\d{2,}\+?)\s*\d*\s*(locations|branches|franchises)|franchise (owner|opportunit)|nationwide network", re.I)


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def valid_email(e: str) -> bool:
    local, _, dom = e.partition("@")
    return bool(local and dom) and dom.rsplit(".", 1)[-1].lower() not in BAD_TLD and ".." not in e and len(e) <= 80


def _clean_quote(q: str) -> bool:
    words = q.split()
    return len(words) >= 4 and q[0].isalnum() and q[0] not in "abcdefghijklmnopqrstuvwxyz" and not re.search(r"opt out|reply stop|privacy|cookie|terms (of|and)|all rights", q, re.I) and len(re.findall(r"[a-z][A-Z]", q)) <= 1 and not re.search(r"facebook|twitter|instagram|©|\bmenu\b|\bhome about\b", q, re.I) \
        and len(re.findall(r"\d{3}[-. ]\d{3}[-. ]\d{4}", q)) <= 1


def quote_for(text: str, pattern: str) -> str | None:
    """Verbatim, human-readable sentence containing the match (25-180 chars), traceable to the page. Skips nav blobs."""
    for m in re.finditer(pattern, text, re.I):
        lo = max(text.rfind("\n", 0, m.start()), text.rfind(". ", 0, m.start()) + 1 if text.rfind(". ", 0, m.start()) >= 0 else -1) + 1
        ends = [i for i in (text.find("\n", m.end()), text.find(". ", m.end())) if i != -1]
        hi = (min(ends) + 1) if ends else len(text)
        q = re.sub(r"\s+", " ", text[lo:hi]).strip()
        if len(q) > 180:  # long run-on line: take a word-aligned window around the match
            a_, b_ = max(0, m.start() - lo - 70), min(len(q), m.end() - lo + 90)
            q = q[a_:b_]
            q = q[q.find(" ") + 1:] if a_ > 0 else q
            q = q[: q.rfind(" ")] if b_ < len(text[lo:hi]) else q
        if 25 <= len(q) <= 180 and _clean_quote(q) and norm_ws(q) in norm_ws(text):
            return q
    return None


def pick_pages(home_url: str, home_html: str, limit: int) -> list[str]:
    host = canonical_domain(home_url)
    urls = []
    for u, _t in extract_links(home_html, home_url):
        p = urllib.parse.urlparse(u)
        if canonical_domain(u) == host and p.scheme in ("http", "https") and PAGE_HINT.search(p.path) and not re.search(r"\.(pdf|jpg|png)$", p.path, re.I):
            u = urllib.parse.urlunparse(p._replace(fragment="", query=""))
            if u not in urls and u.rstrip("/") != home_url.rstrip("/"):
                urls.append(u)
    base = f"{urllib.parse.urlparse(home_url).scheme}://{urllib.parse.urlparse(home_url).netloc}"
    pri = [u for u in urls if re.search(r"contact", u, re.I)] or [base + "/contact"]
    rest = [u for u in urls if u not in pri]
    return (pri[:2] + rest)[: max(0, limit - 1)]


def extract_emails(text: str, site_domain: str) -> list[dict]:
    out, seen = [], set()
    for e in EMAIL_RE.findall(text):
        e = e.strip(".").lower()
        if e in seen or not valid_email(e):
            continue
        seen.add(e)
        local, dom = e.split("@")
        if dom in FREE:
            out.append({"email": e, "ok": False, "why": "free_mail"})
        elif BLOCKED_LOCAL.match(local):
            out.append({"email": e, "ok": False, "why": "blocked_local_part"})
        elif not same_business_domain(dom, site_domain):
            out.append({"email": e, "ok": False, "why": "email_domain_differs"})
        else:
            out.append({"email": e, "ok": True, "type": "role" if ROLE_LOCAL.match(local) else "named_person"})
    return out


def enrich_site(fetcher, cand, pages_per_site: int = 5) -> tuple[dict, list[tuple]]:
    """Returns (fields for disco_candidates, evidence rows (kind, quote, url, fetched_at))."""
    domain, home = cand["domain"], cand["website"]
    ev: list[tuple] = []
    f: dict = {"pages_fetched": 0, "enriched_at": None}
    pg = fetcher.get(home)
    if pg is None:
        return {**f, "stage": "REJECTED", "reject_reason": "robots_denied_or_non_html", "operating_status": "unverified"}, ev
    if pg.status in (401, 403, 429):
        return {**f, "stage": "REJECTED", "reject_reason": "site_blocks_automated_access", "operating_status": f"unverified_http_{pg.status}"}, ev
    if pg.status == 0 or pg.status >= 400:
        return {**f, "stage": "REJECTED", "reject_reason": "dead_site", "operating_status": f"site_unreachable_http_{pg.status}"}, ev
    home_text = html_to_text(pg.text)
    if PARKED.search(home_text[:3000]) and len(home_text) < 1500:
        return {**f, "stage": "REJECTED", "reject_reason": "parked_or_placeholder_site", "operating_status": "not_operating_evidence"}, ev
    pages = [(pg.url, home_text)]
    for u in pick_pages(pg.url, pg.text, pages_per_site):
        p2 = fetcher.get(u)
        if p2 and p2.status == 200:
            pages.append((p2.url, html_to_text(p2.text)))
    f["pages_fetched"] = len(pages)
    alltext = "\n".join(t for _, t in pages)
    now = pg.fetched_at
    sig: dict[str, dict] = {}
    for kind, pat in SIGNALS.items():
        for url, text in pages:
            q = quote_for(text, pat)
            if q:
                sig[kind] = {"quote": q, "url": url}
                ev.append((kind, q, url, now))
                break
    auto = None
    for url, text in pages:
        q = quote_for(text, AUTOMATION_PRESENT)
        if q:
            auto = {"quote": q, "url": url}
            ev.append(("automation_present", q, url, now))
            break
    agg_s = [p for p in AGG_STRONG if re.search(p, alltext[:20000], re.I)]
    agg_w = [p for p in AGG_WEAK if re.search(p, alltext[:20000], re.I)]
    name_hit = bool(AGG_NAME.search(cand["company_name"] or ""))
    agg = agg_s if (len(agg_s) >= 2 or (agg_s and (len(agg_w) >= 2 or name_hit))) else []
    agency = [p for p in AGENCY if re.search(p, alltext[:20000], re.I)]
    chain = bool(CHAIN_TEXT.search(alltext)) or bool(cand["brand"]) or (cand["locations"] or 1) >= 5
    operating_evidence = bool(PHONE.search(alltext) or ADDRESS.search(alltext)) and f["pages_fetched"] >= 1
    # contacts (separate from company discovery): only addresses printed on the company's own site
    contacts = []
    for url, text in pages:
        for c in extract_emails(text, domain):
            c["url"] = url
            c["contact_page"] = bool(re.search(r"contact", url, re.I))
            contacts.append(c)
    best = None
    ok = [c for c in contacts if c["ok"]]
    if ok:
        ok.sort(key=lambda c: (c["type"] != "role", not c["contact_page"]))
        best = ok[0]
        ev.append(("contact_email", best["email"], best["url"], now))
    fields = {
        "stage": "ENRICHED", "enriched_at": now, "pages_fetched": f["pages_fetched"],
        "operating_status": "operating_site_live_with_contact_details" if operating_evidence else "site_live_no_contact_details",
        "signals": __import__("json").dumps({"present": sorted(sig), "automation_present": bool(auto), "aggregator_hits": agg[:5], "aggregator_weak": agg_w[:5], "name_pattern": name_hit, "agency_hits": agency[:5],
                                              "chain": chain, "contact_candidates": [{k: c[k] for k in ("email", "ok", "why", "type", "url") if k in c} for c in contacts[:8]]}),
        "contact_email": best["email"] if best else None, "contact_type": best["type"] if best else None,
        "contact_url": best["url"] if best else None, "contact_checked_at": now,
        "contact_method": ("published_on_official_site_page" + ("(contact_page)" if best and best["contact_page"] else "")) if best else None,
    }
    # best grounded signal quote for drafting: prefer manual workflow evidence
    for k in ("manual_intake", "appointment_dispatch", "after_hours", "phone_dependency", "repetitive_comms", "hiring", "expansion"):
        if k in sig:
            fields["signal_quote"], fields["signal_url"] = sig[k]["quote"], sig[k]["url"]
            break
    return fields, ev
