"""One-shot, idempotent reconciliation of historic CRM rows against Gmail truth. Gmail messages are never modified.

- sales_send_intents: fill thread_id for sends whose Gmail message id and recipient match a message in Gmail Sent.
- sales_inbound: re-run today's classifier over every stored inbound message; fix wrong kinds (e.g. system notifications
  or owner tests stored as REPLY, a permanent NXDOMAIN bounce stored as BOUNCE_SOFT). Original rows are preserved in
  sales_inbound_audit before any change. Replies are never invented: a REPLY needs a human sender.
"""
from __future__ import annotations

import json
import time

from dotz.sales.gmail import Gmail, GmailError, classify_inbound


def _get(gmail, mid: str, tries: int = 6) -> dict:
    """get_parsed with polite pacing; on Google's per-minute quota response wait it out (never retry-storm)."""
    for i in range(tries):
        try:
            time.sleep(0.25)
            return gmail.get_parsed(mid)
        except GmailError as e:
            if e.kind() != "throttle" and "Quota exceeded" not in str(e) or i == tries - 1:
                raise
            time.sleep(30 * (i + 1))
    raise RuntimeError("unreachable")


def _norm(addr: str) -> str:
    """Gmail ignores dots in the local part; CRM and Gmail headers can differ only by dots."""
    local, _, dom = addr.casefold().partition("@")
    return (local.replace(".", "").split("+")[0] if dom in ("gmail.com", "googlemail.com") else local) + "@" + dom


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def plan(store, gmail) -> dict:
    sent_ids = gmail.list_ids("in:sent newer_than:30d", limit=5000)
    gm_sent = {}
    for mid in sent_ids:
        m = _get(gmail, mid)
        gm_sent[mid] = {"thread": m["thread_id"], "to": m["to"], "ts": m["internal_ms"] // 1000, "subject": m["subject"]}
    threads, unmatched_sends = [], []
    for r in store.c.execute("SELECT send_id, recipient, provider_message_id, thread_id FROM sales_send_intents WHERE status='SENT'").fetchall():
        g = gm_sent.get(r["provider_message_id"])
        if g and _norm(r["recipient"]) in {_norm(a) for a in g["to"]}:
            if r["thread_id"] != g["thread"]:
                threads.append((r["send_id"], g["thread"]))
        else:
            unmatched_sends.append(r["send_id"])
    inbound, changes = gmail.list_ids("in:anywhere -in:sent -in:drafts newer_than:30d", limit=2000), []
    for mid in inbound:
        old = store.c.execute("SELECT kind, prospect_id, detail FROM sales_inbound WHERE provider_message_id=?", (mid,)).fetchone()
        k = classify_inbound(_get(gmail, mid))
        new_kind = "BOUNCE_HARD" if k["kind"] == "BOUNCE" and k.get("hard") else ("BOUNCE_SOFT" if k["kind"] == "BOUNCE" else k["kind"])
        if k["kind"] == "BOUNCE" and k.get("reputation"):
            continue  # leave provider-restriction DSNs to the live sync path (it also pauses sending)
        if old and old["kind"] != new_kind:
            changes.append({"id": mid, "old": old["kind"], "new": new_kind, "detail": k["detail"], "recipient": k.get("recipient")})
    return {"thread_links": threads, "sends_not_found_in_gmail": unmatched_sends, "inbound_changes": changes, "gmail_sent_seen": len(gm_sent)}


def apply(store, p: dict) -> dict:
    store.c.execute("CREATE TABLE IF NOT EXISTS sales_inbound_audit(provider_message_id TEXT, old_kind TEXT, old_prospect_id TEXT, old_detail TEXT, new_kind TEXT, reason TEXT, at TEXT)")
    store.c.commit()
    with store.txn():
        for send_id, thread in p["thread_links"]:
            store.c.execute("UPDATE sales_send_intents SET thread_id=? WHERE send_id=? AND thread_id IS NULL", (thread, send_id))
        for ch in p["inbound_changes"]:
            old = store.c.execute("SELECT kind, prospect_id, detail FROM sales_inbound WHERE provider_message_id=?", (ch["id"],)).fetchone()
            store.c.execute("INSERT INTO sales_inbound_audit VALUES(?,?,?,?,?,?,?)", (ch["id"], old["kind"], old["prospect_id"], old["detail"], ch["new"], "reconcile", _iso()))
            store.c.execute("UPDATE sales_inbound SET kind=?, detail=? WHERE provider_message_id=?", (ch["new"], ch["detail"], ch["id"]))
    for ch in p["inbound_changes"]:
        if ch["new"] == "BOUNCE_HARD" and ch.get("recipient"):
            store.add_suppression(ch["recipient"], f"hard bounce {ch['detail']}", "reconcile")
    return {"threads_linked": len(p["thread_links"]), "inbound_reclassified": len(p["inbound_changes"])}


def run(execute: bool) -> dict:
    from dotz.sales.crm_store import CrmStore
    from pathlib import Path
    store = CrmStore()
    p = plan(store, Gmail())
    out = {"plan": {k: (v if k != "thread_links" else len(v)) for k, v in p.items()}}
    if execute:
        out["backup"] = str(store.backup(Path.home() / ".local" / "state" / "dotz" / "backups"))
        out["applied"] = apply(store, p)
    return out
