import json
from datetime import timedelta

from friday.security.permissions import DEFAULT_SCOPES
from conftest import run


def test_workflow_files_are_created_and_listed(svc):
    names = {w["name"] for w in svc.workflows.list()}
    assert {"work_mode", "business_mode", "trading_research_mode", "study_mode", "creator_mode", "personal_mode"} <= names


def test_workflow_runs_every_step_through_the_policy_pipeline(svc):
    res, _ = run(svc, "workflow_run", {"name": "work mode"})
    assert res.ok and [s["tool"] for s in res.data["steps"]] == ["briefing", "task_list"]
    tools = [a["tool"] for a in svc.audit.recent(10)]
    assert "briefing" in tools and "task_list" in tools           # steps are individually audited


def test_workflow_stops_at_a_required_step_that_fails_but_not_an_optional_one(svc):
    (svc.config.path("Automations") / "t.json").write_text(json.dumps({"name": "t", "steps": [
        {"tool": "app_open", "args": {"app": "chrome"}, "optional": True},      # no Chrome here: optional => carries on
        {"tool": "fs_read", "args": {"path": "missing.txt"}},                    # required => fails
        {"tool": "briefing", "args": {}}]}))
    res, _ = run(svc, "workflow_run", {"name": "t"})
    assert res.status in ("failed", "unverified") and [s["status"] for s in res.data["steps"]][2] == "skipped"


def test_workflow_steps_cannot_bypass_permissions_or_estop(svc):
    (svc.config.path("Automations") / "evil.json").write_text(json.dumps({"name": "evil", "steps": [
        {"tool": "shell_run", "args": {"command": "echo pwned > /tmp/friday-pwned"}}]}))
    res, _ = run(svc, "workflow_run", {"name": "evil"})
    assert res.data["steps"][0]["status"] == "approval_required"
    svc.estop.engage("t")
    assert run(svc, "workflow_run", {"name": "work_mode"})[0].status == "denied"


def test_editing_a_workflow_revokes_its_approval(svc):
    assert svc.workflows.approve("work_mode") and svc.workflows.is_approved(svc.workflows.load("work_mode"))
    f = svc.config.path("Automations") / "work_mode.json"
    d = json.loads(f.read_text()); d["steps"].append({"tool": "system_info", "args": {}}); f.write_text(json.dumps(d))
    assert not svc.workflows.is_approved(svc.workflows.load("work_mode"))


def test_unknown_workflow_is_blocked(svc):
    assert run(svc, "workflow_run", {"name": "nonexistent mode"})[0].status == "denied"


def test_all_builtin_skills_are_valid_and_reference_real_tools_and_scopes(svc):
    skills = svc.skills.load_all()
    assert {s["domain"] for s in skills} >= {"finance", "business", "system-administration", "productivity"}
    for s in skills:
        assert svc.skills.validate(s) == [], s["name"]
        assert set(s["permissions"]) <= set(DEFAULT_SCOPES)


def test_skills_are_selected_by_relevance_and_validated_when_broken(svc):
    assert svc.skills.relevant("analyze my XAUUSD trades")[0]["name"] == "trading-journal"
    assert any(s["name"] == "finance-education" for s in svc.skills.relevant("explain how inflation affects gold"))
    assert svc.skills.relevant("zzz qqq") == []
    bad = {"name": "x", "purpose": "p", "tools": ["nope_tool"], "permissions": ["not.scope"], "inputs": [], "outputs": [], "validation": "v"}
    problems = svc.skills.validate(bad)
    assert "unknown tool 'nope_tool'" in problems and "unknown permission scope 'not.scope'" in problems and "no instructions" in problems


def test_user_can_add_a_skill_without_touching_code(svc, agent):
    d = svc.config.path("Skills") / "business" / "ticketing"; d.mkdir(parents=True)
    (d / "skill.json").write_text(json.dumps({"name": "ticketing", "purpose": "Ticket sales ops", "tools": ["task_add"],
        "permissions": ["tasks.write"], "triggers": ["ticket", "refund"], "inputs": [], "outputs": [], "validation": "v"}))
    (d / "instructions.md").write_text("Refunds over RM500 need owner approval.")
    s = svc.skills.relevant("customer wants a ticket refund")[0]
    assert s["name"] == "ticketing" and "RM500" in s["instructions"]
    assert "RM500" in agent._system_prompt("customer wants a ticket refund", "pc")


def test_briefing_content(svc):
    svc.tasks.add("Overdue thing", priority=1, deadline=svc.clock.now() - timedelta(hours=2))
    a = svc.tasks.add("Prereq"); svc.tasks.add("Blocked thing", depends_on=[a["id"]])
    svc.tasks.add("Future", deadline=svc.clock.now() + timedelta(days=3))
    res, _ = run(svc, "shell_run", {"command": "echo x > y"})            # leaves a pending approval
    b = run(svc, "briefing", {})[0].data["briefing"]
    assert [t["title"] for t in b["overdue"]] == ["Overdue thing"]
    assert b["blocked"][0]["title"] == "Blocked thing" and b["blocked"][0]["waiting_on"] == ["Prereq"]
    assert b["pending_approvals"][0]["tool"] == "shell_run"
    assert b["recommended_next_action"].startswith("Decide on pending approval")
    assert "calendar / schedule" in b["not_connected"]


def test_operations_report_is_project_scoped(svc):
    svc.tasks.add("CyZy GC thing", project="CyZy GC"); svc.tasks.add("Ticketing thing", project="CyZy Global Ticketing")
    res, _ = run(svc, "report_generate", {"project": "CyZy GC"})
    txt = open(res.data["path"]).read()
    assert res.ok and res.verification.verified and "CyZy GC thing" in txt and "Ticketing thing" not in txt
