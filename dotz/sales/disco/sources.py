"""Source adapters. Each yields RawRecord for ONE job; the engine persists them idempotently.

Enabled sources use documented/permitted interfaces only: OpenStreetMap Overpass (ODbL open data),
robots-checked public directory pages, and DuckDuckGo's HTML endpoint (auto-cooldown when it blocks;
never worked around).
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

from dotz.sales.disco.util import canonical_domain, is_foreign_platform


class SourceBlocked(RuntimeError):
    """Source refused/limited us; engine applies cooldown. Never retried around."""


class SourceError(RuntimeError):
    pass


@dataclass
class RawRecord:
    source: str
    source_ref: str
    name: str
    website: str
    category: str
    source_url: str
    location: str | None = None
    region: str | None = None
    phone: str | None = None
    brand: str | None = None
    email_hint: str | None = None
    domain: str | None = field(default=None)

    def __post_init__(self):
        if self.domain is None and self.website:
            self.domain = canonical_domain(self.website)


def _site(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().split(";")[0].strip()
    if not v.startswith("http"):
        v = "https://" + v
    d = canonical_domain(v)
    if not d or is_foreign_platform(d):
        return None
    return f"https://{d}/"


def overpass_query(tag_filters: list[str], state: str, timeout: int = 120) -> str:
    parts = []
    for f in tag_filters:
        k, v = f.split("=", 1)
        parts += [f'nwr["{k}"="{v}"]["website"](area.a);', f'nwr["{k}"="{v}"]["contact:website"](area.a);']
    return (f'[out:json][timeout:{timeout}][maxsize:268435456];area["ISO3166-2"="US-{state}"]->.a;(' + "".join(parts) + ");out tags center;")


def parse_overpass(data: dict, category: str, state: str) -> list[RawRecord]:
    out = []
    for e in data.get("elements", []):
        t = e.get("tags", {})
        name = t.get("name") or t.get("operator")
        site = _site(t.get("website") or t.get("contact:website"))
        if not name or not site:
            continue
        city = t.get("addr:city")
        loc = ", ".join(x for x in (city, t.get("addr:state") or state) if x)
        if t.get("addr:state") and t["addr:state"].upper() != state:
            continue
        ref = f"{e['type']}/{e['id']}"
        out.append(RawRecord("osm", ref, name.strip(), site, category, f"https://www.openstreetmap.org/{ref}", loc, state,
                             t.get("phone") or t.get("contact:phone"), t.get("brand:wikidata") or t.get("brand"),
                             t.get("contact:email") or t.get("email")))
    return out


class OsmSource:
    name = "osm"

    def __init__(self, cfg: dict, fetcher):
        self.cfg, self.f = cfg["sources"]["osm"], fetcher

    def run(self, job: dict) -> list[RawRecord]:
        p = json.loads(job["params"])
        q = overpass_query(p["tags"], p["state"], self.cfg["timeout_s"])
        r = self.f.get_raw(self.cfg["endpoint"], self.cfg["min_interval_s"], data={"data": q}, timeout=self.cfg["timeout_s"] + 30)
        if r is None:
            raise SourceError("overpass unreachable")
        if r.status_code in (429, 502, 503, 504):
            raise SourceBlocked(f"overpass {r.status_code} (at capacity; backing off)")
        if r.status_code != 200:
            raise SourceError(f"overpass HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise SourceError("overpass non-JSON (likely runtime error remark)") from e
        if "remark" in data and not data.get("elements"):
            raise SourceBlocked("overpass remark: " + str(data["remark"])[:120])
        return parse_overpass(data, p["category"], p["state"])


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links, self._href, self._txt = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href"); self._txt = []

    def handle_data(self, d):
        if self._href is not None:
            self._txt.append(d)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._txt).split()))); self._href = None


def extract_links(page_html: str, base: str) -> list[tuple[str, str]]:
    p = _Links()
    try:
        p.feed(page_html)
    except Exception:
        pass
    out = []
    for h, t in p.links:
        if h:
            out.append((urllib.parse.urljoin(base, html.unescape(h)), t))
    return out


def parse_directory_page(page_html: str, base_url: str, category: str, region: str | None, source_name="directory") -> tuple[list[RawRecord], str | None]:
    """Chamber/association member pages: outbound links to other domains with a plausible business-name anchor."""
    base_dom = canonical_domain(base_url)
    recs, nxt, seen = [], None, set()
    for url, text in extract_links(page_html, base_url):
        if text.lower() in ("next", "next »", "next ›", "›", "»", "older") and canonical_domain(url) == base_dom:
            nxt = url
        d = canonical_domain(url)
        if not d or d == base_dom or is_foreign_platform(d) or d in seen or url.startswith("mailto:"):
            continue
        if not (3 <= len(text) <= 80) or text.lower() in ("website", "visit website", "click here", "more", "learn more", "visit site"):
            if not (text.lower() in ("website", "visit website", "visit site")):
                continue
        seen.add(d)
        recs.append(RawRecord(source_name, f"{base_dom}:{d}", text if len(text) > 8 or text.lower() not in ("website",) else d, f"https://{d}/",
                              category, base_url, None, region))
    return recs, nxt


class DirectorySource:
    name = "directory"

    def __init__(self, cfg: dict, fetcher):
        self.cfg, self.f = cfg["sources"]["directory"], fetcher

    def run(self, job: dict) -> tuple[list[RawRecord], str | None]:
        p = json.loads(job["params"])
        url = job["cursor"] or p["url"]
        pg = self.f.get(url)
        if pg is None:
            raise SourceBlocked("robots.txt disallows or non-HTML: " + url)
        if pg.status != 200:
            raise SourceError(f"HTTP {pg.status}")
        return parse_directory_page(pg.text, pg.url, p["category"], p.get("region"))


def parse_ddg(page_text: str) -> list[tuple[str, str]]:
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page_text, re.S):
        u = html.unescape(m.group(1))
        u = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("uddg", [u])[0]
        title = re.sub(r"<[^>]+>", "", html.unescape(m.group(2))).strip()
        if u.startswith("http"):
            out.append((u, title))
    return out


class SearchSource:
    name = "search"

    def __init__(self, cfg: dict, fetcher):
        self.cfg, self.f = cfg["sources"]["search"], fetcher

    def run(self, job: dict) -> list[RawRecord]:
        p = json.loads(job["params"])
        r = self.f.get_raw(self.cfg["endpoint"], self.cfg["min_interval_s"], data={"q": p["query"]})
        if r is None:
            raise SourceError("search unreachable")
        if r.status_code in (202, 403, 429) or "anomaly" in r.text[:5000].lower():
            raise SourceBlocked(f"search engine challenge/limit HTTP {r.status_code}")
        if r.status_code != 200:
            raise SourceError(f"search HTTP {r.status_code}")
        hits = parse_ddg(r.text)
        if not hits and "result" not in r.text.lower():
            raise SourceBlocked("search returned no result markup (challenge page)")
        recs = []
        for i, (u, title) in enumerate(hits):
            d = canonical_domain(u)
            if not d or is_foreign_platform(d):
                continue
            recs.append(RawRecord("search", f"{d}:{p['query'][:40]}", title[:100] or d, f"https://{d}/", p["category"], u, p.get("location"), p.get("region")))
        return recs
