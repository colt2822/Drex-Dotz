"""Business Gmail via the supported Gmail REST API and an isolated OAuth grant.

Account is pinned to colt@elyraon.com.co. The token lives only in
~/.config/dotz/gmail_token.json (0600); personal Gmail credentials are never read.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

import httpx

ACCOUNT = "colt@elyraon.com.co"
SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]
CONFIG_DIR = Path.home() / ".config" / "dotz"
CLIENT_FILE = CONFIG_DIR / "gmail_client.json"
TOKEN_FILE = CONFIG_DIR / "gmail_token.json"
REDIRECT_PORT = 8765
API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, body: str = ""):
        super().__init__(msg)
        self.status, self.body = status, body

    def kind(self) -> str:
        """throttle | restricted | auth | transient | other (never used to bypass anything; only to pause/stop)."""
        b = (self.body or str(self)).lower()
        if self.status == 429 or any(k in b for k in ("ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded", "sending limit", "quota")):
            return "throttle"
        if any(k in b for k in ("suspended", "accountdisabled", "account disabled", "account has been", "restricted", "abuse", "terms of service")):
            return "restricted"
        if self.status in (401,) or "invalid_grant" in b or "invalid credentials" in b:
            return "auth"
        if self.status and self.status >= 500:
            return "transient"
        return "other"


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def authorization_url() -> tuple[str, dict]:
    client = json.loads(CLIENT_FILE.read_text())
    state = secrets.token_hex(16)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": f"http://localhost:{REDIRECT_PORT}/callback",
        "response_type": "code", "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "login_hint": ACCOUNT, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return url, {"state": state, "verifier": verifier}


def run_consent_flow(url_file: Path | None = None, timeout: int = 900) -> str:
    """One-shot loopback OAuth. Prints/writes the URL; stores token only if the account matches."""
    client = json.loads(CLIENT_FILE.read_text())
    url, ctx = authorization_url()
    if url_file:
        url_file.write_text(url)
    result: dict[str, Any] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # no request logging (codes are secrets)
            pass

        def do_GET(self):
            q = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(q.query))
            if q.path == "/start":  # short link: avoids copy/paste mangling; pins the business Google session
                self.send_response(302); self.send_header("Location", url + "&authuser=2"); self.end_headers()
                return
            if q.path != "/callback":  # favicon etc.: ignore, keep waiting
                self.send_response(404); self.end_headers()
                return
            if params.get("state") != ctx["state"] or "code" not in params:
                # stale tab or error redirect: report, keep waiting for the real callback
                self.send_response(400); self.end_headers()
                self.wfile.write(b"Stale or invalid link. Open http://localhost:8765/start instead.")
                print("ignored callback:", params.get("error", "state mismatch/no code"), flush=True)
                return
            result["code"] = params["code"]
            self.send_response(200); self.end_headers(); self.wfile.write(b"Authorized. You can close this tab.")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    srv = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), H)
    srv.timeout = timeout
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    t.join(timeout)
    srv.server_close()
    if "code" not in result:
        raise GmailError(f"consent not completed: {result.get('error', 'timeout')}")
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": result["code"], "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": f"http://localhost:{REDIRECT_PORT}/callback", "grant_type": "authorization_code",
        "code_verifier": ctx["verifier"]}, timeout=30)
    tok = r.json()
    if "refresh_token" not in tok:
        raise GmailError(f"token exchange failed: {tok.get('error')}")
    granted = set(tok.get("scope", "").split())
    if not set(SCOPES) <= granted:
        raise GmailError(f"missing scopes: {set(SCOPES) - granted}")
    _write_private(TOKEN_FILE, {"refresh_token": tok["refresh_token"], "scope": tok["scope"], "created": time.time()})
    g = Gmail()
    ident = g.profile()["emailAddress"].casefold()
    if ident != ACCOUNT:
        TOKEN_FILE.unlink(missing_ok=True)
        raise GmailError("authorized account is not the business account; token discarded")
    return ident


class Gmail:
    """Thin Gmail client. `http` is injectable for fake-transport tests."""

    def __init__(self, http_client: Any = None):
        self._http = http_client or httpx.Client(timeout=30)
        self._access: tuple[str, float] | None = None

    # ---- auth -------------------------------------------------------------
    def _token(self) -> str:
        if self._access and self._access[1] > time.time() + 60:
            return self._access[0]
        if not TOKEN_FILE.exists():
            raise GmailError("business Gmail not authorized (no token)")
        client = json.loads(CLIENT_FILE.read_text())
        tok = json.loads(TOKEN_FILE.read_text())
        r = self._http.post("https://oauth2.googleapis.com/token", data={
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        j = r.json()
        if "access_token" not in j:
            raise GmailError(f"token refresh failed: {j.get('error')}")
        self._access = (j["access_token"], time.time() + int(j.get("expires_in", 3000)))
        return j["access_token"]

    def _req(self, method: str, path: str, **kw) -> dict:
        for attempt in range(3):
            r = self._http.request(method, API + path, headers={"Authorization": f"Bearer {self._token()}"}, **kw)
            if r.status_code in (500, 502, 503):  # never retry 429: throttling is surfaced and paused on
                time.sleep(2 ** attempt); continue
            if r.status_code >= 400:
                raise GmailError(f"gmail {method} {path.split('?')[0]} -> {r.status_code} {r.text[:200]}", r.status_code, r.text[:500])
            return r.json() if r.content else {}
        raise GmailError("gmail transient failure after retries")

    def profile(self) -> dict:
        return self._req("GET", "/profile")

    def assert_identity(self) -> str:
        who = self.profile()["emailAddress"].casefold()
        if who != ACCOUNT:
            raise GmailError(f"Gmail identity mismatch: {who}")
        return who

    # ---- sending ----------------------------------------------------------
    @staticmethod
    def build_raw(to: str, subject: str, body: str, send_id: str, unsubscribe_mailto: str = ACCOUNT) -> str:
        if any(c in to + subject for c in "\r\n"):
            raise ValueError("header injection")
        m = EmailMessage(policy=policy.SMTP)
        m["From"] = ACCOUNT
        m["To"] = to
        m["Subject"] = subject
        m["Message-ID"] = f"<{send_id}@elyraon.com.co>"
        m["X-Elyraon-Send-Id"] = send_id
        m["List-Unsubscribe"] = f"<mailto:{unsubscribe_mailto}?subject=unsubscribe>"
        m.set_content(body)
        return base64.urlsafe_b64encode(m.as_bytes()).decode().rstrip("=")

    def send(self, to: str, subject: str, body: str, send_id: str) -> dict:
        self.assert_identity()
        sent = self._req("POST", "/messages/send", json={"raw": self.build_raw(to, subject, body, send_id)})
        return {"provider_message_id": sent["id"], "thread_id": sent["threadId"], "sender": ACCOUNT}

    def find_sent(self, send_id: str, to: str) -> dict | None:
        """Reconcile a possibly-sent message (never resend on a hit). Gmail rewrites Message-ID, but keeps our
        X-Elyraon-Send-Id header, so list recent sent mail to that recipient and match the header."""
        q = f"in:sent to:{to} newer_than:2d"
        res = self._req("GET", "/messages?" + urllib.parse.urlencode({"q": q, "maxResults": 25}))
        for m in res.get("messages", []):
            meta = self._req("GET", f"/messages/{m['id']}?format=metadata&metadataHeaders=X-Elyraon-Send-Id")
            for h in meta.get("payload", {}).get("headers", []):
                if h["name"].lower() == "x-elyraon-send-id" and h["value"] == send_id:
                    return {"provider_message_id": m["id"], "thread_id": m["threadId"], "sender": ACCOUNT}
        return None

    # ---- reading ----------------------------------------------------------
    def list_ids(self, query: str, limit: int = 100) -> list[str]:
        out: list[str] = []
        page = None
        while len(out) < limit:
            p = {"q": query, "maxResults": min(100, limit - len(out))}
            if page:
                p["pageToken"] = page
            res = self._req("GET", "/messages?" + urllib.parse.urlencode(p))
            out += [m["id"] for m in res.get("messages", [])]
            page = res.get("nextPageToken")
            if not page:
                break
        return out

    def count_sent_since(self, epoch: float) -> int:
        """Exact count of ALL messages sent from the account (any client) since `epoch`."""
        return len(self.list_ids(f"in:sent after:{int(epoch)}", limit=20000))

    def get_parsed(self, mid: str) -> dict:
        raw = self._req("GET", f"/messages/{mid}?format=raw")
        b = raw["raw"]; b += "=" * (-len(b) % 4)
        msg = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(b))
        body_part = msg.get_body(preferencelist=("plain",))
        text = body_part.get_content() if body_part else ""
        return {"id": mid, "thread_id": raw.get("threadId"), "internal_ms": int(raw.get("internalDate", 0)),
                "from": [a.casefold() for _, a in getaddresses([str(msg.get("From", ""))]) if a],
                "to": [a.casefold() for _, a in getaddresses([str(msg.get("To", ""))]) if a],
                "subject": str(msg.get("Subject", "")), "text": text, "headers": msg,
                "labels": raw.get("labelIds", [])}


# ---- classification (pure functions; unit/fixture testable) ----------------
_OPT_OUT = re.compile(r"\b(unsubscribe|opt[- ]?out|remove me|stop (emailing|contacting|sending)|do not (contact|email)|"
                      r"don'?t (contact|email)|take me off|no more emails?)\b", re.I)
INTERNAL_SENDERS = {"colt2822@gmail.com", ACCOUNT.casefold()}  # owner canaries/tests are never prospect replies
_BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
_COMPLAINT = re.compile(r"\b(spam|report(ed)? (this|you)|abuse|harass|illegal|cease and desist|lawyer|attorney)\b", re.I)


def classify_inbound(m: dict) -> dict:
    """Return {kind: BOUNCE|OPT_OUT|COMPLAINT|AUTO|REPLY, recipient?, detail}."""
    sender = m["from"][0] if m["from"] else ""
    text = (m["subject"] + "\n" + m["text"])
    if sender.startswith(_BOUNCE_SENDERS):
        recips = re.findall(r"(?:Final-Recipient: *rfc822; *|wasn't delivered to |to )([\w.+-]+@[\w.-]+\.\w+)", text)
        status = re.search(r"\b([45]\.\d{1,3}\.\d{1,3})\b", text)
        permanent = re.search(r"(address not found|couldn'?t be found|does not exist|domain (name )?not found|no such user|unknown user|\b55\d\b)", text, re.I)
        hard = bool((status and status.group(1).startswith("5")) or (not status and permanent))
        reputation = bool(re.search(r"(\b5\.7\.\d+\b|sending limit|rate limit exceeded|blocked as spam|suspected spam|unusual rate|"
                                    r"poor reputation|policy rejection|has been blocked|listed (at|on) )", text, re.I))
        return {"kind": "BOUNCE", "recipient": (recips[0].casefold() if recips else None),
                "hard": hard, "reputation": reputation, "detail": status.group(1) if status else ("permanent-failure text" if permanent else "unknown")}
    system_sender = bool(re.match(r"(no-?reply|do-?not-?reply|notify|notifications?|payments-|updates@|mailer|bounce|newsletter|workspace-)", sender))
    auto = system_sender or str(m["headers"].get("Auto-Submitted", "")).lower() not in ("", "no") or \
        "out of office" in text.lower() or "automatic reply" in text.lower()
    if sender in INTERNAL_SENDERS:
        return {"kind": "INTERNAL_TEST", "recipient": sender, "detail": "owner/internal mail, not a prospect reply"}
    if _COMPLAINT.search(text):
        return {"kind": "COMPLAINT", "recipient": sender, "detail": "complaint language"}
    if _OPT_OUT.search(m["text"][:600] + " " + m["subject"]):
        return {"kind": "OPT_OUT", "recipient": sender, "detail": "opt-out language"}
    if auto:
        return {"kind": "AUTO", "recipient": sender, "detail": "auto-reply"}
    return {"kind": "REPLY", "recipient": sender, "detail": "human reply"}
