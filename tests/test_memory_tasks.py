from datetime import datetime, timedelta, timezone

import pytest

from friday.scheduler import Scheduler
from conftest import run


# ---------------------------------------------------------------- memory
def test_retrieval_ranks_relevant_memory_and_ignores_irrelevant(svc):
    m = svc.memory
    m.add("Customer Acme Sdn Bhd pays invoices on 45 day terms", "business", project="CyZy")
    m.add("The user prefers concise answers and dislikes flattery", "long_term")
    m.add("Gold (XAUUSD) is most volatile during the New York session overlap", "knowledge")
    hits = m.search("when does Acme pay their invoice?")
    assert hits and "Acme" in hits[0]["content"]
    assert all("flattery" not in h["content"] for h in hits)
    assert m.search("zebra quantum submarine") == []


def test_search_survives_hostile_query_syntax(svc):
    svc.memory.add("alpha beta gamma", "long_term")
    for q in ['"', 'alpha AND', 'NEAR(', '*', "alpha'; DROP TABLE memories;--", "a b", ""]:
        svc.memory.search(q)                        # must not raise
    assert svc.db.one("SELECT COUNT(*) c FROM memories")["c"] == 1


def test_importance_and_pinned_context(svc):
    svc.memory.add("User is based in Malaysia (MYT, UTC+8)", "long_term", importance=0.9)
    svc.memory.add("Minor trivia", "long_term", importance=0.2)
    assert [m["content"] for m in svc.memory.pinned()] == ["User is based in Malaysia (MYT, UTC+8)"]


def test_inspect_correct_delete_export(svc):
    mid = svc.memory.add("Office wifi password is on the fridge", "long_term")
    assert svc.memory.update(mid, content="Office wifi details are on the fridge")
    assert svc.memory.get(mid)["content"].endswith("on the fridge") and "password" not in svc.memory.get(mid)["content"]
    assert svc.memory.search("wifi")[0]["id"] == mid
    exported = svc.memory.export()
    assert exported["memories"][0]["id"] == mid
    assert svc.memory.delete(mid) and svc.memory.get(mid) is None and svc.memory.search("wifi") == []
    mid2 = svc.memory.add("temporary", "long_term"); assert svc.memory.delete(mid2, hard=True)
    assert svc.db.one("SELECT 1 FROM memories WHERE id=?", (mid2,)) is None


def test_disabled_category_is_not_written_or_searched(svc):
    svc.memory.add("business secret sauce", "business")
    svc.memory.set_category_enabled("business", False)
    assert svc.memory.search("secret sauce") == []
    with pytest.raises(PermissionError):
        svc.memory.add("another", "business")
    res, _ = run(svc, "memory_remember", {"content": "x y z", "category": "business"})
    assert res.status == "failed"
    svc.memory.set_category_enabled("business", True)
    assert svc.memory.search("secret sauce")


def test_duplicate_memory_is_not_stored_twice(svc):
    a = svc.memory.add("same fact", "long_term"); b = svc.memory.add("same fact", "long_term")
    assert a == b


def test_knowledge_ingest_chunks_and_is_searchable(svc):
    doc = "SOP: refunds\n\n" + "\n\n".join(f"Step {i}: verify the order number before refunding customer {i}." * 3 for i in range(12))
    (svc.config.home / "Documents").mkdir(exist_ok=True)
    (svc.config.home / "Documents" / "refund-sop.md").write_text(doc)
    res, _ = run(svc, "knowledge_ingest", {"path": "Documents/refund-sop.md", "project": "CyZy"})
    assert res.ok and len(res.data["ids"]) > 1 and res.verification.verified
    assert svc.memory.search("refund order number", ["knowledge"])
    assert run(svc, "knowledge_ingest", {"path": "Documents/x.pdf"})[0].status in ("failed", "denied")


def test_operational_memory_is_the_audit_log(svc):
    run(svc, "fs_write", {"path": "Documents/proposal.txt", "content": "x"}, cmd="write the proposal draft")
    res, _ = run(svc, "memory_search", {"query": "proposal", "category": "operational"})
    assert res.ok and res.data["results"] and res.data["results"][0]["tool"] == "fs_write"


# ---------------------------------------------------------------- tasks
def test_task_fields_history_and_completion(svc, clock):
    t = svc.tasks.add("Write CyZy proposal", priority=2, project="CyZy", notes="client call Friday",
                      deadline=clock.now() + timedelta(days=1), owner="user", automation_level=1)
    assert t["priority_name"] == "high" and t["status"] == "todo" and t["automation_level"] == 1
    svc.tasks.update(t["id"], status="in_progress")
    done = svc.tasks.complete(t["id"])
    assert done["status"] == "done" and done["completed_at"]
    assert [h["event"] for h in svc.tasks.history(t["id"])] == ["created", "updated", "updated", "completed"]


