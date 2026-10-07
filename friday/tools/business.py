"""Business tools: briefing, operations report, workflows."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from friday import briefing as briefing_mod
from friday.business import operations_report
from friday.security.policy import Classification, Effect, Risk
from friday.tools.base import Tool, ToolContext, ToolResult, Verification


class Briefing(Tool):
    name = "briefing"
    scope = "tasks.read"
    group = "business"
    description = ("Executive briefing: priorities, deadlines, overdue/blocked work, pending approvals, alerts and "
                   "a recommended next action. Concise. Optionally for one project (e.g. 'CyZy').")

    class Args(BaseModel):
        project: str | None = None

    def run(self, args, ctx):
        d = briefing_mod.build(ctx.svc, args.project)
        return ToolResult.success(d["text"], briefing=d)


class ReportGenerate(Tool):
    name = "report_generate"
    scope = "documents.write"
    group = "business"
    description = "Generate an operations report (Markdown) for a project or all projects and save it under Business/Reports."

    class Args(BaseModel):
        kind: Literal["operations"] = "operations"
        project: str | None = Field(default=None, description="e.g. 'CyZy'")

    def classify(self, args, ctx):
        return Classification(Risk.LOW, Effect.INTERNAL)

    def run(self, args, ctx):
        md = operations_report(ctx.svc, args.project)
        slug = re.sub(r"[^a-z0-9]+", "-", (args.project or "all").lower()).strip("-")
        out_dir = ctx.svc.config.path("Business") / "Reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{ctx.now.astimezone(ctx.svc.config.zone).strftime('%Y-%m-%d_%H%M')}-{slug}-operations.md"
        p.write_text(md, encoding="utf-8")
        return ToolResult.success(f"Saved operations report: {p}", path=str(p), chars=len(md),
                                  headline=[ln for ln in md.splitlines() if ln.startswith("- ")][:2])

    def verify(self, args, result, ctx):
        p = Path(result.data["path"])
        if not p.is_file():
            return Verification(False, "file exists check", "report file missing")
        txt = p.read_text(encoding="utf-8")
        need = ["## Summary", "## Next priorities", "## Not covered"]
        missing = [h for h in need if h not in txt]
        return Verification(not missing, "re-read + section check",
                            f"{len(txt)} chars, all sections present" if not missing else f"missing sections: {missing}")


class WorkflowRun(Tool):
    name = "workflow_run"
    scope = "workflows.run"
    group = "business"
    description = ("Run a saved workflow / work mode by name: work_mode, business_mode, trading_research_mode, "
                   "study_mode, creator_mode, personal_mode (or any file in Automations/). Each step is still permission-checked.")

    class Args(BaseModel):
        name: str

    def classify(self, args, ctx):
        if not ctx.svc.workflows.load(args.name):
            return Classification(blocked=f"no workflow named '{args.name}'")
        return Classification(Risk.SAFE, Effect.READ)

    def run(self, args, ctx):
        wf = ctx.svc.workflows.load(args.name)
        approved = ctx.svc.workflows.is_approved(wf)
        inner = ToolContext(svc=ctx.svc, user_command=ctx.user_command, conversation_id=ctx.conversation_id,
                            device=ctx.device, run_id=ctx.run_id, workflow_approved=approved, tainted=ctx.tainted,
                            reason=f"workflow {wf['name']}")
        steps_out, blocked = [], False
        for i, step in enumerate(wf.get("steps", []), 1):
            if blocked:
                steps_out.append({"step": i, "tool": step["tool"], "status": "skipped", "summary": "earlier step did not complete",
                                  "verified": None, "optional": bool(step.get("optional")), "approval_id": None, "data": {}})
                continue
            res = ctx.svc.executor.run(step["tool"], dict(step.get("args", {})), inner)
            steps_out.append({"step": i, "tool": step["tool"], "status": res.status, "summary": res.summary[:300],
                              "verified": None if not res.verification else res.verification.verified,
                              "optional": bool(step.get("optional")), "approval_id": res.approval_id,
                              "data": res.data if step["tool"] in ("briefing", "task_list") else {}})
            if not res.ok and not step.get("optional"):
                blocked = True
        ctx.tainted = inner.tainted
        required = [s for s in steps_out if not s["optional"]]
        ok_req = all(s["status"] == "ok" for s in required)
        ok_n = sum(1 for s in steps_out if s["status"] == "ok")
        text = f"{wf['name']}: {ok_n}/{len(steps_out)} steps completed" + ("" if ok_req else " — a required step did not complete")
        extra = [s["summary"] for s in steps_out if s["status"] == "ok" and s["tool"] in ("briefing",)]
        status_fn = ToolResult.success if ok_req else (lambda s, **d: ToolResult("failed", s, d, error=s))
        return status_fn(text + ("\n" + extra[0] if extra else ""), workflow=wf["name"], steps=steps_out,
                         approved_workflow=approved)

    def verify(self, args, result, ctx):
        steps = result.data["steps"]
        bad = [s for s in steps if s["status"] not in ("ok", "skipped") and not s["optional"]]
        unv = [s for s in steps if s["status"] == "ok" and s["verified"] is False]
        return Verification(not bad and not unv, "per-step results", f"{len(steps)} step(s) executed through the policy pipeline")


TOOLS = [Briefing, ReportGenerate, WorkflowRun]
