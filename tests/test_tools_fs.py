import os
import sys
from pathlib import Path

import pytest

from conftest import run


def home_file(svc, rel):
    return svc.config.home / rel


def test_write_read_roundtrip_and_hash_verification(svc):
    res, _ = run(svc, "fs_write", {"path": "Documents/x.txt", "content": "héllo wörld"})
    assert res.ok and res.verification.method == "re-read + sha256"
    rd, _ = run(svc, "fs_read", {"path": "Documents/x.txt"})
    assert rd.data["content"] == "héllo wörld"


def test_create_mode_never_clobbers_overwrite_backs_up(svc):
    run(svc, "fs_write", {"path": "a.txt", "content": "v1"})
    res, _ = run(svc, "fs_write", {"path": "a.txt", "content": "v2"})
    assert res.status == "failed" and "already exists" in res.error
    assert home_file(svc, "a.txt").read_text() == "v1"
    res, _ = run(svc, "fs_write", {"path": "a.txt", "content": "v2", "mode": "overwrite"})
    assert res.ok and Path(res.data["backup"]).read_text() == "v1"
    assert home_file(svc, "a.txt").read_text() == "v2"


def test_corrupted_write_is_reported_unverified_not_success(svc, monkeypatch):
    real = os.replace
    def corrupt(src, dst):
        Path(src).write_bytes(b"CORRUPTED")
        real(src, dst)
    monkeypatch.setattr("friday.tools.filesystem.os.replace", corrupt)
    res, _ = run(svc, "fs_write", {"path": "a.txt", "content": "good"})
    assert res.status == "unverified" and res.verification.verified is False and not res.ok
    assert svc.audit.recent(1)[0]["success"] == 0


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "../../etc/hosts", "Documents/../../escape.txt"])
def test_paths_outside_the_sandbox_are_denied(svc, path):
    for tool, args in [("fs_read", {"path": path}), ("fs_write", {"path": path, "content": "x"})]:
        res, _ = run(svc, tool, args)
        assert res.status == "denied", (tool, path)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_escape_is_denied(svc, tmp_path):
    secret = tmp_path / "secret.txt"; secret.write_text("top secret")
    (svc.config.home / "Documents" / "link.txt").symlink_to(secret)
    res, _ = run(svc, "fs_read", {"path": "Documents/link.txt"})
    assert res.status == "denied"


@pytest.mark.parametrize("rel", ["Core/owner.token", "Core/config.json", "Memory/friday.db", ".env", "Documents/id_rsa", "Documents/.ssh/known", "Documents/server.pem"])
def test_secrets_and_own_database_are_never_readable(svc, rel):
    (svc.config.home / "Documents").mkdir(exist_ok=True)
    f = svc.config.home / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("sensitive") if not f.exists() else None
    assert run(svc, "fs_read", {"path": rel})[0].status == "denied"
    assert run(svc, "fs_write", {"path": rel, "content": "x", "mode": "overwrite"})[0].status == "denied"


def test_listing_hides_protected_entries(svc):
    names = [i["name"] for i in run(svc, "fs_list", {"path": "."})[0].data["items"]]
    assert "Documents" in names and "Core" not in names      # credentials folder is not even advertised
    assert run(svc, "fs_list", {"path": "Core"})[0].status == "denied"


def test_append_is_verified(svc):
    run(svc, "fs_write", {"path": "log.txt", "content": "a\n"})
    res, _ = run(svc, "fs_append", {"path": "log.txt", "content": "b\n"})
    assert res.ok and res.verification.verified and home_file(svc, "log.txt").read_text() == "a\nb\n"


def test_move_and_refuse_overwrite(svc):
    run(svc, "fs_write", {"path": "a.txt", "content": "A"}); run(svc, "fs_write", {"path": "b.txt", "content": "B"})
    assert run(svc, "fs_move", {"source": "a.txt", "destination": "b.txt"})[0].status == "failed"
    res, _ = run(svc, "fs_move", {"source": "a.txt", "destination": "Documents/a2.txt"})
    assert res.ok and res.verification.verified and not home_file(svc, "a.txt").exists()


def test_delete_goes_to_trash_after_approval_and_is_recoverable(svc):
    run(svc, "fs_write", {"path": "Documents/del.txt", "content": "keep me"})
    res, _ = run(svc, "fs_delete", {"path": "Documents/del.txt"})
    assert res.status == "approval_required" and home_file(svc, "Documents/del.txt").exists()
    _, out = svc.executor.approve_and_run(res.approval_id, by="owner")
    assert out.ok and out.verification.verified
    assert not home_file(svc, "Documents/del.txt").exists() and Path(out.data["trash"]).read_text() == "keep me"


def test_refuses_to_delete_workspace_root(svc):
    assert run(svc, "fs_delete", {"path": "."})[0].status == "denied"


def test_search_by_name_and_content(svc):
    run(svc, "fs_write", {"path": "Finance/revenue-oct.csv", "content": "a,b\n1,2"})
    run(svc, "fs_write", {"path": "Finance/notes.txt", "content": "the quick brown fox"})
    r1, _ = run(svc, "fs_search", {"root": "Finance", "name_pattern": "*revenue*"})
    assert [Path(i["path"]).name for i in r1.data["items"]] == ["revenue-oct.csv"]
    r2, _ = run(svc, "fs_search", {"root": ".", "contains": "brown fox"})
    assert any(i["path"].endswith("notes.txt") for i in r2.data["items"])


def test_binary_files_are_not_dumped_as_text(svc):
    (svc.config.home / "Documents" / "b.bin").write_bytes(b"\x00\x01\x02binary")
    assert run(svc, "fs_read", {"path": "Documents/b.bin"})[0].status == "failed"
