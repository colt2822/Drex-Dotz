"""Polite HTTP: robots.txt cache, per-host throttle, bounded retries, size cap. Never spoofs a browser."""
from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass

import httpx


@dataclass
class Page:
    url: str
    status: int
    text: str
    fetched_at: float
    content_type: str = ""


class Fetcher:
    def __init__(self, ua: str, per_host_min_interval_s: float = 3.0, timeout_s: float = 15, retries: int = 2,
                 max_body_bytes: int = 1_500_000, robots_ttl_s: float = 21600, client: httpx.Client | None = None,
                 clock=time.time, sleep=time.sleep):
        self.ua, self.interval, self.retries, self.max_body, self.robots_ttl = ua, per_host_min_interval_s, retries, max_body_bytes, robots_ttl_s
        self.http = client or httpx.Client(timeout=timeout_s, headers={"User-Agent": ua}, follow_redirects=True,
                                           limits=httpx.Limits(max_connections=32, max_keepalive_connections=16))
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._next_ok: dict[str, float] = {}
        self._robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
        self.stats = {"requests": 0, "robots_denied": 0, "errors": 0, "retries": 0}

    @classmethod
    def from_cfg(cls, cfg: dict, **kw) -> "Fetcher":
        n = cfg["net"]
        return cls(cfg["user_agent"], n["per_host_min_interval_s"], n["timeout_s"], n["retries"], n["max_body_bytes"], n["robots_ttl_s"], **kw)

    def _throttle(self, host: str, interval: float | None = None) -> None:
        iv = self.interval if interval is None else interval
        with self._lock:
            now = self.clock()
            at = max(now, self._next_ok.get(host, 0.0))
            self._next_ok[host] = at + iv
        if at > now:
            self.sleep(at - now)

    def _raw_get(self, url: str, host: str, interval: float | None = None, timeout: float | None = None) -> httpx.Response | None:
        last = None
        for attempt in range(self.retries + 1):
            self._throttle(host, interval)
            self.stats["requests"] += 1
            try:
                r = self.http.get(url, timeout=timeout) if timeout else self.http.get(url)
            except httpx.HTTPError:
                self.stats["errors"] += 1
                last = None
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = r
                    self.stats["retries"] += 1
                    wait = min(float(r.headers.get("retry-after", "5") or 5), 60) if r.headers.get("retry-after", "5").isdigit() else 5
                    with self._lock:  # back the whole host off
                        self._next_ok[host] = max(self._next_ok.get(host, 0), self.clock() + wait * (attempt + 1))
                    continue
                return r
        return last

    def allowed(self, url: str) -> bool:
        p = urllib.parse.urlparse(url)
        host = p.netloc
        with self._lock:
            ent = self._robots.get(host)
        if not ent or self.clock() - ent[0] > self.robots_ttl:
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            r = self._raw_get(f"{p.scheme}://{host}/robots.txt", host)
            if r is None or r.status_code >= 500:
                rp = None  # unreachable robots => treat as disallowed this round (conservative)
            elif r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp.parse([])  # 404/403-other => no rules
            with self._lock:
                self._robots[host] = (self.clock(), rp)
            ent = (0, rp)
        rp = ent[1]
        ok = bool(rp and rp.can_fetch(self.ua, url))
        if not ok:
            self.stats["robots_denied"] += 1
        return ok

    def get(self, url: str) -> Page | None:
        """HTML page text or None (robots-denied, non-HTML, error). Status kept for dead-site detection."""
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        if not self.allowed(url):
            return None
        r = self._raw_get(url, p.netloc)
        if r is None:
            return Page(url, 0, "", self.clock())
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "html" not in ct:
            return None
        return Page(str(r.url), r.status_code, r.text[: self.max_body] if r.status_code == 200 else "", self.clock(), ct)

    def get_raw(self, url: str, interval: float, data: dict | None = None, timeout: float | None = None):
        """Non-robots API call (documented public APIs): throttled/retried only."""
        p = urllib.parse.urlparse(url)
        if data is None:
            return self._raw_get(url, p.netloc, interval, timeout)
        self._throttle(p.netloc, interval)
        self.stats["requests"] += 1
        try:
            return self.http.post(url, data=data, timeout=timeout) if timeout else self.http.post(url, data=data)
        except httpx.HTTPError:
            self.stats["errors"] += 1
            return None
