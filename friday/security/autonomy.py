from __future__ import annotations

from enum import IntEnum

from friday.db import Database


class Level(IntEnum):
    OBSERVE = 0      # analyse and recommend only
    ASSIST = 1       # prepare actions, wait for the user to approve each external one
    APPROVED = 2     # low-risk actions run automatically; review-level actions ask
    AUTONOMOUS = 3   # approved workflows may run review-level actions without asking
    EXECUTIVE = 4    # like 3, plus continuous monitoring on defined rules (monitoring: not yet built)


LEVEL_DESCRIPTIONS = {
    Level.OBSERVE: "Observe — analyse and recommend; no changes are made",
    Level.ASSIST: "Assist — actions are prepared and wait for your approval",
    Level.APPROVED: "Approved automation — low-risk actions run automatically",
    Level.AUTONOMOUS: "Autonomous operations — approved workflows run without asking each time",
    Level.EXECUTIVE: "Executive — rule-based monitoring (monitoring engine not built yet)",
}


class AutonomyStore:
    KEY = "autonomy_level"

    def __init__(self, db: Database, default: int = 2):
        self.db = db
        self.default = default

    def get(self) -> Level:
        return Level(int(self.db.get_setting(self.KEY, self.default)))

    def set(self, level: int | Level) -> Level:
        lv = Level(int(level))
        self.db.set_setting(self.KEY, int(lv))
        return lv
