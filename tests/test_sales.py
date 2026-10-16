"""Synthetic-recipient tests for the Sales Dot (fake Gmail transport; real CRM schema on a copy)."""
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from email.message import EmailMessage
from pathlib import Path

import pytest

from dotz.sales import worker as W
from dotz.sales.crm_store import CrmStore
from dotz.sales.gmail import ACCOUNT, GmailError, classify_inbound
from dotz.sales.policy import Policy

LIVE = Path.home() / "elyraon-revenue-hunter-20261015/data/crm.sqlite3"
pytestmark = pytest.mark.skipif(not LIVE.exists(), reason="needs the existing CRM as a schema/data source")


class FakeGmail:
    def __init__(self):
        self.sent, self.inbox, self.identity = [], [], ACCOUNT
        self.fail_send = False
        self.external = 0          # non-Sales-Dot sends from the same account (transactional, owner mail)

    def assert_identity(self):
        if self.identity != ACCOUNT:
            raise GmailError(f"Gmail identity mismatch: {self.identity}")

    def send(self, to, subject, body, send_id):
        if self.fail_send:
            raise self.fail_send
        self.sent.append({"to": to, "subject": subject, "body": body, "send_id": send_id})
        return {"provider_message_id": f"m{len(self.sent)}", "thread_id": f"t{len(self.sent)}", "sender": ACCOUNT}

    def count_sent_since(self, epoch):
        return len(self.sent) + self.external

    def find_sent(self, send_id, to):
        for i, m in enumerate(self.sent):
            if m["send_id"] == send_id:
                return {"provider_message_id": f"m{i+1}", "thread_id": f"t{i+1}", "sender": ACCOUNT}

    def list_ids(self, q, limit=100):
        return [m["id"] for m in self.inbox]

    def get_parsed(self, mid):
        return next(m for m in self.inbox if m["id"] == mid)


