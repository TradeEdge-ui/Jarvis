"""System diagnostics (read-only)."""
from __future__ import annotations

import platform
import socket
import sys
import time
from urllib.parse import urlparse

import httpx
import psutil
from pydantic import BaseModel, Field

from friday.security.policy import Classification
from friday.tools.base import Tool, ToolResult


class SystemInfo(Tool):
    name = "system_info"
    scope = "system.diagnostics"
    group = "system"
    description = "CPU, memory, disk, uptime and the top processes by memory on this machine."

    class Args(BaseModel):
        top_processes: int = Field(default=5, ge=0, le=20)

    def run(self, args, ctx):
        vm = psutil.virtual_memory()
        disks = []
        for root in {str(r) for r in ctx.svc.config.read_roots[:1]} | {str(ctx.svc.config.home.anchor or "/")}:
            try:
                du = psutil.disk_usage(root)
                disks.append({"path": root, "total_gb": round(du.total / 1e9, 1), "free_gb": round(du.free / 1e9, 1),
                              "used_percent": du.percent})
            except OSError:
                pass
        procs = []
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                procs.append((p.info["memory_info"].rss, p.info["pid"], p.info["name"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                continue
        procs.sort(reverse=True)
        data = {
            "os": f"{platform.system()} {platform.release()}", "host": socket.gethostname(),
            "python": sys.version.split()[0], "cpu_count": psutil.cpu_count(),
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "memory": {"total_gb": round(vm.total / 1e9, 1), "available_gb": round(vm.available / 1e9, 1), "used_percent": vm.percent},
            "disks": disks, "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
            "top_processes": [{"pid": pid, "name": n, "rss_mb": round(r / 1e6, 1)} for r, pid, n in procs[:args.top_processes]],
        }
        alerts = []
        if vm.percent > 90:
            alerts.append(f"memory use is high ({vm.percent}%)")
        alerts += [f"disk {d['path']} is {d['used_percent']}% full" for d in disks if d["used_percent"] > 90]
        data["alerts"] = alerts
        summ = (f"{data['os']} on {data['host']}: CPU {data['cpu_percent']}%, memory {vm.percent}% used, "
                f"{len(disks)} disk(s) checked" + (f"; ALERTS: {'; '.join(alerts)}" if alerts else "; no alerts"))
        return ToolResult.success(summ, **data)


class ServiceCheck(Tool):
    name = "service_check"
    scope = "system.diagnostics"
    group = "system"
    description = ("Diagnose whether a local service is healthy: process running, port reachable, HTTP responding. "
                   "Known profile: 'ollama'. Otherwise pass a url. Returns findings, likely causes and suggested "
                   "(not executed) fixes.")

    class Args(BaseModel):
        service: str = Field(default="ollama", description="'ollama' or any label")
        url: str | None = Field(default=None, description="Base URL to probe (defaults to the configured Ollama URL)")

    def run(self, args, ctx):
        is_ollama = args.service.lower() == "ollama"
        url = (args.url or (ctx.svc.config.ollama_url if is_ollama else "")).rstrip("/")
        if not url:
            return ToolResult.fail("no url given for this service")
        u = urlparse(url)
        host, port = u.hostname or "localhost", u.port or (443 if u.scheme == "https" else 80)
        findings, causes, fixes = [], [], []
        proc_names = {"ollama", "ollama.exe", "ollama app.exe", "ollama_llama_server"} if is_ollama else set()
        proc_up = None
        if proc_names:
            proc_up = any((p.info["name"] or "").lower() in proc_names
                          for p in psutil.process_iter(["name"]) if p.info.get("name"))
            findings.append(f"process: {'running' if proc_up else 'NOT running'}")
        try:
            with socket.create_connection((host, port), timeout=2):
                port_up = True
        except OSError as e:
            port_up = False
            findings.append(f"port {host}:{port}: closed/unreachable ({e.__class__.__name__})")
        else:
            findings.append(f"port {host}:{port}: open")
        http_ok, models = None, None
        if port_up:
            try:
                r = httpx.get(url + ("/api/tags" if is_ollama else ""), timeout=4.0)
                http_ok = r.status_code < 500
                findings.append(f"HTTP GET: {r.status_code}")
                if is_ollama and r.status_code == 200:
                    models = [m.get("name") for m in r.json().get("models", [])]
                    findings.append(f"models installed: {', '.join(models) if models else 'none'}")
            except Exception as e:
                http_ok = False
                findings.append(f"HTTP GET failed: {e.__class__.__name__}")
        healthy = bool(port_up and http_ok and (models is None or models))
        if not healthy:
            if is_ollama and proc_up is False and not port_up:
                causes.append("The Ollama server is not running.")
                fixes += ["Start the Ollama app, or run `ollama serve` in a terminal."]
            elif not port_up:
                causes.append(f"Nothing is listening on {host}:{port}. The service may be bound to another address/port "
                              "(for Ollama check the OLLAMA_HOST environment variable) or blocked by a firewall.")
                fixes += [f"Confirm FRIDAY_OLLAMA_URL ({url}) matches where the server listens.",
                          "Check Windows Defender Firewall / security software."]
            elif http_ok is False:
                causes.append("The port is open but the service did not answer correctly (wrong service, still starting, or crashed).")
                fixes += ["Check the service log; restart the service."]
            if is_ollama and port_up and http_ok and models == []:
                causes.append("Ollama is running but has no models installed.")
                fixes += [f"Run `ollama pull {ctx.svc.config.ollama_model}`."]
        summary = f"{args.service}: {'healthy' if healthy else 'NOT healthy'} — " + "; ".join(findings)
        return ToolResult.success(summary, healthy=healthy, findings=findings, likely_causes=causes,
                                  suggested_fixes=fixes, url=url, models=models)


TOOLS = [SystemInfo, ServiceCheck]
