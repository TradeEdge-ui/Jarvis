"""Drives the real dashboard in headless Chromium against a live server. Would have caught the broken
autonomy/permission endpoints and the inline-style CSP violation."""
import socket
import threading
import time

import pytest
import uvicorn

from browser_support import chromium, chromium_path, launch_args  # noqa: F401
from friday.api.server import create_app
from friday.core.models import ModelRouter

pytestmark = pytest.mark.browser


@pytest.fixture
def live(svc):
    token = svc.auth.ensure_owner(svc.config.path("Core") / "owner.token")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(svc, ModelRouter(svc), start_scheduler=False), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", token
    server.should_exit = True; t.join(timeout=5)


def test_dashboard_end_to_end(svc, chromium, live):
    from playwright.sync_api import sync_playwright
    base, token = live
    problems = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, executable_path=chromium, args=launch_args()) if chromium else pw.chromium.launch(headless=True, args=launch_args())
        pg = b.new_page(viewport={"width": 1300, "height": 900})
        pg.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: problems.append(str(e)))

        # wrong token is rejected, right token (via URL fragment) connects and the fragment is scrubbed
        pg.goto(base + "/#token=fri_wrong"); pg.wait_for_selector("#login:not([hidden])")
        assert "rejected" in pg.inner_text("#loginErr").lower() or "could not" in pg.inner_text("#loginErr").lower()
        pg.goto("about:blank"); pg.goto(base + "/#token=" + token); pg.wait_for_selector("#app:not([hidden])")  # blank first: a fragment-only change would not reload
        assert "token" not in pg.url
        problems.clear()                       # the deliberate wrong-token attempt above legitimately logs a 401

        def say(text):
            pg.fill("#msg", text); pg.click("#send"); pg.wait_for_selector("#send:not([disabled])"); pg.wait_for_timeout(300)
        say("remind me tomorrow to finish the CyZy proposal")
        assert "Finish the CyZy proposal" in pg.inner_text("#tasks")
        assert "verified" in pg.inner_text("#log .msg.assistant:last-child .chips")

        say("close sleeper")                                           # needs approval -> card appears
        pg.wait_for_selector("#approvals .item")
        pg.click("#approvals .btn.ok")
        pg.wait_for_selector("#approvals .item", state="detached")
        assert "Approved #1" in pg.inner_text("#log .msg.assistant:last-child")

        pg.select_option("#autonomy", "1"); pg.wait_for_timeout(500)          # was silently broken before this test existed
        assert svc.autonomy.get() == 1
        pg.click("[data-tab=security]"); pg.wait_for_selector("#tab-security select.grant")
        pg.select_option("select[aria-label='Grant for filesystem.write']", "deny"); pg.wait_for_timeout(500)
        assert svc.permissions.grant_for("filesystem.write").value == "deny"

        pg.click("#estop"); pg.wait_for_selector("#estopBanner:not([hidden])")
        assert svc.estop.engaged
        pg.click("#resume"); pg.wait_for_selector("#estopBanner", state="hidden")
        assert not svc.estop.engaged

        # untrusted text can never become markup
        svc.tasks.add("<img src=x onerror=alert(1)>", project="<b>x</b>")
        pg.wait_for_timeout(3500)
        assert pg.locator("#tasks img").count() == 0 and "<img" in pg.inner_text("#tasks")
        pg.click("[data-tab=activity]"); pg.wait_for_selector("#tab-activity table")
        assert "intact" in pg.inner_text("#tab-activity")
        b.close()
    assert problems == [], problems