def inbound(mid, frm, subject, text, thread="x", auto=""):
    msg = EmailMessage()
    if auto:
        msg["Auto-Submitted"] = auto
    return {"id": mid, "thread_id": thread, "internal_ms": 0, "from": [frm], "to": [ACCOUNT], "subject": subject, "text": text, "headers": msg, "labels": []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "crm.sqlite3"
    shutil.copy(LIVE, db)
    _c = sqlite3.connect(db); _c.execute("UPDATE prospects SET suppressed=1 WHERE status IN ('MESSAGE_DRAFTED','HUMAN_APPROVED')"); _c.commit(); _c.close()  # live queue must not leak into send-count assertions
    monkeypatch.setattr(W, "STATE_DIR", tmp_path)
    monkeypatch.setattr(W, "STOP_FILE", tmp_path / "STOP")
    monkeypatch.setattr(W, "ALERTS", tmp_path / "alerts.jsonl")
    s = CrmStore(db)
    s.backfill_contacts(); s.backfill_history_sends(); s.import_historic_suppressions()
    pol = Policy(approval_file=tmp_path / "approval.json")
    g = FakeGmail()
    w = W.SalesWorker(s, pol, g, None, now=lambda: 1792508400.0, mx_check=lambda d: True)  # Tuesday 10:00 America/Chicago
    pol.approve(["public_business_role_address"], "test-owner")
    return w, s, g, pol, tmp_path


def synth(s, w, n, email=None, free=False, fit=90):
    pid = f"synthetic-{n}"
    email = email or f"person{n}@synthetic{n}.example"
    s.add_prospect(pid=pid, name="S", company=f"Synthetic {n}", website=f"https://synthetic{n}.example", email=email,
                   source_url=f"https://synthetic{n}.example/contact", signal="Share your project details and we follow up", proposal="p", fit=fit)
    body = f"Hello Synthetic {n} team,\n\nShort synthetic note.\n\nELYRAON services: https://elyraon.com.co/services/\n\n{w.footer()}"
    s.set_stage(pid, "QUALIFIED", "synthetic")
    s.save_draft(pid, "INITIAL_MESSAGE", f"Synthetic subject {n}", body)
    s.event(pid, "DOT_DRAFTED", '{"body_sha": "%s"}' % W.sha(body))
    return pid


def test_window_and_unapproved_policy_blocks(env):
    w, s, g, pol, tmp = env
    pol.revoke()
    synth(s, w, 1)
    assert "disabled" in w.tick()["sending"]
    assert g.sent == []


def test_send_dedupe_suppression_and_legacy_guard(env):
    w, s, g, pol, tmp = env
    p1 = synth(s, w, 1)
    r = w.send_one(p1, pol.approval())
    assert r["sent"] and len(g.sent) == 1
    assert w.send_one(p1, pol.approval())["sent"] is False               # same address: dedupe
    p2 = synth(s, w, 2, email="other@synthetic1.example")                # same domain within 90d
    assert "domain_contacted_90d" in w.send_one(p2, pol.approval())["reason"]
    p3 = synth(s, w, 3); s.add_suppression("person3@synthetic3.example", "test", "test")
    assert w.send_one(p3, pol.approval())["reason"] == "suppressed"
    bounced = s.c.execute("SELECT email FROM sales_suppressions WHERE source='crm.history'").fetchone()[0]
    assert s.is_suppressed(bounced)                                       # historical hard bounce stays blocked
    # pre-existing unapproved drafts are never auto-sent
    legacy = s.c.execute("SELECT p.id FROM prospects p WHERE p.status='MESSAGE_DRAFTED' AND p.id NOT LIKE 'synthetic-%' LIMIT 1").fetchone()
    if legacy:
        assert w.candidate_problems(legacy[0], pol.approval())
    assert len(g.sent) == 1


def test_content_gate_blocks_claims_and_missing_footer(env):
    w, s, g, pol, tmp = env
    p = synth(s, w, 4)
    s.c.execute("UPDATE drafts SET body=body||' We guarantee 40% savings.' WHERE prospect_id=?", (p,)); s.c.commit()
    assert w.send_one(p, pol.approval())["sent"] is False and not g.sent


def clocked(w):
    t = [1792508400.0]
    w.now = lambda: t[0]
    w.sleep = lambda x: t.__setitem__(0, t[0] + x)
    return t


def test_rolling_cap_counts_non_sales_activity_and_reserve(env):
    w, s, g, pol, tmp = env
    pol.cfg["volume"].update(account_rolling_24h_cap=2000, google_account_limit=30, reserve_for_transactional_and_manual=5)
    assert pol.effective_cap() == 30                                      # never above Google's actual limit
    g.external = 10                                                       # transactional/owner mail already sent today
    t = clocked(w)
    for i in range(40):
        synth(s, w, 100 + i)
    for _ in range(300):
        t[0] += 60
        if t[0] > 1792508400.0 + 5.5 * 3600:
            break
        w.tick()
    assert len(g.sent) == 15                                              # 30 - 5 reserve - 10 external
    assert g.count_sent_since(0) == 25 and "quota" in w.tick()["sending"]["status"]


def test_paced_not_burst(env):
    w, s, g, pol, tmp = env
    pol.cfg["volume"].update(google_account_limit=2000, reserve_for_transactional_and_manual=0)
    t = clocked(w)
    for i in range(30):
        synth(s, w, 200 + i)
    times = []
    orig = g.send
    g.send = lambda *a, **k: (times.append(t[0]), orig(*a, **k))[1]
    for _ in range(30):
        t[0] += 60
        w.tick()
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) > 5 and min(gaps) >= pol.cfg["volume"]["pacing"]["min_spacing_seconds"]


def test_throttle_pauses_and_identity_mismatch_stops(env):
    w, s, g, pol, tmp = env
    p = synth(s, w, 300)
    g.fail_send = GmailError("rate", 429, "rateLimitExceeded")
    r = w.tick()["sending"]
    assert r["status"] == "gmail_refused_throttle" and w.paused_until() > w.now()
    assert not s.open_intents()                                           # definitive refusal frees the intent
    assert "paused" in w.tick()["sending"]["status"]
    g.fail_send = False
    g.identity = "colt2822@gmail.com"
    w.pause(0, "x")
    w.s.set_state("pause_until", "0")
    assert "mismatch" in w.tick()["stopped"] and w.stopped()


def test_restricted_account_signal_stops(env):
    w, s, g, pol, tmp = env
    synth(s, w, 310)
    g.fail_send = GmailError("x", 403, "Account has been suspended")
    w.tick()
    assert w.stopped()


def test_mx_verification_blocks_send(env):
    w, s, g, pol, tmp = env
    w.mx_check = lambda d: False
    p = synth(s, w, 320)
    assert "recipient_domain_failed_mx_verification" in w.send_one(p, pol.approval())["reason"] and not g.sent


def test_emergency_stop_blocks_send(env):
    w, s, g, pol, tmp = env
    clocked(w)
    p = synth(s, w, 20)
    w.stop("test")
    assert "stopped" in w.tick() and not g.sent
    w.resume()
    assert w.tick()["sending"]["sent"] == 1 and len(g.sent) == 1


