"""Memory and task tools (FRIDAY's own state). Each write is verified by reading it back from the database."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from friday.clock import UTC, parse_iso
from friday.memory.store import CATEGORIES
from friday.security.policy import Classification, Effect, Risk
from friday.tasks import PRIORITY_NAMES
from friday.timeparse import parse_when, strip_phrases
from friday.tools.base import Tool, ToolContext, ToolResult, Verification


def parse_time_arg(ctx: ToolContext, text: str | None) -> tuple[datetime | None, bool]:
    """Accepts ISO-8601 or natural language ('tomorrow 9am', 'in 2 hours'). -> (utc datetime, had_time_of_day)."""
    if not text:
        return None, False
    t = text.strip()
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ctx.svc.config.zone)
        return dt.astimezone(UTC), ("T" in t or " " in t)
    except ValueError:
        pass
    p = parse_when(t, ctx.now, ctx.svc.config.zone)
    if not p:
        raise ValueError(f"could not understand the time '{text}'. Use ISO format (2026-10-08T09:00) or e.g. 'tomorrow 9am'.")
    return p.when, p.has_time


def fmt_local(ctx: ToolContext, iso_utc: str | None) -> str:
    if not iso_utc:
        return "no deadline"
    return parse_iso(iso_utc).astimezone(ctx.svc.config.zone).strftime("%a %d %b %Y %H:%M")


# ------------------------------------------------------------------ memory
class MemoryRemember(Tool):
    name = "memory_remember"
    scope = "memory.write"
    group = "memory"
    description = ("Store a durable fact, preference, goal or business note in long-term memory. "
                   "Only store what the user asked to remember or clearly stable facts.")

    class Args(BaseModel):
        content: str = Field(description="The fact, written as a self-contained statement.")
        category: Literal["long_term", "business", "device", "knowledge"] = "long_term"
        title: str | None = None
        tags: list[str] = Field(default_factory=list)
        project: str | None = Field(default=None, description="e.g. 'CyZy'")
        importance: float = Field(default=0.5, ge=0.0, le=1.0, description="0.8+ is always shown in context")

    def classify(self, args, ctx):
        return Classification(Risk.SAFE, Effect.INTERNAL)

    def run(self, args, ctx):
        try:
            mid = ctx.svc.memory.add(args.content, args.category, args.title, args.tags, args.project,
                                     source=f"user@{ctx.device or 'unknown'}", importance=args.importance)
        except PermissionError as e:
            return ToolResult.fail(str(e))
        return ToolResult.success(f"Remembered (#{mid}, {args.category}): {args.content[:120]}", id=mid, category=args.category)

    def verify(self, args, result, ctx):
        m = ctx.svc.memory.get(result.data["id"])
        ok = bool(m and m["content"] == args.content.strip())
        return Verification(ok, "re-read from database", f"memory #{result.data['id']} stored" if ok else "memory not found")


class MemorySearch(Tool):
    name = "memory_search"
    scope = "memory.read"
    group = "memory"
    description = ("Search FRIDAY's memory (long_term, business, device, knowledge, or 'operational' = what FRIDAY did). "
                   "Use before answering questions about the user's projects, preferences or past work.")

    class Args(BaseModel):
        query: str
        category: Literal["any", "long_term", "business", "device", "knowledge", "operational"] = "any"
        project: str | None = None
        limit: int = Field(default=6, ge=1, le=20)

    def run(self, args, ctx):
        mem = ctx.svc.memory
        if args.category == "operational":
            rows = mem.search_operational(args.query, args.limit)
            return ToolResult.success(f"{len(rows)} operational record(s) match", results=rows)
        cats = None if args.category == "any" else [args.category]
        rows = mem.search(args.query, cats, args.limit, args.project)
        slim = [{"id": r["id"], "category": r["category"], "title": r["title"], "content": r["content"][:600],
                 "project": r["project"], "updated": r["updated"]} for r in rows]
        return ToolResult.success(f"{len(slim)} memor{'y' if len(slim) == 1 else 'ies'} match '{args.query}'", results=slim)


class MemoryCorrect(Tool):
    name = "memory_correct"
    scope = "memory.write"
    group = "memory"
    description = "Correct the text of an existing memory by id."

    class Args(BaseModel):
        id: int
        content: str

    def classify(self, args, ctx):
        return Classification(Risk.LOW, Effect.INTERNAL)

    def run(self, args, ctx):
        if not ctx.svc.memory.update(args.id, content=args.content.strip()):
            return ToolResult.fail(f"memory #{args.id} not found")
        return ToolResult.success(f"Updated memory #{args.id}", id=args.id)

    def verify(self, args, result, ctx):
        m = ctx.svc.memory.get(args.id)
        ok = bool(m and m["content"] == args.content.strip())
        return Verification(ok, "re-read from database")


class MemoryForget(Tool):
    name = "memory_forget"
    scope = "memory.write"
    group = "memory"
    description = "Delete a memory by id (soft delete; it disappears from search and can be purged for good via the control panel)."

    class Args(BaseModel):
        id: int

    def classify(self, args, ctx):
        return Classification(Risk.LOW, Effect.INTERNAL)

    def run(self, args, ctx):
        if not ctx.svc.memory.delete(args.id):
            return ToolResult.fail(f"memory #{args.id} not found")
        return ToolResult.success(f"Forgot memory #{args.id}", id=args.id)

    def verify(self, args, result, ctx):
        gone = ctx.svc.memory.get(args.id) is None
        return Verification(gone, "re-read from database", "no longer retrievable" if gone else "still present")


# ------------------------------------------------------------------ tasks
class TaskAdd(Tool):
    name = "task_add"
    scope = "tasks.write"
    group = "tasks"
    description = ("Create a task or reminder. 'deadline' and 'remind_at' accept ISO-8601 or natural language "
                   "('tomorrow 9am', 'in 2 hours', 'friday 5pm'). Times are the user's local time.")

    class Args(BaseModel):
        title: str
        description: str = ""
        priority: int = Field(default=3, ge=1, le=4, description="1 critical, 2 high, 3 normal, 4 low")
        deadline: str | None = None
        remind_at: str | None = Field(default=None, description="When to notify the user")
        project: str | None = None
        depends_on: list[int] = Field(default_factory=list)
        owner: Literal["user", "friday"] = "user"
        notes: str = ""

    def classify(self, args, ctx):
        return Classification(Risk.SAFE, Effect.INTERNAL)

    def run(self, args, ctx):
        try:
            deadline, _ = parse_time_arg(ctx, args.deadline)
            remind, _ = parse_time_arg(ctx, args.remind_at)
        except ValueError as e:
            return ToolResult.fail(str(e))
        t = ctx.svc.tasks.add(args.title, description=args.description, priority=args.priority, deadline=deadline,
                              project=args.project, depends_on=args.depends_on, owner=args.owner, notes=args.notes,
                              remind_at=remind, device=ctx.device)
        bits = [f"Task #{t['id']}: {t['title']}", f"due {fmt_local(ctx, t['deadline'])}" if t["deadline"] else None,
                f"reminder {fmt_local(ctx, t['remind_at'])}" if t["remind_at"] else None,
                f"project {t['project']}" if t["project"] else None]
        return ToolResult.success(" · ".join(b for b in bits if b), task=t)

    def verify(self, args, result, ctx):
        t = result.data["task"]
        row = ctx.svc.tasks.get(t["id"])
        ok = bool(row and row["title"] == t["title"] and row["deadline"] == t["deadline"] and row["remind_at"] == t["remind_at"])
        return Verification(ok, "re-read from database", f"task #{t['id']} stored" if ok else "task not found or differs")


class TaskList(Tool):
    name = "task_list"
    scope = "tasks.read"
    group = "tasks"
    description = ("List tasks. view: 'today' (due today/overdue + reminders), 'next' (what to do next, ranked), "
                   "'open', 'blocked' (what is blocking), 'done_today', 'all'.")

    class Args(BaseModel):
        view: Literal["today", "next", "open", "blocked", "done_today", "all"] = "today"
        project: str | None = None
        limit: int = Field(default=15, ge=1, le=50)

    def run(self, args, ctx):
        eng, z = ctx.svc.tasks, ctx.svc.config.zone
        now_local = ctx.now.astimezone(z)
        end_today = now_local.replace(hour=23, minute=59, second=59).astimezone(UTC)
        start_today = now_local.replace(hour=0, minute=0, second=0).astimezone(UTC)
        if args.view == "today":
            rows = eng.list(open_only=True, due_before=end_today, project=args.project, limit=args.limit)
        elif args.view == "next":
            rows = eng.next_actions(args.limit, args.project)
        elif args.view == "open":
            rows = eng.list(open_only=True, project=args.project, limit=args.limit)
        elif args.view == "blocked":
            rows = eng.blockers(args.project)[:args.limit]
        elif args.view == "done_today":
            rows = eng.completed_between(start_today, end_today)
        else:
            rows = eng.list(project=args.project, limit=args.limit)
        slim = [{"id": t["id"], "title": t["title"], "priority": t["priority_name"], "status": t["status"],
                 "due": fmt_local(ctx, t["deadline"]) if t["deadline"] else None, "project": t["project"],
                 "waiting_on": t.get("waiting_on")} for t in rows]
        label = {"today": "due today or overdue", "next": "to do next", "open": "open", "blocked": "blocked",
                 "done_today": "completed today", "all": "total"}[args.view]
        return ToolResult.success(f"{len(slim)} task(s) {label}", tasks=slim, view=args.view)


class TaskUpdate(Tool):
    name = "task_update"
    scope = "tasks.write"
    group = "tasks"
    description = "Change a task: status, priority, deadline ('move to tomorrow'), project, notes, dependencies."

    class Args(BaseModel):
        id: int
        status: Literal["todo", "in_progress", "blocked", "done", "cancelled"] | None = None
        priority: int | None = Field(default=None, ge=1, le=4)
        deadline: str | None = None
        remind_at: str | None = None
        title: str | None = None
        project: str | None = None
        notes: str | None = None
        depends_on: list[int] | None = None

    def classify(self, args, ctx):
        return Classification(Risk.SAFE, Effect.INTERNAL)

    def run(self, args, ctx):
        if not ctx.svc.tasks.get(args.id):
            return ToolResult.fail(f"task #{args.id} not found")
        try:
            deadline, _ = parse_time_arg(ctx, args.deadline)
            remind, _ = parse_time_arg(ctx, args.remind_at)
        except ValueError as e:
            return ToolResult.fail(str(e))
        t = ctx.svc.tasks.update(args.id, status=args.status, priority=args.priority, deadline=deadline,
                                 remind_at=remind, title=args.title, project=args.project, notes=args.notes,
                                 depends_on=args.depends_on)
        return ToolResult.success(f"Updated task #{t['id']}: {t['title']} ({t['status']}, due {fmt_local(ctx, t['deadline'])})", task=t)

    def verify(self, args, result, ctx):
        want, row = result.data["task"], ctx.svc.tasks.get(args.id)
        ok = bool(row) and all(row[k] == want[k] for k in ("status", "priority", "deadline", "remind_at", "title", "project"))
        return Verification(ok, "re-read from database")


class TaskComplete(Tool):
    name = "task_complete"
    scope = "tasks.write"
    group = "tasks"
    description = "Mark a task done, by id or by (part of) its title."

    class Args(BaseModel):
        id: int | None = None
        title_contains: str | None = None

    def classify(self, args, ctx):
        return Classification(Risk.SAFE, Effect.INTERNAL)

    def run(self, args, ctx):
        tid = args.id
        if tid is None:
            if not args.title_contains:
                return ToolResult.fail("give either id or title_contains")
            matches = ctx.svc.tasks.find(args.title_contains)
            if not matches:
                return ToolResult.fail(f"no open task matches '{args.title_contains}'")
            if len(matches) > 1:
                return ToolResult.fail("several tasks match; specify the id: " +
                                       "; ".join(f"#{m['id']} {m['title']}" for m in matches[:5]))
            tid = matches[0]["id"]
        t = ctx.svc.tasks.complete(tid)
        if not t:
            return ToolResult.fail(f"task #{tid} not found")
        return ToolResult.success(f"Completed task #{t['id']}: {t['title']}", task=t)

    def verify(self, args, result, ctx):
        row = ctx.svc.tasks.get(result.data["task"]["id"])
        ok = bool(row and row["status"] == "done" and row["completed_at"])
        return Verification(ok, "re-read from database")


TOOLS = [MemoryRemember, MemorySearch, MemoryCorrect, MemoryForget, TaskAdd, TaskList, TaskUpdate, TaskComplete]
