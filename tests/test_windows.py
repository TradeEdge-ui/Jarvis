"""Windows-only: the real thing — real notepad.exe, real PowerShell, Windows path semantics.
These never run on Linux; CI runs them on windows-latest."""
import sys

import pytest

from friday.tools.base import ToolContext
from conftest import run

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_notepad_open_verify_and_close_for_real(svc):
    f = svc.config.home / "Documents" / "winpad.txt"
    f.write_text("hello from FRIDAY")
    res, _ = run(svc, "app_open", {"app": "notepad", "target": str(f)})
    assert res.ok and res.verification.verified is True, res
    try:
        closing, _ = run(svc, "app_close", {"app": "notepad"})
        assert closing.status == "approval_required"
        _, out = svc.executor.approve_and_run(closing.approval_id, by="owner")
        assert out.ok and out.verification.verified is True
    finally:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower() == "notepad.exe":
                try:
                    p.kill()
                except psutil.Error:
                    pass


def test_headline_scenario_open_notepad_and_create_file_on_windows(svc, agent):
    r = agent.handle("Friday, open Notepad and create a file saying hello", device="pc")
    try:
        assert r.status == "completed", r.reply
        f = next((svc.config.home / "Documents").glob("note-hello-*.txt"))
        assert f.read_text() == "hello"
        assert r.actions[1]["verified"] is True and "process table" in r.actions[1]["verification"]
    finally:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower() == "notepad.exe":
                try:
                    p.kill()
                except psutil.Error:
                    pass


def test_powershell_readonly_diagnostics_run_and_mutations_ask(svc):
    ok, _ = run(svc, "shell_run", {"command": "Get-Date", "shell": "powershell"})
    assert ok.ok and ok.data["stdout"].strip() and ok.decision["verdict"] == "execute"
    ps, _ = run(svc, "shell_run", {"command": "Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First 3", "shell": "powershell"})
    assert ps.ok and "Handles" in ps.data["stdout"] or "ProcessName" in ps.data["stdout"]
    res, _ = run(svc, "shell_run", {"command": "New-Item -ItemType File x.txt", "shell": "powershell"})
    assert res.status == "approval_required"
    _, out = svc.executor.approve_and_run(res.approval_id, by="owner")
    assert out.ok and (svc.config.home / "x.txt").exists()


@pytest.mark.parametrize("path", [r"C:\Windows\win.ini", r"..\outside.txt", r"Documents\..\..\escape.txt", r"\\server\share\x.txt",
                                  r"Core\owner.token", r"core\OWNER.TOKEN", r"Memory\friday.db", "C:/Windows/System32/config/SAM"])
def test_windows_paths_outside_or_protected_are_denied(svc, path):
    assert run(svc, "fs_read", {"path": path})[0].status == "denied", path
    assert run(svc, "fs_write", {"path": path, "content": "x", "mode": "overwrite"})[0].status == "denied", path


def test_case_insensitive_paths_inside_the_workspace_work(svc):
    run(svc, "fs_write", {"path": "Documents/Case.txt", "content": "abc"})
    res, _ = run(svc, "fs_read", {"path": str(svc.config.home).upper() + "\\DOCUMENTS\\case.txt"})
    assert res.ok and res.data["content"] == "abc"


def test_windows_builtin_apps_resolve_on_a_standard_runner(svc):
    apps = {a["app"]: a for a in run(svc, "app_list", {})[0].data["apps"]}
    assert apps["notepad"]["launchable"] and apps["explorer"]["launchable"] and apps["terminal"]["launchable"]
