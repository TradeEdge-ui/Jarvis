from conftest import run


def test_every_attempt_is_logged_with_required_fields(svc):
    run(svc, "fs_write", {"path": "Documents/a.txt", "content": "x", "reason": "unit test"}, cmd="write a file")
    run(svc, "shell_run", {"command": "echo x > y"})
    rows = svc.audit.recent(10)
    assert {r["status"] for r in rows} == {"ok", "approval_required"}
    w = next(r for r in rows if r["tool"] == "fs_write")
    assert w["user_command"] == "write a file" and w["reason"] == "unit test" and w["verified"] == 1
    assert w["scope"] == "filesystem.write" and "grant=allow" in w["permission"] and w["autonomy"] == 2 and w["ts"]


def test_chain_detects_tampering(svc):
    for i in range(4):
        run(svc, "fs_write", {"path": f"Documents/{i}.txt", "content": str(i)})
    assert svc.audit.verify_chain() == (True, None)
    svc.db.execute("UPDATE audit_log SET summary='nothing happened' WHERE id=2")
    ok, bad = svc.audit.verify_chain()
    assert not ok and bad == 2


def test_chain_detects_deleted_row(svc):
    for i in range(4):
        run(svc, "fs_write", {"path": f"Documents/{i}.txt", "content": str(i)})
    svc.db.execute("DELETE FROM audit_log WHERE id=2")
    assert svc.audit.verify_chain()[0] is False


def test_secrets_are_redacted_in_the_log(svc):
    run(svc, "shell_run", {"command": "echo password=hunter2secret token=abc123 sk-ABCDEFGHIJKLMNOPQRSTUV"})
    row = svc.audit.recent(1)[0]
    blob = json.dumps(row) if (json := __import__("json")) else ""
    assert "hunter2secret" not in blob and "abc123" not in blob and "ABCDEFGHIJKLMNOPQRSTUV" not in blob
