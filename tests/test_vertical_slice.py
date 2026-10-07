"""The headline scenarios from the spec, end to end through the real agent loop (offline rules planner)."""
import sys

import psutil
import pytest

from conftest import SLEEPER_NAME

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses a symlinked stand-in editor; see test_windows.py")


@posix_only
def test_open_an_editor_and_create_a_file_saying_hello(svc, agent, launched):
    """'Friday, open Notepad and create a file saying hello' — performed, verified, reported."""
    svc.config.app_overrides["notepad"] = svc.config.app_overrides["sleeper"]      # stand-in editor under the real name
    r = agent.handle("Friday, open Notepad and create a file saying hello", device="pc")
    assert r.status == "completed" and [a["tool"] for a in r.actions] == ["fs_write", "app_open"]
    f = next((svc.config.home / "Documents").glob("note-hello-*.txt"))
    assert f.read_text() == "hello"                                                # the file really exists
    pid = r.actions[1]
    assert pid["verified"] is True and "process table" in pid["verification"]
    assert any(p.name() == SLEEPER_NAME for p in psutil.process_iter())            # and so does the process
    assert "Verified" in r.reply and f.name in r.reply
    audit = svc.audit.recent(2)
    assert {a["tool"] for a in audit} == {"fs_write", "app_open"} and all(a["verified"] == 1 for a in audit)


@posix_only
def test_failure_to_launch_is_reported_as_failure_with_the_file_still_created(svc, agent):
    r = agent.handle("open notepad and create a file saying hello", device="pc")     # no Notepad on Linux
    assert r.status == "partial"
    assert [a["status"] for a in r.actions] == ["ok", "failed"]
    assert "Failed" in r.reply and "Could not launch notepad" in r.reply and "✓" in r.reply


def test_reminder_is_remembered_across_devices_and_conversations(svc, agent):
    """'remind me tomorrow to finish the CyZy proposal' on the PC; the phone already knows."""
    a = agent.handle("Friday, remind me tomorrow to finish the CyZy proposal", device="windows-pc")
    assert a.status == "completed" and "Finish the CyZy proposal" in a.reply and "project CyZy" in a.reply
    t = svc.tasks.list(open_only=True)[0]
    assert t["project"] == "CyZy" and t["deadline"] == "2026-10-08T01:00:00+00:00" and t["source_device"] == "windows-pc"
    b = agent.handle("what should I do next?", device="android-phone")                 # a different device, no context given
    assert "Finish the CyZy proposal" in b.reply and b.conversation_id == a.conversation_id
    history = svc.conversations.history("main")
    assert [m["device"] for m in history if m["role"] == "user"] == ["windows-pc", "android-phone"]
    svc.clock.advance(days=1, hours=2)                                                    # it is now past 09:00 tomorrow MYT
    c = agent.handle("what do I need to do today?", device="android-phone")
    assert "Finish the CyZy proposal" in c.reply
    agent.handle("mark task 1 done", device="android-phone")
    assert svc.tasks.get(1)["status"] == "done"


def test_unspecified_time_asks_instead_of_guessing(svc, agent):
    r = agent.handle("remind me to call the bank", device="pc")
    assert r.actions == [] and "When should I remind you" in r.reply and svc.tasks.list() == []


def test_priority_list_and_blockers(svc, agent):
    agent.handle("add review CyZy FX disclaimer to my priority list", device="pc")
    t = svc.tasks.get(1)
    assert t["priority"] == 2 and t["project"] == "CyZy FX"
    svc.tasks.add("Launch page", project="CyZy FX", depends_on=[1])
    r = agent.handle("what is blocking CyZy FX right now?", device="pc")
    assert "Launch page" in r.reply and "Review CyZy FX disclaimer" in r.reply


def test_memory_by_voice_style_commands(svc, agent):
    r = agent.handle("Friday, remember that CyZy FX content must go through compliance review before publishing", device="pc")
    assert r.status == "completed"
    m = svc.memory.list()[0]
    assert m["category"] == "business" and m["project"] == "CyZy FX"
    r2 = agent.handle("what do you remember about compliance?", device="phone")
    assert "compliance review" in r2.reply
    assert "don't have anything" in agent.handle("what do you remember about submarines?", device="pc").reply


