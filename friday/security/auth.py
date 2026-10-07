"""Device registration and bearer-token authentication. Only SHA-256 hashes of tokens are stored."""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from friday.clock import Clock, iso, parse_iso
from friday.db import Database

OWNER, DEVICE = "owner", "device"


@dataclass
class Principal:
    device_id: int
    name: str
    role: str
    can_approve: bool

    @property
    def is_owner(self) -> bool:
        return self.role == OWNER


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DeviceRegistry:
    def __init__(self, db: Database, clock: Clock):
        self.db = db
        self.clock = clock

    def _new_token(self) -> str:
        return "fri_" + secrets.token_urlsafe(32)

    def register(self, name: str, kind: str = "other", role: str = DEVICE, can_approve: bool = True) -> tuple[int, str]:
        """Returns (device_id, plaintext_token). The token is shown once and never stored."""
        if role not in (OWNER, DEVICE):
            raise ValueError("role must be owner or device")
        token = self._new_token()
        cur = self.db.execute(
            "INSERT INTO devices(name,kind,role,token_hash,can_approve,created) VALUES (?,?,?,?,?,?)",
            (name, kind, role, _hash(token), int(can_approve), iso(self.clock.now())),
        )
        return int(cur.lastrowid), token

    def ensure_owner(self, token_file: Path) -> str | None:
        """Create the owner device on first run. Returns the token only when newly created."""
        if self.db.one("SELECT 1 FROM devices WHERE role='owner' AND revoked=0"):
            return None
        _, token = self.register("owner-console", "pc", OWNER, True)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        return token

    def authenticate(self, token: str | None) -> Principal | None:
        if not token:
            return None
        row = self.db.one("SELECT * FROM devices WHERE token_hash=? AND revoked=0", (_hash(token),))
        if not row:
            return None
        now = self.clock.now()
        if not row["last_seen"] or (now - parse_iso(row["last_seen"])).total_seconds() > 60:
            self.db.execute("UPDATE devices SET last_seen=? WHERE id=?", (iso(now), row["id"]))
        return Principal(row["id"], row["name"], row["role"], bool(row["can_approve"]))

    def list(self) -> list[dict]:
        return [{k: r[k] for k in ("id", "name", "kind", "role", "can_approve", "created", "last_seen", "revoked")}
                for r in self.db.query("SELECT * FROM devices ORDER BY id")]

    def revoke(self, device_id: int) -> bool:
        row = self.db.one("SELECT role FROM devices WHERE id=?", (device_id,))
        if not row:
            return False
        if row["role"] == OWNER:
            n = self.db.one("SELECT COUNT(*) c FROM devices WHERE role='owner' AND revoked=0")["c"]
            if n <= 1:
                raise ValueError("cannot revoke the last owner device")
        self.db.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,))
        return True
