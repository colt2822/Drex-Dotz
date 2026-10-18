import json

from dotz.fleet.contributions import HunterConfig, eligible_issue, run_once, score_issue, update_job


class Client:
    def __init__(self, items):
        self.items = items

    def search_issues(self, query, limit):
        return self.items


def issue(url="https://github.com/acme/tool/issues/1", title="Fix reproducible MCP agent regression"):
    return {"html_url": url, "repository_url": "https://api.github.com/repos/acme/tool", "number": 1,
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
    import sqlite3
    row = sqlite3.connect(db).execute("SELECT status,evidence,draft FROM contribution_jobs").fetchone()
    assert row[0] == "DRAFT" and json.loads(row[1])["tests"]["passed"] and json.loads(row[2])["title"] == "Fix"