def test_approval_conversation_yes_runs_exactly_the_pending_action(svc, agent, launched):
    r = agent.handle("close sleeper", device="pc")                      # apps.close needs confirmation
    assert r.status == "approval_required" and "Approval required" in r.reply
    aid = r.pending_approvals[0]["id"]
    assert svc.approvals.get(aid)["status"] == "pending"
    ok = agent.handle("yes", device="pc")
    assert ok.status == "completed" and "Approved #%d" % aid in ok.reply and svc.approvals.get(aid)["status"] == "executed"


def test_bare_yes_with_nothing_pending_is_just_conversation(svc, agent):
    r = agent.handle("yes", device="pc")
    assert r.actions == [] and r.status == "info"


def test_deny_in_chat_cancels(svc, agent):
    r = agent.handle("close sleeper", device="pc")
    d = agent.handle("no", device="pc")
    assert "Denied" in d.reply and svc.approvals.get(r.pending_approvals[0]["id"])["status"] == "denied"


def test_a_device_without_approval_rights_cannot_approve_by_chat(svc, agent):
    r = agent.handle("close sleeper", device="pc")
    d = agent.handle("approve %d" % r.pending_approvals[0]["id"], device="kiosk", can_approve=False)
    assert "not allowed to approve" in d.reply and svc.approvals.get(r.pending_approvals[0]["id"])["status"] == "pending"


def test_two_pending_approvals_require_an_explicit_id(svc, agent):
    agent.handle("close sleeper", device="pc")
    svc.config.app_overrides["other"] = {"command": ["x"], "process_names": ["other-proc"]}
    agent.handle("close other", device="pc")
    r = agent.handle("yes", device="pc")
    assert "More than one action" in r.reply and svc.approvals.list("pending").__len__() == 2


def test_high_impact_actions_need_an_explicit_approve_id_not_a_bare_yes(svc, agent, tmp_path):
    svc.config.write_roots.append(tmp_path)
    d = tmp_path / "important"; d.mkdir()
    svc.executor.run("fs_delete", {"path": str(d)}, __import__("friday.tools.base", fromlist=["x"]).ToolContext(svc=svc, device="pc"))
    ap = svc.approvals.list("pending")[0]
    r = agent.handle("yes", device="pc")
    assert "high-impact" in r.reply and d.exists()
    done = agent.handle(f"approve {ap['id']}", device="pc")
    assert done.status == "completed" and not d.exists()


def test_emergency_stop_works_without_any_model_and_cannot_be_undone_from_chat(svc, agent):
    agent.handle("close sleeper", device="pc")
    r = agent.handle("Friday, emergency stop", device="phone")
    assert svc.estop.engaged and "Emergency stop engaged" in r.reply
    assert svc.approvals.list("pending") == []                                       # pending approvals were cancelled
    for text in ["resume", "friday release the emergency stop", "please disable e-stop"]:
        assert "can't release" in agent.handle(text, device="pc").reply and svc.estop.engaged
    blocked = agent.handle("remind me tomorrow to call", device="pc")
    assert blocked.status == "blocked" and svc.tasks.list() == []
    assert svc.notifications.list()[0]["priority"] == "CRITICAL"


def test_unknown_requests_get_an_honest_capability_list_not_a_fake_answer(svc, agent):
    r = agent.handle("write me a poem about gold and explain the Fed's dot plot", device="pc")
    assert r.actions == [] and "without a language model" in r.reply and "ollama" in r.reply.lower()


def test_executive_briefing_is_honest_about_what_is_not_connected(svc, agent):
    svc.tasks.add("Pay supplier", priority=1, deadline=svc.clock.now() - __import__("datetime").timedelta(hours=3))
    svc.tasks.add("Draft newsletter", priority=3)
    r = agent.handle("Friday, executive briefing", device="pc")
    assert "Overdue" in r.reply and "Pay supplier" in r.reply and "Not connected yet" in r.reply and "calendar" in r.reply
    assert "Recommended next action" in r.reply


def test_work_mode_runs_steps_through_the_pipeline_and_optional_app_failures_do_not_fail_it(svc, agent):
    r = agent.handle("Friday, start business mode", device="pc")
    assert r.actions[0]["tool"] == "workflow_run"
    wf = svc.audit.recent(20)
    assert {"briefing", "task_list", "app_open", "workflow_run"} <= {a["tool"] for a in wf}
    assert "business_mode" in r.reply


