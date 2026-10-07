"""Pending approvals. An approval is bound to the exact tool + arguments (by fingerprint)."""
from __future__ import annotations

import json
from datetime import timedelta

from friday.clock import Clock, iso, parse_iso
from friday.db import Database
from friday.util import redact

APPROVAL_TTL = timedelta(hours=24)


class ApprovalError(Exception):
    pass


class ApprovalService:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    def create(self, *, tool: str, args: dict, fingerprint: str, scope: str, risk: str, reason: str,
               why_needed: str, user_command: str = "", conversation_id: str = "", device: str = "") -> dict:
        existing = self.db.one(
            "SELECT * FROM approvals WHERE status='pending' AND fingerprint=? AND expires>?",
            (fingerprint, iso(self.clock.now())))
        if existing:
            return self._row(existing)
        now = self.clock.now()
        cur = self.db.execute(
            "INSERT INTO approvals(tool,args,fingerprint,scope,risk,reason,why_needed,user_command,"
            "conversation_id,device,status,created,expires) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending',?,?)",
            (tool, json.dumps(args, default=str), fingerprint, scope, risk, reason, why_needed, user_command,
             conversation_id, device, iso(now), iso(now + APPROVAL_TTL)))
        return self.get(int(cur.lastrowid))

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["args"] = json.loads(d["args"])
        d["display_args"] = json.loads(redact(json.dumps(d["args"], default=str)))
        if d.get("result"):
            d["result"] = json.loads(d["result"])
        return d

    def get(self, approval_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return self._row(r) if r else None

    def list(self, status: str | None = "pending", limit: int = 50) -> list[dict]:
        self.expire_old()
        if status:
            rows = self.db.query("SELECT * FROM approvals WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.query("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]

    def expire_old(self) -> None:
        self.db.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND expires<=?",
                        (iso(self.clock.now()),))

    def decide(self, approval_id: int, approve: bool, by: str) -> dict:
        """Marks pending -> approved/denied. Raises if not pending (single use)."""
        self.expire_old()
        cur = self.db.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE id=? AND status='pending'",
            ("approved" if approve else "denied", iso(self.clock.now()), by, approval_id))
        if cur.rowcount != 1:
            row = self.get(approval_id)
            if not row:
                raise ApprovalError(f"approval {approval_id} not found")
            raise ApprovalError(f"approval {approval_id} is {row['status']}, not pending")
        return self.get(approval_id)

    def finish(self, approval_id: int, ok: bool, result: dict) -> None:
        self.db.execute("UPDATE approvals SET status=?, result=? WHERE id=?",
                        ("executed" if ok else "failed", json.dumps(result, default=str), approval_id))

    # ---- standing approvals ("always allow this exact action") --------------
    def add_standing(self, tool: str, fingerprint: str, ttl: timedelta | None = None) -> None:
        exp = iso(self.clock.now() + ttl) if ttl else None
        self.db.execute("INSERT OR REPLACE INTO standing_approvals(tool,fingerprint,created,expires) VALUES (?,?,?,?)",
                        (tool, fingerprint, iso(self.clock.now()), exp))

    def has_standing(self, tool: str, fingerprint: str) -> bool:
        r = self.db.one("SELECT expires FROM standing_approvals WHERE tool=? AND fingerprint=?", (tool, fingerprint))
        if not r:
            return False
        return r["expires"] is None or parse_iso(r["expires"]) > self.clock.now()

    def list_standing(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM standing_approvals ORDER BY id DESC")]

    def revoke_standing(self, standing_id: int) -> None:
        self.db.execute("DELETE FROM standing_approvals WHERE id=?", (standing_id,))
