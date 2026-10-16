from __future__ import annotations

import re
import urllib.parse

TWO_LEVEL = {"co", "com", "org", "net", "gov", "edu"}
# website builders / social / link hubs / portals: a business here has no canonical domain of its own
NOT_OWN_DOMAIN = ("facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "tiktok.com", "yelp.com",
                  "nextdoor.com", "linktr.ee", "business.site", "wixsite.com", "weebly.com", "sites.google.com", "godaddysites.com",
                  "squarespace.com", "wordpress.com", "blogspot.com", "mapquest.com", "yellowpages.com", "angi.com", "angieslist.com",
                  "homeadvisor.com", "thumbtack.com", "bbb.org", "manta.com", "porch.com", "houzz.com", "google.com", "bing.com",
                  "expertise.com", "buildzoom.com", "networx.com", "fixr.com", "homeguide.com", "upwork.com", "indeed.com", "glassdoor.com",
                  "carfax.com", "repairpal.com", "superpages.com", "chamberofcommerce.com", "alignable.com", "duckduckgo.com")


def host_of(url_or_host: str) -> str:
    s = url_or_host.strip()
    if "//" not in s:
        s = "//" + s
    h = (urllib.parse.urlparse(s).hostname or "").lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def registrable(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def canonical_domain(url_or_host: str) -> str | None:
    h = host_of(url_or_host)
    if not h or "." not in h or not re.fullmatch(r"[a-z0-9.-]+", h):
        return None
    return h


def is_foreign_platform(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in NOT_OWN_DOMAIN)


def same_business_domain(email_domain: str, site_domain: str) -> bool:
    return registrable(email_domain.lower()) == registrable(site_domain.lower())


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.casefold()).strip("-")[:n]
