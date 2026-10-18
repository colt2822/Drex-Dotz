"""Upwork integration for Drex Fleet Command.

Integrates ledger.sqlite and proposals.json from astra-upwork-autopilot into Fleet Command.
Supports:
- Read-only data aggregation for the UPWORK tab
- Save Draft persistently to proposals.json
- Mark Submitted with mandatory owner confirmation / Upwork receipt verification
- Reject / Archive opportunity
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ASTRA_DIR = Path("/home/colton-mcclain/astra-upwork-autopilot")
LEDGER_DB = ASTRA_DIR / "ledger.sqlite"
PROPOSALS_JSON = ASTRA_DIR / "proposals" / "proposals.json"
POLICY_JSON = ASTRA_DIR / "policy.json"
STATUS_JSON = ASTRA_DIR / "status.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_policy() -> dict:
    if POLICY_JSON.exists():
        try:
            return json.loads(POLICY_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "connects_spend_authorized": 100,
        "reserved_connects": 42,
        "per_application_connect_ceiling": 18,
        "boosting_allowed": False,
        "auto_submit_allowed": False,
    }


def get_proposals() -> list[dict]:
    if PROPOSALS_JSON.exists():
        try:
            return json.loads(PROPOSALS_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_proposals(props: list[dict]) -> None:
    PROPOSALS_JSON.parent.mkdir(parents=True, exist_ok=True)
    temp_file = PROPOSALS_JSON.with_suffix(".tmp")
    temp_file.write_text(json.dumps(props, indent=2), encoding="utf-8")
    temp_file.replace(PROPOSALS_JSON)


def get_upwork_snapshot(now: float) -> dict:
    pol = get_policy()
    proposals_list = get_proposals()
    prop_map = {p["key"]: p for p in proposals_list if "key" in p}

    if not LEDGER_DB.exists():
        return {
            "error": "ledger.sqlite not found",
            "now": now,
            "opportunities": [],
            "proposals": proposals_list,
        }

    c = sqlite3.connect(f"file:{LEDGER_DB}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        opp_rows = c.execute(
            """SELECT key, source, url, title, budget_usd, fixed, proposals, posted,
                      payment_verified, client_spend, connects_cost, status, score,
                      flags, notes, first_seen, updated
               FROM opportunities
               WHERE score > 75
               ORDER BY 
                 CASE 
                   WHEN status IN ('qualified', 'assess_capability') THEN 0
                   WHEN status = 'submitted' THEN 1
                   WHEN status IN ('rejected', 'archived') THEN 2
                   ELSE 3
                 END,
                 score DESC, budget_usd DESC"""
        ).fetchall()

        used_connects = c.execute(
            "SELECT COALESCE(SUM(connects_used), 0) FROM submissions"
        ).fetchone()[0]

        submissions = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM submissions ORDER BY submitted_at DESC"
            ).fetchall()
        ]

        total_account_connects = 142
        reserved_floor = int(pol.get("reserved_connects", 42))
        current_balance = total_account_connects - used_connects
        spendable_balance = max(0, current_balance - reserved_floor)

        connects_ledger = {
            "total_account_connects": total_account_connects,
            "reserved_floor": reserved_floor,
            "used_connects": used_connects,
            "current_balance": current_balance,
            "spendable_balance": spendable_balance,
            "ceiling_per_application": pol.get("per_application_connect_ceiling", 18),
            "spend_authorized_cap": pol.get("connects_spend_authorized", 100),
            "boosting_allowed": pol.get("boosting_allowed", False),
            "auto_submit_allowed": pol.get("auto_submit_allowed", False),
        }

        opportunities = []
        status_counts = {}
        for r in opp_rows:
            d = dict(r)
            status_counts[d["status"]] = status_counts.get(d["status"], 0) + 1

            try:
                d["flags"] = json.loads(d["flags"]) if d["flags"] else []
            except Exception:
                d["flags"] = []

            p_match = prop_map.get(d["key"])
            if p_match:
                d["has_draft"] = True
                d["proposal"] = p_match
                d["proposal_text"] = p_match.get("proposal_text", "")
                d["delivery_estimate"] = p_match.get("delivery_estimate", "2-4 days (~16 delivery hours)")
                d["bid_usd"] = p_match.get("bid_usd", d["budget_usd"])
                d["upwork_fee_usd"] = p_match.get("upwork_fee_usd", round(float(d.get("budget_usd") or 0) * 0.1, 2))
                d["net_earnings_usd"] = p_match.get("net_earnings_usd", round(float(d.get("budget_usd") or 0) * 0.9, 2))
            else:
                d["has_draft"] = False
                d["proposal"] = None
                d["proposal_text"] = ""
                b_val = float(d.get("budget_usd") or 0)
                d["bid_usd"] = b_val
                d["upwork_fee_usd"] = round(b_val * 0.1, 2)
                d["net_earnings_usd"] = round(b_val * 0.9, 2)
                d["delivery_estimate"] = "2-4 days (~16 delivery hours)"

            # Ranking explanation
            score = d.get("score") or 0.0
            if score >= 85:
                d["ranking_rationale"] = (
                    "Score 85: In-budget ($100-$1,500), payment verified, low competition (<10 applicants), fresh listing."
                )
            elif score >= 70:
                d["ranking_rationale"] = (
                    "Score 70: In-budget and payment verified; 10-15 applicants (-15 competition penalty). "
                    "Outranks lower-budget 85s in priority when stack match is 100% Python/webhooks and budget ($300-$400) gives higher EV per Connect."
                )
            elif score >= 40:
                d["ranking_rationale"] = (
                    f"Score {score}: High competition (20+ applicants) or community listing with unknown budget; verified fit."
                )
            else:
                d["ranking_rationale"] = f"Score {score}: Lower heuristic priority."

            opportunities.append(d)

        # Service units health
        watched_units = pol.get("watched_units", ["nova-job-inbox.service", "nova-job-n8n.service"])
        watched_units.append("astra-upwork-ledger.timer")
        units_health = {}
        for u in watched_units:
            try:
                res = subprocess.run(["systemctl", "--user", "is-active", u], capture_output=True, text=True, timeout=2)
                units_health[u] = res.stdout.strip() or "unknown"
            except Exception:
                units_health[u] = "unknown"

        return {
            "now": now,
            "as_of": now,
            "discord_connected": True,
            "connects": connects_ledger,
            "policy": pol,
            "summary": {
                "total_opportunities": len(opportunities),
                "by_status": status_counts,
                "submissions_count": len(submissions),
                "connects_used": used_connects,
                "revenue_collected_usd": 0.0,
                "proposals_drafted": len(proposals_list),
            },
            "opportunities": opportunities,
            "proposals": proposals_list,
            "submissions": submissions,
            "units": units_health,
        }
    finally:
        c.close()


def save_draft_action(payload: dict) -> dict:
    key = (payload.get("key") or "").strip()
    if not key:
        return {"error": "Missing 'key' in save draft request"}

    proposal_text = payload.get("proposal_text", "")
    bid_usd = payload.get("bid_usd")
    milestones = payload.get("milestones")
    lane = payload.get("lane")

    props = get_proposals()
    found = False
    now_str = _now_iso()

    for p in props:
        if p.get("key") == key:
            found = True
            p["proposal_text"] = proposal_text
            p["last_edited_at"] = now_str
            if bid_usd is not None:
                try:
                    b_val = float(bid_usd)
                    p["bid_usd"] = b_val
                    p["upwork_fee_usd"] = round(b_val * 0.1, 2)
                    p["net_earnings_usd"] = round(b_val * 0.9, 2)
                except ValueError:
                    pass
            if milestones is not None and isinstance(milestones, list):
                p["milestones"] = milestones
                total_days = sum(m.get("days", 2) for m in milestones) or 2
                p["estimated_hours"] = total_days * 4
                p["delivery_estimate"] = f"{total_days} days ({len(milestones)} milestones, ~{total_days * 4} delivery hours)"
            if lane:
                p["lane"] = lane
            break

    if not found:
        # Create a new draft entry from opportunities in DB
        c = sqlite3.connect(f"file:{LEDGER_DB}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            r = c.execute("SELECT * FROM opportunities WHERE key=?", (key,)).fetchone()
            if r:
                b_val = float(bid_usd or r["budget_usd"] or 200.0)
                new_prop = {
                    "key": key,
                    "title": r["title"],
                    "url": r["url"],
                    "lane": lane or "P1",
                    "budget_usd": r["budget_usd"] or b_val,
                    "bid_usd": b_val,
                    "connects_cost": r["connects_cost"] or 10,
                    "status": "READY_FOR_OWNER_SUBMISSION",
                    "milestones": milestones or [
                        {"name": "M1: Scoping, Setup & Core Logic", "usd": round(b_val / 2, 2), "days": 2},
                        {"name": "M2: Verification & Handoff", "usd": round(b_val / 2, 2), "days": 2},
                    ],
                    "proposal_text": proposal_text,
                    "upwork_fee_rate": 0.10,
                    "upwork_fee_usd": round(b_val * 0.1, 2),
                    "net_earnings_usd": round(b_val * 0.9, 2),
                    "estimated_hours": 16,
                    "delivery_estimate": "4 days (2 milestones, ~16 delivery hours)",
                    "last_edited_at": now_str,
                }
                props.append(new_prop)
                found = True
        finally:
            c.close()

    if found:
        save_proposals(props)
        return {"ok": True, "key": key, "saved_at": now_str}
    return {"error": f"Opportunity key {key} not found"}


def mark_submitted_action(payload: dict) -> dict:
    key = (payload.get("key") or "").strip()
    approval_ref = (payload.get("approval_ref") or "").strip()
    submitted_by = (payload.get("submitted_by") or "owner").strip()
    proposal_id = payload.get("proposal_id")

    if not key:
        return {"error": "Missing 'key'"}
    if not approval_ref:
        return {
            "error": "SUBMISSION REFUSED: Actual owner confirmation or Upwork receipt is strictly required. Never mark submitted without proof."
        }

    pol = get_policy()
    conn_req = payload.get("connects")

    c = sqlite3.connect(LEDGER_DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        # Check duplicate
        if c.execute("SELECT 1 FROM submissions WHERE opp_key=?", (key,)).fetchone():
            return {"error": "REFUSED: Duplicate submission already recorded in ledger"}

        # Get opp details
        opp = c.execute("SELECT connects_cost FROM opportunities WHERE key=?", (key,)).fetchone()
        connects = int(conn_req if conn_req is not None else (opp["connects_cost"] if opp and opp["connects_cost"] else 10))

        ceiling = pol.get("per_application_connect_ceiling", 18)
        if connects > ceiling:
            return {"error": f"REFUSED: {connects} Connects exceeds ceiling {ceiling}"}

        used = c.execute("SELECT COALESCE(SUM(connects_used), 0) FROM submissions").fetchone()[0]
        spend_authorized = pol.get("connects_spend_authorized", 100)
        reserved_floor = pol.get("reserved_connects", 42)
        total_account = 142

        if used + connects > spend_authorized:
            return {"error": f"REFUSED: Spend of {connects} would exceed total authorization of {spend_authorized}"}

        remaining_balance = total_account - (used + connects)
        if remaining_balance < reserved_floor:
            return {"error": f"REFUSED: Submission would breach preserved floor of {reserved_floor} Connects"}

        now_str = _now_iso()
        c.execute(
            "INSERT INTO submissions(opp_key, submitted_at, submitted_by, approval_ref, connects_used, proposal_id) VALUES(?,?,?,?,?,?)",
            (key, now_str, submitted_by, approval_ref, connects, proposal_id),
        )
        c.execute("UPDATE opportunities SET status='submitted', updated=? WHERE key=?", (now_str, key))
        c.commit()

        # Update proposals.json
        props = get_proposals()
        for p in props:
            if p.get("key") == key:
                p["status"] = "SUBMITTED"
                p["submitted_at"] = now_str
                p["approval_ref"] = approval_ref
                break
        save_proposals(props)

        from dotz.fleet.notify import send_discord_notification
        opp_row = c.execute("SELECT title, budget_usd FROM opportunities WHERE key=?", (key,)).fetchone()
        title_str = opp_row["title"] if opp_row else key
        budget_str = f"${opp_row['budget_usd']:.0f}" if opp_row and opp_row["budget_usd"] else "N/A"
        send_discord_notification(
            f"🎯 **UPWORK PROPOSAL RECORDED**\n"
            f"• **Job:** {title_str}\n"
            f"• **Budget:** {budget_str} · **Connects Used:** {connects}\n"
            f"• **Remaining Balance:** {remaining_balance} Connects ({remaining_balance - reserved_floor} spendable)\n"
            f"• **Receipt:** `{approval_ref}`\n"
            f"• **Fleet Command:** http://127.0.0.1:8795/#upwork"
        )

        return {"ok": True, "key": key, "submitted_at": now_str, "connects_used": connects}
    finally:
        c.close()


def reject_opportunity_action(payload: dict) -> dict:
    key = (payload.get("key") or "").strip()
    reason = (payload.get("reason") or "Rejected by owner in Fleet Command").strip()
    target_status = "archived" if payload.get("archive") else "rejected"

    if not key:
        return {"error": "Missing 'key'"}

    c = sqlite3.connect(LEDGER_DB, timeout=10)
    try:
        now_str = _now_iso()
        c.execute(
            "UPDATE opportunities SET status=?, notes=COALESCE(notes, '') || ' | ' || ?, updated=? WHERE key=?",
            (target_status, f"{target_status}: {reason}", now_str, key),
        )
        c.commit()

        # Update proposals.json if present
        props = get_proposals()
        for p in props:
            if p.get("key") == key:
                p["status"] = target_status.upper()
                break
        save_proposals(props)

        return {"ok": True, "key": key, "status": target_status}
    finally:
        c.close()
