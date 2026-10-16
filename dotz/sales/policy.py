"""Standing campaign policy: load, hash-bound owner approval, ramp, window, stop rules."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

POLICY_FILE = Path(__file__).with_name("policy.yaml")
APPROVAL_FILE = Path.home() / ".config" / "dotz" / "policy_approval.json"
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com",
             "live.com", "msn.com", "proton.me", "protonmail.com", "me.com", "comcast.net", "att.net", "sbcglobal.net"}
CATEGORIES = ("inbound_or_invited", "public_business_role_address")


def policy_hash(path: Path = POLICY_FILE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Policy:
    def __init__(self, path: Path = POLICY_FILE, approval_file: Path = APPROVAL_FILE):
        self.path = path
        self.approval_file = approval_file
        self.cfg = yaml.safe_load(path.read_text())
        self.hash = policy_hash(path)

    # ---- owner approval (written only by the owner-run CLI command) ----------
    def approval(self) -> dict | None:
        try:
            a = json.loads(self.approval_file.read_text())
        except (OSError, ValueError):
            return None
        if a.get("policy_hash") != self.hash or not a.get("categories") or not a.get("approved_at"):
            return None  # edited policy or malformed approval: not approved
        return a

    def approve(self, categories: list[str], approver: str) -> dict:
        bad = [c for c in categories if c not in CATEGORIES]
        if bad or not categories:
            raise ValueError(f"categories must be from {CATEGORIES}")
        rec = {"policy_id": self.cfg["id"], "policy_hash": self.hash, "categories": sorted(categories),
               "approver": approver, "approved_at": time.time()}
        self.approval_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.approval_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(rec, fh)
        return rec

    def revoke(self) -> None:
        self.approval_file.unlink(missing_ok=True)

    # ---- rules ---------------------------------------------------------------
    def in_window(self, now: float | None = None) -> bool:
        s = self.cfg["schedule"]
        dt = datetime.fromtimestamp(now if now is not None else time.time(), ZoneInfo(s["timezone"]))
        return dt.weekday() in s["weekdays"] and s["start_hour"] <= dt.hour < s["end_hour"]

    def effective_cap(self) -> int:
        """Hard ceiling for ALL account messages per rolling 24h: owner cap, never above Google's actual limit."""
        v = self.cfg["volume"]
        return min(int(v["account_rolling_24h_cap"]), int(v["google_account_limit"]))

    def sales_budget(self, account_sends_24h: int) -> int:
        v = self.cfg["volume"]
        return max(0, self.effective_cap() - int(v["reserve_for_transactional_and_manual"]) - account_sends_24h)

    def window_seconds_left(self, now: float) -> float:
        s = self.cfg["schedule"]
        dt = datetime.fromtimestamp(now, ZoneInfo(s["timezone"]))
        end = dt.replace(hour=s["end_hour"], minute=0, second=0, microsecond=0)
        return max(0.0, (end - dt).total_seconds())

    def is_free_mail(self, email: str) -> bool:
        return email.rsplit("@", 1)[-1].casefold() in FREE_MAIL
