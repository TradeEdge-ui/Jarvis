"""Tool contract. Every tool declares: input schema, scope (permission), classification, run, verify."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from friday.security.policy import Classification

if TYPE_CHECKING:
    from friday.services import Services


@dataclass
class Verification:
    verified: bool | None      # True = independently confirmed, False = checked and failed, None = n/a
    method: str = ""
    detail: str = ""

    def as_text(self) -> str:
        state = {True: "verified", False: "VERIFICATION FAILED", None: "not applicable"}[self.verified]
        return f"{state} ({self.method}){': ' + self.detail if self.detail else ''}"


@dataclass
class ToolResult:
    status: str                          # ok | failed | unverified | denied | approval_required | proposed | skipped
    summary: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""
    verification: Verification | None = None
    approval_id: int | None = None
    attempts: int = 1
    retryable: bool = False
    decision: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @classmethod
    def success(cls, summary: str, **data: Any) -> "ToolResult":
        return cls("ok", summary, data)

    @classmethod
    def fail(cls, error: str, retryable: bool = False, **data: Any) -> "ToolResult":
        return cls("failed", error, data, error=error, retryable=retryable)

    def to_dict(self) -> dict:
        return {
            "status": self.status, "summary": self.summary, "error": self.error, "data": self.data,
            "verification": None if not self.verification else {
                "verified": self.verification.verified, "method": self.verification.method,
                "detail": self.verification.detail},
            "approval_id": self.approval_id, "attempts": self.attempts, "decision": self.decision,
        }


@dataclass
class ToolContext:
    svc: "Services"
    user_command: str = ""
    conversation_id: str = ""
    device: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    workflow_approved: bool = False
    tainted: bool = False          # set once untrusted external content has entered this run
    reason: str = ""

    @property
    def now(self):
        return self.svc.clock.now()


class Tool:
    name: ClassVar[str]
    description: ClassVar[str]
    scope: ClassVar[str]
    Args: ClassVar[type[BaseModel]]
    untrusted_output: ClassVar[bool] = False
    max_attempts: ClassVar[int] = 1
    # which user-facing capability area this belongs to (for the UI)
    group: ClassVar[str] = "general"

    extra_scopes: ClassVar[tuple[str, ...]] = ()

    def scope_for(self, args: BaseModel) -> str:
        """Permission scope for this particular invocation (a tool may span several scopes)."""
        return self.scope

    def classify(self, args: BaseModel, ctx: ToolContext) -> Classification:
        return Classification()

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def verify(self, args: BaseModel, result: ToolResult, ctx: ToolContext) -> Verification:
        return Verification(None, "n/a", "read-only or self-evident")

    def action_text(self, args: BaseModel) -> str:
        """Short human description used in approvals and the audit log."""
        return f"{self.name}({', '.join(f'{k}={str(v)[:80]!r}' for k, v in args.model_dump().items() if v not in (None, '', []))})"

    # -- schema for the model --
    def schema(self) -> dict:
        def clean(o):
            if isinstance(o, dict):
                return {k: clean(v) for k, v in o.items() if k != "title"}
            if isinstance(o, list):
                return [clean(v) for v in o]
            return o
        params = clean(self.Args.model_json_schema())
        params.setdefault("properties", {})["reason"] = {
            "type": "string", "description": "Why this action is being taken (recorded in the audit log)."}
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": params}}
