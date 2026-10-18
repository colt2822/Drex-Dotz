import http.client
import json
import threading

from http.server import ThreadingHTTPServer

from dotz.fleet import dashboard
from dotz.fleet.contributions import HunterConfig, _db


def server(tmp_path, monkeypatch):
    db = tmp_path / "fleet.db"
    monkeypatch.setattr(dashboard, "DB_PATH", db)
    monkeypatch.setenv("FETT_HUNTER_CONFIG", str(tmp_path / "hunter.json"))
    c = _db(db)
    c.execute("INSERT INTO contribution_candidates VALUES(?,?,?,?,?,?,?,?)",
              ("https://github.com/a/r/issues/1", "a/r", 1, "Fix", "{}", 90, "SELECTED", 100))
    c.execute("INSERT INTO contribution_jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("FETT-1", "https://github.com/a/r/issues/1", "a/r", 1, "DRAFT", 1, 100, "{}", 100, 100))
    c.close()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(srv, method, path, body=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{srv.server_port}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body)
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    return r.status, json.loads(r.read())


def test_hunter_api_shape_and_config_validation(tmp_path, monkeypatch):
    srv = server(tmp_path, monkeypatch)
    try:
        status, data = call(srv, "GET", "/api/hunter")
        assert status == 200
        assert {"config", "quota", "concurrency", "discovered", "eligible", "active_jobs",
                "drafts_awaiting_approval", "counts_by_status", "failures"} <= set(data)
        assert data["discovered"] == 1 and data["eligible"] == 1
        assert data["drafts_awaiting_approval"] == 1
        status, data = call(srv, "POST", "/api/hunter/config", {"fix_jobs_per_day": 3, "max_concurrent_fixes": 2})
        assert status == 200 and data["config"]["fix_jobs_per_day"] == 3
        status, _ = call(srv, "POST", "/api/hunter/config", {"fix_jobs_per_day": -1})
        assert status == 400
        status, _ = call(srv, "POST", "/api/hunter/config", {"unknown": 1})
        assert status == 400
    finally:
        srv.shutdown()


def test_hunter_refuses_external_submission(tmp_path, monkeypatch):
    srv = server(tmp_path, monkeypatch)
    try:
        status, data = call(srv, "POST", "/api/hunter/config", {"auto_external_submission": True})
        assert status == 400
        assert "cannot be enabled" in data["error"]
        assert HunterConfig.load().auto_external_submission is False
    finally:
        srv.shutdown()
