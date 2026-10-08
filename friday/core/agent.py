"""The agent loop:  INPUT → UNDERSTAND → RETRIEVE → PLAN → PERMISSION CHECK → EXECUTE → OBSERVE → VERIFY → REPORT → REMEMBER.

Plan/understand is delegated to a ModelProvider. Permission checks, execution, verification and audit happen in plain code
(`Executor`), so a model can never grant itself access, and a report never claims more than the tool results show.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from friday.core.models import ModelError, classify_task
from friday.core.persona import build_system_prompt
from friday.core.rules import RulesProvider
from friday.security.approvals import ApprovalError
from friday.security.autonomy import LEVEL_DESCRIPTIONS
from friday.tools.base import ToolContext, ToolResult
from friday.util import redact

TOOL_MSG_LIMIT = 8000
HISTORY_LIMIT = 20

# The <untrusted_content> marker belongs only inside tool results (see tool_message_content below). A model was
# observed (real Ollama testing, qwen3:8b) echoing it into its OWN reply after reading the persona rule that merely
# describes the marker. Wording the rule more carefully helps, but this strips it defensively either way so a
# prompt-level slip can never reach the user as a confusing raw tag.
_LEAKED_TAG = re.compile(r"\s*</?untrusted_content>\s*", re.I)


def strip_leaked_tags(text: str) -> str:
    return _LEAKED_TAG.sub(" ", text).strip() if text else text

_ESTOP = re.compile(r"^(?:emergency\s*stop|e-?stop|stop everything|halt everything|abort everything|kill switch|stop all (?:actions|tasks|automation|tools))\b", re.I)
_RESUME = re.compile(r"\b(resume|release|disable|lift|clear|reset|turn off)\b.*\b(emergency|e-?stop)\b|\b(emergency|e-?stop)\b.*\b(resume|release|disable|lift|clear|reset|off)\b", re.I)
_YES = re.compile(r"^(?:yes|yep|yeah|approve|approved|go ahead|do it|confirm|confirmed|proceed|ok|okay|sure|yes please)\W*$", re.I)
_NO = re.compile(r"^(?:no|nope|deny|denied|cancel|reject|don'?t|do not|stop|never mind|nevermind)\W*$", re.I)
_APPROVE_N = re.compile(r"^(approve|deny|reject|cancel)\s+#?(\d+)(\s+always)?\W*$", re.I)
_PENDING = re.compile(r"(pending approvals?|waiting (?:for|on) (?:my )?approval|what needs my approval|approvals?)\??$", re.I)


@dataclass
class AgentResult:
    conversation_id: str
    reply: str
    status: str                                  # info | completed | partial | failed | approval_required | blocked
    actions: list[dict] = field(default_factory=list)
    pending_approvals: list[dict] = field(default_factory=list)
    model: str = ""
    degraded: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ------------------------------------------------------------------ tool-message serialisation
def _shrink(o, s_lim=800, l_lim=20):
    if isinstance(o, str):
        return o if len(o) <= s_lim else o[:s_lim] + f"…[+{len(o) - s_lim} chars]"
    if isinstance(o, list):
        return [_shrink(x, s_lim, l_lim) for x in o[:l_lim]] + ([f"…[+{len(o) - l_lim} more]"] if len(o) > l_lim else [])
    if isinstance(o, dict):
        return {k: _shrink(v, s_lim, l_lim) for k, v in o.items()}
    return o


def tool_message_content(res: ToolResult, untrusted: bool) -> str:
    data = dict(res.data)
    data.pop("traceback", None)
    if untrusted:
        for k in ("text",):
            if isinstance(data.get(k), str):
                data[k] = f"<untrusted_content>\n{data[k]}\n</untrusted_content>"
    d = {"status": res.status, "summary": res.summary, "error": res.error or None,
         "verification": None if not res.verification else {"verified": res.verification.verified,
                                                            "method": res.verification.method, "detail": res.verification.detail},
         "approval_id": res.approval_id, "attempts": res.attempts, "data": data}
    s = json.dumps(d, default=str, ensure_ascii=False)
    if len(s) > TOOL_MSG_LIMIT:
        d["data"] = _shrink(data)
        s = json.dumps(d, default=str, ensure_ascii=False)
    if len(s) > TOOL_MSG_LIMIT:
        d["data"] = {"omitted": "result too large; see summary"}
        s = json.dumps(d, default=str, ensure_ascii=False)
    return s


class Agent:
    def __init__(self, svc, router):
        self.svc = svc
        self.router = router

    # ============================================================== public
    def handle(self, text: str, *, conversation_id: str | None = None, device: str = "", can_approve: bool = True) -> AgentResult:
        svc = self.svc
        text = (text or "").strip()
        cid = svc.conversations.resolve(conversation_id, device)
        svc.conversations.add(cid, "user", text, device)
        ctx = ToolContext(svc=svc, user_command=text, conversation_id=cid, device=device)

        fast = self._fast_path(text, ctx, can_approve)
        if fast is not None:
            svc.conversations.add(cid, "assistant", fast.reply, device, {"status": fast.status, "actions": fast.actions})
            return fast

        kind = classify_task(text)
        provider, backend = self.router.provider_for(kind)
        history = svc.conversations.history(cid, HISTORY_LIMIT + 1)[:-1]   # everything before this message
        messages = [{"role": m["role"], "content": m["content"]} for m in history if m["role"] in ("user", "assistant") and m["content"]]
        messages.append({"role": "user", "content": text})

        system = self._system_prompt(text, device)
        tool_schemas = svc.registry.schemas(svc.registry.relevant(text))
        actions: list[dict] = []
        pending: list[dict] = []
        seen: dict[str, int] = {}
        final = ""

        try:
            for _step in range(svc.config.max_steps):
                resp = provider.chat(messages, tool_schemas, system)
                resp.text = strip_leaked_tags(resp.text)
                if not resp.tool_calls:
                    final = resp.text
                    break
                messages.append({"role": "assistant", "content": resp.text,
                                 "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in resp.tool_calls]})
                halted = False
                for call in resp.tool_calls:
                    key = call.name + json.dumps(call.arguments, sort_keys=True, default=str)
                    seen[key] = seen.get(key, 0) + 1
                    if halted:
                        res = ToolResult("skipped", "Skipped because an earlier step in this plan did not complete.")
                    elif seen[key] > 2:
                        res = ToolResult("skipped", "Skipped: this exact call was already tried twice in this request.")
                    else:
                        res = svc.executor.run(call.name, dict(call.arguments), ctx)
                    tool = svc.registry.get(call.name)
                    actions.append(self._action(call, res))
                    if res.status == "approval_required":
                        ap = svc.approvals.get(res.approval_id)
                        if ap:
                            pending.append(self._pending(ap))
                    if not res.ok:
                        halted = True
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                     "content": tool_message_content(res, bool(tool and tool.untrusted_output))})
            else:
                final = ("I stopped after reaching the step limit for one request "
                         f"({svc.config.max_steps}). Here is what happened so far:\n" + self._ledger(actions))
        except ModelError as e:
            final = f"The language model failed ({e}). No further actions were taken in this request."
        except Exception as e:   # a bug in a planner/provider must never take the request (or the server) down
            svc.audit.record(tool="agent", status="failed", success=False, action="internal error",
                             summary=f"{type(e).__name__}: {e}", user_command=text, device=device, run_id=ctx.run_id)
            final = (f"Something went wrong inside FRIDAY ({type(e).__name__}: {e}). It has been logged. "
                     "Nothing further was done for this request.")

        status = self._status(actions, pending)
        reply = self._ground(final, actions, pending, provider.name)
        svc.conversations.add(cid, "assistant", reply, device, {"status": status, "actions": actions, "model": backend.get("model")})
        return AgentResult(cid, reply, status, actions, pending, f"{backend.get('provider')}:{backend.get('model')}",
                           bool(backend.get("degraded")))

    # ============================================================== fast paths (never reach a model)
    def _fast_path(self, text: str, ctx: ToolContext, can_approve: bool) -> AgentResult | None:
        svc, cid, device = self.svc, ctx.conversation_id, ctx.device
        low = re.sub(r"^\s*(?:hey\s+)?friday\s*[,:!.\-]*\s*", "", text, flags=re.I).strip()

        def out(reply, status="info", actions=None, pending=None):
            return AgentResult(cid, reply, status, actions or [], pending or [], "system")

        if _ESTOP.match(low) and len(low.split()) <= 8:
            svc.estop.engage(by=device or "chat", reason=text)
            svc.audit.record(tool="emergency_stop", status="ok", success=True, scope="system.config",
                             action="emergency stop engaged", summary=f"Engaged by {device or 'chat'}: {text[:100]}",
                             user_command=text, device=device, permission="always allowed")
            svc.notifications.push("CRITICAL", "Emergency stop engaged", "All tool execution is disabled.", source="security")
            return out("Emergency stop engaged. All tool execution is disabled and pending approvals were cancelled. "
                       "I cannot release it from chat — use the control panel or `friday resume`.", "blocked")

        bare_resume = svc.estop.engaged and re.match(r"^(resume|unlock|release|re-?enable|continue|restart|start again|go ahead and resume)\W*$", low, re.I)
        if (bare_resume or _RESUME.search(low)) and len(low.split()) <= 10:
            return out("I can't release the emergency stop from chat, by design. Use the control panel (Security) or "
                       "run `friday resume` on the PC.", "blocked")

        m = _APPROVE_N.match(low)
        yes, no = bool(_YES.match(low)), bool(_NO.match(low))
        pend = svc.approvals.list("pending") if (m or yes or no or _PENDING.search(low)) else []
        if _PENDING.search(low) and not (m or yes or no):
            if not pend:
                return out("Nothing is waiting for approval.")
            lines = [f"  #{a['id']} {a['tool']}: {self._approval_text(a)}  ({a['risk']})" for a in pend]
            return out("Waiting for your approval:\n" + "\n".join(lines) + "\nSay \"approve <id>\" or \"deny <id>\".", "approval_required",
                       pending=[self._pending(a) for a in pend])
        if m or yes or no:
            if not pend and not m:
                return None   # a bare "yes"/"no" with nothing pending is ordinary conversation
            if not can_approve:
                return out("This device is not allowed to approve actions.", "blocked")
            if m:
                verb, aid, always = m.group(1).lower(), int(m.group(2)), bool(m.group(3))
                approve = verb == "approve"
            else:
                approve = yes
                always = False
                if len(pend) != 1:
                    lines = [f"  #{a['id']} {a['tool']}: {self._approval_text(a)}" for a in pend]
                    return out("More than one action is waiting. Say \"approve <id>\" or \"deny <id>\":\n" + "\n".join(lines),
                               "approval_required", pending=[self._pending(a) for a in pend])
                aid = pend[0]["id"]
            ap = svc.approvals.get(aid)
            if ap and approve and ap["risk"] == "mandatory_approval" and not m:
                return out(f"#{aid} is a high-impact action ({self._approval_text(ap)}). Say \"approve {aid}\" to confirm it explicitly.",
                           "approval_required", pending=[self._pending(ap)])
            try:
                if not approve:
                    svc.executor.deny(aid, by=device or "chat")
                    return out(f"Denied #{aid}. Nothing was done.")
                ap, res = svc.executor.approve_and_run(aid, by=device or "chat", remember=always)
            except ApprovalError as e:
                return out(str(e), "blocked")
            rendered = RulesProvider(svc)._render(ap["tool"], res.to_dict())
            action = {"tool": ap["tool"], "args": ap["display_args"], "status": res.status, "summary": res.summary,
                      "verified": None if not res.verification else res.verification.verified,
                      "verification": None if not res.verification else res.verification.as_text(), "approval_id": aid}
            head = f"Approved #{aid}." + (" I'll remember this exact action as pre-approved." if always else "")
            return out(f"{head}\n{rendered}", "completed" if res.ok else "failed", [action])
        return None

    # ============================================================== helpers
    def _system_prompt(self, text: str, device: str) -> str:
        svc = self.svc
        now_local = svc.clock.now().astimezone(svc.config.zone).strftime("%A %d %B %Y %H:%M")
        mems = svc.memory.pinned(4)
        have = {m["id"] for m in mems}
        mems += [m for m in svc.memory.search(text, limit=5) if m["id"] not in have]
        lvl = svc.autonomy.get()
        return build_system_prompt(
            now_local=now_local, tz=svc.config.tz, device=device, autonomy=int(lvl), autonomy_text=LEVEL_DESCRIPTIONS[lvl],
            estop=svc.estop.engaged, memories=mems, skills=svc.skills.relevant(text), next_tasks=svc.tasks.next_actions(3))

    def _action(self, call, res: ToolResult) -> dict:
        v = res.verification
        args = {k: (v_ if not isinstance(v_, str) or len(v_) < 200 else v_[:200] + "…") for k, v_ in call.arguments.items()}
        return {"tool": call.name, "args": json.loads(redact(json.dumps(args, default=str))),
                "status": res.status, "summary": res.summary, "error": res.error or None,
                "verified": None if not v else v.verified, "verification": None if not v else v.as_text(),
                "approval_id": res.approval_id, "attempts": res.attempts,
                "risk": res.decision.get("risk") if res.decision else None}

    @staticmethod
    def _approval_text(ap: dict) -> str:
        a = ap["display_args"]
        if ap["tool"] == "shell_run":
            return a.get("command", "")
        return ", ".join(f"{k}={str(v)[:60]}" for k, v in a.items())

    @staticmethod
    def _pending(ap: dict) -> dict:
        return {"id": ap["id"], "tool": ap["tool"], "args": ap["display_args"], "risk": ap["risk"],
                "why": ap["why_needed"], "expires": ap["expires"]}

    @staticmethod
    def _status(actions: list[dict], pending: list[dict]) -> str:
        if not actions:
            return "info"
        sts = [a["status"] for a in actions]
        if pending:
            return "approval_required"
        if all(s == "ok" for s in sts):
            return "completed"
        if "denied" in sts and not any(s == "ok" for s in sts):
            return "blocked"
        if any(s == "ok" for s in sts):
            return "partial"
        return "failed"

    @staticmethod
    def _ledger(actions: list[dict]) -> str:
        lines = []
        for a in actions:
            mark = {"ok": "✓" if a["verified"] else "•", "approval_required": "⏸", "denied": "⛔", "proposed": "💡"}.get(a["status"], "✗")
            lines.append(f"{mark} {a['tool']}: {a['status']}" + (f" (verified)" if a["verified"] else "") + f" — {a['summary'][:140]}")
        return "\n".join(lines)

    def _ground(self, text: str, actions: list[dict], pending: list[dict], provider_name: str) -> str:
        """The reply is the planner's text; if a *language model* wrote it, append a factual ledger whenever any
        action did not fully succeed so the reply can never silently overstate what happened."""
        text = (text or "").strip()
        problems = [a for a in actions if a["status"] != "ok" or a["verified"] is False]
        if provider_name == "rules" or not actions:
            return text or ("Done." if actions else "…")
        if not text:
            text = "Here is what happened:"
        if problems:
            text += "\n\n— Action status (from tool results) —\n" + self._ledger(actions)
        if pending:
            text += "\nApproval required. " + " ".join(f"Say \"approve {p['id']}\" or \"deny {p['id']}\"." for p in pending)
        return text
