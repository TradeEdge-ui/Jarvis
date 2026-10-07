from __future__ import annotations

from friday.tools.base import Tool


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

    def implemented_scopes(self) -> set[str]:
        out: set[str] = set()
        for t in self._tools.values():
            out.add(t.scope)
            out.update(t.extra_scopes)
        return out

    def describe(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "scope": t.scope, "group": t.group,
                 "untrusted_output": t.untrusted_output} for t in self._tools.values()]
