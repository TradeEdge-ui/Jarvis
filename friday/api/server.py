"""HTTP API used by the web UI, the Android app, Telegram bridges and any future device."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from friday import __version__, briefing as briefing_mod
from friday.clock import UTC
from friday.config import Config
from friday.core.agent import Agent
from friday.core.models import ModelRouter
from friday.memory.store import CATEGORIES
from friday.scheduler import Scheduler
from friday.security.approvals import ApprovalError
from friday.security.autonomy import LEVEL_DESCRIPTIONS, Level
from friday.security.auth import Principal
from friday.security.permissions import Grant
from friday.services import Services, build_services
from friday.tools.personal import parse_time_arg
from friday.tools.base import ToolContext

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None


class TaskIn(BaseModel):
    title: str
    description: str = ""
    priority: int = Field(default=3, ge=1, le=4)
    deadline: str | None = None
    remind_at: str | None = None
    project: str | None = None
    depends_on: list[int] = []
    notes: str = ""


class TaskPatch(BaseModel):
    title: str | None = None
    status: Literal["todo", "in_progress", "blocked", "done", "cancelled"] | None = None
    priority: int | None = Field(default=None, ge=1, le=4)
    deadline: str | None = None
    remind_at: str | None = None
    project: str | None = None
    notes: str | None = None
    depends_on: list[int] | None = None


class MemoryIn(BaseModel):
    content: str
    category: Literal["long_term", "business", "device", "knowledge"] = "long_term"
    title: str | None = None
    tags: list[str] = []
    project: str | None = None
    importance: float = Field(default=0.5, ge=0, le=1)


class MemoryPatch(BaseModel):
    content: str | None = None
    title: str | None = None
    tags: list[str] | None = None
    project: str | None = None
    importance: float | None = Field(default=None, ge=0, le=1)
    category: Literal["long_term", "business", "device", "knowledge"] | None = None


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["pc", "phone", "browser", "bot", "other"] = "other"
    can_approve: bool = True


class ApproveIn(BaseModel):
    remember: bool = False


class AckIn(BaseModel):
    id: int | None = None

class CatIn(BaseModel):
    enabled: bool

class GrantIn(BaseModel):
    grant: Grant

class LevelIn(BaseModel):
    level: int = Field(ge=0, le=4)


def create_app(svc: Services | None = None, router: ModelRouter | None = None, start_scheduler: bool = True) -> FastAPI:
    svc = svc or build_services(Config.from_env())
    router = router or ModelRouter(svc)
    agent = Agent(svc, router)
    sched = Scheduler(svc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            sched.start()
        yield
        sched.stop()

    app = FastAPI(title="FRIDAY", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.svc, app.state.agent, app.state.router, app.state.scheduler = svc, agent, router, sched

    # ---------------------------------------------------------------- auth
    def current(authorization: str | None = Header(default=None)) -> Principal:
        token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
        p = svc.auth.authenticate(token)
        if not p:
            raise HTTPException(401, "invalid or missing token", headers={"WWW-Authenticate": "Bearer"})
        return p

    def owner(p: Principal = Depends(current)) -> Principal:
        if not p.is_owner:
            raise HTTPException(403, "owner role required")
        return p

    @app.middleware("http")
    async def headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ---------------------------------------------------------------- public
    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/")
    def index():
        return FileResponse(UI_DIR / "index.html", media_type="text/html")

    @app.get("/ui/{name}")
    def ui_asset(name: str):
        f = (UI_DIR / name).resolve()
        if UI_DIR.resolve() not in f.parents or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f)

    # ---------------------------------------------------------------- status / chat
    @app.get("/v1/status")
    def status(p: Principal = Depends(current)):
        b = router.backend()
        return {
            "version": __version__, "device": p.name, "role": p.role, "model": b,
            "autonomy": {"level": int(svc.autonomy.get()), "description": LEVEL_DESCRIPTIONS[svc.autonomy.get()]},
            "emergency_stop": svc.estop.state(), "tools": len(svc.registry.all()),
            "pending_approvals": len(svc.approvals.list("pending")), "unread_notifications": svc.notifications.unread_count(),
            "open_tasks": len(svc.tasks.list(open_only=True)), "timezone": svc.config.tz, "workspace": str(svc.config.home),
        }

    @app.post("/v1/chat")
    def chat(body: ChatIn, p: Principal = Depends(current)):
        r = agent.handle(body.message, conversation_id=body.conversation_id, device=p.name, can_approve=p.can_approve)
        return r.to_dict()

    @app.get("/v1/conversations")
    def conversations(p: Principal = Depends(current)):
        return svc.conversations.list()

    @app.get("/v1/conversations/{cid}/messages")
    def messages(cid: str, limit: int = Query(50, ge=1, le=200), p: Principal = Depends(current)):
        return svc.conversations.history(cid, limit)

    # ---------------------------------------------------------------- tasks
    def _ctx(p):
        return ToolContext(svc=svc, device=p.name)

    @app.get("/v1/tasks")
    def tasks(view: Literal["open", "next", "blocked", "all", "done"] = "open", project: str | None = None,
              p: Principal = Depends(current)):
        t = svc.tasks
        if view == "next":
            return t.next_actions(20, project)
        if view == "blocked":
            return t.blockers(project)
        if view == "done":
            return t.list(status="done", project=project, limit=50)
        return t.list(open_only=(view == "open"), project=project)

    @app.post("/v1/tasks", status_code=201)
    def add_task(body: TaskIn, p: Principal = Depends(current)):
        try:
            dl, _ = parse_time_arg(_ctx(p), body.deadline)
            rm, _ = parse_time_arg(_ctx(p), body.remind_at)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return svc.tasks.add(body.title, description=body.description, priority=body.priority, deadline=dl, project=body.project,
                             depends_on=body.depends_on, notes=body.notes, remind_at=rm, device=p.name)

    @app.patch("/v1/tasks/{tid}")
    def patch_task(tid: int, body: TaskPatch, p: Principal = Depends(current)):
        f = body.model_dump(exclude_unset=True)
        try:
            for k in ("deadline", "remind_at"):
                if k in f:
                    f[k], _ = parse_time_arg(_ctx(p), f[k])
            t = svc.tasks.update(tid, **f)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if not t:
            raise HTTPException(404, "task not found")
        return t

    @app.post("/v1/tasks/{tid}/complete")
    def complete_task(tid: int, p: Principal = Depends(current)):
        t = svc.tasks.complete(tid)
        if not t:
            raise HTTPException(404, "task not found")
        return t

    @app.get("/v1/tasks/{tid}/history")
    def task_history(tid: int, p: Principal = Depends(current)):
        return svc.tasks.history(tid)

    # ---------------------------------------------------------------- approvals
    @app.get("/v1/approvals")
    def approvals(status: str | None = "pending", p: Principal = Depends(current)):
        return svc.approvals.list(None if status == "all" else status)

    def _need_approver(p: Principal):
        if not p.can_approve:
            raise HTTPException(403, "this device may not approve actions")

    @app.post("/v1/approvals/{aid}/approve")
    def approve(aid: int, body: ApproveIn | None = None, p: Principal = Depends(current)):
        _need_approver(p)
        try:
            ap, res = svc.executor.approve_and_run(aid, by=p.name, remember=bool(body and body.remember))
        except ApprovalError as e:
            raise HTTPException(409, str(e))
        return {"approval": ap, "result": res.to_dict()}

    @app.post("/v1/approvals/{aid}/deny")
    def deny(aid: int, p: Principal = Depends(current)):
        _need_approver(p)
        try:
            return svc.executor.deny(aid, by=p.name)
        except ApprovalError as e:
            raise HTTPException(409, str(e))

    # ---------------------------------------------------------------- activity / notifications
    @app.get("/v1/activity")
    def activity(limit: int = Query(50, ge=1, le=500), tool: str | None = None, q: str | None = None, p: Principal = Depends(current)):
        return svc.audit.recent(limit, tool, q)

    @app.get("/v1/activity/verify")
    def verify_chain(p: Principal = Depends(current)):
        ok, bad = svc.audit.verify_chain()
        return {"intact": ok, "first_bad_id": bad}

    @app.get("/v1/notifications")
    def notifications(unread: bool = False, p: Principal = Depends(current)):
        return svc.notifications.list(unread_only=unread)


    @app.post("/v1/notifications/ack")
    def ack(body: AckIn, p: Principal = Depends(current)):
        return {"acknowledged": svc.notifications.ack(body.id)}

    # ---------------------------------------------------------------- memory
    @app.get("/v1/memory")
    def memory(category: str | None = None, q: str | None = None, limit: int = Query(50, ge=1, le=200), p: Principal = Depends(current)):
        if q:
            return svc.memory.search(q, [category] if category else None, limit)
        return svc.memory.list(category, limit)

    @app.post("/v1/memory", status_code=201)
    def add_memory(body: MemoryIn, p: Principal = Depends(current)):
        try:
            mid = svc.memory.add(body.content, body.category, body.title, body.tags, body.project, f"user@{p.name}", body.importance)
        except (ValueError, PermissionError) as e:
            raise HTTPException(422, str(e))
        return svc.memory.get(mid)

    @app.patch("/v1/memory/{mid}")
    def patch_memory(mid: int, body: MemoryPatch, p: Principal = Depends(current)):
        if not svc.memory.update(mid, **body.model_dump(exclude_unset=True)):
            raise HTTPException(404, "memory not found")
        return svc.memory.get(mid)

    @app.delete("/v1/memory/{mid}")
    def del_memory(mid: int, hard: bool = False, p: Principal = Depends(current)):
        if not svc.memory.delete(mid, hard):
            raise HTTPException(404, "memory not found")
        return {"deleted": mid, "hard": hard}

    @app.get("/v1/memory-export")
    def export_memory(p: Principal = Depends(current)):
        return svc.memory.export()

    @app.get("/v1/memory-categories")
    def mem_cats(p: Principal = Depends(current)):
        return svc.memory.categories_status()


    @app.put("/v1/memory-categories/{cat}")
    def set_cat(cat: str, body: CatIn, p: Principal = Depends(owner)):
        if cat not in CATEGORIES:
            raise HTTPException(404, "unknown category")
        svc.memory.set_category_enabled(cat, body.enabled)
        return svc.memory.categories_status()

    # ---------------------------------------------------------------- devices
    @app.get("/v1/devices")
    def devices(p: Principal = Depends(current)):
        return svc.auth.list()

    @app.post("/v1/devices", status_code=201)
    def register_device(body: DeviceIn, p: Principal = Depends(owner)):
        did, token = svc.auth.register(body.name, body.kind, "device", body.can_approve)
        svc.audit.record(tool="device_register", status="ok", success=True, scope="system.config",
                         action=f"registered device '{body.name}' ({body.kind})", summary=f"device #{did}", device=p.name)
        return {"id": did, "name": body.name, "token": token, "note": "This token is shown once. Store it in the device's secure storage."}

    @app.delete("/v1/devices/{did}")
    def revoke_device(did: int, p: Principal = Depends(owner)):
        try:
            if not svc.auth.revoke(did):
                raise HTTPException(404, "device not found")
        except ValueError as e:
            raise HTTPException(409, str(e))
        svc.audit.record(tool="device_revoke", status="ok", success=True, scope="system.config",
                         action=f"revoked device #{did}", summary="revoked", device=p.name)
        return {"revoked": did}

    # ---------------------------------------------------------------- security
    @app.get("/v1/security")
    def security(p: Principal = Depends(current)):
        return {
            "permissions": svc.permissions.all(svc.registry.implemented_scopes()),
            "autonomy": {"level": int(svc.autonomy.get()), "levels": {int(k): v for k, v in LEVEL_DESCRIPTIONS.items()}},
            "emergency_stop": svc.estop.state(), "standing_approvals": svc.approvals.list_standing(),
            "audit_intact": svc.audit.verify_chain()[0],
        }


    @app.put("/v1/security/permissions/{scope}")
    def set_permission(scope: str, body: GrantIn, p: Principal = Depends(owner)):
        try:
            svc.permissions.set(scope, body.grant)
        except ValueError as e:
            raise HTTPException(404, str(e))
        svc.audit.record(tool="permission_change", status="ok", success=True, scope="system.config",
                         action=f"{scope} -> {body.grant.value}", summary="permission changed", device=p.name)
        return {"scope": scope, "grant": body.grant.value}


    @app.put("/v1/security/autonomy")
    def set_autonomy(body: LevelIn, p: Principal = Depends(owner)):
        lv = svc.autonomy.set(body.level)
        svc.audit.record(tool="autonomy_change", status="ok", success=True, scope="system.config",
                         action=f"autonomy level -> {int(lv)}", summary=LEVEL_DESCRIPTIONS[lv], device=p.name)
        return {"level": int(lv), "description": LEVEL_DESCRIPTIONS[lv]}

    @app.post("/v1/security/estop")
    def estop(p: Principal = Depends(current)):   # any authenticated device may pull the brake
        svc.estop.engage(by=p.name, reason="API")
        svc.audit.record(tool="emergency_stop", status="ok", success=True, scope="system.config",
                         action="emergency stop engaged", summary=f"by {p.name}", device=p.name)
        svc.notifications.push("CRITICAL", "Emergency stop engaged", f"Triggered from {p.name}", source="security")
        return svc.estop.state()

    @app.post("/v1/security/resume")
    def resume(p: Principal = Depends(owner)):    # releasing it is owner-only
        svc.estop.release(by=p.name)
        svc.audit.record(tool="emergency_stop", status="ok", success=True, scope="system.config",
                         action="emergency stop released", summary=f"by {p.name}", device=p.name)
        return svc.estop.state()

    @app.delete("/v1/security/standing-approvals/{sid}")
    def revoke_standing(sid: int, p: Principal = Depends(owner)):
        svc.approvals.revoke_standing(sid)
        return {"revoked": sid}

    # ---------------------------------------------------------------- misc reads
    @app.get("/v1/briefing")
    def briefing(project: str | None = None, p: Principal = Depends(current)):
        return briefing_mod.build(svc, project)

    @app.get("/v1/tools")
    def tools(p: Principal = Depends(current)):
        return svc.registry.describe()

    @app.get("/v1/workflows")
    def workflows(p: Principal = Depends(current)):
        return svc.workflows.list()

    @app.post("/v1/workflows/{name}/approve")
    def approve_workflow(name: str, p: Principal = Depends(owner)):
        if not svc.workflows.approve(name):
            raise HTTPException(404, "workflow not found")
        svc.audit.record(tool="workflow_approve", status="ok", success=True, scope="workflows.run",
                         action=f"approved workflow {name}", summary="definition hash approved", device=p.name)
        return {"approved": name}

    @app.get("/v1/skills")
    def skills(p: Principal = Depends(current)):
        out = []
        for s in svc.skills.load_all():
            out.append({"name": s["name"], "domain": s["domain"], "purpose": s["purpose"], "tools": s["tools"],
                        "permissions": s["permissions"], "problems": svc.skills.validate(s)})
        return out

    @app.get("/v1/projects")
    def projects(p: Principal = Depends(current)):
        from friday.business import list_projects
        return list_projects(svc.db)

    return app
