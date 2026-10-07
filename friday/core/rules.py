"""Offline rules planner — FRIDAY's fallback "brain" when no language model is reachable.

It implements the same ModelProvider interface as Ollama/OpenAI, so the agent loop, permission policy, execution,
verification and audit are exactly the same code path. It is NOT a language model: it understands a fixed set of
natural-language command shapes (listed in `CAPABILITIES`) and tells the user plainly when it cannot understand.
Reports are rendered strictly from tool results, so they cannot claim anything the tools did not return.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta

from friday.core.models import ModelProvider, ModelResponse, ToolCall
from friday.timeparse import parse_when, strip_phrases
from friday.tools.apps import find_app, load_registry

CAPABILITIES = """I'm running without a language model, so I only understand direct commands such as:
- "open notepad and create a file saying hello"  ·  "open chrome"  ·  "create a file called plan.txt saying ..."
- "remind me tomorrow to finish the CyZy proposal"  ·  "add <task> to my priority list"
- "what do I need to do today?"  ·  "what should I do next?"  ·  "what is blocking CyZy?"  ·  "mark task 3 done"
- "remember that ..."  ·  "what do you remember about ..."  ·  "executive briefing"  ·  "start work mode"
- "system status"  ·  "Ollama isn't connecting"  ·  "analyze my last 30 XAUUSD trades in <file>"
- "prepare the CyZy operations report"  ·  "total Revenue in <file.csv>"  ·  "list files in <folder>"  ·  "emergency stop"
For open-ended conversation, research and reasoning, install Ollama (https://ollama.com), pull a model that supports tools
(e.g. `ollama pull qwen2.5:7b-instruct`) and FRIDAY will use it automatically."""

_LEAD = re.compile(r"^\s*(?:hey\s+|ok\s+|okay\s+)?friday\s*[,:!.\-]*\s*", re.I)
_POLITE = re.compile(r"^(?:please\s+|can you\s+|could you\s+|would you\s+|will you\s+|i want you to\s+|i need you to\s+)+", re.I)


def normalise(text: str) -> str:
    t = _LEAD.sub("", text.strip())
    t = _POLITE.sub("", t)
    t = re.sub(r"\s+(?:please|for me|now)\s*$", "", t, flags=re.I)
    return t.strip().rstrip(".!").strip()


def _unq(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`“”":
        return s[1:-1]
    return s.strip("\"'`“”")


def _slug(s: str, n: int = 24) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n].strip("-")) or "note"


def _tc(tool: str, /, **args) -> ToolCall:
    """Build a tool call. `tool` is positional-only so a tool argument may itself be called `name`."""
    return ToolCall(f"call_{uuid.uuid4().hex[:8]}", tool, {k: v for k, v in args.items() if v is not None})


class RulesProvider(ModelProvider):
    name = "rules"
    model = "offline-rules"

    def __init__(self, svc):
        self.svc = svc

    # ------------------------------------------------------------------ entry
    def chat(self, messages, tools, system=""):
        last = messages[-1]
        if last["role"] == "tool":
            return ModelResponse(self._report(messages), [], self.model, self.name)
        plan = self._plan(last["content"])
        if isinstance(plan, str):
            return ModelResponse(plan, [], self.model, self.name)
        if plan is None:
            return ModelResponse("I didn't understand that as one of my direct commands.\n\n" + CAPABILITIES, [], self.model, self.name)
        return ModelResponse("", plan, self.model, self.name)

    # ------------------------------------------------------------------ helpers
    @property
    def _zone(self):
        return self.svc.config.zone

    def _projects(self) -> list[str]:
        rows = self.svc.db.query("SELECT name FROM projects ORDER BY length(name) DESC")
        return [r["name"] for r in rows]

    def _project_in(self, text: str) -> str | None:
        for p in self._projects():  # longest first so "CyZy FX" wins over "CyZy"
            if re.search(r"\b" + re.escape(p) + r"\b", text, re.I):
                return p
        return None

    def _local_iso(self, dt) -> str:
        return dt.astimezone(self._zone).isoformat(timespec="minutes")

    def _parse_file_clause(self, rest: str):
        name = None
        m = re.search(r"(?:called|named|name)\s+(\"[^\"]+\"|'[^']+'|\S+)", rest, re.I)
        if m:
            name = _unq(m.group(1)).rstrip(",.")
        m2 = re.search(r"(?:saying|that says|which says|with(?:\s+the)?\s+(?:text|content|message|words)|containing|says|reading)\s*[:\-]?\s*(.+)$",
                       rest, re.I | re.S)
        content = _unq(m2.group(1)) if m2 else None
        return name, content

    # ------------------------------------------------------------------ planning
    def _plan(self, raw: str):
        t = normalise(raw)
        low = t.lower()
        if not t:
            return "I'm here. What do you need?"

        if re.fullmatch(r"(help|what can you do|capabilities|commands)\??", low):
            return CAPABILITIES

        # ---- workflows / modes
        m = re.fullmatch(r"(?:start|run|activate|enter|begin|switch to|launch)\s+(?:my\s+)?([\w ]+?)\s+mode", low)
        if m:
            return [_tc("workflow_run", name=m.group(1).strip() + " mode", reason="user asked to start a mode")]

        # ---- briefing
        if re.search(r"\b(executive briefing|daily briefing|morning briefing|brief me|give me a briefing|briefing)\b", low) or \
                re.fullmatch(r"good morning\W*", low) or re.search(r"what'?s important( today)?", low):
            return [_tc("briefing", project=self._project_in(t), reason="briefing requested")]

        # ---- open app (+ create file)
        m = re.match(r"(?:open|launch|start|run)\s+(?:up\s+)?(?:the\s+)?(?P<app>[\w .+-]+?)\s+(?:and|then)\s+"
                     r"(?:create|write|make|save|type)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:text\s+)?(?:file|note|document)\b(?P<rest>.*)$",
                     t, re.I | re.S)
        if m:
            reg = load_registry(self.svc.config.app_overrides)
            spec = find_app(reg, m.group("app"))
            if not spec:
                return f"I don't have an application called '{m.group('app')}' registered. Known: {', '.join(sorted(reg))}."
            name, content = self._parse_file_clause(m.group("rest"))
            if content is None:
                return "What should the file say?"
            stamp = self.svc.clock.now().astimezone(self._zone).strftime("%H%M%S")
            path = name or f"Documents/note-{_slug(content, 20)}-{stamp}.txt"
            if "." not in path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
                path += ".txt"
            return [_tc("fs_write", path=path, content=content, reason=f"create the file the user asked for before opening {spec.name}"),
                    _tc("app_open", app=spec.name, target=path, reason=f"open {spec.name} on the new file")]

        m = re.match(r"(?:create|write|make|save)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:text\s+)?(?:file|note)\b(?P<rest>.*)$", t, re.I | re.S)
        if m:
            name, content = self._parse_file_clause(m.group("rest"))
            if content is None:
                return "What should the file say?"
            stamp = self.svc.clock.now().astimezone(self._zone).strftime("%H%M%S")
            path = name or f"Documents/note-{_slug(content, 20)}-{stamp}.txt"
            if "." not in path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
                path += ".txt"
            return [_tc("fs_write", path=path, content=content, reason="user asked to create a file")]

        m = re.fullmatch(r"(?:close|quit|exit|kill)\s+(?:the\s+)?([\w .+-]+?)", low)
        if m and find_app(load_registry(self.svc.config.app_overrides), m.group(1)):
            return [_tc("app_close", app=m.group(1), reason="user asked to close the app")]

        m = re.fullmatch(r"(?:open|launch|start|run)\s+(?:up\s+)?(?:the\s+)?(?P<app>[\w .+-]+?)(?:\s+(?:and\s+)?(?:go to|visit|navigate to|with|to open)\s+(?P<target>\S+))?", t, re.I)
        if m:
            reg = load_registry(self.svc.config.app_overrides)
            spec = find_app(reg, m.group("app"))
            if spec:
                target = m.group("target")
                if target and not target.lower().startswith("http") and re.match(r"^[\w-]+(\.[\w-]+)+(/.*)?$", target):
                    target = "https://" + target
                return [_tc("app_open", app=spec.name, target=target, reason="user asked to open the application")]
            if not re.search(r"\b(folder|directory|file)\b", low):
                return f"I don't have an application called '{m.group('app')}' registered. Known: {', '.join(sorted(reg))}."

        # ---- reminders / tasks
        m = re.match(r"remind me\s+(?P<rest>.+)$", t, re.I)
        if m:
            rest = m.group("rest")
            pt = parse_when(rest, self.svc.clock.now(), self._zone)
            title = strip_phrases(rest, pt.matched) if pt else rest
            title = re.sub(r"^(?:to|that|about)\s+", "", title, flags=re.I).strip()
            if not title:
                return "What should I remind you about?"
            if not pt:
                return f"When should I remind you to {title}? (e.g. 'tomorrow 9am', 'in 2 hours', 'friday')"
            when = self._local_iso(pt.when)
            return [_tc("task_add", title=title[0].upper() + title[1:], deadline=when, remind_at=when,
                        project=self._project_in(t), reason="user asked for a reminder")]

        m = re.match(r"add\s+(?P<t>.+?)\s+(?:to|on|in)\s+(?:my\s+)?(?P<p>priority\s+)?(?:to-?do\s+)?(?:list|tasks?|todos?)$", t, re.I)
        if m or re.match(r"(?:new\s+)?task\s*[:\-]\s*.+", t, re.I):
            title = m.group("t") if m else re.sub(r"^(?:new\s+)?task\s*[:\-]\s*", "", t, flags=re.I)
            pt = parse_when(title, self.svc.clock.now(), self._zone)
            if pt:
                title = strip_phrases(title, pt.matched)
            title = _unq(title)
            return [_tc("task_add", title=title[:1].upper() + title[1:], priority=2 if (m and m.group("p")) else 3,
                        deadline=self._local_iso(pt.when) if pt else None, project=self._project_in(t),
                        reason="user asked to add a task")]

        if re.search(r"what(?:'s| is| do i need| should i do| do i have| am i doing)?\b.*\b(today|on my plate|on my list|to do)\b", low) or \
                re.fullmatch(r"(?:show\s+)?(?:my\s+)?(?:tasks|to-?do list|todos?|task list)(?: for today)?", low):
            view = "today" if "today" in low else "open"
            return [_tc("task_list", view=view, project=self._project_in(t), reason="user asked what is on their list")]
        if re.search(r"what should i (?:do|work on) next|what'?s next|next action|what now", low):
            return [_tc("task_list", view="next", project=self._project_in(t), reason="user asked what to do next")]
        m = re.search(r"what(?:'s| is)\s+blocking\s*(?P<p>.*)$", low)
        if m:
            return [_tc("task_list", view="blocked", project=self._project_in(t), reason="user asked what is blocking")]
        if re.search(r"what did i (?:accomplish|complete|finish|get done|do) today|what have i (?:done|completed) today", low):
            return [_tc("task_list", view="done_today", reason="end-of-day review")]

        m = re.fullmatch(r"(?:mark\s+)?(?:task\s+)?#?(\d+)\s+(?:as\s+)?(?:done|complete|completed|finished)", low) or \
            re.fullmatch(r"(?:i\s+)?(?:finished|completed|done with|complete|finish)\s+(?:task\s+)?#?(\d+)", low)
        if m:
            return [_tc("task_complete", id=int(m.group(1)), reason="user marked the task done")]
        m = re.fullmatch(r"(?:i\s+)?(?:finished|completed|done with|complete)\s+(?:the\s+)?(.+)", low)
        if m and not re.search(r"\b(today|report)\b", low):
            return [_tc("task_complete", title_contains=m.group(1), reason="user marked the task done")]

        m = re.fullmatch(r"(?:move|postpone|push|reschedule)\s+(?:task\s+)?#?(\d+)\s+(?:to|until|till|for)\s+(.+)", low)
        if m:
            pt = parse_when(m.group(2), self.svc.clock.now(), self._zone)
            if not pt:
                return f"I couldn't understand '{m.group(2)}' as a time."
            when = self._local_iso(pt.when)
            return [_tc("task_update", id=int(m.group(1)), deadline=when, remind_at=when, reason="user rescheduled the task")]

        # ---- memory
        m = re.match(r"(?:remember|note|keep in mind)\s+(?:that\s+)?(?P<f>.+)$", t, re.I | re.S)
        if m:
            fact = m.group("f").strip()
            proj = self._project_in(fact)
            biz = bool(proj) or bool(re.search(r"\b(customer|client|invoice|revenue|lead|supplier|pricing|product|kpi)\b", fact, re.I))
            return [_tc("memory_remember", content=fact[0].upper() + fact[1:] if fact else fact,
                        category="business" if biz else "long_term", project=proj, reason="user asked FRIDAY to remember this")]
        m = re.fullmatch(r"forget\s+(?:memory\s+)?#?(\d+)", low)
        if m:
            return [_tc("memory_forget", id=int(m.group(1)), reason="user asked to forget this memory")]
        m = re.match(r"(?:what do you (?:remember|know)(?: about)?|do you remember|recall|search memory(?: for)?)\s*(?P<q>.+?)\??$", t, re.I)
        if m:
            return [_tc("memory_search", query=m.group("q"), reason="user asked what FRIDAY remembers")]

        # ---- system / diagnostics
        if re.search(r"ollama", low) and re.search(r"(isn'?t|not|won'?t|can'?t|cannot|doesn'?t|fail|down|broken|check|diagnos|status|running)", low):
            return [_tc("service_check", service="ollama", reason="diagnose Ollama connectivity")]
        if re.search(r"(system status|pc status|how(?:'s| is) my (?:pc|computer|system|machine|laptop)|check (?:my )?(?:cpu|memory|ram|disk|system|pc|computer)|diagnos\w* my (?:pc|computer|system))", low):
            return [_tc("system_info", reason="user asked for system status")]

        # ---- finance
        m = re.search(r"analy[sz]e\s+(?:my\s+)?(?:last\s+(?P<n>\d+)\s+)?(?:(?P<sym>[A-Za-z]{6})\s+)?(?:trades|journal|trading journal)"
                      r"(?:\s+(?:in|from|at|of)\s+(?P<path>\S+))?", t, re.I)
        if m:
            path = m.group("path")
            if not path:
                fin = self.svc.config.path("Finance")
                cands = sorted([p for p in fin.glob("*") if p.suffix.lower() in (".csv", ".xlsx")], key=lambda p: p.stat().st_mtime, reverse=True)
                if not cands:
                    return "Which file is your trading journal? Give me the path to a CSV/XLSX (or put it in the Finance folder)."
                path = str(cands[0])
            return [_tc("finance_journal_analyze", path=_unq(path), last=int(m.group("n")) if m.group("n") else None,
                        symbol=m.group("sym").upper() if m.group("sym") else None, reason="user asked for a journal analysis")]

        # ---- reports & spreadsheets
        m = re.search(r"(?:prepare|generate|create|make|write|build)\s+(?:me\s+)?(?:today'?s\s+|the\s+|a\s+|my\s+|our\s+)?(?:weekly\s+|daily\s+)?(.*?)\s*(?:operations\s+)?report\b", t, re.I)
        if m:
            return [_tc("report_generate", kind="operations", project=self._project_in(t), reason="user asked for a report")]
        m = re.match(r"(?P<op>total|sum|average|mean|max|maximum|min|minimum|count)\s+(?:of\s+)?(?:the\s+)?(?P<col>[\w ]+?)\s+(?:in|from|of)\s+(?P<path>\S+\.(?:csv|xlsx|xlsm|tsv))"
                     r"(?:\s+(?:for|in)\s+(?P<month>\d{4}-\d{2}))?$", t, re.I)
        if m:
            op = {"total": "sum", "sum": "sum", "average": "mean", "mean": "mean", "max": "max", "maximum": "max",
                  "min": "min", "minimum": "min", "count": "count"}[m.group("op").lower()]
            return [_tc("spreadsheet_calc", path=_unq(m.group("path")), column=m.group("col").strip(), op=op,
                        month=m.group("month"), date_column="Date" if m.group("month") else None, reason="user asked for a calculation")]
        m = re.match(r"(?:summari[sz]e|describe|inspect)\s+(?:the\s+)?(?:spreadsheet|sheet|file|csv)\s+(?P<path>\S+)$", t, re.I)
        if m:
            return [_tc("spreadsheet_summary", path=_unq(m.group("path")), reason="user asked about a spreadsheet")]

        # ---- web (before files: "example.com" looks like a filename)
        m = re.match(r"(?:read|summari[sz]e|fetch|get)\s+(?:the\s+)?(?:page\s+|website\s+|url\s+)?(?P<u>https?://\S+)$", t, re.I)
        if m:
            return [_tc("browser_read", url=m.group("u"), reason="user asked to read a web page")]

        # ---- files
        m = re.match(r"(?:list|show)\s+(?:me\s+)?(?:the\s+)?(?:files|contents|everything)\s+(?:in|of|inside)\s+(?:my\s+|the\s+)?(?P<p>.+?)(?:\s+(?:folder|directory))?$", t, re.I) or \
            re.match(r"(?:open|show)\s+(?:my\s+|the\s+)?(?P<p>.+?)\s+(?:folder|directory)$", t, re.I)
        if m:
            return [_tc("fs_list", path=_unq(m.group("p")), reason="user asked to see a folder")]
        m = re.match(r"(?:find|search for|locate)\s+(?:files?\s+)?(?:named\s+|called\s+|matching\s+)?(?P<pat>\S+)(?:\s+(?:in|under|inside)\s+(?P<root>.+))?$", t, re.I)
        if m:
            pat = _unq(m.group("pat"))
            if "*" not in pat and "?" not in pat:
                pat = f"*{pat}*"
            return [_tc("fs_search", root=_unq(m.group("root") or "."), name_pattern=pat, reason="user asked to find files")]
        m = re.match(r"(?:read|show|display|cat|print|view)\s+(?:me\s+)?(?:the\s+)?(?:contents?\s+of\s+)?(?:file\s+)?(?P<p>\S+\.\w{1,6})$", t, re.I)
        if m:
            return [_tc("fs_read", path=_unq(m.group("p")), reason="user asked to read a file")]

        return None

    # ------------------------------------------------------------------ reporting
    def _report(self, messages) -> str:
        i = max(j for j, m in enumerate(messages) if m["role"] == "user")
        results = []
        for m in messages[i + 1:]:
            if m["role"] == "tool":
                try:
                    d = json.loads(m["content"])
                except ValueError:
                    d = {"status": "failed", "summary": m["content"][:200]}
                results.append((m.get("name", ""), d))
        return "\n".join(filter(None, (self._render(n, d) for n, d in results))) or "Done."

    def _render(self, name: str, d: dict) -> str:
        st, summ, data = d.get("status"), d.get("summary", ""), d.get("data") or {}
        v = d.get("verification")
        if st == "approval_required":
            return (f"Approval required (#{d.get('approval_id')}): {summ.split('): ', 1)[-1]}\n"
                    f"Reply \"approve {d.get('approval_id')}\" to proceed or \"deny {d.get('approval_id')}\" to cancel.")
        if st == "denied":
            return f"Denied: {summ}"
        if st == "proposed":
            return summ
        if st == "skipped":
            return f"Skipped {name}: an earlier step did not complete."
        if st == "unverified":
            return f"I could not confirm this worked — {d.get('error') or summ}"
        if st == "failed":
            return f"Failed: {summ}"
        # ok
        body = self._render_data(name, summ, data)
        if v and v.get("verified") is True:
            return f"{body}\n  ✓ Verified: {v.get('method')} — {v.get('detail')}"
        if v and v.get("verified") is None and name in ("shell_run",):
            return f"{body}\n  (not independently verified: {v.get('detail')})"
        return body

    def _render_data(self, name: str, summ: str, data: dict) -> str:
        if name == "task_list":
            rows = data.get("tasks", [])
            if not rows:
                return {"today": "Nothing is due today.", "next": "No actionable tasks recorded.",
                        "blocked": "Nothing is blocked.", "done_today": "Nothing completed yet today."}.get(data.get("view"), "No tasks.")
            lines = [summ + ":"]
            for t in rows:
                extra = f" — due {t['due']}" if t.get("due") else ""
                wait = f" (waiting on: {', '.join(w['title'] for w in t['waiting_on'])})" if t.get("waiting_on") else ""
                lines.append(f"  #{t['id']} [{t['priority']}] {t['title']}{extra}{wait}")
            return "\n".join(lines)
        if name == "memory_search":
            rows = data.get("results", [])
            if not rows:
                return "I don't have anything stored about that."
            return summ + ":\n" + "\n".join(f"  #{r['id']} ({r.get('category', 'ops')}) {r.get('content') or r.get('summary') or r.get('user_command')}"[:220] for r in rows)
        if name == "briefing":
            return data["briefing"]["text"]
        if name == "system_info":
            tp = data.get("top_processes", [])
            return summ + ("\n  Top memory: " + ", ".join(f"{p['name']} {p['rss_mb']}MB" for p in tp) if tp else "")
        if name == "service_check":
            lines = [summ]
            if data.get("likely_causes"):
                lines += ["  Likely cause: " + c for c in data["likely_causes"]]
            if data.get("suggested_fixes"):
                lines += ["  Suggested (not run): " + f for f in data["suggested_fixes"]]
            return "\n".join(lines)
        if name == "finance_journal_analyze":
            r = data["report"]
            out = [summ, "OBSERVATIONS:"] + [f"  • {x}" for x in r["observations"]]
            if r["interpretations"]:
                out += ["INTERPRETATIONS (hypotheses, not facts):"] + [f"  • {x}" for x in r["interpretations"]]
            out += ["RECOMMENDATIONS:"] + [f"  • {x}" for x in r["recommendations"]]
            if r["caveats"] or r.get("warnings"):
                out += ["CAVEATS:"] + [f"  • {x}" for x in r["caveats"] + r.get("warnings", [])]
            out.append(r["disclaimer"])
            return "\n".join(out)
        if name in ("fs_list",):
            items = data.get("items", [])
            return summ + ("\n" + "\n".join(f"  {'[dir] ' if i['type'] == 'dir' else ''}{i['name']}  ({i['size']} B)" for i in items[:40]) if items else "")
        if name == "fs_search":
            items = data.get("items", [])
            return summ + ("\n" + "\n".join(f"  {i['path']}  ({i['modified']})" for i in items[:20]) if items else "")
        if name == "fs_read":
            return f"{summ}:\n{data.get('content', '')[:3000]}"
        if name == "workflow_run":
            lines = [summ.split("\n")[0]]
            for s in data.get("steps", []):
                mark = {"ok": "✓", "skipped": "–"}.get(s["status"], "✗" if not s["optional"] else "·")
                lines.append(f"  {mark} {s['tool']}: {s['status']}" + (f" — {s['summary'][:100]}" if s["status"] != "ok" else ""))
            ex = [s for s in data.get("steps", []) if s["tool"] == "briefing" and s["status"] == "ok"]
            if ex and ex[0].get("data", {}).get("briefing"):
                lines.append(ex[0]["data"]["briefing"]["text"])
            return "\n".join(lines)
        if name == "browser_read":
            return f"{summ}\n{data.get('text', '')[:1500]}"
        return summ