def test_next_actions_respect_priority_dependencies_and_overdue(svc, clock):
    base = svc.tasks.add("Draft contract", priority=3)
    svc.tasks.add("Send contract", priority=1, depends_on=[base["id"]])           # blocked by dependency
    svc.tasks.add("Low-priority tidy", priority=4)
    od = svc.tasks.add("Pay supplier", priority=3, deadline=clock.now() - timedelta(days=1))
    order = [t["title"] for t in svc.tasks.next_actions()]
    assert "Send contract" not in order
    assert order[0] == "Pay supplier" and order.index("Draft contract") < order.index("Low-priority tidy")
    svc.tasks.complete(base["id"])
    assert "Send contract" in [t["title"] for t in svc.tasks.next_actions()]


def test_blockers_report_what_is_waiting_on_what(svc):
    a = svc.tasks.add("Get licence approval", project="CyZy FX")
    svc.tasks.add("Launch FX education page", project="CyZy FX", depends_on=[a["id"]])
    b = svc.tasks.blockers("CyZy FX")
    assert len(b) == 1 and b[0]["waiting_on"][0]["title"] == "Get licence approval"
    res, _ = run(svc, "task_list", {"view": "blocked", "project": "CyZy FX"})
    assert res.data["tasks"][0]["waiting_on"]


def test_task_tools_are_verified_and_parse_natural_time(svc):
    res, _ = run(svc, "task_add", {"title": "Call supplier", "deadline": "tomorrow 3pm", "remind_at": "tomorrow 2:30pm"})
    assert res.ok and res.verification.verified
    t = res.data["task"]
    assert t["deadline"] == "2026-10-08T07:00:00+00:00"        # 15:00 MYT == 07:00 UTC
    assert t["remind_at"] == "2026-10-08T06:30:00+00:00"
    assert run(svc, "task_add", {"title": "x", "deadline": "whenever"})[0].status == "failed"


def test_complete_by_title_is_unambiguous(svc):
    svc.tasks.add("Review proposal A"); svc.tasks.add("Review proposal B")
    assert run(svc, "task_complete", {"title_contains": "Review proposal"})[0].status == "failed"
    assert run(svc, "task_complete", {"title_contains": "proposal B"})[0].ok


def test_move_task_to_tomorrow(svc):
    t = svc.tasks.add("Do thing")
    res, _ = run(svc, "task_update", {"id": t["id"], "deadline": "tomorrow"})
    assert res.ok and svc.tasks.get(t["id"])["deadline"] == "2026-10-08T01:00:00+00:00"  # tomorrow 09:00 MYT (clock: 20:00 MYT on the 7th)


# ---------------------------------------------------------------- scheduler / notifications
def test_reminder_notification_fires_once_when_due(svc, clock):
    t = svc.tasks.add("Finish proposal", remind_at=clock.now() + timedelta(hours=2), project="CyZy")
    sch = Scheduler(svc)
    assert sch.tick() == 0
    clock.advance(hours=3)
    assert sch.tick() == 1
    assert sch.tick() == 0                                              # no repeat
    n = svc.notifications.list()[0]
    assert "Finish proposal" in n["title"] and n["priority"] == "ACTION_REQUIRED"
    assert svc.tasks.get(t["id"])["reminded_at"]


def test_overdue_escalates_only_for_critical_and_high_and_only_once(svc, clock):
    svc.tasks.add("Chill task", priority=3, deadline=clock.now() - timedelta(hours=1))
    c = svc.tasks.add("Critical task", priority=1, deadline=clock.now() - timedelta(hours=1))
    sch = Scheduler(svc)
    assert sch.tick() == 1 and sch.tick() == 0
    n = svc.notifications.list()[0]
    assert n["priority"] == "CRITICAL" and n["ref"]["task_id"] == c["id"]


def test_notifications_are_priority_ordered_and_ackable(svc):
    svc.notifications.push("INFORMATIONAL", "fyi"); svc.notifications.push("CRITICAL", "fire")
    assert [n["title"] for n in svc.notifications.list()] == ["fire", "fyi"]
    assert svc.notifications.ack() == 2 and svc.notifications.unread_count() == 0
    assert svc.notifications.push("IMPORTANT", "x", dedupe_key="k") and svc.notifications.push("IMPORTANT", "x", dedupe_key="k") is None
    with pytest.raises(ValueError):
        svc.notifications.push("LOUD", "bad")
