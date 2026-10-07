"""Append-only, hash-chained action log. Tampering with a past row breaks the chain."""
from __future__ import annotations

import hashlib
import threading

from friday.clock import Clock, iso
from friday.db import Database
from friday.util import canonical_json, redact, truncate

_FIELDS = ("ts", "run_id", "device", "user_command", "reason", "tool", "scope", "action", "args",
           "status", "summary", "verified", "verification", "success", "permission", "risk", "autonomy")
GENESIS = "0" * 64


class AuditLog:
    def __init__(self, db: Database, clock: Clock):
        self.db = db
        self.clock = clock
        self._lock = threading.Lock()

    @staticmethod
    def _hash(prev: str, row: dict) -> str:
        payload = canonical_json({k: row.get(k) for k in _FIELDS})
        return hashlib.sha256((prev + payload).encode()).hexdigest()

    def record(self, *, tool: str, status: str, success: bool, scope: str = "", action: str = "",
               args: str = "", summary: str = "", verified: bool | None = None, verification: str = "",
               permission: str = "", risk: str = "", autonomy: int | None = None, run_id: str = "",
               device: str = "", user_command: str = "", reason: str = "") -> int:
        row = {
            "ts": iso(self.clock.now()), "run_id": run_id, "device": device,
            "user_command": redact(truncate(user_command or "", 2000)),
            "reason": redact(truncate(reason or "", 1000)), "tool": tool, "scope": scope,
            "action": redact(truncate(action or "", 1000)), "args": redact(truncate(args or "", 2000)),
            "status": status, "summary": redact(truncate(summary or "", 2000)),
            "verified": None if verified is None else int(verified),
            "verification": redact(truncate(verification or "", 1000)),
            "success": int(success), "permission": permission, "risk": risk, "autonomy": autonomy,
        }
        with self._lock, self.db.transaction(immediate=True):
            last = self.db.one("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
            prev = last["hash"] if last else GENESIS
            h = self._hash(prev, row)
            cols = ",".join(_FIELDS) + ",prev_hash,hash"
            cur = self.db.execute(
                f"INSERT INTO audit_log({cols}) VALUES ({','.join('?' * (len(_FIELDS) + 2))})",
                (*[row[k] for k in _FIELDS], prev, h),
            )
            return int(cur.lastrowid)

    def recent(self, limit: int = 50, tool: str | None = None, query: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM audit_log WHERE 1=1", []
        if tool:
            sql += " AND tool=?"; params.append(tool)
        if query:
            sql += " AND (user_command LIKE ? OR summary LIKE ? OR action LIKE ?)"
            params += [f"%{query}%"] * 3
        sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
        return [dict(r) for r in self.db.query(sql, tuple(params))]

    def verify_chain(self) -> tuple[bool, int | None]:
        prev = GENESIS
        for r in self.db.query("SELECT * FROM audit_log ORDER BY id"):
            row = dict(r)
            if row["prev_hash"] != prev or self._hash(prev, row) != row["hash"]:
                return False, int(row["id"])
            prev = row["hash"]
        return True, None
