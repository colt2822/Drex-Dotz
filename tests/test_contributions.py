import json
import sqlite3

from dotz.fleet.contributions import HunterConfig, eligible_issue, run_once, score_issue, update_job


class Client:
    def __init__(self, items):
        self.items = items

    def search_issues(self, query, limit):
        return self.items


def issue(url="https://github.com/acme/tool/issues/1", title="Fix reproducible MCP agent regression", number=1):
    return {"html_url": url, "repository_url": "https://api.github.com/repos/acme/tool", "number": number,
            "title": title, "body": "Steps to reproduce. Maintainer confirmed bug; expected behavior is documented.",
            "state": "open", "repository": {"full_name": "acme/tool", "stargazers_count": 500}}


def test_score_and_filters():
    assert score_issue(issue()) == 100
    assert eligible_issue(issue())
    assert not eligible_issue(issue(title="crypto airdrop"))
    assert not eligible_issue({**issue(), "pull_request": {"url": "x"}})


def test_run_once_is_daily_and_draft_only(tmp_path):
    db = tmp_path / "fleet.db"
    cfg = HunterConfig()
    result = run_once(Client([issue()]), cfg, db, now=100000)
    assert result["status"] == "QUEUED"
    assert run_once(Client([issue("https://github.com/acme/tool/issues/2")]), cfg, db, now=100001) is None
    assert update_job(result["job_id"], "DRAFT", {"tests": {"passed": True}}, {"title": "Fix"}, db)
    row = sqlite3.connect(db).execute("SELECT status,evidence,draft FROM contribution_jobs").fetchone()
    assert row[0] == "DRAFT" and json.loads(row[1])["tests"]["passed"] and json.loads(row[2])["title"] == "Fix"


def test_config_load_save_and_env_override(tmp_path, monkeypatch):
    path = tmp_path / "pr_hunter.json"
    saved = HunterConfig(fix_jobs_per_day=4, max_concurrent_fixes=2, daily_budget_usd=12.5)
    saved.save(path)
    monkeypatch.setenv("FETT_FIX_JOBS_PER_DAY", "7")
    loaded = HunterConfig.load(path)
    assert loaded.fix_jobs_per_day == 7
    assert loaded.max_concurrent_fixes == 2 and loaded.daily_budget_usd == 12.5
    try:
        HunterConfig(daily_budget_usd=-1).save(tmp_path / "bad.json")
        assert False, "invalid config was accepted"
    except ValueError:
        pass


def test_quota_and_concurrency_limits(tmp_path):
    db = tmp_path / "fleet.db"
    cfg = HunterConfig(fix_jobs_per_day=2, max_concurrent_fixes=1)
    first = run_once(Client([issue("https://github.com/acme/tool/issues/10")]), cfg, db, now=100000)
    assert first and run_once(Client([issue("https://github.com/acme/tool/issues/11")]), cfg, db, now=100001) is None
    update_job(first["job_id"], "CLOSED", db_path=db)
    second = run_once(Client([issue("https://github.com/acme/tool/issues/11", number=2)]), cfg, db, now=100002)
    assert second and second["number"] == 2
    assert run_once(Client([issue("https://github.com/acme/tool/issues/12", number=3)]), cfg, db, now=100003) is None


def test_no_qualified_issue_is_recorded(tmp_path):
    db = tmp_path / "fleet.db"
    result = run_once(Client([issue(title="Unrelated request")]), HunterConfig(), db, now=100000)
    assert result["status"] == "NO_QUALIFIED_ISSUE"
    row = sqlite3.connect(db).execute("SELECT status FROM contribution_runs").fetchone()
    assert row[0] == "NO_QUALIFIED_ISSUE"
