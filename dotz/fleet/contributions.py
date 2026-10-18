"""Bounded GitHub contribution pipeline for the existing fleet state store.

This module prepares one maintainer-reviewable fix job at a time.  It never
pushes branches, opens pull requests, or sends messages; those are explicit
owner-release actions outside this pipeline.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotz.fleet.db import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS contribution_candidates(
  issue_url TEXT PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
  title TEXT NOT NULL, payload TEXT NOT NULL, score REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'ELIGIBLE', discovered_at REAL NOT NULL,
  UNIQUE(repository, number));
CREATE TABLE IF NOT EXISTS contribution_jobs(
  job_id TEXT PRIMARY KEY, issue_url TEXT NOT NULL UNIQUE, repository TEXT NOT NULL,
  number INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  evidence TEXT, draft TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_contribution_jobs_day ON contribution_jobs(created_at);
"""


@dataclass(frozen=True)
class HunterConfig:
    enabled: bool = True
    fixes_per_day: int = 1
    max_concurrent: int = 1
    candidates_per_run: int = 50
    min_issue_score: float = 75
    max_attempts: int = 2
    max_work_hours: float = 3
    external_submission: bool = False

    @classmethod
    def from_env(cls) -> "HunterConfig":
        b = lambda k, d: os.environ.get(k, str(d)).lower() in {"1", "true", "yes", "on"}
        i = lambda k, d: int(os.environ.get(k, str(d)))
        return cls(b("FETT_ENABLED", True), i("FETT_FIX_JOBS_PER_DAY", 1),
                   i("FETT_MAX_CONCURRENT_FIXES", 1), i("FETT_DISCOVERY_CANDIDATES_PER_RUN", 50),
                   float(os.environ.get("FETT_MIN_ISSUE_SCORE", "75")), i("FETT_MAX_ATTEMPTS_PER_ISSUE", 2),
                   float(os.environ.get("FETT_MAX_WORK_HOURS_PER_FIX", "3")),
                   b("FETT_AUTO_EXTERNAL_SUBMISSION", False))


def _db(path: Path | str = DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    c.executescript(SCHEMA)
    return c


def _query_text(item: dict) -> str:
    return " ".join(str(item.get(k, "")) for k in ("title", "body", "comments_url")).lower()


def score_issue(item: dict) -> float:
    """Score only observable issue metadata; unknown criteria receive no points."""
    text = _query_text(item)
    reproducibility = 30 if any(x in text for x in ("steps to reproduce", "reproduction", "repro", "expected behavior")) else 0
    acceptance = 25 if any(x in text for x in ("confirmed", "bug", "accepted", "reproduced", "good first issue")) else 0
    relevance = 20 if any(x in text for x in ("agent", "automation", "mcp", "developer", "python", "typescript")) else 0
    visibility = 15 if (item.get("repository", {}).get("stargazers_count", 0) or 0) >= 100 else 0
    effort = 10 if any(x in text for x in ("small", "simple", "typo", "regression", "fix")) else 0
    return float(reproducibility + acceptance + relevance + visibility + effort)


def eligible_issue(item: dict, minimum: float = 75) -> bool:
    """Reject pull requests, closed/archived/spam-looking, and duplicate issues."""
    if item.get("pull_request") or item.get("state") not in (None, "open"):
        return False
    repo = item.get("repository", {})
    if repo.get("archived") or not item.get("html_url") or not item.get("title"):
        return False
    text = _query_text(item)
    labels = {str(x.get("name", "")).lower() for x in item.get("labels", [])}
    if labels.intersection({"duplicate", "spam", "wontfix", "fixed", "resolved"}):
        return False
    if any(x in text for x in ("giveaway", "seo", "paid promotion", "crypto airdrop", "already fixed")):
        return False
    return score_issue(item) >= minimum


class GitHubClient:
    def __init__(self, token: str | None = None, api: str = "https://api.github.com"):
        self.api, self.token = api.rstrip("/"), token or os.environ.get("GITHUB_TOKEN", "")

    def get(self, path: str) -> dict:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "FETT-Lite-contributor"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.api + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.loads(response.read() or b"{}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"GitHub discovery failed: {type(exc).__name__}") from exc

    def search_issues(self, query: str, limit: int = 50) -> list[dict]:
        q = urllib.parse.urlencode({"q": query, "per_page": min(max(limit, 1), 100), "sort": "updated", "order": "desc"})
        data = self.get("/search/issues?" + q)
        return data.get("items", [])[:limit]


def discover(client: GitHubClient, config: HunterConfig | None = None) -> list[dict]:
    config = config or HunterConfig.from_env()
    query = "is:issue is:open stars:>=100 -label:duplicate -label:spam (agent OR automation OR MCP OR developer OR Python OR TypeScript)"
    items = client.search_issues(query, config.candidates_per_run)
    return sorted((i for i in items if eligible_issue(i, config.min_issue_score)),
                  key=score_issue, reverse=True)


def run_once(client: GitHubClient, config: HunterConfig | None = None, db_path: Path | str = DB_PATH,
             now: float | None = None) -> dict | None:
    """Discover and queue the best issue, subject to the daily and concurrency gates."""
    config, now = config or HunterConfig.from_env(), time.time() if now is None else now
    if not config.enabled or config.fixes_per_day < 1:
        return None
    c = _db(db_path)
    try:
        day = now - 86400
        active = c.execute("SELECT count(*) FROM contribution_jobs WHERE status IN ('QUEUED','RUNNING','VERIFYING','DRAFT')").fetchone()[0]
        made = c.execute("SELECT count(*) FROM contribution_jobs WHERE created_at>=?", (day,)).fetchone()[0]
        if active >= config.max_concurrent or made >= config.fixes_per_day:
            return None
        candidates = discover(client, config)
        for item in candidates:
            url = item["html_url"]
            repo = (item.get("repository") or {}).get("full_name") or item.get("repository_url", "").split("/repos/", 1)[-1]
            number = item["number"]
            if c.execute("SELECT 1 FROM contribution_candidates WHERE issue_url=?", (url,)).fetchone():
                continue
            score = score_issue(item)
            c.execute("INSERT INTO contribution_candidates VALUES(?,?,?,?,?,?,?,?)", (url, repo, number, item["title"], json.dumps(item), score, "SELECTED", now))
            jid = "FETT-" + hashlib.sha256(url.encode()).hexdigest()[:12].upper()
            c.execute("INSERT INTO contribution_jobs VALUES(?,?,?,?,?,?,?,?,?,?)", (jid, url, repo, number, "QUEUED", 0, None, None, now, now))
            return {"job_id": jid, "issue_url": url, "repository": repo, "number": number, "score": score, "status": "QUEUED"}
        return None
    finally:
        c.close()


def update_job(job_id: str, status: str, evidence: dict | None = None, draft: dict | None = None,
               db_path: Path | str = DB_PATH) -> bool:
    """Record agent/reviewer evidence; only known lifecycle states are accepted."""
    states = {"QUEUED", "RUNNING", "VERIFYING", "DRAFT", "REJECTED", "SUBMITTED", "MERGED", "CLOSED"}
    if status not in states:
        raise ValueError(f"unsupported contribution state: {status}")
    c = _db(db_path)
    try:
        cur = c.execute("UPDATE contribution_jobs SET status=?, evidence=COALESCE(?,evidence), draft=COALESCE(?,draft), updated_at=? WHERE job_id=?",
                        (status, json.dumps(evidence) if evidence is not None else None, json.dumps(draft) if draft is not None else None, time.time(), job_id))
        return cur.rowcount == 1
    finally:
        c.close()
