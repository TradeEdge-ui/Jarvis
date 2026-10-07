"""PowerShell / shell execution.

Read-only diagnostics (a strict allowlist, no redirection / chaining / subexpressions) run without asking.
Everything else is REVIEW-level and, with the default `shell.execute: confirm` permission, waits for approval.
Destructive or security-sensitive patterns are MANDATORY approval; catastrophic ones are blocked outright.
The classifier is best-effort: the real control is that arbitrary commands need a human to read and approve them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Literal

from pydantic import BaseModel, Field

from friday.security.policy import Classification, Effect, Risk
from friday.tools.base import Tool, ToolContext, ToolResult, Verification
from friday.util import truncate

IS_WINDOWS = sys.platform.startswith("win")

READONLY_CMDS = {
    "hostname", "whoami", "ipconfig", "systeminfo", "tasklist", "get-process", "get-service", "get-computerinfo",
    "get-netipaddress", "get-netadapter", "get-date", "get-psdrive", "get-ciminstance", "get-volume",
    "get-disk", "get-hotfix", "get-timezone", "get-uptime", "test-connection", "ping", "nslookup", "ver",
    "uname", "uptime", "df", "free", "ps", "date", "id", "select-object", "sort-object", "where-object",
    "format-table", "format-list", "measure-object", "out-string", "select-string", "ft", "fl", "sort", "select",
}
NETWORK_CMDS = {"test-connection", "ping", "nslookup"}

BLOCKED = [
    (r"format-volume|\bformat\s+[a-z]:|\bmkfs\b|\bdiskpart\b|clear-disk|remove-partition", "disk formatting/partitioning"),
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)[a-z]*\s+(/|~|\$home|c:\\?)(\s|$)", "recursive delete of a root/home folder"),
    (r"\bdd\s+.*\bof=/dev/", "raw disk write"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"remove-item\s+.*-recurse.*\s+[a-z]:\\?(\s|$)", "recursive delete of a drive root"),
    (r"\bcipher\s+/w", "secure wipe of free space"),
]
MANDATORY = [
    (r"\bshutdown\b|restart-computer|stop-computer|\breboot\b|\bpoweroff\b|\bhalt\b", "shuts down or restarts the machine"),
    (r"\breg(\.exe)?\s+(add|delete|import)\b|remove-itemproperty|set-itemproperty|new-itemproperty", "edits the registry"),
    (r"\bnet\s+(user|localgroup)\b|new-localuser|add-localgroupmember|set-localuser", "changes user accounts"),
    (r"set-executionpolicy", "changes the PowerShell execution policy"),
    (r"\bnetsh\b|netfirewallrule|set-netfirewall", "changes network/firewall configuration"),
    (r"\bschtasks\b|register-scheduledtask|unregister-scheduledtask", "changes scheduled tasks"),
    (r"\bsc(\.exe)?\s+(delete|config|create)\b|remove-service|set-service|new-service", "changes services"),
    (r"remove-item|\brm\b|\bdel\b|\brmdir\b|\brd\b|\berase\b|\bunlink\b", "deletes files"),
    (r"\bcurl\b|\bwget\b|invoke-webrequest|\biwr\b|invoke-restmethod|\birm\b|downloadstring|downloadfile|start-bitstransfer",
     "downloads from the network"),
    (r"invoke-expression|\biex\b|-encodedcommand|\s-enc\s", "executes dynamically generated code"),
    (r"\bbcdedit\b|\btakeown\b|\bicacls\b|\bchmod\b|\bchown\b|\bsudo\b|\brunas\b", "changes permissions/boot/elevation"),
    (r"taskkill|stop-process|\bkill(all)?\b", "terminates processes"),
    (r"set-mppreference|add-mppreference|disable-windowsoptionalfeature", "changes security software settings"),
    (r"\bformat-", "formatting"),  # placeholder never matches Format-Table because handled before
]
_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL)", re.I)


def _is_readonly(cmd: str) -> bool:
    if any(ch in cmd for ch in ";&><`{}\n\r") or "$(" in cmd or "${" in cmd:
        return False
    segs = [s.strip() for s in cmd.split("|")]
    if not segs or any(not s for s in segs):
        return False
    return all(s.split()[0].lower() in READONLY_CMDS for s in segs)


def classify_command(cmd: str) -> tuple[str, str]:
    """-> (kind, detail) with kind in blocked | mandatory | readonly | review."""
    low = cmd.lower()
    for pat, why in BLOCKED:
        if re.search(pat, low):
            return "blocked", why
    for pat, why in MANDATORY:
        if pat == r"\bformat-":
            continue
        if re.search(pat, low):
            return "mandatory", why
    if _is_readonly(cmd):
        return "readonly", ""
    return "review", "arbitrary command"


def _shell_argv(shell: str, command: str) -> list[str]:
    if shell == "auto":
        shell = "powershell" if IS_WINDOWS else "sh"
    if shell == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            raise FileNotFoundError("PowerShell (pwsh / powershell.exe) not found on PATH")
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
    sh = shutil.which("sh") or shutil.which("bash")
    if not sh:
        raise FileNotFoundError("no POSIX shell found")
    return [sh, "-c", command]


class ShellRun(Tool):
    name = "shell_run"
    scope = "shell.execute"
    extra_scopes = ("shell.diagnostics",)
    group = "system"
    description = ("Run a PowerShell (Windows) or sh command and return its output. Read-only diagnostics such as "
                   "Get-Process, ipconfig, hostname run immediately; anything else needs the user's approval. "
                   "Prefer fs_* tools for files.")

    class Args(BaseModel):
        command: str = Field(description="The command line to run.")
        shell: Literal["auto", "powershell", "sh"] = "auto"
        timeout_s: int = Field(default=30, ge=1, le=300)

    def scope_for(self, args):
        return "shell.diagnostics" if classify_command(args.command)[0] == "readonly" else "shell.execute"

    def classify(self, args, ctx):
        kind, why = classify_command(args.command)
        if kind == "blocked":
            return Classification(blocked=f"command is never allowed ({why})")
        if kind == "mandatory":
            return Classification(Risk.MANDATORY, Effect.EXTERNAL, note=f"This command {why}. Approval is always required.")
        if kind == "readonly":
            first = args.command.strip().split()[0].lower()
            if ctx.tainted and first in NETWORK_CMDS:
                return Classification(Risk.REVIEW, Effect.EXTERNAL, note="Network probe after reading untrusted content.")
            return Classification(Risk.SAFE, Effect.READ)
        return Classification(Risk.REVIEW, Effect.EXTERNAL, note="Arbitrary command: please read it before approving.")

    def action_text(self, args):
        return f"{args.shell}: {args.command}"

    def run(self, args, ctx):
        try:
            argv = _shell_argv(args.shell, args.command)
        except FileNotFoundError as e:
            return ToolResult.fail(str(e))
        env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k) and not k.startswith("FRIDAY_")}
        try:
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=args.timeout_s,
                                cwd=str(ctx.svc.config.home), env=env, errors="replace")
        except subprocess.TimeoutExpired as e:
            partial = truncate((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), 2000)
            return ToolResult.fail(f"Command timed out after {args.timeout_s}s", partial_output=partial)
        out, err = truncate(cp.stdout or "", 20000), truncate(cp.stderr or "", 5000)
        data = {"exit_code": cp.returncode, "stdout": out, "stderr": err, "argv0": os.path.basename(argv[0])}
        if cp.returncode != 0:
            return ToolResult("failed", f"Command exited with code {cp.returncode}: {truncate(err or out, 300)}",
                              data, error=f"exit code {cp.returncode}")
        return ToolResult.success(f"Command exited 0 ({len(out)} chars of output)", **data)

    def verify(self, args, result, ctx):
        kind, _ = classify_command(args.command)
        if kind == "readonly":
            return Verification(None, "n/a", "read-only diagnostic; output is the result")
        return Verification(None, "exit code", "exit code 0 — the command's effect was NOT independently verified")


TOOLS = [ShellRun]
