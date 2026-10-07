"""The execution pipeline: validate -> classify -> policy -> (approval) -> run -> VERIFY -> audit."""
from __future__ import annotations

import json
import time
import traceback

from pydantic import ValidationError

from friday.security.policy import Verdict
from friday.security.approvals import ApprovalError
from friday.tools.base import Tool, ToolContext, ToolResult, Verification
from friday.tools.registry import ToolRegistry
from friday.util import fingerprint, redact, truncate


class Executor:
    def __init__(self, svc):
        self.svc = svc

    @property
    def registry(self) -> ToolRegistry:
        return self.svc.registry

    # ------------------------------------------------------------------
    def run(self, name: str, raw_args: dict | None, ctx: ToolContext, *, approval: dict | None = None) -> ToolResult:
        raw = dict(raw_args or {})
        ctx.reason = str(raw.pop("reason", "") or ctx.reason or "")
        tool = self.registry.get(name)
        if tool is None:
            res = ToolResult("failed", f"Unknown tool '{name}'.", error=f"unknown tool '{name}'")
            self._audit(ctx, name, None, "", res, {}, "")
            return res
        try:
            args = tool.Args(**raw)
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            res = ToolResult("failed", f"Invalid arguments for {name}: {msg}", error=f"invalid arguments: {msg}")
            self._audit(ctx, name, tool, json.dumps(raw, default=str), res, {}, "")
            return res

        norm = args.model_dump()
        fp = fingerprint(name, norm)
        action = redact(tool.action_text(args))
        try:
            cls = tool.classify(args, ctx)
        except Exception as e:  # classification must never crash the pipeline; fail closed
            res = ToolResult("failed", f"Could not classify action: {e}", error=str(e))
            self._audit(ctx, name, tool, action, res, {}, json.dumps(norm, default=str))
            return res

        scope = tool.scope_for(args)
        decision = self.svc.policy.decide(tool=name, scope=scope, cls=cls, fingerprint=fp,
                                          workflow_approved=ctx.workflow_approved, tainted=ctx.tainted)
        dec = {"verdict": decision.verdict.value, "reason": decision.reason, "grant": decision.grant,
               "risk": decision.risk, "autonomy": decision.autonomy}

        verdict = decision.verdict
        if approval is not None and verdict in (Verdict.APPROVAL, Verdict.PROPOSE):
            if approval["fingerprint"] == fp and approval["status"] == "approved":
                verdict = Verdict.EXECUTE
                dec["reason"] = f"Explicitly approved (approval #{approval['id']} by {approval['decided_by']})."

        args_json = json.dumps(norm, default=str)
        if verdict == Verdict.DENY:
            res = ToolResult("denied", decision.reason, error=decision.reason, decision=dec)
            self._audit(ctx, name, tool, action, res, dec, args_json, scope)
            return res
        if verdict == Verdict.PROPOSE:
            res = ToolResult("proposed", f"Proposed (not performed): {action}. {decision.reason}", decision=dec,
                             data={"proposed_action": action})
            self._audit(ctx, name, tool, action, res, dec, args_json, scope)
            return res
        if verdict == Verdict.APPROVAL:
            ap = self.svc.approvals.create(
                tool=name, args=norm, fingerprint=fp, scope=scope, risk=decision.risk, reason=ctx.reason,
                why_needed=decision.reason, user_command=ctx.user_command, conversation_id=ctx.conversation_id,
                device=ctx.device)
            self.svc.notifications.push(
                "ACTION_REQUIRED", f"Approval required: {action[:90]}", decision.reason, source="approvals",
                dedupe_key=f"approval:{ap['id']}", ref={"approval_id": ap["id"]})
            res = ToolResult("approval_required",
                             f"Approval required (#{ap['id']}): {action}. {decision.reason}",
                             approval_id=ap["id"], decision=dec, data={"approval_id": ap["id"], "action": action})
            self._audit(ctx, name, tool, action, res, dec, args_json, scope)
            return res

        # ---- EXECUTE ----
        res = self._execute(tool, args, ctx)
        res.decision = dec
        if tool.untrusted_output and res.ok:
            ctx.tainted = True
        self._audit(ctx, name, tool, action, res, dec, args_json, scope)
        return res

    # ------------------------------------------------------------------
    def _execute(self, tool: Tool, args, ctx: ToolContext) -> ToolResult:
        res: ToolResult | None = None
        for attempt in range(1, max(1, tool.max_attempts) + 1):
            t0 = time.monotonic()
            try:
                res = tool.run(args, ctx)
            except Exception as e:
                res = ToolResult("failed", f"{type(e).__name__}: {e}", error=f"{type(e).__name__}: {e}",
                                 data={"traceback": truncate(traceback.format_exc(), 1500)})
            res.attempts = attempt
            res.data.setdefault("duration_ms", int((time.monotonic() - t0) * 1000))
            if res.ok or not res.retryable:
                break
            time.sleep(min(0.2 * attempt, 1.0))
        assert res is not None
        if res.ok:
            try:
                res.verification = tool.verify(args, res, ctx)
            except Exception as e:
                res.verification = Verification(False, "verifier crashed", f"{type(e).__name__}: {e}")
            if res.verification.verified is False:
                res.status = "unverified"
                res.error = f"Action reported success but verification failed: {res.verification.detail}"
        return res

    # ------------------------------------------------------------------
    def _audit(self, ctx: ToolContext, name: str, tool: Tool | None, action: str, res: ToolResult,
               dec: dict, args_json: str, scope: str = "") -> None:
        v = res.verification
        self.svc.audit.record(
            tool=name, status=res.status, success=res.ok, scope=scope,
            action=action, args=args_json, summary=res.summary or res.error,
            verified=v.verified if v else None, verification=v.as_text() if v else "",
            permission=(f"grant={dec.get('grant')};verdict={dec.get('verdict')};reason={dec.get('reason')}"
                        if dec else ""),
            risk=dec.get("risk", ""), autonomy=dec.get("autonomy"), run_id=ctx.run_id, device=ctx.device,
            user_command=ctx.user_command, reason=ctx.reason)

    # ------------------------------------------------------------------
    def approve_and_run(self, approval_id: int, by: str, *, remember: bool = False) -> tuple[dict, ToolResult]:
        """Human approval path. Returns (approval_row, result)."""
        if self.svc.estop.engaged:
            raise ApprovalError("Emergency stop is engaged; release it before approving actions.")
        ap = self.svc.approvals.decide(approval_id, True, by)
        ctx = ToolContext(svc=self.svc, user_command=ap["user_command"] or "", conversation_id=ap["conversation_id"] or "",
                          device=by, reason=ap["reason"] or "")
        if remember and ap["risk"] != "mandatory_approval":
            self.svc.approvals.add_standing(ap["tool"], ap["fingerprint"])
        res = self.run(ap["tool"], {**ap["args"]}, ctx, approval=ap)
        self.svc.approvals.finish(approval_id, res.ok, res.to_dict())
        return self.svc.approvals.get(approval_id), res

    def deny(self, approval_id: int, by: str) -> dict:
        ap = self.svc.approvals.decide(approval_id, False, by)
        self.svc.audit.record(tool=ap["tool"], status="denied", success=False, scope=ap["scope"] or "",
                              action=f"approval #{approval_id} denied by {by}", summary="Denied by user",
                              user_command=ap["user_command"] or "", device=by)
        return ap
