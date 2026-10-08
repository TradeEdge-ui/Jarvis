from __future__ import annotations

import re

from friday.tools.base import Tool

# Always offered to the model regardless of what the message is about: the core personal-assistant verbs
# (open/read/write/list files, launch apps, tasks, memory, briefing, system status). Small/local models were
# observed (real Ollama testing) to hallucinate or deny having tools that exist once the full tool list grows
# large; keeping these always present means the headline PC-control scenarios never depend on keyword matching.
ALWAYS_AVAILABLE = (
    "briefing", "task_list", "task_add", "task_update", "task_complete",
    "memory_search", "memory_remember",
    "fs_list", "fs_read", "fs_write", "fs_search",
    "app_open", "system_info",
)

_STOP = {"a", "an", "the", "and", "or", "but", "if", "then", "of", "to", "in", "on", "at", "for", "with", "about",
         "from", "by", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these", "those",
         "i", "me", "my", "we", "our", "you", "your", "do", "does", "did", "please", "can", "could", "would",
         "should", "will", "shall", "may", "might", "friday", "now", "me,", "a,"}
_TOKEN = re.compile(r"[A-Za-z0-9]+")  # underscore is NOT a word char here, so "business_mode" splits into "business", "mode"


def _terms(text: str) -> set[str]:
    return {t for t in (m.lower() for m in _TOKEN.findall(text)) if t not in _STOP and len(t) > 1}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self, names: list[str] | None = None) -> list[dict]:
        return [t.schema() for t in self._tools.values() if names is None or t.name in names]

    def relevant(self, text: str, limit: int = 20) -> list[str]:
        """Tool names to offer the model for this message: the always-available core set, plus whichever other
        tools best match the message by keyword overlap with their name/description/group, up to `limit` total.

        This exists because small/local models were observed to perform worse (hallucinated or denied tools) when
        handed the full tool list on every request; trimming it is a standard mitigation. `limit` is generous
        enough that it rarely excludes something genuinely needed — when in doubt, callers can pass names=None
        to ToolRegistry.schemas() to get everything.
        """
        always = [n for n in ALWAYS_AVAILABLE if n in self._tools]
        terms = _terms(text)
        scored = []
        for t in self._tools.values():
            if t.name in always:
                continue
            hay = _terms(t.name.replace("_", " ") + " " + t.description + " " + t.group)
            score = len(terms & hay)
            if score:
                scored.append((score, t.name))
        scored.sort(key=lambda x: (-x[0], x[1]))
        extra = [name for _, name in scored[:max(0, limit - len(always))]]
        return always + extra

    def implemented_scopes(self) -> set[str]:
        out: set[str] = set()
        for t in self._tools.values():
            out.add(t.scope)
            out.update(t.extra_scopes)
        return out

    def describe(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "scope": t.scope, "group": t.group,
                 "untrusted_output": t.untrusted_output} for t in self._tools.values()]
