"""Service container: wires config, storage, security, tools and the executor together."""
from __future__ import annotations

from friday.business import seed_projects
from friday.clock import Clock
from friday.config import Config
from friday.conversations import Conversations
from friday.db import Database
from friday.memory.store import MemoryStore
from friday.notifications import Notifications
from friday.security.approvals import ApprovalService
from friday.security.audit import AuditLog
from friday.security.auth import DeviceRegistry
from friday.security.autonomy import AutonomyStore
from friday.security.estop import EmergencyStop
from friday.security.permissions import PermissionStore
from friday.security.policy import PolicyEngine
from friday.skills import SkillLibrary
from friday.tasks import TaskEngine
from friday.tools.executor import Executor
from friday.tools.pathpolicy import PathPolicy
from friday.tools.registry import ToolRegistry
from friday.workflows import Workflows


class Services:
    def __init__(self, config: Config, clock: Clock | None = None):
        config.ensure_workspace()
        self.config = config
        self.clock = clock or Clock()
        self.db = Database(config.db_path)
        self.permissions = PermissionStore(self.db)
        self.autonomy = AutonomyStore(self.db, config.default_autonomy)
        self.estop = EmergencyStop(self.db, self.clock)
        self.approvals = ApprovalService(self.db, self.clock)
        self.audit = AuditLog(self.db, self.clock)
        self.auth = DeviceRegistry(self.db, self.clock)
        self.policy = PolicyEngine(self.permissions, self.autonomy, self.estop, self.approvals)
        self.memory = MemoryStore(self.db, self.clock)
        self.tasks = TaskEngine(self.db, self.clock)
        self.notifications = Notifications(self.db, self.clock)
        self.conversations = Conversations(self.db, self.clock)
        self.paths = PathPolicy(config)
        self.registry = ToolRegistry()
        self._register_tools()
        self.executor = Executor(self)
        self.workflows = Workflows(self)
        self.skills = SkillLibrary(self)
        self.workflows.ensure_defaults()
        self.skills.install_builtin()
        seed_projects(self.db, self.clock)

    def _register_tools(self) -> None:
        from friday.tools import apps, browser, business, data, filesystem, personal, shell, system
        for mod in (filesystem, shell, apps, system, personal, data, business, browser):
            for cls in mod.TOOLS:
                self.registry.register(cls())

    def close(self) -> None:
        self.db.close()


def build_services(config: Config | None = None, clock: Clock | None = None) -> Services:
    return Services(config or Config.from_env(), clock)
