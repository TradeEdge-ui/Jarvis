"""Server-side conversation threads. Every device reads/writes the same 'main' thread, so context follows the user."""
from __future__ import annotations

import json
import uuid

from friday.clock import Clock, iso
from friday.db import Database

MAIN = "main"


class Conversations:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    def resolve(self, conversation_id: str | None, device: str = "") -> str:
        cid = conversation_id or MAIN
        if not self.db.one("SELECT 1 FROM conversations WHERE id=?", (cid,)):
            now = iso(self.clock.now())
            self.db.execute("INSERT INTO conversations(id,title,device,created,updated) VALUES (?,?,?,?,?)",
                            (cid, "Main conversation" if cid == MAIN else f"Conversation {cid[:8]}", device, now, now))
        return cid

    def new(self, title: str = "", device: str = "") -> str:
        cid = uuid.uuid4().hex[:12]
        now = iso(self.clock.now())
        self.db.execute("INSERT INTO conversations(id,title,device,created,updated) VALUES (?,?,?,?,?)",
                        (cid, title or f"Conversation {cid[:6]}", device, now, now))
        return cid

    def add(self, cid: str, role: str, content: str, device: str = "", meta: dict | None = None) -> int:
        now = iso(self.clock.now())
        cur = self.db.execute("INSERT INTO messages(conversation_id,role,content,ts,device,meta) VALUES (?,?,?,?,?,?)",
                              (cid, role, content, now, device, json.dumps(meta) if meta else None))
        self.db.execute("UPDATE conversations SET updated=? WHERE id=?", (now, cid))
        return int(cur.lastrowid)

    def history(self, cid: str, limit: int = 20) -> list[dict]:
        rows = self.db.query("SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", (cid, limit))
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["meta"] = json.loads(d["meta"]) if d["meta"] else None
            out.append(d)
        return out

    def list(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM conversations ORDER BY updated DESC LIMIT ?", (limit,))]
