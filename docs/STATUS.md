# Status report — what is built, what is verified, what is not

Legend: ✅ built **and tested here** · 🟡 built, verification limited (reason given) · ⬜ not built

Test suite, from CI run 1 on commit `b041163` (GitHub Actions, all three jobs green):
**Ubuntu 3.11 and 3.13 — 235 passed, 14 skipped** (all skips are Windows-only tests) ·
**Windows (windows-latest, 3.13) — 220 passed, 29 skipped** (all skips are POSIX-only stand-in tests; see "Windows" below).

## Spec section by section

| § | Area | Status | Notes |
|---|---|---|---|
| 1,36,37 | One identity, cross-device continuity | ✅ | Shared server-side conversation/tasks/memory/approvals. Tested over HTTP with two device tokens (PC creates reminder → phone sees it; approval raised on one device decided on another). Only *web/CLI* clients exist. |
| 5,29 | Executive loop, verify, self-correction | ✅ | Plan → permission → execute → verify → report; bounded retries, skip-after-failure, duplicate-call cap, honest `unverified`. Self-correction is *model-driven* (the model sees errors and may try ≤2 alternatives); apps try multiple launch candidates. |
| 6,12 | Tool system | ✅ | 31 tools; each has input schema, scope, risk classification, run, verify, audit. Output schema is the `ToolResult` envelope (+ free-form `data`). |
| 6 | Windows PC control | ✅/🟡 | App launch/close, files, PowerShell, system info. **Verified on a real Windows runner in CI**: real `notepad.exe` launch → process-table verification → approved close, the "open Notepad and create a file saying hello" scenario, real PowerShell (read-only runs freely, mutation waits for approval), UNC/drive-root/backslash-traversal/credential paths denied, case-insensitive paths. 🟡 The runner is a GitHub-hosted Windows Server image, not a Windows 11 desktop (Store-version Notepad hand-off, UAC, antivirus and your own installed apps are unexercised). Not built: window switching, clipboard, form filling, downloads/uploads. |
| 7 | Computer vision / screen awareness | ⬜ | No screen capture, no UI-element detection, no mouse/keyboard control. |
| 8 | Android app | ⬜ | Not built. The API it needs exists. |
| 9,10 | Voice, wake word | 🟡/⬜ | Browser-native speech recognition/synthesis buttons in the web UI (Chromium/Edge/Safari) — **not testable headless; untested**. Local STT/TTS providers, wake word, interruption handling: not built. |
| 11 | Memory | ✅ | Categories, FTS5 retrieval (BM25 + importance + recency), inspect/correct/delete/export, per-category switches, knowledge ingest (text-like files only). ⬜ vector/semantic search. ⬜ PDF/DOCX/PPTX ingestion. |
| 13,27,45 | Autonomy levels, permissions, human-in-the-loop | ✅ | Levels 0–3 enforced and tested; **level 4 currently behaves like 3** (no monitoring engine to be "executive" about). |
| 14,15 | Finance education & trading journal | ✅/🟡 | Journal analytics verified against hand-computed values; observation/interpretation/recommendation separation; sample-size caveats; calculators. Education/explanations come from the language model + a finance skill with strict rules — **untested with a real model**. ⬜ economic calendar/market feeds. |
| 16,32 | Business operations / dashboard | 🟡 | Project registry (CyZy + divisions), tasks, operations report, briefing, business memory. ⬜ **No revenue/leads/invoices/payments data source exists yet**; the report and briefing say so explicitly. ⬜ proposals/quotations/invoices generation, KPI dashboard. |
| 17 | Customer service agent | ⬜ | No Telegram/email/web-chat connectors, no lead routing. |
| 18,19 | Daily routine, executive briefing | 🟡 | Briefing and end-of-day review work from tasks/approvals/notifications/alerts. ⬜ calendar, email, health reminders, economic events. |
| 20,41 | Work modes, automation engine | 🟡 | Six modes as editable JSON workflows, run through the policy pipeline, approval bound to the definition hash. Triggers: **manual only** (+ task reminders). ⬜ time/calendar/message/file/webhook/metric triggers. |
| 21 | Proactive intelligence | 🟡 | Reminders, overdue/due-soon escalation for critical/high tasks, deduped, priority-ordered. ⬜ KPI/market/customer-waiting monitors. |
| 22 | Research engine | 🟡 | `browser_read` (one page, headless, SSRF-guarded, untrusted-fenced). ⬜ search, multi-source comparison, citation tracking, saved research. |
| 23 | File intelligence | 🟡 | txt/md/csv/json/code, CSV/XLSX summary + calculations. ⬜ PDF, DOCX, PPTX, images, comparison, classification, organisation. |
| 24 | IT assistant | 🟡 | System info, read-only diagnostics, `service_check` (Ollama scenario), approval-gated shell. ⬜ log analysis helpers, service management. |
| 25 | Local AI + routing | 🟡 | Ollama + OpenAI-compatible providers, per-task routes, graceful fallback. **Tested against a fake server speaking the wire format — never against a real model** (no network in the build sandbox). Local-model tool-calling quality is unknown until you run it. ⬜ vision/speech routes. |
| 26 | Security | ✅/🟡 | See `ARCHITECTURE.md`. ⬜ encryption at rest, OS keychain, brute-force lockout. TLS verified manually (self-signed cert, `curl`), not in the automated suite. |
| 28 | Audit log | ✅ | All fields required by the spec; hash-chained; tamper-evident (tested). |
| 30 | Task management | ✅ | All listed fields + commands. |
| 31 | Decision support | 🟡 | Prompted behaviour for the language model only (persona rule); no dedicated tool; untested with a real model. |
| 33 | Knowledge graph | ⬜ | Not built (relations exist only as task dependencies and project tags). |
| 34,35 | Command center UI | ✅/🟡 | Chat, approvals, tasks, notifications, activity, memory, business, devices, security, e-stop. Tested in headless Chromium (login, approvals, autonomy, permissions, e-stop, XSS-safety, zero console/CSP errors). ⬜ waveform, desktop overlay, global hotkey. |
| 40 | Routine learning | ⬜ | Not built. |
| 42,43 | Skills, plugins | 🟡 | Skills as files (validated, relevance-selected, user-addable). ⬜ connectors: Telegram, email, calendars, Google Workspace, CRM. |
| 44 | Specialised agents | ⬜ | Single orchestrator only. |