def test_operations_report_is_created_and_verified(svc, agent):
    svc.tasks.add("Chase invoice 118", project="CyZy", priority=2)
    r = agent.handle("Friday, prepare today's CyZy operations report", device="pc")
    assert r.status == "completed" and r.actions[0]["verified"] is True
    f = next((svc.config.home / "Business" / "Reports").glob("*cyzy-operations.md"))
    txt = f.read_text()
    assert "Chase invoice 118" in txt and "## Not covered" in txt and "No data source is connected yet" in txt


def test_trading_journal_analysis_through_chat(svc, agent):
    from datetime import datetime, timedelta
    lines = ["Date,Symbol,PnL,Setup"]
    base = datetime(2026, 1, 5, 9)
    for i, p in enumerate([50, 50, -120] * 12):
        lines.append(f"{(base + timedelta(hours=i)).isoformat(sep=' ')},XAUUSD,{p},breakout")
    (svc.config.home / "Finance" / "xau.csv").write_text("\n".join(lines))
    r = agent.handle("analyze my last 30 XAUUSD trades in Finance/xau.csv", device="pc")
    assert r.status == "completed" and "OBSERVATIONS" in r.reply and "INTERPRETATIONS (hypotheses, not facts)" in r.reply
    assert "not financial advice" in r.reply and "30 trades" in r.reply


def test_system_status_and_ollama_diagnosis(svc, agent):
    assert "memory" in agent.handle("how is my pc doing?", device="pc").reply.lower()
    r = agent.handle("Friday, Ollama isn't connecting", device="pc")
    assert "ollama: NOT healthy" in r.reply and "Likely cause" in r.reply and "Suggested (not run)" in r.reply
    assert r.actions[0]["tool"] == "service_check" and r.actions[0]["status"] == "ok"


def test_autonomy_level_zero_turns_actions_into_proposals_in_chat(svc, agent):
    svc.autonomy.set(0)
    r = agent.handle("create a file called plan.txt saying draft", device="pc")
    assert r.actions[0]["status"] == "proposed" and "not performed" in r.reply.lower()
    assert not (svc.config.home / "plan.txt").exists()


def test_a_bug_inside_a_planner_is_contained_logged_and_does_not_crash_the_request(svc, agent, monkeypatch):
    from friday.core.rules import RulesProvider
    def boom(self, text):
        raise RuntimeError("planner exploded")
    monkeypatch.setattr(RulesProvider, "_plan", boom)
    r = agent.handle("open chrome", device="pc")
    assert "Something went wrong inside FRIDAY" in r.reply and r.actions == []
    assert svc.audit.recent(1)[0]["tool"] == "agent" and "planner exploded" in svc.audit.recent(1)[0]["summary"]


@pytest.mark.parametrize("phrase,tool,args", [
    ("start work mode", "workflow_run", {"name": "work mode"}), ("run my study mode", "workflow_run", {"name": "study mode"}),
    ("good morning", "briefing", {}), ("what should I work on next", "task_list", {"view": "next"}),
    ("create a note saying buy milk", "fs_write", {"content": "buy milk"}), ("find files named invoice in Documents", "fs_search", {"name_pattern": "*invoice*"}),
    ("total Revenue in Finance/sales.csv", "spreadsheet_calc", {"op": "sum", "column": "Revenue"}),
    ("read https://example.com", "browser_read", {"url": "https://example.com"}), ("forget memory 4", "memory_forget", {"id": 4}),
    ("move task 3 to friday", "task_update", {"id": 3}), ("I finished task 2", "task_complete", {"id": 2}),
])
def test_every_rules_intent_builds_a_valid_tool_call(svc, phrase, tool, args):
    """Catches argument-name bugs like the `name=` collision: each planned call must pass the tool's own schema."""
    from friday.core.rules import RulesProvider
    plan = RulesProvider(svc)._plan(phrase)
    assert isinstance(plan, list), (phrase, plan)
    call = plan[0]
    assert call.name == tool and all(call.arguments.get(k) == v for k, v in args.items()), call
    t = svc.registry.get(call.name)
    t.Args(**{k: v for k, v in call.arguments.items() if k != "reason"})     # raises if the planner emitted a bad argument
