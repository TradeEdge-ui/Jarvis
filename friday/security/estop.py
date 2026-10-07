"""Emergency stop. Persisted, so a restart does not silently re-enable tool execution."""
from __future__ import annotations

from friday.clock import Clock, iso
from friday.db import Database


class EmergencyStop:
    KEY = "emergency_stop"

    def __init__(self, db: Database, clock: Clock):
        self.db = db
        self.clock = clock

    @property
    def engaged(self) -> bool:
        return bool((self.db.get_setting(self.KEY) or {}).get("engaged"))

    def state(self) -> dict:
        return self.db.get_setting(self.KEY) or {"engaged": False}

    def engage(self, by: str, reason: str = "") -> None:
        self.db.set_setting(self.KEY, {"engaged": True, "by": by, "reason": reason, "at": iso(self.clock.now())})
        # an emergency stop also kills anything waiting for approval
        self.db.execute("UPDATE approvals SET status='cancelled', decided_at=?, decided_by=? WHERE status IN ('pending','approved')",
                        (iso(self.clock.now()), f"estop:{by}"))

    def release(self, by: str) -> None:
        self.db.set_setting(self.KEY, {"engaged": False, "released_by": by, "at": iso(self.clock.now())})
