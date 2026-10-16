"""Operator CLI:  python -m dotz.sales <command>"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dotz.sales import worker as W
from dotz.sales.crm_store import CrmStore
from dotz.sales.gmail import ACCOUNT, Gmail, run_consent_flow
from dotz.sales.luna import Luna
from dotz.sales.policy import CATEGORIES, Policy

BACKUPS = Path.home() / ".local" / "state" / "dotz" / "backups"


def make_worker(with_luna: bool = True) -> W.SalesWorker:
    return W.SalesWorker(CrmStore(), Policy(), Gmail(), Luna() if with_luna else None)


def cmd_init(a):
    s = CrmStore()
    before = s.original_counts()
    b = s.backup(BACKUPS)
    out = {"backup": str(b), "contacts_added": s.backfill_contacts(), "history_sends_mirrored": s.backfill_history_sends(),
           "suppressions_imported": s.import_historic_suppressions(), "counts_before": before, "counts_after": s.original_counts()}
    print(json.dumps(out, indent=1))


def cmd_auth(a):
    print("AUTHORIZED", run_consent_flow(Path(a.url_file) if a.url_file else None))


def cmd_status(a):
    w = make_worker(False)
    ap = w.p.approval()
    s = w.s
    print(json.dumps({
        "policy_hash": w.p.hash[:12], "policy_approved": bool(ap), "categories": ap and ap["categories"],
        "emergency_stop": w.stopped(), "heartbeat_age_s": (time.time() - float(s.get_state("heartbeat", "0"))) if s.get_state("heartbeat") else None,
        "last_reply_sync_age_s": (time.time() - float(s.get_state("last_reply_sync", "0"))) if s.get_state("last_reply_sync") else None,
        "effective": {"owner_cap_24h": w.p.cfg["volume"]["account_rolling_24h_cap"], "google_limit": w.p.cfg["volume"]["google_account_limit"],
                      "effective_cap": w.p.effective_cap(), "transactional_reserve": w.p.cfg["volume"]["reserve_for_transactional_and_manual"],
                      "sales_budget_if_idle": w.p.sales_budget(0), "window": "Mon-Fri 09:00-16:00 America/Chicago", "paused_until": w.paused_until() or None,
                      "pause_reason": s.get_state("pause_reason")},
        "sales_sent_24h": w.sales_sent_24h(),
        "queued_eligible": sum(1 for r in s.c.execute("SELECT p.id FROM prospects p JOIN drafts d ON d.prospect_id=p.id AND d.kind='INITIAL_MESSAGE' WHERE p.suppressed=0 AND p.status IN ('MESSAGE_DRAFTED','HUMAN_APPROVED')") if ap and not w.candidate_problems(r[0], ap)),
        "queued_drafts_total": s.c.execute("SELECT count(*) FROM prospects p JOIN drafts d ON d.prospect_id=p.id AND d.kind='INITIAL_MESSAGE' WHERE p.suppressed=0 AND p.status='MESSAGE_DRAFTED'").fetchone()[0],
        "sends_total": s.c.execute("SELECT count(*) FROM sales_send_intents WHERE status='SENT'").fetchone()[0],
        "open_intents": len(s.open_intents()), "suppressed_addresses": s.c.execute("SELECT count(*) FROM sales_suppressions").fetchone()[0],
        "crm_counts": s.original_counts(), "last_tick": json.loads(s.get_state("last_tick", "null") or "null")}, indent=1, default=str))


def cmd_approve(a):
    p = Policy()
    print(f"Policy {p.cfg['id']} hash {p.hash[:12]}\nCategories to enable: {a.categories}")
    if a.yes != p.hash[:12]:
        print(f"Re-run with --yes {p.hash[:12]} to confirm you reviewed this exact policy file."); sys.exit(2)
    print(json.dumps(p.approve(a.categories.split(","), a.approver)))


def cmd_stop(a):
    make_worker(False).stop(a.reason or "operator emergency stop"); print("STOPPED")


def cmd_resume(a):
    make_worker(False).resume(); print("resumed (sending still requires policy approval)")


def cmd_tick(a):
    print(json.dumps(make_worker().tick(), indent=1, default=str))


def cmd_run(a):
    W.run_loop(make_worker, a.interval)


def cmd_reconcile(a):
    from dotz.sales import reconcile
    print(json.dumps(reconcile.run(a.execute), indent=1, default=str))


def cmd_canary(a):
    """One internal canary to the business mailbox itself. Requires the explicit flag."""
    if a.authorize != "SEND-ONE-CANARY-TO-COLT@ELYRAON.COM.CO":
        print("refused: pass --authorize SEND-ONE-CANARY-TO-COLT@ELYRAON.COM.CO"); sys.exit(2)
    w = make_worker(False)
    if w.stopped():
        print("refused: emergency stop active"); sys.exit(3)
    g = w.g
    g.assert_identity()
    sid = f"canary-{int(time.time())}"
    body = f"Internal Drex Sales Dot canary {sid}. No action needed.\n\n" + w.footer()
    res = g.send(ACCOUNT, f"[canary] Drex Sales Dot {sid}", body, sid)
    back = g.get_parsed(res["provider_message_id"])
    ok = back["from"] == [ACCOUNT] and back["to"] == [ACCOUNT] and sid in back["subject"]
    w.s.set_state("canary_" + sid, json.dumps({**res, "verified": ok}))
    print(json.dumps({**res, "readback_verified": ok}))


def cmd_prove_crm(a):
    """Real round-trip on the live CRM with a tagged synthetic prospect; removes only what it created."""
    s = CrmStore()
    b = s.backup(BACKUPS)
    before_counts, before_fp = s.original_counts(), s.original_fingerprint()
    pid = f"synthetic-roundtrip-{int(time.time())}"
    assert s.add_prospect(pid=pid, name="SYNTHETIC", company="SYNTHETIC ROUNDTRIP", website=f"https://{pid}.invalid", email=f"{pid}@example.invalid",
                          source_url=f"https://{pid}.invalid/", signal="synthetic", proposal="synthetic", fit=1)
    assert not s.add_prospect(pid=pid + "-dup", name="x", company="x", website="https://other.invalid", email=f"{pid}@example.invalid",
                              source_url="https://other.invalid/", signal="x", proposal="x", fit=1), "dedupe failed"
    s.set_stage(pid, "QUALIFIED", "synthetic"); s.save_draft(pid, "INITIAL_MESSAGE", "s", "b")
    s.suppress_prospect(pid, "synthetic roundtrip")
    read = dict(s.prospect(pid))
    mid = s.original_counts()
    # cleanup: only rows we created
    with s.txn() as c:
        for t in ("events", "replies", "drafts", "followups"):
            c.execute(f"DELETE FROM {t} WHERE prospect_id=?", (pid,))
        c.execute("DELETE FROM sales_contacts WHERE prospect_id=?", (pid,))
        c.execute("DELETE FROM prospects WHERE id=?", (pid,))
    after_counts, after_fp = s.original_counts(), s.original_fingerprint()
    print(json.dumps({"backup": str(b), "counts_before": before_counts, "counts_during": mid, "counts_after": after_counts,
                      "read_back_status": read["status"], "read_back_suppressed": read["suppressed"],
                      "history_preserved": before_counts == after_counts and before_fp == after_fp}, indent=1))


def main():
    ap = argparse.ArgumentParser(prog="dotz.sales")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init").set_defaults(f=cmd_init)
    x = sp.add_parser("auth"); x.add_argument("--url-file"); x.set_defaults(f=cmd_auth)
    sp.add_parser("status").set_defaults(f=cmd_status)
    x = sp.add_parser("approve-policy"); x.add_argument("--categories", required=True, help=",".join(CATEGORIES))
    x.add_argument("--approver", default="owner"); x.add_argument("--yes", default=""); x.set_defaults(f=cmd_approve)
    x = sp.add_parser("stop"); x.add_argument("--reason"); x.set_defaults(f=cmd_stop)
    sp.add_parser("resume").set_defaults(f=cmd_resume)
    sp.add_parser("tick").set_defaults(f=cmd_tick)
    x = sp.add_parser("run"); x.add_argument("--interval", type=int, default=60); x.set_defaults(f=cmd_run)
    x = sp.add_parser("reconcile"); x.add_argument("--execute", action="store_true", help="apply (default is a dry-run plan)"); x.set_defaults(f=cmd_reconcile)
    x = sp.add_parser("canary"); x.add_argument("--authorize", default=""); x.set_defaults(f=cmd_canary)
    sp.add_parser("prove-crm").set_defaults(f=cmd_prove_crm)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
