"""Central task engine: priorities, deadlines, dependencies, history, reminders."""
from __future__ import annotations

import json
from datetime import datetime

from friday.clock import Clock, iso, parse_iso
from friday.db import Database

PRIORITY_NAMES = {1: "critical", 2: "high", 3: "normal", 4: "low"}
STATUSES = ("todo", "in_progress", "blocked", "done", "cancelled")
OPEN = ("todo", "in_progress", "blocked")


class TaskEngine:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    def _hist(self, task_id: int, event: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO task_history(task_id,ts,event,detail) VALUES (?,?,?,?)",
                        (task_id, iso(self.clock.now()), event, detail))

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["depends_on"] = json.loads(d["depends_on"] or "[]")
        d["priority_name"] = PRIORITY_NAMES.get(d["priority"], "normal")
        return d

    def add(self, title: str, *, description: str = "", priority: int = 3, deadline: datetime | None = None,
            project: str | None = None, depends_on: list[int] | None = None, owner: str = "user",
            automation_level: int | None = None, notes: str = "", remind_at: datetime | None = None,
            device: str = "") -> dict:
        title = title.strip()
        if not title:
            raise ValueError("task title is empty")
        if priority not in PRIORITY_NAMES:
            raise ValueError("priority must be 1 (critical) … 4 (low)")
        now = iso(self.clock.now())
        cur = self.db.execute(
            "INSERT INTO tasks(title,description,priority,deadline,project,status,depends_on,owner,automation_level,"
            "notes,remind_at,created,updated,source_device) VALUES (?,?,?,?,?, 'todo', ?,?,?,?,?,?,?,?)",
            (title, description, priority, iso(deadline) if deadline else None, project,
             json.dumps(depends_on or []), owner, automation_level, notes,
             iso(remind_at) if remind_at else None, now, now, device))
        tid = int(cur.lastrowid)
        self._hist(tid, "created", f"from {device or 'unknown device'}")
        return self.get(tid)

    def get(self, task_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return self._row(r) if r else None

    def update(self, task_id: int, **f) -> dict | None:
        cur = self.get(task_id)
        if not cur:
            return None
        sets: dict = {}
        for k in ("title", "description", "priority", "project", "owner", "automation_level", "notes"):
            if k in f and f[k] is not None:
                sets[k] = f[k]
        if "status" in f and f["status"] is not None:
            if f["status"] not in STATUSES:
                raise ValueError(f"status must be one of {STATUSES}")
            sets["status"] = f["status"]
            if f["status"] == "done":
                sets["completed_at"] = iso(self.clock.now())
        for k in ("deadline", "remind_at"):
            if k in f and f[k] is not None:
                sets[k] = iso(f[k]) if isinstance(f[k], datetime) else f[k]
                if k == "remind_at":
                    sets["reminded_at"] = None
        if "depends_on" in f and f["depends_on"] is not None:
            sets["depends_on"] = json.dumps(f["depends_on"])
        if not sets:
            return cur
        sets["updated"] = iso(self.clock.now())
        self.db.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), task_id))
        self._hist(task_id, "updated", ", ".join(f"{k}→{v}" for k, v in sets.items() if k != "updated"))
        return self.get(task_id)

    def complete(self, task_id: int) -> dict | None:
        t = self.update(task_id, status="done")
        if t:
            self._hist(task_id, "completed")
        return t

    def list(self, *, status: str | None = None, project: str | None = None, open_only: bool = False,
             due_before: datetime | None = None, limit: int = 100) -> list[dict]:
        sql, p = "SELECT * FROM tasks WHERE 1=1", []
        if status:
            sql += " AND status=?"; p.append(status)
        if open_only:
            sql += " AND status IN ('todo','in_progress','blocked')"
        if project:
            sql += " AND project=?"; p.append(project)
        if due_before:
            sql += " AND deadline IS NOT NULL AND deadline<=?"; p.append(iso(due_before))
        sql += " ORDER BY priority ASC, COALESCE(deadline,'9999') ASC, id ASC LIMIT ?"; p.append(limit)
        return [self._row(r) for r in self.db.query(sql, tuple(p))]

    def find(self, text: str) -> list[dict]:
        rows = self.db.query("SELECT * FROM tasks WHERE title LIKE ? AND status IN ('todo','in_progress','blocked') "
                             "ORDER BY id DESC LIMIT 10", (f"%{text}%",))
        return [self._row(r) for r in rows]

    def unmet_dependencies(self, task: dict) -> list[dict]:
        out = []
        for dep_id in task["depends_on"]:
            d = self.get(dep_id)
            if d and d["status"] not in ("done", "cancelled"):
                out.append(d)
        return out

    def blockers(self, project: str | None = None) -> list[dict]:
        """Open tasks that are explicitly blocked or waiting on unfinished dependencies."""
        out = []
        for t in self.list(open_only=True, project=project):
            unmet = self.unmet_dependencies(t)
            if t["status"] == "blocked" or unmet:
                out.append({**t, "waiting_on": [{"id": u["id"], "title": u["title"]} for u in unmet]})
        return out

    def next_actions(self, limit: int = 5, project: str | None = None) -> list[dict]:
        """Actionable now (not blocked, dependencies met), ranked by priority then deadline."""
        now = self.clock.now()
        cands = []
        for t in self.list(open_only=True, project=project):
            if t["status"] == "blocked" or self.unmet_dependencies(t):
                continue
            overdue = bool(t["deadline"]) and parse_iso(t["deadline"]) < now
            score = (t["priority"] - (1.5 if overdue else 0), t["deadline"] or "9999", t["id"])
            cands.append((score, t))
        cands.sort(key=lambda x: x[0])
        return [t for _, t in cands[:limit]]

    def due_reminders(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM tasks WHERE remind_at IS NOT NULL AND reminded_at IS NULL "
                             "AND status IN ('todo','in_progress','blocked') AND remind_at<=? ORDER BY remind_at",
                             (iso(self.clock.now()),))
        return [self._row(r) for r in rows]

    def mark_reminded(self, task_id: int) -> None:
        self.db.execute("UPDATE tasks SET reminded_at=? WHERE id=?", (iso(self.clock.now()), task_id))
        self._hist(task_id, "reminded")

    def history(self, task_id: int) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM task_history WHERE task_id=? ORDER BY id", (task_id,))]

    def completed_between(self, start: datetime, end: datetime) -> list[dict]:
        rows = self.db.query("SELECT * FROM tasks WHERE status='done' AND completed_at>=? AND completed_at<? "
                             "ORDER BY completed_at", (iso(start), iso(end)))
        return [self._row(r) for r in rows]
