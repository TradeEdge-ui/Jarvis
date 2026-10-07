import sys
import time

import psutil
import pytest

from conftest import run, SLEEPER_NAME

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="stand-in executables use POSIX symlinks; Windows has test_windows.py")


def test_launch_is_verified_against_the_real_process_table(svc, launched):
    res, _ = run(svc, "app_open", {"app": "sleeper"})
    assert res.ok and res.verification.verified is True
    pid = res.data["pid"]
    assert psutil.pid_exists(pid) and psutil.Process(pid).name() == SLEEPER_NAME


def test_unregistered_app_is_refused(svc):
    res, _ = run(svc, "app_open", {"app": "rm -rf"})
    assert res.status == "denied" and "not a registered application" in res.summary


def test_target_outside_sandbox_is_refused(svc):
    assert run(svc, "app_open", {"app": "sleeper", "target": "/etc/passwd"})[0].status == "denied"


def test_launch_failure_is_honest_and_lists_what_was_tried(svc):
    svc.config.app_overrides["ghost"] = {"command": ["/nonexistent/binary"], "process_names": []}
    res, _ = run(svc, "app_open", {"app": "ghost"})
    assert res.status == "failed" and "Could not launch" in res.summary and res.data["tried"]


def test_process_that_dies_immediately_is_unverified_not_success(svc):
    svc.config.app_overrides["crasher"] = {"command": [sys.executable, "-c", "import sys; sys.exit(3)"], "process_names": []}
    res, _ = run(svc, "app_open", {"app": "crasher"})
    assert res.status == "unverified" and res.verification.verified is False and "exit code 3" in res.verification.detail


def test_builtin_apps_without_a_binary_fail_gracefully_on_this_machine(svc):
    res, _ = run(svc, "app_open", {"app": "excel"})
    assert res.status == "failed"          # no Excel on Linux: an honest failure, not a fake launch


def test_close_requires_approval_then_terminates_and_verifies(svc, launched):
    opened, _ = run(svc, "app_open", {"app": "sleeper"})
    pid = opened.data["pid"]
    res, _ = run(svc, "app_close", {"app": "sleeper"})
    assert res.status == "approval_required" and psutil.pid_exists(pid)
    _, out = svc.executor.approve_and_run(res.approval_id, by="owner")
    assert out.ok and out.verification.verified
    time.sleep(0.2)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_apps_without_process_names_cannot_be_closed(svc):
    assert run(svc, "app_close", {"app": "standin"})[0].status == "denied"


def test_app_list_reports_running_state(svc, launched):
    run(svc, "app_open", {"app": "sleeper"})
    apps = {a["app"]: a for a in run(svc, "app_list", {})[0].data["apps"]}
    assert apps["sleeper"]["running"] is True and apps["sleeper"]["launchable"] is True
