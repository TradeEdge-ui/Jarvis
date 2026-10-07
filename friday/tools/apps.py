"""Application control. Apps are launched from a registry (never arbitrary executables), and every launch
is verified by inspecting the real process table."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from pydantic import BaseModel, Field

from friday.security.policy import Classification, Effect, Risk
from friday.tools.base import Tool, ToolContext, ToolResult, Verification
from friday.tools.pathpolicy import PathDenied

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
_LAUNCHED: list[subprocess.Popen] = []   # keep handles so exited children are reaped


@dataclass
class AppSpec:
    name: str
    aliases: list[str] = field(default_factory=list)
    windows: list[list[str]] = field(default_factory=list)   # candidate argv, tried in order
    linux: list[list[str]] = field(default_factory=list)
    mac: list[list[str]] = field(default_factory=list)
    command: list[str] = field(default_factory=list)          # user-defined, all platforms
    process_names: list[str] = field(default_factory=list)    # to verify / close

    def candidates(self) -> list[list[str]]:
        if self.command:
            return [self.command]
        return self.windows if IS_WINDOWS else self.mac if IS_MAC else self.linux


_PF = r"%ProgramFiles%"
_PF86 = r"%ProgramFiles(x86)%"
_LA = r"%LocalAppData%"
BUILTIN_APPS: list[AppSpec] = [
    AppSpec("notepad", ["text editor", "editor"], windows=[["notepad.exe"]],
            linux=[["gedit"], ["kate"], ["mousepad"], ["xed"], ["gnome-text-editor"]], mac=[["open", "-a", "TextEdit"]],
            process_names=["notepad.exe", "gedit", "kate", "mousepad", "xed", "gnome-text-editor", "TextEdit"]),
    AppSpec("calculator", ["calc"], windows=[["calc.exe"]],
            linux=[["gnome-calculator"], ["kcalc"], ["galculator"]], mac=[["open", "-a", "Calculator"]],
            process_names=["calc.exe", "CalculatorApp.exe", "Calculator.exe", "gnome-calculator", "kcalc", "galculator", "Calculator"]),
    AppSpec("chrome", ["google chrome", "browser"],
            windows=[["chrome.exe"], [_PF + r"\Google\Chrome\Application\chrome.exe"],
                     [_PF86 + r"\Google\Chrome\Application\chrome.exe"], [_LA + r"\Google\Chrome\Application\chrome.exe"]],
            linux=[["google-chrome"], ["google-chrome-stable"], ["chromium"], ["chromium-browser"]],
            mac=[["open", "-a", "Google Chrome"]],
            process_names=["chrome.exe", "chrome", "google-chrome", "chromium", "Google Chrome"]),
    AppSpec("edge", ["microsoft edge"], windows=[["msedge.exe"], [_PF86 + r"\Microsoft\Edge\Application\msedge.exe"]],
            mac=[["open", "-a", "Microsoft Edge"]], process_names=["msedge.exe", "msedge"]),
    AppSpec("firefox", [], windows=[["firefox.exe"], [_PF + r"\Mozilla Firefox\firefox.exe"]], linux=[["firefox"]],
            mac=[["open", "-a", "Firefox"]], process_names=["firefox.exe", "firefox"]),
    AppSpec("vscode", ["vs code", "visual studio code", "code"], windows=[["code.cmd"], [_LA + r"\Programs\Microsoft VS Code\Code.exe"]],
            linux=[["code"]], mac=[["open", "-a", "Visual Studio Code"]], process_names=["Code.exe", "code", "Code"]),
    AppSpec("explorer", ["file explorer", "files", "file manager"], windows=[["explorer.exe"]],
            linux=[["nautilus"], ["dolphin"], ["thunar"], ["nemo"]], mac=[["open", "-a", "Finder"]],
            process_names=["explorer.exe", "nautilus", "dolphin", "thunar", "nemo", "Finder"]),
    AppSpec("terminal", ["windows terminal", "console"], windows=[["wt.exe"], ["cmd.exe"]],
            linux=[["gnome-terminal"], ["konsole"], ["xterm"]], mac=[["open", "-a", "Terminal"]],
            process_names=["WindowsTerminal.exe", "wt.exe", "cmd.exe", "gnome-terminal", "konsole", "xterm", "Terminal"]),
    AppSpec("excel", ["microsoft excel"], windows=[["excel.exe"]], mac=[["open", "-a", "Microsoft Excel"]],
            process_names=["EXCEL.EXE", "Microsoft Excel"]),
    AppSpec("word", ["microsoft word"], windows=[["winword.exe"]], mac=[["open", "-a", "Microsoft Word"]],
            process_names=["WINWORD.EXE", "Microsoft Word"]),
    AppSpec("telegram", [], windows=[["Telegram.exe"], [_LA + r"\Programs\Telegram Desktop\Telegram.exe"],
                                     [r"%AppData%\Telegram Desktop\Telegram.exe"]], linux=[["telegram-desktop"]],
            mac=[["open", "-a", "Telegram"]], process_names=["Telegram.exe", "telegram-desktop", "Telegram"]),
]


def load_registry(overrides: dict | None = None) -> dict[str, AppSpec]:
    reg = {a.name: a for a in BUILTIN_APPS}
    for name, o in (overrides or {}).items():
        reg[name.lower()] = AppSpec(
            name.lower(), [a.lower() for a in o.get("aliases", [])], command=list(o.get("command", [])),
            process_names=list(o.get("process_names", [])))
    return reg


def find_app(registry: dict[str, AppSpec], query: str) -> AppSpec | None:
    q = query.strip().lower().replace(".exe", "")
    if q in registry:
        return registry[q]
    for a in registry.values():
        if q in a.aliases:
            return a
    return None


def _resolve_exe(argv: list[str]) -> list[str] | None:
    exe = os.path.expandvars(argv[0])
    if os.path.isabs(exe):
        return [exe, *argv[1:]] if os.path.exists(exe) else None
    found = shutil.which(exe)
    return [found, *argv[1:]] if found else None


def running_matches(names: list[str], since: float | None = None) -> list[dict]:
    want = {n.lower() for n in names}
    out = []
    for p in psutil.process_iter(["pid", "name", "create_time", "status"]):
        try:
            nm = (p.info["name"] or "").lower()
            if nm in want and p.info["status"] != psutil.STATUS_ZOMBIE:
                out.append({"pid": p.info["pid"], "name": p.info["name"], "create_time": p.info["create_time"],
                            "new": since is not None and p.info["create_time"] >= since})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


class _Base(Tool):
    group = "apps"

    def _registry(self, ctx: ToolContext) -> dict[str, AppSpec]:
        return load_registry(ctx.svc.config.app_overrides)


class AppOpen(_Base):
    name = "app_open"
    scope = "apps.launch"
    description = ("Launch a registered application (notepad, calculator, chrome, edge, firefox, vscode, explorer, "
                   "terminal, excel, word, telegram, or a user-defined app), optionally opening one file or http(s) URL in it.")

    class Args(BaseModel):
        app: str = Field(description="Application name or alias, e.g. 'notepad', 'chrome'.")
        target: str | None = Field(default=None, description="Optional file path or http(s) URL to open with the app.")

    def _check_target(self, ctx, target: str | None) -> str | None:
        if not target:
            return None
        if target.lower().startswith(("http://", "https://")):
            return target
        p = ctx.svc.paths.check_read(target)
        if not p.exists():
            raise FileNotFoundError(f"{p} does not exist")
        return str(p)

    def classify(self, args, ctx):
        spec = find_app(self._registry(ctx), args.app)
        if spec is None:
            return Classification(blocked=f"'{args.app}' is not a registered application "
                                          f"(known: {', '.join(sorted(self._registry(ctx)))})")
        try:
            self._check_target(ctx, args.target)
        except (PathDenied, FileNotFoundError) as e:
            return Classification(blocked=str(e))
        return Classification(Risk.LOW, Effect.EXTERNAL)

    def run(self, args, ctx):
        spec = find_app(self._registry(ctx), args.app)
        target = self._check_target(ctx, args.target)
        tried: list[str] = []
        for cand in spec.candidates():
            argv = _resolve_exe(cand)
            if argv is None:
                tried.append(f"{cand[0]}: not found")
                continue
            if target:
                argv = [*argv, target]
            before = time.time() - 1.0
            kw: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            if IS_WINDOWS:
                kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            else:
                kw["start_new_session"] = True
            try:
                proc = subprocess.Popen(argv, **kw)
            except OSError as e:
                tried.append(f"{argv[0]}: {e}")
                continue
            _LAUNCHED.append(proc)
            return ToolResult.success(f"Launched {spec.name}" + (f" with {Path(target).name if not target.startswith('http') else target}" if target else ""),
                                      app=spec.name, argv=argv, pid=proc.pid, launched_at=before,
                                      process_names=spec.process_names, tried=tried)
        return ToolResult.fail(f"Could not launch {spec.name}: no working launch command ({'; '.join(tried) or 'no candidates for this OS'})",
                               tried=tried)

    SETTLE_S = 0.7   # a process seen once may still crash during startup; require it to survive this long

    @staticmethod
    def _alive(d: dict, proc) -> str | None:
        """Describe a live process belonging to this launch, or None."""
        if d["process_names"]:
            new = [x for x in running_matches(d["process_names"], since=d["launched_at"]) if x["new"]]
            if new:
                return f"{new[0]['name']} running (pid {new[0]['pid']}, started by this launch)"
        if proc is not None and proc.poll() is None:
            try:
                if psutil.Process(d["pid"]).status() != psutil.STATUS_ZOMBIE:
                    return f"launched pid {d['pid']} is alive"
            except psutil.NoSuchProcess:
                pass
        return None

    def verify(self, args, result, ctx):
        d = result.data
        proc = next((p for p in _LAUNCHED if p.pid == d["pid"]), None)
        deadline = time.time() + 8.0
        while True:
            seen = self._alive(d, proc)
            if seen:
                time.sleep(self.SETTLE_S)
                if self._alive(d, proc):
                    return Verification(True, "process table", f"{seen}; still running after {self.SETTLE_S:g}s")
                break   # it appeared and then died: do not call that a success
            if time.time() > deadline:
                break
            time.sleep(0.2)
        # single-instance apps (Chrome, Explorer, VS Code...) hand off to a running instance and the launcher exits 0
        if d["process_names"] and proc is not None and proc.poll() == 0:
            m = running_matches(d["process_names"])
            if m:
                return Verification(True, "process table",
                                    f"launcher exited cleanly and {m[0]['name']} (pid {m[0]['pid']}) is running "
                                    "— likely handed off to an existing instance")
        code = proc.poll() if proc is not None else None
        return Verification(False, "process table", f"no running {args.app} process after launch"
                            + (f" (launcher exit code {code})" if code is not None else ""))


class AppClose(_Base):
    name = "app_close"
    scope = "apps.close"
    description = "Close a registered application by terminating its processes. Unsaved work in it may be lost."

    class Args(BaseModel):
        app: str

    def classify(self, args, ctx):
        spec = find_app(self._registry(ctx), args.app)
        if spec is None:
            return Classification(blocked=f"'{args.app}' is not a registered application")
        if not spec.process_names:
            return Classification(blocked=f"'{spec.name}' has no process names registered, so it cannot be closed safely")
        return Classification(Risk.REVIEW, Effect.EXTERNAL,
                              note=f"Closing {spec.name} will end its processes; unsaved work may be lost.")

    def run(self, args, ctx):
        spec = find_app(self._registry(ctx), args.app)
        procs = running_matches(spec.process_names)
        if not procs:
            return ToolResult.success(f"{spec.name} is not running, so there was nothing to close", app=spec.name, pids=[],
                                      process_names=spec.process_names)
        pids = [p["pid"] for p in procs]
        for pid in pids:
            try:
                psutil.Process(pid).terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs([psutil.Process(p) for p in pids if psutil.pid_exists(p)], timeout=5)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        return ToolResult.success(f"Terminated {len(pids)} {spec.name} process(es)", app=spec.name, pids=pids,
                                  process_names=spec.process_names)

    def verify(self, args, result, ctx):
        if not result.data["pids"]:
            return Verification(True, "process table", f"no {args.app} process is running")
        time.sleep(0.3)
        left = [m for m in running_matches(result.data["process_names"]) if m["pid"] in result.data["pids"]]
        return Verification(not left, "process table", "all terminated" if not left else f"still running: {left[0]['pid']}")


class AppList(_Base):
    name = "app_list"
    scope = "system.diagnostics"
    description = "List registered applications and whether each is currently running."

    class Args(BaseModel):
        pass

    def run(self, args, ctx):
        rows = []
        for spec in self._registry(ctx).values():
            rows.append({"app": spec.name, "aliases": spec.aliases,
                         "running": bool(spec.process_names and running_matches(spec.process_names)),
                         "launchable": any(_resolve_exe(c) for c in spec.candidates())})
        return ToolResult.success(f"{len(rows)} registered apps", apps=rows)


TOOLS = [AppOpen, AppClose, AppList]
