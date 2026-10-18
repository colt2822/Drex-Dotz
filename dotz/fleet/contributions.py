"""Bounded GitHub contribution pipeline for the existing fleet state store.

This module discovers and claims one maintainer-reviewable fix job at a time.
It never pushes branches, opens pull requests, or sends messages itself; the
later stages live in hunter_pipeline and external writes there are gated by
auto_external_submission.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

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
CREATE TABLE IF NOT EXISTS contribution_runs(
  run_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_contribution_runs_created ON contribution_runs(created_at);
"""


@dataclass(frozen=True)
class HunterConfig:
    enabled: bool = True
    fix_jobs_per_day: int = 1
    max_concurrent_fixes: int = 1
    discovery_candidates_per_run: int = 50
    min_issue_score: float = 75
    max_attempts_per_issue: int = 2
    max_work_hours_per_fix: float = 3
    auto_external_submission: bool = False
    daily_budget_usd: float = 0

    # Compatibility with the names used by the original hunter core.
    @property
    def fixes_per_day(self): return self.fix_jobs_per_day
    @property
    def max_concurrent(self): return self.max_concurrent_fixes
    @property
    def candidates_per_run(self): return self.discovery_candidates_per_run
    @property
    def max_attempts(self): return self.max_attempts_per_issue
    @property
    def max_work_hours(self): return self.max_work_hours_per_fix
    @property
    def external_submission(self): return self.auto_external_submission

    ENV_KEYS: ClassVar[dict[str, str]] = {
        "enabled": "FETT_ENABLED", "fix_jobs_per_day": "FETT_FIX_JOBS_PER_DAY",
        "max_concurrent_fixes": "FETT_MAX_CONCURRENT_FIXES",
        "discovery_candidates_per_run": "FETT_DISCOVERY_CANDIDATES_PER_RUN",
        "min_issue_score": "FETT_MIN_ISSUE_SCORE",
        "max_attempts_per_issue": "FETT_MAX_ATTEMPTS_PER_ISSUE",
        "max_work_hours_per_fix": "FETT_MAX_WORK_HOURS_PER_FIX",
        "auto_external_submission": "FETT_AUTO_EXTERNAL_SUBMISSION",
        "daily_budget_usd": "FETT_DAILY_BUDGET_USD",
    }

    @property
    def KEYS(self): return tuple(self.ENV_KEYS)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.ENV_KEYS}

    def validate(self) -> "HunterConfig":
        if self.fix_jobs_per_day < 0 or self.max_concurrent_fixes < 0 or self.discovery_candidates_per_run < 1:
            raise ValueError("job, concurrency, and candidate limits must be non-negative (candidates >= 1)")
        if not 0 <= self.min_issue_score <= 100:
            raise ValueError("min_issue_score must be between 0 and 100")
        if self.max_attempts_per_issue < 1 or self.max_work_hours_per_fix <= 0 or self.daily_budget_usd < 0:
            raise ValueError("attempts and work hours must be positive; budget must be non-negative")
        return self

    @classmethod
    def load(cls, path: Path | str | None = None) -> "HunterConfig":
        path = Path(path or os.environ.get("FETT_HUNTER_CONFIG", Path.home() / ".config" / "dotz" / "pr_hunter.json"))
        values = {k: getattr(cls(), k) for k in cls.ENV_KEYS}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise ValueError(f"invalid hunter config {path}") from exc
            if not isinstance(data, dict):
                raise ValueError("hunter config must be a JSON object")
            values.update({k: data[k] for k in values if k in data})
        for field, env_key in cls.ENV_KEYS.items():
            if env_key in os.environ:
                raw = os.environ[env_key]
                values[field] = (raw.lower() in {"1", "true", "yes", "on"} if isinstance(getattr(cls(), field), bool)
                                 else type(getattr(cls(), field))(raw))
        return cls(**values).validate()

    def save(self, path: Path | str | None = None) -> Path:
        self.validate()
        target = Path(path or os.environ.get("FETT_HUNTER_CONFIG", Path.home() / ".config" / "dotz" / "pr_hunter.json"))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: getattr(self, k) for k in self.ENV_KEYS}
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return target

    @classmethod
    def from_env(cls) -> "HunterConfig":
        return cls.load()