## Windows

CI (`.github/workflows/ci.yml`, run 1, `windows-latest`, Python 3.13): **220 passed, 29 skipped, 0 failed.**

Ran and passed on Windows: all of `tests/test_windows.py` (real `notepad.exe` open/verify/approved-close; the headline
"open Notepad and create a file saying hello" scenario; real PowerShell; Windows path-sandbox cases; built-in app resolution),
plus the shared suite — policy, audit, memory, tasks, finance, API, CLI, model/agent loop with injection tests, and the Chromium-based
`browser_read` tests (Playwright's Chromium installed on the runner).

Not covered on Windows, and why:
* 29 skips are tests that need the POSIX "sleeper" stand-in executable or POSIX shell/symlinks. Their real-Windows equivalents are in `test_windows.py`.
* **The dashboard browser test (`test_ui_browser.py`) is skipped on Windows** only because it reuses that stand-in to produce an approval card. It passes on Ubuntu. (Easy fix: produce the approval with an app registered under a unique non-running process name, which works on every OS.)
* Not exercised anywhere: a real Windows 11 desktop session, Store-version Notepad, UAC prompts, antivirus interference, long-running operation.

## Defects found by testing while building (and fixed)

| What failed | Why | Found by | Fix |
|---|---|---|---|
| "start work mode" crashed the request | planner helper's first parameter `name` collided with a tool argument called `name` | vertical-slice test | positional-only parameter; planner exceptions are now contained, logged and reported |
| Autonomy, permission, memory-category and notification-ack endpoints always returned 422 (UI controls dead) | request models were defined inside `create_app` under `from __future__ import annotations`; FastAPI treated the body as a query param | API tests | models moved to module level; UI regression test added |
| Launch of a process that crashes immediately was reported verified | first process-table check ran while the process was still starting | app tests | settle window: must still be alive after 0.7 s |
| `app_close` verifier crashed on "nothing to close" | missing key in result | live UI run | fixed; verifier crashes now degrade to *unverified*, never success |
| "read https://example.com" was routed to the file reader | `.com` looks like a file extension | parametrized planner test | URL rule evaluated before the file rule |
| Inline `style=` blocked by own CSP | UI used one inline style | live UI run | CSS class |
| Mandatory-risk rule had no effective test | default `confirm` grants masked it | mutation check | tests for loosened grants and approved workflows |

## Not yet exercised anywhere

A real Ollama model; a real microphone; any phone; a real Windows desktop session beyond CI's runner; long-running operation
(memory growth, scheduler over days); concurrency under load.

## Recommended next steps (in order)

1. Run it with a real tool-calling model (`friday doctor`, then try the example commands) and note where the planner/model behaves badly — the agent loop and prompts are the part most likely to need tuning.
2. Windows hardening from your own machine's feedback (apps you actually use → `Core/config.json`).
3. Phase 3: local STT/TTS providers (e.g. faster-whisper + Piper/SAPI) behind the same `/v1/chat`; wake word after push-to-talk is solid.
4. Phase 4: Android client (Compose) — registration, chat, approvals, notifications.
5. Phase 5: first business connector (Telegram front door → lead capture → task), then semantic search and document generation (DOCX/XLSX).
