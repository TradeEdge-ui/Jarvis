"""Background checks that turn time passing into notifications. Deliberately conservative: no notification spam."""
from __future__ import annotations

import threading
from datetime import timedelta

from friday.clock import parse_iso


class Scheduler:
    def __init__(self, svc, interval_s: float = 30.0):
        self.svc, self.interval = svc, interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        """One pass. Returns the number of notifications created."""
        svc, n = self.svc, 0
        now = svc.clock.now()
        z = svc.config.zone
        for t in svc.tasks.due_reminders():
            when = parse_iso(t["deadline"]).astimezone(z).strftime("%a %d %b %H:%M") if t["deadline"] else "now"
            made = svc.notifications.push(
                "IMPORTANT" if t["priority"] <= 2 else "ACTION_REQUIRED", f"Reminder: {t['title']}",
                f"Task #{t['id']}" + (f" · {t['project']}" if t["project"] else "") + f" · due {when}",
                source="tasks", dedupe_key=f"reminder:{t['id']}:{t['remind_at']}", ref={"task_id": t["id"]})
            svc.tasks.mark_reminded(t["id"])
            n += 1 if made else 0
        for t in svc.tasks.list(open_only=True):
            if not t["deadline"] or t["priority"] > 2:
                continue   # only critical/high tasks escalate on their own
            dl = parse_iso(t["deadline"])
            if dl < now:
                made = svc.notifications.push("CRITICAL" if t["priority"] == 1 else "IMPORTANT", f"Overdue: {t['title']}",
                                              f"Task #{t['id']} was due {dl.astimezone(z).strftime('%a %d %b %H:%M')}",
                                              source="tasks", dedupe_key=f"overdue:{t['id']}:{t['deadline']}", ref={"task_id": t["id"]})
            elif dl - now <= timedelta(hours=1):
                made = svc.notifications.push("IMPORTANT", f"Due within the hour: {t['title']}",
                                              f"Task #{t['id']} due {dl.astimezone(z).strftime('%H:%M')}",
                                              source="tasks", dedupe_key=f"soon:{t['id']}:{t['deadline']}", ref={"task_id": t["id"]})
            else:
                made = None
            n += 1 if made else 0
        svc.approvals.expire_old()
        return n

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.tick()
                except Exception:   # a bad tick must never kill the scheduler
                    pass
        self._thread = threading.Thread(target=loop, name="friday-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