def _db(path: Path | str = DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    c.executescript(SCHEMA)
    return c


BOUNTY_LABELS = {"bounty", "💎 bounty", "algora", "bounty:"}


def is_bounty(item: dict) -> bool:
    labels = {str(x.get("name", "")).lower() for x in item.get("labels", [])}
    return bool(labels & BOUNTY_LABELS) or any(x.startswith("$") and x[1:].isdigit() for x in labels)


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
    fallback = float(reproducibility + acceptance + relevance + visibility + effort)
    try:
        from dotz.fleet.model_usage import drex_decide
        decision = drex_decide(
            f"Classify this GitHub issue for the fleet hunter: {text[:3000]}",
            choices=["QUALIFY", "REJECT"],
        )
        if decision:
            return 100.0 if str(decision["answer"]).upper() == "QUALIFY" else 0.0
    except Exception:
        pass
    return fallback


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
    # Quality floor: a maintainer-acknowledged bug of fixable size, not a campaign or feature wish-list.
    if not labels.intersection({"bug", "good first issue", "help wanted", "confirmed", "regression"} | BOUNTY_LABELS):
        return False
    body = item.get("body") or ""
    if not 80 <= len(body) <= 6000 or (item.get("comments") or 0) > 20 or item.get("assignee") or item.get("assignees"):
        return False
    if re.search(r"\b(master|campaign|roadmap|epic|tracking|umbrella|execution request|megathread)\b", item.get("title", ""), re.I):
        return False
    if (item.get("user") or {}).get("type") == "Bot":
        return False
    stars = (item.get("repository") or {}).get("stargazers_count")
    if stars is not None and stars < 100:
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
    items: dict[str, dict] = {}
    for lang in ("python", "typescript"):
        query = f"is:issue is:open label:bug language:{lang} comments:<=20 no:assignee -label:duplicate"
        for item in client.search_issues(query, max(1, config.candidates_per_run // 2)):
            items.setdefault(item.get("html_url", ""), item)
        # Bounty lane: same quality floor (stars, size, unassigned, no campaign titles), found by bounty labels.
        for label in ("bounty", '"💎 Bounty"'):
            query = f"is:issue is:open label:{label} language:{lang} comments:<=10 no:assignee -label:duplicate"
            try:
                for item in client.search_issues(query, 10):
                    items.setdefault(item.get("html_url", ""), item)
            except RuntimeError:
                continue
    ranked = sorted((i for i in items.values() if eligible_issue(i, 0)), key=lambda i: (is_bounty(i), score_issue(i)), reverse=True)
    # Star counts need one repo call each (60/h unauthenticated): enrich only the leading handful.
    out = []
    for item in ranked[:10]:
        try:
            repo = client.get("/repos/" + item["repository_url"].split("/repos/", 1)[-1])
        except (RuntimeError, KeyError):
            continue
        item["repository"] = {k: repo.get(k) for k in ("full_name", "stargazers_count", "archived", "pushed_at")}
        if eligible_issue(item, config.min_issue_score):
            out.append(item)
    return sorted(out, key=lambda i: (is_bounty(i), score_issue(i)), reverse=True)


def run_once(client: GitHubClient, config: HunterConfig | None = None, db_path: Path | str = DB_PATH,
             now: float | None = None) -> dict | None:
    """Discover and queue the best issue, subject to the daily and concurrency gates."""
    config, now = config or HunterConfig.from_env(), time.time() if now is None else now
    if not config.enabled or config.fix_jobs_per_day < 1:
        return None
    c = _db(db_path)
    try:
        day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        tomorrow = day + 86400
        active = c.execute("SELECT count(*) FROM contribution_jobs WHERE status IN ('QUEUED','RUNNING','VERIFYING','DRAFT')").fetchone()[0]
        # A job rejected at preflight cost nothing (no work, no external write), so it does not use up the day's quota.
        made = c.execute("SELECT count(*) FROM contribution_jobs WHERE created_at>=? AND created_at<? "
                         "AND NOT (status='REJECTED' AND evidence LIKE '%\"gate\": \"preflight\"%')", (day, tomorrow)).fetchone()[0]
        if active >= config.max_concurrent_fixes or made >= config.fix_jobs_per_day:
            return None
        candidates = discover(client, config)
        for item in candidates:
            url = item["html_url"]
            repo = (item.get("repository") or {}).get("full_name") or item.get("repository_url", "").split("/repos/", 1)[-1]
            number = item["number"]
            if c.execute("SELECT 1 FROM contribution_candidates WHERE issue_url=? OR (repository=? AND number=?)",
                         (url, repo, number)).fetchone():
                continue
            score = score_issue(item)
            c.execute("INSERT INTO contribution_candidates VALUES(?,?,?,?,?,?,?,?)", (url, repo, number, item["title"], json.dumps(item), score, "SELECTED", now))
            jid = "FETT-" + hashlib.sha256(url.encode()).hexdigest()[:12].upper()
            c.execute("INSERT INTO contribution_jobs VALUES(?,?,?,?,?,?,?,?,?,?)", (jid, url, repo, number, "QUEUED", 0, None, None, now, now))
            return {"job_id": jid, "issue_url": url, "repository": repo, "number": number, "score": score, "status": "QUEUED"}
        run_id = "RUN-" + hashlib.sha256(f"{now}:{day}".encode()).hexdigest()[:12].upper()
        detail = {"reason": "no issue met min_issue_score", "min_issue_score": config.min_issue_score,
                  "candidates": len(candidates)}
        c.execute("INSERT OR REPLACE INTO contribution_runs VALUES(?,?,?,?)",
                  (run_id, "NO_QUALIFIED_ISSUE", json.dumps(detail), now))
        return {"run_id": run_id, "status": "NO_QUALIFIED_ISSUE", **detail}
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
