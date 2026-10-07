# FRIDAY architecture

## Principles (from the spec) and how the code enforces them

| Principle | Mechanism |
|---|---|
| Real execution over simulation | Tools do real work (`subprocess`, filesystem, SQLite, Playwright). There is no "pretend" mode. |
| Verification over assumption | `Tool.verify()` independently re-checks the world (re-read + SHA-256, process table, DB re-read). `Executor` downgrades a run whose verification fails to `unverified`; replies are rendered from statuses. |
| Permission over unrestricted access | Every tool declares a scope; `PermissionStore` holds allow/confirm/deny per scope; unknown scopes fail closed to "confirm". |
| Human approval for high-impact actions | `Risk.MANDATORY` always asks, at every autonomy level, in approved workflows, and standing approvals never apply to it. |
| Privacy by design / local-first | SQLite on disk, Ollama by default, no telemetry, loopback bind, tokens stored as hashes, secrets never reach subprocess environments or the audit log. |
| Modular / replaceable | `ModelProvider` interface (Ollama, OpenAI-compatible, offline rules), tools registered in one place, skills as files, workflows as JSON. |
| Persistent cross-device context | Conversations, tasks, memory, approvals are server-side; devices are just authenticated clients. |
| Transparent action logging | `audit_log`: every attempt (including denied/approval/proposed), hash-chained, secrets redacted. |
| No fabricated results | Honest degraded modes: no model → rules planner says so; no Chromium → tool fails with instructions; unconnected sources are listed as "not connected". |

## The agent loop

```
INPUT ─▶ fast paths (no model): emergency stop · approve/deny · "resume?" refusal
      ─▶ UNDERSTAND/PLAN   provider.chat(history, tool schemas, system prompt + retrieved memory + skills)
      ─▶ for each proposed tool call:
            validate args (pydantic) ─▶ classify (risk/effect/blocked, per-argument)
            ─▶ PolicyEngine.decide ─▶ EXECUTE | APPROVAL | PROPOSE | DENY
            ─▶ run (bounded retries for retryable errors) ─▶ VERIFY ─▶ audit
      ─▶ result fed back to the provider (≤ max_steps, ≤ 2 identical calls, stop-after-failure within a batch)
      ─▶ REPORT (grounded) ─▶ persist conversation
```

The **policy engine is plain code**. A model (or text it read) can only *request* an action. This is what makes prompt injection a
bounded problem: injected instructions can at worst produce a pending approval that a human reads, never an executed action.
On top of that, once untrusted content (web page) enters a run, the run is *tainted* and any external-effect action needs approval.

### Policy decision table

Order: e-stop → blocked → grant=deny → risk=MANDATORY → grant=confirm (standing approval may satisfy it) → effect.

| effect \ autonomy | 0 Observe | 1 Assist | 2 Approved | 3/4 Autonomous |
|---|---|---|---|---|
| read | run | run | run | run |
| internal (tasks, memory, reports) | propose | run | run | run |
| external, low risk | propose | **approval** | run (unless tainted → approval) | run (unless tainted) |
| external, review risk | propose | **approval** | approval (or standing) | run only inside an owner-approved workflow, else approval |
| mandatory | approval | approval | approval | approval |

## Data model (SQLite, WAL, FTS5)

`memories(+fts)` · `messages/conversations` · `tasks/task_history` · `projects` · `approvals/standing_approvals` ·
`audit_log` (hash chain) · `notifications` · `devices` · `settings` (permissions, autonomy, e-stop, category switches).
Workspace folders mirror the spec: `Core Memory Projects Finance Business Documents Research Automations Logs Skills Backups`.

## Technology choices (and why)

* **Python + FastAPI + SQLite.** One process, no services to install on a Windows PC, trivially backed up (one file), and FTS5 gives
  ranked retrieval with zero dependencies. *Not* PostgreSQL/pgvector yet: they add install friction on Windows without a benefit at this scale.
  Semantic (embedding) search is a planned addition behind the same `MemoryStore.search` interface.
* **Ollama native `/api/chat` tool calling** plus an **OpenAI-compatible** provider (covers LM Studio, llama.cpp, vLLM, hosted APIs).
  A small salvage path handles models that print the tool call as JSON text.
* **Vanilla JS UI served by the API.** No build toolchain, strict CSP (`script-src 'self'`), `textContent` only. Tauri/Electron can wrap
  it later for a global hotkey/overlay without changing the API.
* **Android: not built.** Recommended path: Kotlin + Jetpack Compose client against this API (device token in Android Keystore,
  FCM for push, a foreground service for the wake word). The API surface it needs (chat, tasks, approvals, notifications, devices) already exists and is tested.
* **GUI automation / vision: not built.** The architecture slot is `ToolRegistry` (add `screen_capture`, `ui_click`, `type_text` with scopes
  `screen.read` / `browser.interact` / `input.control`, default `confirm`), preferring structured APIs and Playwright over pixel clicking.

## Security model in one page

* Auth: bearer tokens (`fri_…`, 256-bit), SHA-256 hashed in the DB; roles `owner` / `device` (+ `can_approve`); instant revocation; the last owner can't be revoked.
* Network: loopback by default; optional TLS (`FRIDAY_TLS_CERT/KEY`, verified); warning when binding publicly without TLS.
* Sandbox: tool file access limited to allow-listed roots after symlink resolution; credential files/dirs, `Core/` and the DB are never reachable.
* Shell: strict read-only allowlist (no chaining/redirection/subexpressions) runs freely; destructive/security patterns are mandatory-approval; catastrophic ones are blocked outright. This classifier is best-effort — the real control is that arbitrary commands wait for a human to read them.
* Approvals bind to the exact action (fingerprint of tool + arguments), are single-use, expire after 24h, and are cancelled by the emergency stop.
* Emergency stop: persisted; any device can engage it; only the owner can release it, never via chat; handled before any model sees the message.
* Audit: append-only hash chain; `friday audit --verify` and `/v1/activity/verify` detect edits/deletions.
* Known gaps: no encryption at rest, no OS-keychain secret storage (owner token is a 0600 file), no brute-force lockout (tokens are 256-bit), no per-process sandboxing of launched apps.
