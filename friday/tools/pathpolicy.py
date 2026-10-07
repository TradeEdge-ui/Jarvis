"""Filesystem sandbox: which paths FRIDAY tools may read/write. Resolves symlinks before checking."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

SECRET_NAMES = [".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*", "id_ecdsa*",
                "*.kdbx", ".netrc", ".git-credentials", "credentials", "credentials.json", "token.json",
                "owner.token", "Login Data", "Cookies", "*.keystore"]
SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker"}


class PathDenied(PermissionError):
    pass


class PathPolicy:
    def __init__(self, config):
        self.config = config

    # -- resolution --
    def resolve(self, raw: str) -> Path:
        s = (raw or "").strip().strip('"')
        if not s:
            raise PathDenied("empty path")
        low = s.lower().rstrip("/\\")
        user = Path.home()
        aliases = {"desktop": user / "Desktop", "downloads": user / "Downloads", "documents": user / "Documents"}
        if low in aliases and aliases[low].exists():
            return aliases[low].resolve()
        p = Path(os.path.expandvars(s)).expanduser()
        if not p.is_absolute():
            p = self.config.home / p
        return p.resolve()

    # -- checks --
    def _protected(self, p: Path) -> str | None:
        home = self.config.home
        try:
            if p == home / "Core" or p.is_relative_to(home / "Core"):
                return "FRIDAY's Core/ folder holds credentials and configuration"
            if p.parent == home / "Memory" and p.name.startswith("friday.db"):
                return "FRIDAY's own database is not accessible through file tools"
        except ValueError:
            pass
        for part in p.parts:
            if part.lower() in SECRET_DIRS:
                return f"'{part}' folders contain credentials"
        name = p.name
        for pat in SECRET_NAMES:
            if fnmatch.fnmatch(name.lower(), pat.lower()):
                return f"'{name}' looks like a credential/secret file"
        return None

    @staticmethod
    def _within(p: Path, roots: list[Path]) -> bool:
        return any(p == r or p.is_relative_to(r) for r in roots)

    def check_read(self, raw: str) -> Path:
        p = self.resolve(raw)
        why = self._protected(p)
        if why:
            raise PathDenied(f"Access to {p} denied: {why}.")
        if not self._within(p, self.config.read_roots):
            raise PathDenied(f"{p} is outside the folders FRIDAY may read. Permitted: "
                             + ", ".join(str(r) for r in self.config.read_roots))
        return p

    def check_write(self, raw: str) -> Path:
        p = self.resolve(raw)
        why = self._protected(p)
        if why:
            raise PathDenied(f"Writing {p} denied: {why}.")
        if not self._within(p, self.config.write_roots):
            raise PathDenied(f"{p} is outside the folders FRIDAY may write. Permitted: "
                             + ", ".join(str(r) for r in self.config.write_roots))
        return p

    def in_workspace(self, p: Path) -> bool:
        return p == self.config.home or p.is_relative_to(self.config.home)
