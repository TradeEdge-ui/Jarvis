import pytest


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def phone(api):
    r = api.post("/v1/devices", json={"name": "android", "kind": "phone"}, headers=api.owner)
    assert r.status_code == 201
    return r.json()


def test_health_is_public_everything_else_needs_a_valid_token(api):
    assert api.get("/health").json()["status"] == "ok"
    for path in ["/v1/status", "/v1/tasks", "/v1/memory", "/v1/activity", "/v1/security", "/v1/devices"]:
        assert api.get(path).status_code == 401
        assert api.get(path, headers=H("fri_wrong")).status_code == 401
        assert api.get(path, headers={"Authorization": "Basic abc"}).status_code == 401
    assert api.post("/v1/chat", json={"message": "hi"}).status_code == 401


def test_tokens_are_stored_only_as_hashes(api, phone):
    rows = api.svc.db.query("SELECT token_hash FROM devices")
    assert all(len(r["token_hash"]) == 64 for r in rows)
    assert phone["token"] not in open(api.svc.config.db_path, "rb").read().decode("latin1")


def test_device_role_cannot_use_owner_endpoints(api, phone):
    h = H(phone["token"])
    assert api.get("/v1/status", headers=h).json()["role"] == "device"
    assert api.put("/v1/security/autonomy", json={"level": 4}, headers=h).status_code == 403
    assert api.put("/v1/security/permissions/shell.execute", json={"grant": "allow"}, headers=h).status_code == 403
    assert api.post("/v1/security/resume", headers=h).status_code == 403
    assert api.post("/v1/devices", json={"name": "evil"}, headers=h).status_code == 403
    assert api.delete("/v1/devices/1", headers=h).status_code == 403
    assert api.put("/v1/memory-categories/business", json={"enabled": False}, headers=h).status_code == 403
    assert api.svc.autonomy.get() == 2 and api.svc.permissions.grant_for("shell.execute").value == "confirm"


def test_any_device_can_pull_the_emergency_stop_but_only_the_owner_can_release_it(api, phone):
    assert api.post("/v1/security/estop", headers=H(phone["token"])).json()["engaged"] is True
    assert api.post("/v1/chat", json={"message": "remind me tomorrow to x"}, headers=api.owner).json()["status"] == "blocked"
    assert api.post("/v1/security/resume", headers=H(phone["token"])).status_code == 403
    assert api.post("/v1/security/resume", headers=api.owner).json()["engaged"] is False
    assert api.post("/v1/chat", json={"message": "remind me tomorrow to x"}, headers=api.owner).json()["status"] == "completed"


def test_revoked_device_loses_access_immediately_and_last_owner_cannot_be_revoked(api, phone):
    h = H(phone["token"])
    assert api.get("/v1/status", headers=h).status_code == 200
    assert api.delete(f"/v1/devices/{phone['id']}", headers=api.owner).status_code == 200
    assert api.get("/v1/status", headers=h).status_code == 401
    owner_id = next(d["id"] for d in api.get("/v1/devices", headers=api.owner).json() if d["role"] == "owner")
    assert api.delete(f"/v1/devices/{owner_id}", headers=api.owner).status_code == 409


def test_cross_device_continuity_over_http(api, phone):
    a = api.post("/v1/chat", json={"message": "Friday, remind me tomorrow to finish the CyZy proposal"}, headers=api.owner).json()
    b = api.post("/v1/chat", json={"message": "what should I do next?"}, headers=H(phone["token"])).json()
    assert "Finish the CyZy proposal" in b["reply"] and a["conversation_id"] == b["conversation_id"] == "main"
    msgs = api.get("/v1/conversations/main/messages", headers=H(phone["token"])).json()
    assert [m["device"] for m in msgs if m["role"] == "user"] == ["owner-console", "android"]
    tasks = api.get("/v1/tasks", headers=H(phone["token"])).json()
    assert tasks[0]["title"] == "Finish the CyZy proposal" and tasks[0]["source_device"] == "owner-console"


def test_approval_raised_on_one_device_can_be_decided_on_another(api, phone):
    r = api.post("/v1/chat", json={"message": "close sleeper"}, headers=api.owner).json()
    aid = r["pending_approvals"][0]["id"]
    pend = api.get("/v1/approvals", headers=H(phone["token"])).json()
    assert [p["id"] for p in pend] == [aid]
    out = api.post(f"/v1/approvals/{aid}/approve", json={}, headers=H(phone["token"])).json()
    assert out["result"]["status"] == "ok" and out["approval"]["decided_by"] == "android"
    assert api.post(f"/v1/approvals/{aid}/approve", json={}, headers=H(phone["token"])).status_code == 409


def test_a_non_approver_device_is_refused(api):
    d = api.post("/v1/devices", json={"name": "kiosk", "kind": "bot", "can_approve": False}, headers=api.owner).json()
    r = api.post("/v1/chat", json={"message": "close sleeper"}, headers=api.owner).json()
    aid = r["pending_approvals"][0]["id"]
    assert api.post(f"/v1/approvals/{aid}/approve", json={}, headers=H(d["token"])).status_code == 403
    assert api.post(f"/v1/approvals/{aid}/deny", headers=H(d["token"])).status_code == 403


