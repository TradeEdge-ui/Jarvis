"""Prioritised notifications. Dedupe keys prevent the same alert from firing repeatedly."""
from __future__ import annotations

import json

from friday.clock import Clock, iso
from friday.db import Database

PRIORITIES = ("CRITICAL", "IMPORTANT", "ACTION_REQUIRED", "INFORMATIONAL")


class Notifications:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    def push(self, priority: str, title: str, body: str = "", source: str = "", dedupe_key: str | None = None,
             ref: dict | None = None) -> int | None:
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        try:
            cur = self.db.execute(
                "INSERT INTO notifications(ts,priority,title,body,source,dedupe_key,ref) VALUES (?,?,?,?,?,?,?)",
                (iso(self.clock.now()), priority, title, body, source, dedupe_key, json.dumps(ref or {})))
            return int(cur.lastrowid)
        except Exception as e:  # sqlite3.IntegrityError on duplicate dedupe_key
            if "UNIQUE" in str(e):
                return None
            raise

    def list(self, unread_only: bool = False, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM notifications" + (" WHERE read_at IS NULL" if unread_only else "")
        sql += (" ORDER BY CASE priority WHEN 'CRITICAL' THEN 0 WHEN 'IMPORTANT' THEN 1 "
                "WHEN 'ACTION_REQUIRED' THEN 2 ELSE 3 END, id DESC LIMIT ?")
        out = []
        for r in self.db.query(sql, (limit,)):
            d = dict(r); d["ref"] = json.loads(d["ref"] or "{}"); out.append(d)
        return out

    def ack(self, notification_id: int | None = None) -> int:
        now = iso(self.clock.now())
        if notification_id is None:
            return self.db.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL", (now,)).rowcount
        return self.db.execute("UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL",
                               (now, notification_id)).rowcount

    def unread_count(self) -> int:
        return self.db.one("SELECT COUNT(*) c FROM notifications WHERE read_at IS NULL")["c"]