def test_reply_sync_bounce_optout_complaint(env):
    w, s, g, pol, tmp = env
    pa, pb, pc = synth(s, w, 30), synth(s, w, 31), synth(s, w, 32)
    for p in (pa, pb, pc):
        s.c.execute("UPDATE sales_send_intents SET status='SENT' WHERE prospect_id=?", (p,))
    dsn = EmailMessage()
    g.inbox = [
        inbound("b1", "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)", "Final-Recipient: rfc822; person30@synthetic30.example\nStatus: 5.1.1"),
        inbound("o1", "person31@synthetic31.example", "Re: hello", "Please unsubscribe me"),
        inbound("r1", "person32@synthetic32.example", "Re: hello", "Interesting, tell me more."),
        inbound("a1", "noreply@synthetic32.example", "Automatic reply", "I am out of office", auto="auto-replied"),
    ]
    st = w.sync_replies()
    assert st["bounce"] == 1 and st["opt_out"] == 1 and st["reply"] == 1 and st["auto"] == 1
    assert s.is_suppressed("person30@synthetic30.example") and s.is_suppressed("person31@synthetic31.example")
    assert s.is_suppressed("anyone@synthetic31.example")                  # opt-out suppresses the domain
    assert s.prospect(pc)["status"] == "REPLIED"
    assert s.c.execute("SELECT count(*) FROM events WHERE prospect_id=? AND event_type='HARD_BOUNCE'", (pa,)).fetchone()[0] == 1
    assert w.sync_replies()["new"] == 0                                   # idempotent
    g.inbox = [inbound("c1", "person32@synthetic32.example", "Re: x", "This is spam, I will report you")]
    w.sync_replies()
    assert w.stopped()