def test_owner_permission_change_takes_effect_and_is_audited(api):
    assert api.put("/v1/security/permissions/filesystem.write", json={"grant": "deny"}, headers=api.owner).status_code == 200
    r = api.post("/v1/chat", json={"message": "create a file called a.txt saying hi"}, headers=api.owner).json()
    assert r["actions"][0]["status"] == "denied"
    assert any(a["tool"] == "permission_change" for a in api.get("/v1/activity", headers=api.owner).json())
    assert api.put("/v1/security/permissions/not.a.scope", json={"grant": "allow"}, headers=api.owner).status_code == 404
    sec = api.get("/v1/security", headers=api.owner).json()
    row = next(p for p in sec["permissions"] if p["scope"] == "filesystem.write")
    assert row["grant"] == "deny" and row["implemented"] is True
    assert next(p for p in sec["permissions"] if p["scope"] == "email.send")["implemented"] is False


def test_autonomy_endpoint_validates_range(api):
    assert api.put("/v1/security/autonomy", json={"level": 9}, headers=api.owner).status_code == 422
    assert api.put("/v1/security/autonomy", json={"level": 1}, headers=api.owner).json()["level"] == 1


def test_memory_crud_export_and_category_switch(api):
    m = api.post("/v1/memory", json={"content": "Acme pays on 45-day terms", "category": "business", "project": "CyZy"}, headers=api.owner).json()
    assert api.get("/v1/memory?q=Acme", headers=api.owner).json()[0]["id"] == m["id"]
    assert api.patch(f"/v1/memory/{m['id']}", json={"content": "Acme pays on 30-day terms"}, headers=api.owner).json()["content"].endswith("30-day terms")
    assert api.get("/v1/memory-export", headers=api.owner).json()["memories"][0]["content"].endswith("30-day terms")
    api.put("/v1/memory-categories/business", json={"enabled": False}, headers=api.owner)
    assert api.post("/v1/memory", json={"content": "more", "category": "business"}, headers=api.owner).status_code == 422
    assert api.delete(f"/v1/memory/{m['id']}", headers=api.owner).status_code == 200
    assert api.delete(f"/v1/memory/{m['id']}", headers=api.owner).status_code == 404


def test_task_endpoints(api):
    t = api.post("/v1/tasks", json={"title": "Ship it", "priority": 1, "deadline": "tomorrow 5pm", "project": "CyZy"}, headers=api.owner).json()
    assert t["deadline"] == "2026-10-08T09:00:00+00:00"
    assert api.post("/v1/tasks", json={"title": "x", "deadline": "never-ish"}, headers=api.owner).status_code == 422
    assert api.patch(f"/v1/tasks/{t['id']}", json={"status": "in_progress"}, headers=api.owner).json()["status"] == "in_progress"
    assert api.patch("/v1/tasks/999", json={"status": "done"}, headers=api.owner).status_code == 404
    assert api.post(f"/v1/tasks/{t['id']}/complete", headers=api.owner).json()["status"] == "done"
    assert api.get("/v1/tasks?view=open", headers=api.owner).json() == []
    assert len(api.get(f"/v1/tasks/{t['id']}/history", headers=api.owner).json()) >= 3


def test_notifications_and_ack(api):
    api.svc.notifications.push("IMPORTANT", "Reminder: x")
    assert api.get("/v1/status", headers=api.owner).json()["unread_notifications"] == 1
    assert api.post("/v1/notifications/ack", json={}, headers=api.owner).json()["acknowledged"] == 1
    assert api.get("/v1/notifications?unread=true", headers=api.owner).json() == []


def test_audit_endpoints_and_tamper_detection(api):
    api.post("/v1/chat", json={"message": "create a file called a.txt saying hi"}, headers=api.owner)
    assert api.get("/v1/activity/verify", headers=api.owner).json()["intact"] is True
    api.svc.db.execute("UPDATE audit_log SET summary='edited' WHERE id=1")
    assert api.get("/v1/activity/verify", headers=api.owner).json() == {"intact": False, "first_bad_id": 1}


def test_security_headers_and_ui_serving(api):
    r = api.get("/")
    assert r.status_code == 200 and "FRIDAY" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert "<script>" not in r.text.replace('<script src="/ui/app.js"></script>', "")      # no inline script
    assert api.get("/ui/app.js").status_code == 200 and api.get("/ui/style.css").status_code == 200
    for evil in ["../server.py", "..%2Fserver.py", "%2e%2e/api/server.py"]:
        assert api.get(f"/ui/{evil}").status_code in (404, 422)


def test_chat_input_limits(api):
    assert api.post("/v1/chat", json={"message": ""}, headers=api.owner).status_code == 422
    assert api.post("/v1/chat", json={"message": "x" * 9000}, headers=api.owner).status_code == 422


def test_briefing_tools_workflows_skills_projects(api):
    assert "recommended_next_action" in api.get("/v1/briefing", headers=api.owner).json()
    assert len(api.get("/v1/tools", headers=api.owner).json()) == len(api.svc.registry.all())
    assert any(w["name"] == "work_mode" for w in api.get("/v1/workflows", headers=api.owner).json())
    skills = api.get("/v1/skills", headers=api.owner).json()
    assert len(skills) >= 5 and all(s["problems"] == [] for s in skills)
    assert "CyZy FX" in [p["name"] for p in api.get("/v1/projects", headers=api.owner).json()]
    assert api.post("/v1/workflows/work_mode/approve", headers=api.owner).status_code == 200
    assert api.post("/v1/workflows/nope/approve", headers=api.owner).status_code == 404


def test_scheduler_thread_starts_and_stops_with_the_app(svc):
    from fastapi.testclient import TestClient
    from friday.api.server import create_app
    app = create_app(svc, start_scheduler=True)
    with TestClient(app):
        assert app.state.scheduler._thread.is_alive()
    assert app.state.scheduler._stop.is_set()
