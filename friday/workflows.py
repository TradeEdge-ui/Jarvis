"""Workflows / work modes: named sequences of tool calls stored as JSON in Automations/.

Each step still goes through the full policy pipeline. A workflow only gets `workflow_approved` (which lets
review-level steps run at autonomy >= 3) if the owner approved this exact definition — editing it revokes approval.
"""
from __future__ import annotations

import json
from pathlib import Path

from friday.util import canonical_json
import hashlib

DEFAULTS = {
    "work_mode": {
        "description": "Start the working day: briefing, then the ranked next actions.",
        "steps": [{"tool": "briefing", "args": {}}, {"tool": "task_list", "args": {"view": "next"}}],
    },
    "business_mode": {
        "description": "CyZy business focus: project briefing, blockers, and (optional) business apps.",
        "steps": [{"tool": "briefing", "args": {"project": "CyZy"}},
                  {"tool": "task_list", "args": {"view": "blocked", "project": "CyZy"}},
                  {"tool": "app_open", "args": {"app": "telegram"}, "optional": True},
                  {"tool": "app_open", "args": {"app": "chrome"}, "optional": True}],
    },
    "trading_research_mode": {
        "description": "Prepare for market research: briefing and your browser. Add your chart/journal apps by editing this file.",
        "steps": [{"tool": "briefing", "args": {}},
                  {"tool": "app_open", "args": {"app": "chrome"}, "optional": True}],
    },
    "study_mode": {
        "description": "Focused learning session: next actions and a browser.",
        "steps": [{"tool": "task_list", "args": {"view": "next"}},
                  {"tool": "app_open", "args": {"app": "chrome"}, "optional": True}],
    },
    "creator_mode": {
        "description": "Content creation: next actions and your editor.",
        "steps": [{"tool": "task_list", "args": {"view": "next"}},
                  {"tool": "app_open", "args": {"app": "notepad"}, "optional": True}],
    },
    "personal_mode": {
        "description": "Personal view: what is due today.",
        "steps": [{"tool": "task_list", "args": {"view": "today"}}],
    },
}


def _hash(defn: dict) -> str:
    return hashlib.sha256(canonical_json(defn.get("steps", [])).encode()).hexdigest()


class Workflows:
    def __init__(self, svc):
        self.svc = svc

    @property
    def dir(self) -> Path:
        return self.svc.config.path("Automations")

    def ensure_defaults(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for name, d in DEFAULTS.items():
            f = self.dir / f"{name}.json"
            if not f.exists():
                f.write_text(json.dumps({"name": name, **d}, indent=2), encoding="utf-8")

    def load(self, name: str) -> dict | None:
        key = name.strip().lower().replace(" ", "_").replace("-", "_")
        for cand in (key, key if key.endswith("_mode") else key + "_mode"):
            f = self.dir / f"{cand}.json"
            if f.is_file():
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except ValueError:
                    return None
                d.setdefault("name", cand)
                return d
        return None

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            d.setdefault("name", f.stem)
            d["approved"] = self.is_approved(d)
            out.append(d)
        return out

    def is_approved(self, defn: dict) -> bool:
        return (self.svc.db.get_setting("workflow_approvals", {}) or {}).get(defn["name"]) == _hash(defn)

    def approve(self, name: str) -> bool:
        d = self.load(name)
        if not d:
            return False
        ap = self.svc.db.get_setting("workflow_approvals", {}) or {}
        ap[d["name"]] = _hash(d)
        self.svc.db.set_setting("workflow_approvals", ap)
        return True
