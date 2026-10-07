"""The policy engine: the single place that decides whether an action may run.

The language model never makes this decision. It can only *request* a tool call; this
module (plain code, no model in the loop) answers EXECUTE / APPROVAL / PROPOSE / DENY.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum

from friday.security.approvals import ApprovalService
from friday.security.autonomy import AutonomyStore, Level
from friday.security.estop import EmergencyStop
from friday.security.permissions import Grant, PermissionStore


class Risk(IntEnum):
    SAFE = 0        # SAFE AUTOMATIC
    LOW = 1         # LOW RISK
    REVIEW = 2      # REVIEW RECOMMENDED
    MANDATORY = 3   # MANDATORY APPROVAL — asks at every autonomy level, standing approvals do not apply

    @property
    def label(self) -> str:
        return {0: "safe_automatic", 1: "low_risk", 2: "review_recommended", 3: "mandatory_approval"}[int(self)]


class Effect(str, Enum):
    READ = "read"             # observes only
    INTERNAL = "internal"     # changes FRIDAY's own state (tasks, memory, workspace reports)
    EXTERNAL = "external"     # changes the PC / outside world


@dataclass
class Classification:
    risk: Risk = Risk.SAFE
    effect: Effect = Effect.READ
    blocked: str | None = None    # non-None: this exact action is never allowed
    note: str = ""


class Verdict(str, Enum):
    EXECUTE = "execute"
    APPROVAL = "approval_required"
    PROPOSE = "proposed"
    DENY = "denied"


@dataclass
class Decision:
    verdict: Verdict
    reason: str
    scope: str
    grant: str
    risk: str
    autonomy: int


class PolicyEngine:
    def __init__(self, permissions: PermissionStore, autonomy: AutonomyStore,
                 estop: EmergencyStop, approvals: ApprovalService):
        self.permissions, self.autonomy, self.estop, self.approvals = permissions, autonomy, estop, approvals

    def decide(self, *, tool: str, scope: str, cls: Classification, fingerprint: str,
               workflow_approved: bool = False, tainted: bool = False) -> Decision:
        level = self.autonomy.get()
        grant = self.permissions.grant_for(scope)

        def d(verdict: Verdict, reason: str) -> Decision:
            return Decision(verdict, reason, scope, grant.value, cls.risk.label, int(level))

        if self.estop.engaged:
            return d(Verdict.DENY, "Emergency stop is engaged. Tool execution is disabled until it is released.")
        if cls.blocked:
            return d(Verdict.DENY, f"Blocked: {cls.blocked}")
        if grant == Grant.DENY:
            return d(Verdict.DENY, f"Permission '{scope}' is set to deny.")
        if cls.risk == Risk.MANDATORY:
            return d(Verdict.APPROVAL, cls.note or "High-impact action: explicit approval is always required.")

        standing = level >= Level.APPROVED and cls.risk <= Risk.REVIEW and \
            self.approvals.has_standing(tool, fingerprint)

        if grant == Grant.CONFIRM:
            if standing:
                return d(Verdict.EXECUTE, "Matches a standing approval for this exact action.")
            return d(Verdict.APPROVAL, f"Permission '{scope}' requires confirmation.")

        # grant == ALLOW from here
        if cls.effect == Effect.READ:
            return d(Verdict.EXECUTE, "Read-only.")

        if level == Level.OBSERVE:
            return d(Verdict.PROPOSE, "Autonomy level 0 (Observe): action is proposed, not performed.")

        if cls.effect == Effect.INTERNAL:
            if cls.risk >= Risk.REVIEW and not (workflow_approved and level >= Level.AUTONOMOUS) and not standing:
                return d(Verdict.APPROVAL, cls.note or "Review recommended before changing this.")
            return d(Verdict.EXECUTE, "Change to FRIDAY's own data.")

        # EXTERNAL effect
        if level == Level.ASSIST:
            return d(Verdict.APPROVAL, "Autonomy level 1 (Assist): external actions wait for your approval.")
        if tainted:
            return d(Verdict.APPROVAL, "This follows reading untrusted content (web page / external text), "
                                       "so the action needs your approval.")
        if cls.risk <= Risk.LOW:
            return d(Verdict.EXECUTE, "Low-risk action allowed at this autonomy level.")
        if standing or (workflow_approved and level >= Level.AUTONOMOUS):
            return d(Verdict.EXECUTE, "Pre-approved (standing approval or approved workflow).")
        return d(Verdict.APPROVAL, cls.note or "Review-level action: approval required.")
