"""Skills: modular capability packs. A skill declares purpose, tools, permissions, instructions, inputs, outputs
and validation, and lives as files under Skills/<domain>/<name>/ (skill.json + instructions.md)."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from friday.security.permissions import DEFAULT_SCOPES

BUILTIN_DIR = Path(__file__).parent / "skills_builtin"


class SkillLibrary:
    def __init__(self, svc):
        self.svc = svc

    @property
    def dir(self) -> Path:
        return self.svc.config.path("Skills")

    def install_builtin(self) -> None:
        if not BUILTIN_DIR.is_dir():
            return
        for src in BUILTIN_DIR.glob("*/*"):
            if not (src / "skill.json").is_file():
                continue
            dest = self.dir / src.parent.name / src.name
            if not dest.exists():
                shutil.copytree(src, dest)

    def load_all(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*/*/skill.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            instr = f.parent / d.get("instructions_file", "instructions.md")
            d["instructions"] = instr.read_text(encoding="utf-8") if instr.is_file() else d.get("instructions", "")
            d["domain"] = f.parent.parent.name
            d["path"] = str(f.parent)
            out.append(d)
        return out

    def relevant(self, text: str, limit: int = 2) -> list[dict]:
        low = text.lower()
        scored = []
        for s in self.load_all():
            hits = sum(1 for kw in s.get("triggers", []) if re.search(r"\b" + re.escape(kw.lower()), low))
            if hits:
                scored.append((hits, s))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:limit]]

    def validate(self, skill: dict) -> list[str]:
        problems = []
        for key in ("name", "purpose", "tools", "permissions", "inputs", "outputs", "validation"):
            if key not in skill:
                problems.append(f"missing field '{key}'")
        for t in skill.get("tools", []):
            if not self.svc.registry.get(t):
                problems.append(f"unknown tool '{t}'")
        for s in skill.get("permissions", []):
            if s not in DEFAULT_SCOPES:
                problems.append(f"unknown permission scope '{s}'")
        if not skill.get("instructions"):
            problems.append("no instructions")
        return problems
