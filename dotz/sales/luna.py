"""Luna: real model inference via the local OpenAI-compatible NOVA router (codex worker, gpt-5.6-luna).

No mock path. If the router or model is unavailable, callers get LunaUnavailable and stay resumable.
Grounding is enforced in code: any extracted fact must appear verbatim in the fetched public page.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.robotparser
from html.parser import HTMLParser

import httpx

API_BASE = "http://127.0.0.1:4000/v1"
MODEL = "codex_1"
UA = "ELYRAON-research/1.0 (+https://elyraon.com.co; contact colt@elyraon.com.co)"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class LunaUnavailable(RuntimeError):
    pass


class Luna:
    def __init__(self, base: str = API_BASE, model: str = MODEL, http: httpx.Client | None = None):
        self.base, self.model = base.rstrip("/"), model
        self.http = http or httpx.Client(timeout=180, headers={"User-Agent": UA}, follow_redirects=True)
        self.calls = 0

    def complete(self, prompt: str, system: str = "You are Luna, ELYRAON's careful research and drafting model.",
                 max_tokens: int = 900) -> str:
        try:
            r = self.http.post(f"{self.base}/chat/completions", json={
                "model": self.model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
            r.raise_for_status()
            self.calls += 1
            return r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise LunaUnavailable(f"Luna router call failed: {type(e).__name__}: {str(e)[:150]}") from e

    def complete_json(self, prompt: str, **kw) -> dict:
        out = self.complete(prompt + "\n\nReturn ONLY one JSON object, no prose, no code fence.", **kw)
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            raise LunaUnavailable("Luna returned no JSON")
        try:
            return json.loads(m.group(0))
        except ValueError as e:
            raise LunaUnavailable("Luna returned invalid JSON") from e

    # ---- public web (polite, robots-aware) ---------------------------------
    def fetch_text(self, url: str, max_chars: int = 12000) -> str | None:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        try:
            rp = urllib.robotparser.RobotFileParser()
            rr = self.http.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10)
            if rr.status_code == 200:
                rp.parse(rr.text.splitlines())
                if not rp.can_fetch(UA, url):
                    return None
            r = self.http.get(url, timeout=20)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                return None
        except httpx.HTTPError:
            return None
        return html_to_text(r.text)[:max_chars]

    def search(self, query: str, limit: int = 8) -> list[str]:
        """Public search results (DuckDuckGo HTML). Returns result URLs; best-effort, no key, no cost."""
        try:
            r = self.http.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=20)
        except httpx.HTTPError:
            return []
        urls = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"', r.text):
            u = html.unescape(m.group(1))
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            u = q.get("uddg", [u])[0]
            if u.startswith("http") and not any(b in u for b in ("duckduckgo", "yelp.", "facebook.", "linkedin.", "yellowpages", "angi.", "bbb.org", "upwork.")):
                urls.append(u)
        return list(dict.fromkeys(urls))[:limit]

    # ---- grounded extraction -------------------------------------------------
    def extract_prospect(self, url: str, text: str, offer_context: str) -> dict | None:
        j = self.complete_json(
            f"Public page text from {url} follows between <page> tags. Extract facts ONLY from it.\n<page>\n{text}\n</page>\n"
            f"ELYRAON builds custom software/automation for small businesses ({offer_context}).\n"
            "JSON keys: company (string), us_business (bool), operating_business (bool: true ONLY if this is itself an operating local service/trade business that sells services to customers; FALSE for directories, review sites, lead-generation/quote-marketplaces, agencies, publishers, software vendors), contact_email (an email address printed in the page text that "
            "is a business/role address, else null), signal_quote (an exact sentence or phrase copied verbatim from the page "
            "showing a manual workflow ELYRAON could help with, else null), fit_score (0-100), proposal (one factual sentence "
            "on what ELYRAON could build; no claims about results).")
        return ground(j, text, url)

    def draft_message(self, prospect: dict, offer_url: str) -> dict:
        j = self.complete_json(
            "Write a short, honest first-contact business email from Colt McClain at ELYRAON (custom software and workflow "
            "automation) to the company below. Rules: under 90 words in the body; plain text; start with 'Hello <company> team,'; "
            f"reference this exact observed signal naturally: \"{prospect['signal']}\"; one low-pressure question; mention "
            f"{offer_url}; NO prices, guarantees, results, urgency, flattery, or claims you cannot support; do not include a "
            "signature, address, or opt-out line (added separately).\n"
            f"Company: {prospect['company']}\nWebsite: {prospect['website']}\nJSON keys: subject (under 60 chars), body.")
        return {"subject": str(j.get("subject", "")).strip(), "body": str(j.get("body", "")).strip()}

    def classify_reply(self, text: str) -> str:
        j = self.complete_json(
            "Classify this inbound reply to a cold business email. Allowed: INTERESTED, PRICING, NOT_NOW, DECLINE, OTHER. "
            f"Reply text:\n<reply>\n{text[:3000]}\n</reply>\nJSON key: classification.")
        c = str(j.get("classification", "OTHER")).upper()
        return c if c in {"INTERESTED", "PRICING", "NOT_NOW", "DECLINE", "OTHER"} else "OTHER"


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def ground(j: dict, text: str, url: str) -> dict | None:
    """Accept extraction only if its facts appear verbatim in the fetched page."""
    email = (j.get("contact_email") or "").strip()
    quote = (j.get("signal_quote") or "").strip()
    if not email or not quote or not j.get("us_business") or not j.get("operating_business"):
        return None
    local = email.split("@")[0].casefold()
    if re.match(r"(privacy|legal|abuse|dmca|noreply|no-reply|donotreply|careers?|jobs?|hr|press|media|webmaster|postmaster|security|compliance|billing|support-?legal|unsubscribe)", local):
        return None
    if email.casefold() not in {e.casefold() for e in EMAIL_RE.findall(text)}:
        return None
    if norm_ws(quote) not in norm_ws(text) or len(quote) < 20:
        return None
    try:
        fit = int(j.get("fit_score", 0))
    except (TypeError, ValueError):
        return None
    host = urllib.parse.urlparse(url).hostname or ""
    return {"company": str(j.get("company", ""))[:120], "email": email, "signal": quote[:300], "fit": fit,
            "proposal": str(j.get("proposal", ""))[:300], "website": f"{urllib.parse.urlparse(url).scheme}://{host}",
            "source_url": url}


class _T(HTMLParser):
    def __init__(self):
        super().__init__(); self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v and v.startswith("mailto:"):
                    self.out.append(" " + v[7:].split("?")[0] + " ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_text(src: str) -> str:
    p = _T()
    try:
        p.feed(src)
    except Exception:
        pass
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", "".join(p.out)))
