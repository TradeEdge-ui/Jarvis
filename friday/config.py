"""Runtime configuration: environment variables + an optional Core/config.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE_DIRS = [
    "Core", "Memory", "Projects", "Projects/CyZy", "Finance", "Business",
    "Business/Reports", "Documents", "Research", "Automations", "Logs",
    "Skills", "Backups",
]


def _split_route(value: str | None, default_provider: str, default_model: str) -> tuple[str, str]:
    """'provider:model' or just 'model'."""
    if not value:
        return default_provider, default_model
    if ":" in value and value.split(":", 1)[0] in {"ollama", "openai", "rules"}:
        p, m = value.split(":", 1)
        return p, m
    return default_provider, value


@dataclass
class Config:
    home: Path
    tz: str = "Asia/Kuala_Lumpur"
    host: str = "127.0.0.1"
    port: int = 8765
    model_provider: str = "auto"          # auto | ollama | openai | rules
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""
    routes: dict[str, tuple[str, str]] = field(default_factory=dict)  # kind -> (provider, model)
    max_steps: int = 8
    default_autonomy: int = 2
    read_roots: list[Path] = field(default_factory=list)
    write_roots: list[Path] = field(default_factory=list)
    allow_local_browse: bool = False
    app_overrides: dict = field(default_factory=dict)
    tls_cert: str = ""
    tls_key: str = ""

    # ---- derived paths -------------------------------------------------
    def path(self, name: str) -> Path:
        return self.home / name

    @property
    def db_path(self) -> Path:
        return self.home / "Memory" / "friday.db"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    # ---- construction --------------------------------------------------
    @classmethod
    def from_env(cls, home: str | Path | None = None, env: dict | None = None) -> "Config":
        e = os.environ if env is None else env
        home_p = Path(home or e.get("FRIDAY_HOME") or Path.home() / "FRIDAY").expanduser().resolve()
        ollama_model = e.get("FRIDAY_OLLAMA_MODEL", "qwen2.5:7b-instruct")
        provider = e.get("FRIDAY_MODEL_PROVIDER", "auto").lower()
        cfg = cls(
            home=home_p,
            tz=e.get("FRIDAY_TZ", "Asia/Kuala_Lumpur"),
            host=e.get("FRIDAY_HOST", "127.0.0.1"),
            port=int(e.get("FRIDAY_PORT", "8765")),
            model_provider=provider,
            ollama_url=e.get("FRIDAY_OLLAMA_URL", "http://localhost:11434").rstrip("/"),
            ollama_model=ollama_model,
            openai_base_url=e.get("FRIDAY_OPENAI_BASE_URL", "").rstrip("/"),
            openai_api_key=e.get("FRIDAY_OPENAI_API_KEY", ""),
            openai_model=e.get("FRIDAY_OPENAI_MODEL", ""),
            max_steps=int(e.get("FRIDAY_MAX_STEPS", "8")),
            default_autonomy=int(e.get("FRIDAY_AUTONOMY", "2")),
            tls_cert=e.get("FRIDAY_TLS_CERT", ""),
            tls_key=e.get("FRIDAY_TLS_KEY", ""),
        )
        # Per-task model routes. Only routes the user sets explicitly are stored; anything else uses the active backend.
        base_p = "openai" if provider == "openai" else "ollama"
        for kind in ("simple", "reasoning", "coding", "vision"):
            raw = e.get(f"FRIDAY_MODEL_{kind.upper()}")
            if raw:
                cfg.routes[kind] = _split_route(raw, base_p, raw)

        # default filesystem roots: the workspace plus the usual user folders that exist
        user = Path.home()
        extra = [user / n for n in ("Documents", "Desktop", "Downloads") if (user / n).is_dir()]
        cfg.read_roots = [home_p, *extra]
        cfg.write_roots = [home_p, *extra]
        cfg._load_file()
        return cfg

    def _load_file(self) -> None:
        f = self.home / "Core" / "config.json"
        if not f.is_file():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if "read_roots" in data:
            self.read_roots = [self.home, *[Path(p).expanduser().resolve() for p in data["read_roots"]]]
        if "write_roots" in data:
            self.write_roots = [self.home, *[Path(p).expanduser().resolve() for p in data["write_roots"]]]
        self.allow_local_browse = bool(data.get("allow_local_browse", self.allow_local_browse))
        self.app_overrides = data.get("apps", {})

    def ensure_workspace(self) -> None:
        for d in WORKSPACE_DIRS:
            (self.home / d).mkdir(parents=True, exist_ok=True)
