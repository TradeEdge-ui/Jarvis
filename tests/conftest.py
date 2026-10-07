import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.api.server import create_app
from friday.clock import FakeClock
from friday.config import Config
from friday.core.agent import Agent
from friday.core.models import ModelRouter
from friday.services import build_services
from friday.tools.base import ToolContext

START = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)   # 20:00 Wed 7 Oct in Kuala Lumpur
STAND_IN = {"command": [sys.executable, "-c", "import time; time.sleep(60)"], "process_names": []}


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def home(tmp_path):
    return tmp_path / "friday-home"


SLEEPER_NAME = "friday-test-sleeper"


@pytest.fixture
def cfg(home, tmp_path):
    c = Config.from_env(home=home, env={"FRIDAY_MODEL_PROVIDER": "rules"})
    c.read_roots, c.write_roots = [c.home], [c.home]          # never touch the real Documents/Desktop in tests
    c.app_overrides = {"standin": dict(STAND_IN)}
    if sys.platform != "win32":
        # A uniquely named executable, so app_close can match *only* this process (never "python" in general).
        link = tmp_path / SLEEPER_NAME
        link.symlink_to(sys.executable)
        c.app_overrides["sleeper"] = {"command": [str(link), "-c", "import time; time.sleep(60)"],
                                      "process_names": [SLEEPER_NAME]}
    return c


@pytest.fixture
def svc(cfg, clock):
    s = build_services(cfg, clock)
    yield s
    s.close()


@pytest.fixture
def ctx(svc):
    return ToolContext(svc=svc, user_command="test", device="pytest")


@pytest.fixture
def agent(svc):
    return Agent(svc, ModelRouter(svc))


@pytest.fixture
def launched():
    """Kill stand-in processes started by a test (tracked by pid, never by command-line matching)."""
    import psutil
    from friday.tools import apps
    before = len(apps._LAUNCHED)
    yield
    for p in apps._LAUNCHED[before:]:
        try:
            psutil.Process(p.pid).kill()
            p.wait(timeout=3)
        except Exception:
            pass


@pytest.fixture
def api(svc):
    token = svc.auth.ensure_owner(svc.config.path("Core") / "owner.token")
    app = create_app(svc, ModelRouter(svc), start_scheduler=False)
    with TestClient(app) as c:
        c.owner = {"Authorization": f"Bearer {token}"}
        c.svc = svc
        yield c


def run(svc, tool, args, **kw):
    ctx = ToolContext(svc=svc, user_command=kw.pop("cmd", "test"), device=kw.pop("device", "pytest"), **kw)
    return svc.executor.run(tool, args, ctx), ctx


def pytest_collection_modifyitems(config, items):
    """Windows has no symlinked stand-in executable (see the `cfg` fixture), so tests that close the 'sleeper'
    app can't run there. The real Windows equivalents live in test_windows.py (real notepad.exe)."""
    if sys.platform != "win32":
        return
    skip = pytest.mark.skip(reason="needs the POSIX 'sleeper' stand-in app; Windows is covered by test_windows.py")
    for item in items:
        fn = getattr(item, "function", None)
        if fn is None:
            continue
        consts = [c for c in fn.__code__.co_consts if isinstance(c, str)]
        if any("sleeper" in c for c in consts):
            item.add_marker(skip)
