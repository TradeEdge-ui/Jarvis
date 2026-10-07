# FRIDAY — personal AI operating system

One assistant identity, shared across your PC, phone and browser: it remembers, plans, acts on your computer
through **permissioned tools**, **verifies** what it did, and tells you the truth about the result.

> **Status: Phase 1–2 vertical slice, working and tested. Not the whole vision.**
> [`docs/STATUS.md`](docs/STATUS.md) lists, section by section of the spec, what is built, what is tested and how,
> and what is not built yet. Read it before trusting anything.

## What works today

```text
you ─ text / browser voice ─▶ FRIDAY core ─▶ brain (Ollama | OpenAI-compatible | offline rules)
                                  │              │ proposes tool calls
                                  │              ▼
                                  │   policy engine (plain code, no model):  permission × risk × autonomy × estop
                                  │              │ EXECUTE / APPROVAL / PROPOSE / DENY
                                  ▼              ▼
              SQLite: memory (FTS5) · tasks · approvals · hash-chained audit log · devices
                                                 │
                                                 ▼   run ─▶ VERIFY (re-read file, process table, DB) ─▶ report
```

* **Real PC actions, verified.** Launch/close apps (checked against the process table, with a startup-survival
  window), read/write/move/delete files (re-read + SHA-256; overwrites and deletes are always recoverable from `Backups/`),
  run PowerShell/sh (read-only diagnostics run freely, anything else needs your approval), read web pages in a headless browser.
* **Never claims success it didn't verify.** A tool result is `ok`, `unverified`, `failed`, `denied`, `approval_required` or
  `proposed`. Replies are rendered from those results; if a language model's wording disagrees with them, a factual ledger is appended.
* **You stay in control.** Per-capability grants (`allow` / `confirm` / `deny`), autonomy levels 0–4, mandatory approval for
  high-impact actions at *every* level, standing approvals only for exact low-risk actions, an emergency stop that survives restarts
  and can only be released by the owner (never from chat), and an audit log whose hash chain exposes tampering.
* **One identity across devices.** Conversations, tasks, memory and approvals live server-side; a reminder created on the PC is
  there on the phone, and an approval raised on one device can be decided on another.
* **Memory with retrieval, not prompt-stuffing.** Ranked full-text search (SQLite FTS5/BM25 + importance + recency) over long-term,
  business, device and knowledge memory; inspect / correct / delete / export; per-category switches.
* **Task engine** (priorities, deadlines, dependencies, history, reminders), **executive briefing**, **work modes/workflows**,
  **operations reports**, **trading-journal analytics** (observation vs interpretation vs recommendation, sample-size aware),
  spreadsheet calculations, and an installable **skills** system.
* **Command-center web UI** (chat, approvals, tasks, notifications, activity, memory, business, devices, security) —
  strict CSP, no inline script, works on a phone-sized screen, optional browser-native voice input/output.

## Quick start

Requires Python 3.11+.

```bash
git clone <this repo> && cd Jarvis
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[browser]"                          # browser extra = Playwright (optional)
python -m playwright install chromium                # optional, for the browser tool

friday init        # creates ~/FRIDAY and your owner token
friday doctor      # honest health check (is a model reachable? is the audit log intact?)
friday chat        # talk to it in the terminal
friday serve       # web UI + API at http://127.0.0.1:8765  (paste the owner token)
```

**Brain.** Install [Ollama](https://ollama.com) and pull a tool-calling model (`ollama pull qwen2.5:7b-instruct`);
FRIDAY detects it automatically. Without one, FRIDAY falls back to a **rules planner** that understands a fixed list of direct
commands (see `help`) and says so plainly — it is not a language model and will not pretend to be.

Try (works with no model at all):

```text
Friday, open Notepad and create a file saying hello
Friday, remind me tomorrow to finish the CyZy proposal
what should I do next?          what is blocking CyZy FX?          Friday, executive briefing
remember that CyZy FX content needs compliance review     ·     what do you remember about compliance?
analyze my last 30 XAUUSD trades in Finance/journal.csv   ·   Friday, prepare today's CyZy operations report
Friday, Ollama isn't connecting       Friday, start work mode       Friday, emergency stop
```

## Use it from another device

```bash
friday token my-phone --kind phone     # prints a device token once (only its hash is stored)
```

The phone uses `Authorization: Bearer <token>` against `/v1/chat`, `/v1/tasks`, `/v1/approvals`, `/v1/notifications` …
The server listens on `127.0.0.1` by default. To reach it from a phone **do not** just bind `0.0.0.0` over plain HTTP:
use a VPN overlay (Tailscale/WireGuard) or set `FRIDAY_TLS_CERT` / `FRIDAY_TLS_KEY`. `friday serve` warns when you don't.
Device tokens can chat, manage tasks/memory and decide approvals; changing permissions/autonomy, registering devices and
releasing the emergency stop are **owner-only**.

## Configuration

See [`.env.example`](.env.example). Permissions and autonomy are changed in the UI (Security tab, owner only) or
`PUT /v1/security/...`. Extra apps, readable/writable folders:

```jsonc
// ~/FRIDAY/Core/config.json
{ "apps": { "ledger": { "command": ["C:\\Tools\\ledger.exe"], "process_names": ["ledger.exe"], "aliases": ["books"] } },
  "read_roots":  ["D:\\CyZy"],  "write_roots": ["D:\\CyZy\\Reports"] }
```

By default tools may touch only `~/FRIDAY` plus your Documents/Desktop/Downloads; credentials folders, key files, `.env`, FRIDAY's own
`Core/` and database are never accessible to tools.

## Tests

```bash
pip install -e ".[dev]" && python -m pytest        # ~235 tests; browser tests skip if Chromium can't launch
```

CI runs Ubuntu (Python 3.11, 3.13) and Windows. The suite includes adversarial cases (tampered approvals, prompt injection through
web content, sandbox escapes, audit tampering) and was mutation-checked: removing the taint rule, the approval fingerprint check, the
mandatory-risk rule, the e-stop check, the blocklist, the verification downgrade or the launch settle-window each makes tests fail.

## Layout

```text
friday/
  core/        agent loop, model providers + router, persona, offline rules planner
  security/    permissions, autonomy, policy engine, approvals, emergency stop, audit chain, device auth
  tools/       tool contract, executor pipeline, filesystem, shell, apps, system, browser, data, business, personal
  memory/ tasks.py notifications.py briefing.py business.py workflows.py skills.py finance.py scheduler.py
  api/server.py   ui/   cli.py   skills_builtin/
tests/   docs/   .github/workflows/ci.yml
```
Design notes and rationale: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
