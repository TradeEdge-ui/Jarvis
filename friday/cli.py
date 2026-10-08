"""`friday` command line. Local commands talk to the local workspace directly (you are the owner on your own machine)."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys

from friday import __version__
from friday.config import Config
from friday.core.agent import Agent
from friday.core.models import ModelRouter
from friday.services import Services, build_services

BANNER = "FRIDAY — personal AI operating system"


def _services(args) -> Services:
    cfg = Config.from_env(home=getattr(args, "home", None))
    return build_services(cfg)


def _first_run(svc: Services, quiet: bool = False) -> None:
    tok = svc.auth.ensure_owner(svc.config.path("Core") / "owner.token")
    if tok and not quiet:
        print(f"\nOwner token created and saved to {svc.config.path('Core') / 'owner.token'}")
        print("Use it as 'Authorization: Bearer <token>' or paste it into the web UI. It is only stored as a hash in the database.\n")


def cmd_init(args) -> int:
    svc = _services(args)
    print(f"{BANNER} v{__version__}\nWorkspace: {svc.config.home}")
    _first_run(svc)
    print("Next:  friday doctor   ·   friday chat   ·   friday serve")
    return 0


def cmd_doctor(args) -> int:
    svc = _services(args)
    _first_run(svc, quiet=True)
    router = ModelRouter(svc)
    rows: list[tuple[str, str, str]] = []

    def add(name, ok, detail):
        rows.append(("OK  " if ok is True else "WARN" if ok is None else "FAIL", name, detail))

    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    add("platform", True, f"{platform.system()} {platform.release()}")
    add("workspace", os.access(svc.config.home, os.W_OK), str(svc.config.home))
    integ = svc.db.one("PRAGMA integrity_check")[0]
    add("database", integ == "ok", f"{svc.config.db_path.name}: integrity {integ}")
    ok, bad = svc.audit.verify_chain()
    add("audit log", ok, "hash chain intact" if ok else f"TAMPERING suspected at row {bad}")
    b = router.backend()
    probe = router.probe_ollama(force=True)
    add("ollama", probe["reachable"] or None,
        f"{svc.config.ollama_url} reachable, models: {', '.join(probe['models']) or 'none'}" if probe["reachable"]
        else f"not reachable at {svc.config.ollama_url} ({probe['error']}) — optional, but needed for open-ended conversation")
    add("active brain", None if b["degraded"] else True, f"{b['provider']}:{b['model']}" + (f" — {b['reason']}" if b["reason"] else ""))
    shell = shutil.which("pwsh") or shutil.which("powershell") if sys.platform.startswith("win") else shutil.which("sh")
    add("shell", bool(shell), shell or "not found")
    try:
        import playwright  # noqa: F401
        add("browser (playwright)", True, "installed (run `playwright install chromium` if launching fails)")
    except ImportError:
        add("browser (playwright)", None, "not installed: pip install 'friday-ai[browser]'")
    from friday.tools.apps import _resolve_exe, load_registry
    launchable = [n for n, s in load_registry(svc.config.app_overrides).items() if any(_resolve_exe(c) for c in s.candidates())]
    add("launchable apps", bool(launchable) or None, ", ".join(launchable) or "none detected")
    add("autonomy", True, f"level {int(svc.autonomy.get())}")
    add("emergency stop", not svc.estop.engaged or None, "ENGAGED" if svc.estop.engaged else "released")
    bad_skills = [(s["name"], svc.skills.validate(s)) for s in svc.skills.load_all() if svc.skills.validate(s)]
    add("skills", not bad_skills, f"{len(svc.skills.load_all())} loaded" + (f"; problems: {bad_skills}" if bad_skills else ""))
    w = max(len(r[1]) for r in rows)
    for st, name, detail in rows:
        print(f"[{st}] {name:<{w}}  {detail}")
    return 1 if any(r[0] == "FAIL" for r in rows) else 0


def _print_result(r) -> None:
    print(r.reply)
    if r.pending_approvals:
        print(f"\n({len(r.pending_approvals)} action(s) waiting: run `friday approvals`)")


def cmd_chat(args) -> int:
    svc = _services(args)
    _first_run(svc, quiet=True)
    agent = Agent(svc, ModelRouter(svc))
    b = agent.router.backend()
    print(f"{BANNER}  ·  brain: {b['provider']}:{b['model']}" + (f"  [degraded: {b['reason']}]" if b["degraded"] else ""))
    cid = svc.conversations.new(device="cli") if getattr(args, "new", False) else None
    if cid:
        print(f"Starting a fresh conversation ({cid}); the shared 'main' history is untouched.")
    print("Type a message. Ctrl-C or 'exit' to quit.\n")
    from friday.scheduler import Scheduler
    Scheduler(svc).tick()
    for n in svc.notifications.list(unread_only=True, limit=5):
        print(f"[{n['priority']}] {n['title']}")
    while True:
        try:
            line = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.lower() in ("exit", "quit"):
            return 0
        if line:
            _print_result(agent.handle(line, device="cli", conversation_id=cid))
            print()


def cmd_ask(args) -> int:
    svc = _services(args)
    _first_run(svc, quiet=True)
    cid = svc.conversations.new(device="cli") if getattr(args, "new", False) else None
    r = Agent(svc, ModelRouter(svc)).handle(" ".join(args.text), device="cli", conversation_id=cid)
    _print_result(r)
    return 0 if r.status in ("info", "completed") else 2


def cmd_serve(args) -> int:
    import uvicorn
    from friday.api.server import create_app
    svc = _services(args)
    tok = svc.auth.ensure_owner(svc.config.path("Core") / "owner.token")
    if tok:
        print(f"Owner token (shown once, also saved to Core/owner.token):\n  {tok}\n")
    host, port = args.host or svc.config.host, args.port or svc.config.port
    tls = bool(svc.config.tls_cert and svc.config.tls_key)
    if host not in ("127.0.0.1", "localhost", "::1") and not tls:
        print("WARNING: listening on a non-local address WITHOUT TLS. Tokens would cross the network in clear text.\n"
              "         Set FRIDAY_TLS_CERT / FRIDAY_TLS_KEY, or put FRIDAY behind a VPN (e.g. Tailscale/WireGuard).", file=sys.stderr)
    print(f"{BANNER} v{__version__}\nOpen {'https' if tls else 'http'}://{host}:{port}/ and paste your owner token.")
    uvicorn.run(create_app(svc), host=host, port=port, log_level="warning",
                ssl_certfile=svc.config.tls_cert or None, ssl_keyfile=svc.config.tls_key or None)
    return 0


def cmd_approvals(args) -> int:
    svc = _services(args)
    rows = svc.approvals.list(None if getattr(args, "all", False) else "pending")
    if not rows:
        print("Nothing waiting for approval.")
    for a in rows:
        print(f"#{a['id']} [{a['status']}] {a['tool']} ({a['risk']}) — {json.dumps(a['display_args'])[:200]}\n    why: {a['why_needed']}")
    return 0


def cmd_approve(args) -> int:
    svc = _services(args)
    from friday.security.approvals import ApprovalError
    try:
        ap, res = svc.executor.approve_and_run(args.id, by="cli-owner", remember=args.always)
    except ApprovalError as e:
        print(f"Cannot approve: {e}")
        return 1
    print(f"{res.status}: {res.summary}")
    if res.verification:
        print("verification:", res.verification.as_text())
    return 0 if res.ok else 2


def cmd_deny(args) -> int:
    svc = _services(args)
    from friday.security.approvals import ApprovalError
    try:
        svc.executor.deny(args.id, by="cli-owner")
    except ApprovalError as e:
        print(f"Cannot deny: {e}")
        return 1
    print(f"Denied #{args.id}.")
    return 0


def cmd_estop(args) -> int:
    svc = _services(args)
    svc.estop.engage(by="cli-owner", reason="cli")
    svc.audit.record(tool="emergency_stop", status="ok", success=True, scope="system.config",
                     action="emergency stop engaged", summary="by cli-owner", device="cli")
    print("Emergency stop ENGAGED. All tool execution is disabled.")
    return 0


def cmd_resume(args) -> int:
    svc = _services(args)
    svc.estop.release(by="cli-owner")
    svc.audit.record(tool="emergency_stop", status="ok", success=True, scope="system.config",
                     action="emergency stop released", summary="by cli-owner", device="cli")
    print("Emergency stop released.")
    return 0


def cmd_token(args) -> int:
    svc = _services(args)
    did, tok = svc.auth.register(args.name, args.kind, "device", not args.no_approve)
    svc.audit.record(tool="device_register", status="ok", success=True, scope="system.config",
                     action=f"registered device '{args.name}' ({args.kind})", summary=f"device #{did}", device="cli")
    print(f"Device #{did} '{args.name}' registered. Token (shown once):\n  {tok}")
    return 0


def cmd_devices(args) -> int:
    for d in _services(args).auth.list():
        print(f"#{d['id']} {d['name']:<20} {d['kind']:<8} {d['role']:<6} {'REVOKED' if d['revoked'] else 'active':<8} last seen {d['last_seen'] or '—'}")
    return 0


def cmd_audit(args) -> int:
    svc = _services(args)
    if args.verify:
        ok, bad = svc.audit.verify_chain()
        print("Audit chain intact." if ok else f"AUDIT CHAIN BROKEN at row {bad}")
        return 0 if ok else 1
    for r in reversed(svc.audit.recent(args.n)):
        v = {1: "verified", 0: "UNVERIFIED", None: "—"}[r["verified"]]
        print(f"{r['ts'][:19]}  {r['tool']:<22} {r['status']:<18} {v:<10} {r['summary'][:80]}")
    return 0


def cmd_memory(args) -> int:
    svc = _services(args)
    if args.action == "export":
        print(json.dumps(svc.memory.export(), indent=2, default=str))
        return 0
    rows = svc.memory.search(args.query, limit=20) if args.query else svc.memory.list(limit=30)
    for m in rows:
        print(f"#{m['id']} [{m['category']}] {m['content'][:140]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="friday", description=BANNER)
    ap.add_argument("--home", help="workspace folder (default: $FRIDAY_HOME or ~/FRIDAY)")
    ap.add_argument("--version", action="version", version=f"friday {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in [("init", cmd_init, "create the workspace and owner token"), ("doctor", cmd_doctor, "check the installation"),
                            ("estop", cmd_estop, "engage the emergency stop"), ("resume", cmd_resume, "release the emergency stop"),
                            ("devices", cmd_devices, "list registered devices")]:
        sub.add_parser(name, help=help_).set_defaults(fn=fn)
    p = sub.add_parser("chat", help="interactive chat")
    p.add_argument("--new", action="store_true", help="start a fresh conversation instead of continuing 'main'")
    p.set_defaults(fn=cmd_chat)
    p = sub.add_parser("approvals", help="list pending approvals"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_approvals)
    p = sub.add_parser("ask", help="send one message"); p.add_argument("text", nargs="+")
    p.add_argument("--new", action="store_true", help="start a fresh conversation instead of continuing 'main'")
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("serve", help="run the API + web UI"); p.add_argument("--host"); p.add_argument("--port", type=int); p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("approve", help="approve a pending action"); p.add_argument("id", type=int)
    p.add_argument("--always", action="store_true", help="remember this exact action as pre-approved"); p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("deny", help="deny a pending action"); p.add_argument("id", type=int); p.set_defaults(fn=cmd_deny)
    p = sub.add_parser("token", help="register a device and print its token")
    p.add_argument("name"); p.add_argument("--kind", default="phone", choices=["pc", "phone", "browser", "bot", "other"])
    p.add_argument("--no-approve", action="store_true"); p.set_defaults(fn=cmd_token)
    p = sub.add_parser("audit", help="show the action log"); p.add_argument("-n", type=int, default=20)
    p.add_argument("--verify", action="store_true"); p.set_defaults(fn=cmd_audit)
    p = sub.add_parser("memory", help="search / export memory"); p.add_argument("action", choices=["search", "export"])
    p.add_argument("query", nargs="?"); p.set_defaults(fn=cmd_memory)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
