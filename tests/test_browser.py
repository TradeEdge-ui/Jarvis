import pytest

from browser_support import chromium, web  # noqa: F401  (fixtures)
from friday.tools.browser import _host_is_private
from friday.tools.base import ToolContext
from conftest import run

pytestmark = pytest.mark.browser


def test_reads_a_real_page_and_verifies_it(svc, chromium, web):
    svc.config.allow_local_browse = True
    res, ctx = run(svc, "browser_read", {"url": web + "/", "screenshot": True})
    assert res.ok and res.data["title"] == "Test Page" and "Hello FRIDAY" in res.data["text"] and res.data["status"] == 200
    assert res.verification.verified is True and any(l["text"] == "More news" for l in res.data["links"])
    import os; assert os.path.getsize(res.data["screenshot"]) > 1000
    assert ctx.tainted is True                                    # web content marks the run as untrusted


def test_http_errors_are_failures_not_content(svc, chromium, web):
    svc.config.allow_local_browse = True
    res, _ = run(svc, "browser_read", {"url": web + "/missing"})
    assert res.status == "unverified" and "HTTP 404" in res.error


def test_after_reading_a_page_external_actions_need_approval(svc, chromium, web):
    svc.config.allow_local_browse = True
    _, ctx = run(svc, "browser_read", {"url": web + "/inject"})
    assert ctx.tainted
    res = svc.executor.run("app_open", {"app": "standin"}, ctx)                    # same run, now tainted
    assert res.status == "approval_required"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/", "http://localhost/", "http://169.254.169.254/latest/meta-data/",
                                 "http://10.0.0.5/admin", "http://192.168.1.1/", "http://[::1]/", "file:///etc/passwd",
                                 "ftp://example.com/x", "javascript:alert(1)", "http://metadata.google.internal/"])
def test_private_and_non_http_urls_are_blocked_by_default(svc, url):
    res, _ = run(svc, "browser_read", {"url": url})
    assert res.status == "denied", url


def test_private_host_detection():
    assert _host_is_private("127.0.0.1") and _host_is_private("10.1.2.3") and _host_is_private("169.254.169.254")
    assert _host_is_private("localhost") and _host_is_private("foo.localhost")
    assert not _host_is_private("8.8.8.8")
