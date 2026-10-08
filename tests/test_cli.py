import io
import json
import sys

from friday.cli import main


def run_cli(capsys, home, *argv):
    code = main(["--home", str(home), *argv])
    return code, capsys.readouterr().out


def test_init_creates_workspace_and_a_protected_owner_token(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_MODEL_PROVIDER", "rules")
    home = tmp_path / "F"
    code, out = run_cli(capsys, home, "init")
    assert code == 0 and "Owner token created" in out
    for d in ["Core", "Memory", "Projects/CyZy", "Finance", "Business", "Documents", "Research", "Automations", "Logs", "Skills", "Backups"]:
        assert (home / d).is_dir(), d
    tok = (home / "Core" / "owner.token").read_text()
    assert tok.startswith("fri_")
    if sys.platform != "win32":                                      # POSIX permission bits; Windows uses ACLs
        assert (home / "Core" / "owner.token").stat().st_mode & 0o077 == 0
    code, out = run_cli(capsys, home, "init")                       # idempotent: no second token
    assert "Owner token created" not in out


def test_doctor_reports_honestly(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_MODEL_PROVIDER", "rules")
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "F"))
    code, out = run_cli(capsys, tmp_path / "F", "doctor")
    assert code == 0 and "[OK  ] database" in out and "[OK  ] audit log" in out and "active brain" in out and "skills" in out
    assert "[WARN] active brain" in out                             # rules fallback is flagged, not hidden


def test_ask_estop_resume_approvals_roundtrip(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_MODEL_PROVIDER", "rules")
    home = tmp_path / "F"
    code, out = run_cli(capsys, home, "ask", "remind", "me", "tomorrow", "to", "call", "Ben")
    assert code == 0 and "Call Ben" in out and "Verified" in out
    run_cli(capsys, home, "estop")
    code, out = run_cli(capsys, home, "ask", "what", "should", "i", "do", "next?")
    assert code == 2 and "Emergency stop" in out
    run_cli(capsys, home, "resume")
    code, out = run_cli(capsys, home, "ask", "what", "should", "i", "do", "next?")
    assert code == 0 and "Call Ben" in out


def test_token_devices_audit_memory(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_MODEL_PROVIDER", "rules")
    home = tmp_path / "F"
    code, out = run_cli(capsys, home, "token", "my-phone", "--kind", "phone")
    assert code == 0 and "fri_" in out
    _, out = run_cli(capsys, home, "devices"); assert "my-phone" in out
    run_cli(capsys, home, "ask", "remember", "that", "Acme", "pays", "late")
    _, out = run_cli(capsys, home, "memory", "search", "Acme"); assert "Acme pays late" in out
    _, out = run_cli(capsys, home, "memory", "export"); assert json.loads(out)["memories"][0]["content"] == "Acme pays late"
    _, out = run_cli(capsys, home, "audit", "--verify"); assert "intact" in out
    _, out = run_cli(capsys, home, "audit"); assert "memory_remember" in out and "verified" in out


def test_approve_and_deny_from_the_cli(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_MODEL_PROVIDER", "rules")
    home = tmp_path / "F"
    from friday.config import Config
    from friday.services import build_services
    from friday.tools.base import ToolContext
    svc = build_services(Config.from_env(home=home))
    ctx = ToolContext(svc=svc, device="x")
    a = svc.executor.run("shell_run", {"command": "echo cli-approved > out.txt"}, ctx).approval_id
    b = svc.executor.run("shell_run", {"command": "echo never > no.txt"}, ctx).approval_id
    svc.close()
    _, out = run_cli(capsys, home, "approvals"); assert f"#{a}" in out and f"#{b}" in out
    code, out = run_cli(capsys, home, "approve", str(a)); assert code == 0 and "ok" in out
    assert (home / "out.txt").read_text().strip() == "cli-approved"
    code, out = run_cli(capsys, home, "deny", str(b)); assert "Denied" in out and not (home / "no.txt").exists()
    code, out = run_cli(capsys, home, "approve", str(b)); assert code == 1 and "Cannot approve" in out


def test_ask_new_starts_a_separate_conversation_from_main(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_MODEL_PROVIDER", "rules")
    home = tmp_path / "F"
    run_cli(capsys, home, "ask", "what", "should", "i", "do", "next?")                          # -> main
    run_cli(capsys, home, "ask", "--new", "what", "do", "you", "remember", "about", "unicorns")  # -> fresh thread
    from friday.config import Config
    from friday.services import build_services
    svc = build_services(Config.from_env(home=home))
    try:
        convs = svc.conversations.list()
        assert len(convs) == 2
        main_msgs = [m["content"] for m in svc.conversations.history("main")]
        assert not any("unicorns" in c for c in main_msgs)
        fresh_id = next(c["id"] for c in convs if c["id"] != "main")
        fresh_msgs = [m["content"] for m in svc.conversations.history(fresh_id)]
        assert any("unicorns" in c for c in fresh_msgs)
    finally:
        svc.close()


def test_chat_new_flag_is_wired(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_MODEL_PROVIDER", "rules")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))   # EOF immediately -> cmd_chat's input() loop exits cleanly
    code, out = run_cli(capsys, tmp_path / "F", "chat", "--new")
    assert code == 0 and "Starting a fresh conversation" in out
