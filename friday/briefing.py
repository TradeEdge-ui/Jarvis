"""Executive briefing: a concise, honest status of what FRIDAY actually knows."""
from __future__ import annotations

from datetime import timedelta

import psutil

from friday.clock import UTC, iso, parse_iso

NOT_CONNECTED = ["calendar / schedule", "email", "business metrics (revenue, leads, payments)",
                 "economic calendar / market data"]


def build(svc, project: str | None = None) -> dict:
    now = svc.clock.now()
    z = svc.config.zone
    local = now.astimezone(z)
    end_today = local.replace(hour=23, minute=59, second=59).astimezone(UTC)
    start_today = local.replace(hour=0, minute=0, second=0).astimezone(UTC)
    t = svc.tasks

    def slim(rows):
        return [{"id": r["id"], "title": r["title"], "priority": r["priority_name"], "project": r["project"],
                 "due": parse_iso(r["deadline"]).astimezone(z).strftime("%a %d %b %H:%M") if r["deadline"] else None}
                for r in rows]

    open_tasks = t.list(open_only=True, project=project)
    overdue = [r for r in open_tasks if r["deadline"] and parse_iso(r["deadline"]) < now]
    due_today = [r for r in open_tasks if r["deadline"] and now <= parse_iso(r["deadline"]) <= end_today]
    nxt = t.next_actions(5, project)
    blockers = t.blockers(project)
    approvals = svc.approvals.list("pending")
    unread = svc.notifications.list(unread_only=True, limit=5)
    failures = [r for r in svc.audit.recent(200) if not r["success"] and r["status"] in ("failed", "unverified")
                and parse_iso(r["ts"]) > now - timedelta(hours=24)]
    alerts = []
    vm = psutil.virtual_memory()
    if vm.percent > 90:
        alerts.append(f"memory use {vm.percent:.0f}%")
    try:
        du = psutil.disk_usage(str(svc.config.home))
        if du.percent > 90:
            alerts.append(f"disk {du.percent:.0f}% full")
    except OSError:
        pass
    if svc.estop.engaged:
        alerts.insert(0, "EMERGENCY STOP is engaged — tool execution disabled")

    if approvals:
        rec = f"Decide on pending approval #{approvals[0]['id']}: {approvals[0]['tool']}."
    elif overdue:
        rec = f"Overdue first: #{overdue[0]['id']} {overdue[0]['title']}."
    elif nxt:
        rec = f"Next: #{nxt[0]['id']} {nxt[0]['title']}."
    else:
        rec = "Nothing urgent is recorded. Capture your priorities so I can track them."
    data = {
        "generated": local.strftime("%A %d %B %Y, %H:%M"), "timezone": svc.config.tz, "project": project,
        "priorities": slim(nxt), "due_today": slim(due_today), "overdue": slim(overdue),
        "blocked": [{"id": b["id"], "title": b["title"],
                     "waiting_on": [w["title"] for w in b["waiting_on"]] or ["(marked blocked)"]} for b in blockers],
        "completed_today": slim(t.completed_between(start_today, end_today)),
        "pending_approvals": [{"id": a["id"], "tool": a["tool"], "why": a["why_needed"]} for a in approvals],
        "notifications": [{"id": n["id"], "priority": n["priority"], "title": n["title"]} for n in unread],
        "recent_failures": [{"tool": f["tool"], "summary": f["summary"][:120]} for f in failures[:5]],
        "system_alerts": alerts, "not_connected": NOT_CONNECTED, "recommended_next_action": rec,
        "autonomy_level": int(svc.autonomy.get()),
    }
    data["text"] = render(data)
    return data


def render(d: dict) -> str:
    L = [f"Executive briefing — {d['generated']}" + (f" · {d['project']}" if d["project"] else "")]
    if d["system_alerts"]:
        L.append("ALERTS: " + "; ".join(d["system_alerts"]))
    if d["pending_approvals"]:
        L.append("Awaiting your approval: " + "; ".join(f"#{a['id']} {a['tool']}" for a in d["pending_approvals"]))
    if d["overdue"]:
        L.append("Overdue: " + "; ".join(f"#{r['id']} {r['title']} (was {r['due']})" for r in d["overdue"]))
    if d["due_today"]:
        L.append("Due today: " + "; ".join(f"#{r['id']} {r['title']} ({r['due']})" for r in d["due_today"]))
    L.append("Priorities: " + ("; ".join(f"#{r['id']} {r['title']} [{r['priority']}]" for r in d["priorities"]) or "none recorded"))
    if d["blocked"]:
        L.append("Blocked: " + "; ".join(f"#{b['id']} {b['title']} → waiting on {', '.join(b['waiting_on'])}" for b in d["blocked"]))
    if d["completed_today"]:
        L.append(f"Completed today: {len(d['completed_today'])}")
    if d["notifications"]:
        L.append("Unread: " + "; ".join(f"[{n['priority']}] {n['title']}" for n in d["notifications"]))
    if d["recent_failures"]:
        L.append("Recent action failures: " + "; ".join(f"{f['tool']}: {f['summary']}" for f in d["recent_failures"]))
    L.append("Recommended next action: " + d["recommended_next_action"])
    L.append("Not connected yet (so not covered): " + ", ".join(d["not_connected"]) + ".")
    return "\n".join(L)
