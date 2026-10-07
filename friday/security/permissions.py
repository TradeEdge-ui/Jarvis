"""Permission scopes. Every tool declares a scope; the user decides allow / confirm / deny."""
from __future__ import annotations

from enum import Enum

from friday.db import Database


class Grant(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


# scope -> (default grant, description). Scopes without a registered tool are "reserved":
# the default is fixed now so the safe behaviour is already in place when a tool arrives.
DEFAULT_SCOPES: dict[str, tuple[Grant, str]] = {
    "filesystem.read": (Grant.ALLOW, "Read and list files in permitted folders"),
    "filesystem.write": (Grant.ALLOW, "Create/edit files in permitted folders (overwrites are backed up)"),
    "filesystem.delete": (Grant.CONFIRM, "Delete files (moved to Backups/trash, never permanently removed)"),
    "shell.diagnostics": (Grant.ALLOW, "Run read-only diagnostic commands"),
    "shell.execute": (Grant.CONFIRM, "Run arbitrary PowerShell / shell commands"),
    "apps.launch": (Grant.ALLOW, "Launch registered applications"),
    "apps.close": (Grant.CONFIRM, "Close applications"),
    "system.diagnostics": (Grant.ALLOW, "Inspect CPU, memory, disk, processes, services"),
    "system.config": (Grant.CONFIRM, "Change system or security configuration"),
    "browser.read": (Grant.ALLOW, "Open web pages and read their content"),
    "browser.navigate": (Grant.ALLOW, "Navigate pages (reserved: interactive browsing)"),
    "browser.interact": (Grant.CONFIRM, "Click / type / submit forms (reserved)"),
    "browser.purchase": (Grant.CONFIRM, "Purchases (reserved — always needs approval)"),
    "email.read": (Grant.ALLOW, "Read email (reserved)"),
    "email.draft": (Grant.ALLOW, "Draft email (reserved)"),
    "email.send": (Grant.CONFIRM, "Send email (reserved — always needs approval)"),
    "finance.research": (Grant.ALLOW, "Financial research and education"),
    "finance.analysis": (Grant.ALLOW, "Analyse supplied financial / trading data"),
    "finance.transaction": (Grant.CONFIRM, "Move money / place trades (reserved — never automatic)"),
    "memory.read": (Grant.ALLOW, "Search and read memory"),
    "memory.write": (Grant.ALLOW, "Store, correct and delete memory"),
    "tasks.read": (Grant.ALLOW, "Read tasks"),
    "tasks.write": (Grant.ALLOW, "Create and change tasks"),
    "documents.write": (Grant.ALLOW, "Generate reports and documents in the workspace"),
    "workflows.run": (Grant.ALLOW, "Run saved workflows / work modes"),
}


class PermissionStore:
    KEY = "permissions"

    def __init__(self, db: Database):
        self.db = db

    def _overrides(self) -> dict[str, str]:
        return self.db.get_setting(self.KEY, {}) or {}

    def grant_for(self, scope: str) -> Grant:
        ov = self._overrides()
        if scope in ov:
            return Grant(ov[scope])
        if scope in DEFAULT_SCOPES:
            return DEFAULT_SCOPES[scope][0]
        return Grant.CONFIRM  # unknown scope: fail closed to "ask"

    def set(self, scope: str, grant: Grant | str) -> None:
        if scope not in DEFAULT_SCOPES:
            raise ValueError(f"unknown scope: {scope}")
        g = Grant(grant)
        ov = self._overrides()
        ov[scope] = g.value
        self.db.set_setting(self.KEY, ov)

    def reset(self) -> None:
        self.db.set_setting(self.KEY, {})

    def all(self, implemented: set[str] | None = None) -> list[dict]:
        return [
            {
                "scope": s,
                "grant": self.grant_for(s).value,
                "default": d.value,
                "description": desc,
                "implemented": (s in implemented) if implemented is not None else None,
            }
            for s, (d, desc) in DEFAULT_SCOPES.items()
        ]