def test_bounce_rate_stop_rule(env):
    w, s, g, pol, tmp = env
    p = synth(s, w, 40)
    last = s.c.execute("SELECT prospect_id FROM sales_send_intents WHERE status='SENT' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    s.event(last, "HARD_BOUNCE", "synthetic bounce in rolling window")
    assert "bounce rate" in w.stop_rule_violation()
    assert "stopped" in w.tick() and not g.sent


def test_crash_between_gmail_and_crm_record_never_double_sends(env, tmp_path):
    w, s, g, pol, tmp = env
    p = synth(s, w, 50)

    class Crash(Exception):
        pass

    def boom(point):
        if point == "after_gmail_before_record":
            raise Crash()
    w._crash_hook = boom
    with pytest.raises(Crash):
        w.send_one(p, pol.approval())
    assert len(g.sent) == 1 and s.open_intents()                         # Gmail got it, CRM did not record it
    w2 = W.SalesWorker(CrmStore(s.path), pol, g, None, now=w.now, mx_check=lambda d: True)       # "restart"
    out = w2.recover_intents()
    assert out["reconciled"] == 1 and not w2.s.open_intents()
    assert w2.s.prospect(p)["status"] == "SENT"
    assert w2.send_one(p, pol.approval())["sent"] is False and len(g.sent) == 1


def test_real_process_kill_recovery(tmp_path):
    """SIGKILL-equivalent: child os._exit()s between the Gmail send and the CRM write; fresh process reconciles."""
    db = tmp_path / "crm.sqlite3"
    shutil.copy(LIVE, db)
    child = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, "{Path(__file__).parent.parent}")
        from pathlib import Path
        from dotz.sales import worker as W
        from dotz.sales.crm_store import CrmStore
        from dotz.sales.policy import Policy
        import json
        W.STATE_DIR = Path("{tmp_path}"); W.STOP_FILE = W.STATE_DIR/"STOP"; W.ALERTS = W.STATE_DIR/"a.jsonl"
        s = CrmStore(Path("{db}")); s.backfill_contacts(); s.backfill_history_sends()
        pol = Policy(approval_file=Path("{tmp_path}/ap.json")); pol.approve(["public_business_role_address"], "t")
        class G:
            def assert_identity(self): pass
            def send(self, to, subject, body, send_id):
                open("{tmp_path}/gmail_sent.json", "w").write(json.dumps({{"send_id": send_id}}))
                return {{"provider_message_id": "m1", "thread_id": "t1", "sender": "colt@elyraon.com.co"}}
        w = W.SalesWorker(s, pol, G(), None, now=lambda: 1792508400.0, mx_check=lambda d: True)
        pid = "synthetic-kill"
        s.add_prospect(pid=pid, name="S", company="Kill", website="https://kill.example", email="a@kill.example", source_url="https://kill.example/c", signal="x", proposal="p", fit=90)
        body = "Hello Kill team,\\n\\nHi.\\n\\nELYRAON services: https://elyraon.com.co/services/\\n\\n" + w.footer()
        s.set_stage(pid, "QUALIFIED", "x"); s.save_draft(pid, "INITIAL_MESSAGE", "Hi", body); s.event(pid, "DOT_DRAFTED", json.dumps({{"body_sha": W.sha(body)}}))
        w._crash_hook = lambda point: os._exit(9) if point == "after_gmail_before_record" else None
        w.send_one(pid, pol.approval())
    """)
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True)
    assert r.returncode == 9, r.stderr
    import json
    sent_id = json.loads((tmp_path / "gmail_sent.json").read_text())["send_id"]
    s = CrmStore(db)
    assert [x["send_id"] for x in s.open_intents()] == [sent_id]          # intent survived the kill
    class G2:
        def find_sent(self, sid, to): return {"provider_message_id": "m1", "thread_id": "t1", "sender": ACCOUNT} if sid == sent_id else None
    w = W.SalesWorker(s, Policy(approval_file=tmp_path / "ap.json"), G2(), None, now=lambda: 1792508400.0, mx_check=lambda d: True)
    assert w.recover_intents()["reconciled"] == 1 and s.prospect("synthetic-kill")["status"] == "SENT"


def test_classifier_edge_cases():
    m = inbound("x", "a@b.example", "hi", "Thanks, not interested.")
    assert classify_inbound(m)["kind"] == "REPLY"
    m = inbound("x", "a@b.example", "STOP", "stop emailing us")
    assert classify_inbound(m)["kind"] == "OPT_OUT"


# ---- v1.1: provider ceiling, bounce classification, reconcile ------------------------------------
def test_live_policy_provider_ceiling_500_keeps_owner_target():
    p = Policy()
    v = p.cfg["volume"]
    assert v["account_rolling_24h_cap"] == 1800                           # owner target retained for later
    assert v["google_account_limit"] == 500 and p.effective_cap() == 500  # billing activation does not raise the ceiling
    assert p.sales_budget(0) == 300 and p.sales_budget(62) == 238         # 500 - 200 reserve - account sends
    assert p.sales_budget(400) == 0


def test_nxdomain_dsn_is_permanent_and_notifications_are_not_replies():
    dsn = inbound("d1", "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
                  "** Address not found **\nYour message wasn't delivered to partnerships@aenfinite.us because the domain aenfinite.us couldn't be found.\n"
                  "DNS Error: DNS type 'mx' lookup of aenfinite.us responded with code NXDOMAIN\nDomain name not found: aenfinite.us")
    k = classify_inbound(dsn)
    assert k["kind"] == "BOUNCE" and k["hard"] and k["recipient"] == "partnerships@aenfinite.us" and not k["reputation"]
    for sender in ("workspace-noreply@google.com", "payments-noreply@google.com", "updates@email.grok.com", "no-reply@accounts.google.com"):
        assert classify_inbound(inbound("n", sender, "Welcome", "hello"))["kind"] == "AUTO"
    assert classify_inbound(inbound("t", "colt2822@gmail.com", "ELYRAON Business Email — Delivery Test", "test"))["kind"] == "INTERNAL_TEST"
    assert classify_inbound(inbound("r", "owner@acmeroofing.com", "Re: Estimates", "Sounds interesting, call me"))["kind"] == "REPLY"


def test_reconcile_fixes_kinds_and_links_threads_without_inventing_replies(env):
    from dotz.sales import reconcile
    w, s, g, pol, tmp = env
    s.c.execute("INSERT INTO sales_inbound VALUES('x1','REPLY',NULL,'human reply','2026-10-16T00:00:00')")
    s.c.execute("INSERT INTO sales_inbound VALUES('x2','BOUNCE_SOFT','p1','unknown','2026-10-16T00:00:00')")
    s.c.commit()
    plan = {"thread_links": [], "inbound_changes": [
        {"id": "x1", "old": "REPLY", "new": "AUTO", "detail": "auto-reply", "recipient": None},
        {"id": "x2", "old": "BOUNCE_SOFT", "new": "BOUNCE_HARD", "detail": "5.1.2", "recipient": "gone@nxdomain-example.us"}]}
    assert reconcile.apply(s, plan)["inbound_reclassified"] == 2
    assert reconcile.apply(s, {"thread_links": [], "inbound_changes": []})["inbound_reclassified"] == 0
    assert s.c.execute("SELECT kind FROM sales_inbound WHERE provider_message_id='x1'").fetchone()[0] == "AUTO"
    assert s.c.execute("SELECT kind FROM sales_inbound WHERE provider_message_id='x2'").fetchone()[0] == "BOUNCE_HARD"
    assert s.is_suppressed("gone@nxdomain-example.us")
    assert s.c.execute("SELECT count(*) FROM sales_inbound_audit WHERE provider_message_id IN ('x1','x2')").fetchone()[0] == 2           # originals preserved
    assert reconcile._norm("Story.Remodeling+x@gmail.com") == reconcile._norm("storyremodeling@gmail.com")
