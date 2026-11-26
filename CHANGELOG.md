# Work log: Agent Workspace

This file records implemented changes. Claims below describe repository history; current capability and limitations are summarized in `README.md` and `PLAN.md`.

## v0.8.3 — 2026-11-26 (recovery failures end in a terminal status)

- Recovery can no longer strand a task. `Orchestrator.resume_interrupted_task()` is now a thin wrapper around the former body (renamed `_recover_interrupted_task()`) that catches `TaskCancelledByUser` and every other error, persisting `failed` through the new `_record_recovery_failure()` helper — the same terminal write `run()` and the human-answer resume path already performed. Previously a provider that exhausted its retries during startup recovery raised straight out of the spawned coroutine, which only logged `Task execution failed: ...`, leaving the task in `running`/`review` with an `in_progress` step and no error visible through the API. Observed live on 2026-11-26 on a recovered task whose `writer` step used the `hemattoken` provider.
- Resumed work clears stale `finished_at`. The interrupted `planning`, `running`, and `review` branches plus `_resume_cancelled_task()` now reset `finished_at` when a task returns to a non-terminal status, so a task that is running again no longer advertises a completion timestamp.
- Added regression tests: a recovered `running` step whose provider keeps failing ends `failed` with an `error` message, a recovered `review` task whose summary provider fails ends `failed`, and a task resumed mid-step reports `finished_at=None` while it is running. `pytest tests -q` → 42 passed.

## v0.8.2 — 2026-11-26 (idempotent recovery and cancel/recovery interaction)

- Recovery no longer duplicates provider calls. Steps that already stored a `result` before the interruption are adopted as-is: `_resume_cancelled_task()`, the remaining-step loop, and the interrupted-`review` summary path skip them instead of handing the work to a provider again.
- Recovery no longer duplicates transcript rows. Instruction, assignment, response, and summary messages now carry deterministic ids (`{task_id}:instruction`, `{task_id}:step:{index}:assignment|response`, `{task_id}:summary`), so a replayed workflow overwrites its own rows rather than appending a second copy.
- Recovered workflows are tracked like freshly created ones. `Orchestrator._spawn()` now routes startup recovery through the same `app.state.task_jobs` tracker used by `POST /api/tasks`, so `POST /api/tasks/{task_id}/cancel` can stop a recovered workflow instead of leaving it running detached.
- A still-finishing workflow can no longer overwrite an explicit cancellation. `Orchestrator._set_status()` reads the persisted task first and raises the new `TaskCancelledByUser` error when the user cancelled it, so recovery never writes `planning`/`running`/`review`/`done` over an explicit `cancelled` status.
- Removed dead scaffolding: `app/main_complete.py` and `app/main.py.backup` (unreferenced route-identical duplicates of `app/main.py`), the unused `Database.__init__` attributes, and unused imports in `app/main.py`. The `app/main.py` version string now matches this changelog (`0.8.2`).
- Added regression tests: replayed recovery of a `running` task keeps the recorded result and writes one assignment/response pair, a partial summary row is replaced instead of duplicated, a cancelled recovered workflow stays cancelled, and `_set_status()` refuses to overwrite a user cancellation. `pytest tests -q` → 39 passed.

## v0.8.1 — 2026-11-25 (cancellation and agent-deletion fixes)

- Fixed explicit cancellation being resumed after restart. `POST /api/tasks/{task_id}/cancel` cancelled the workflow coroutine, whose `CancelledError` handler persisted `cancelled_by_user=False`; the endpoint then skipped its own stamp because the status already read `cancelled`, so startup recovery resumed a task the user had stopped. Added `Orchestrator.cancel_by_user()`, which stamps `cancelled_by_user=True` and clears `interrupted_status` even when the workflow already persisted its interruption record, and the endpoint now uses it after re-reading the task.
- Fixed agents being undeletable once their task ended. `Database.count_active_tasks_for_agent()` counted `pending`/`in_progress`/`needs_input` steps without checking the owning task, so steps of `cancelled`/`failed`/`done` tasks blocked `DELETE /api/agents/{id}` forever. The query now joins `tasks` and only counts steps of active tasks (`queued`, `planning`, `running`, `review`, `waiting_input`).
- Startup recovery coroutines are now tracked in `app.state.background_tasks`, so shutdown cancels them instead of leaving them running detached.
- Added regression tests: a cancelled task stays cancelled across a restart, an agent with a running task still cannot be deleted, and an agent can be deleted once its task is cancelled.

## v0.8.0 — 2026-11-25 (documentation reconciliation)

- Corrected feature descriptions and completed focused hardening: authentication scaffolding was removed, Redis 429 handling was corrected, CORS/proxy/WebSocket settings were added, and explicit cancellation is separated from interruption recovery.
- Added startup recovery dispatch for interrupted planning, running, and review states, plus focused tests for explicit-cancellation and planning-recovery startup behavior.
- Updated README and PLAN to distinguish implemented behavior from scaffolding and known limitations.

## v0.8.0 — 2026-11-25 (implementation claims under review)

Earlier notes in this section were written as if authentication, user management, and distributed limiting were complete. Source inspection shows those claims are not accurate: `main.py` defines a token dependency but attaches it to no route, has no auth routes, and uses a placeholder user; `rate_limit.py` attempts Redis then falls back to memory, and catches its own 429 exception as a generic error. User ORM models and settings exist, but are not a working multi-user authentication system. Treat this milestone entry as historical intent, not evidence that these features work.

The source does include startup hooks to restore waiting-input waiters and to resume cancelled tasks with saved execution metadata. This is partial recovery; ordinary in-flight tasks interrupted by a crash are not recovered. The cancelled-task startup behavior can also cause a user-cancelled task to run again.

## v0.7.0 — 2026-11-25 — Resume metadata for cancelled execution

Added persisted step/context fields and a startup resume path for eligible cancelled tasks. See limitations above: this does not provide general crash-safe job recovery.

## v0.6.0 — 2026-11-24 — Resume waiting-input workflows

Added persisted waiter metadata and logic to rebuild/resume a task waiting for a human answer after restart.

## v0.5.0 — 2026-11-24 — Provider retry and task hardening

Added provider timeout/retry settings and related error handling.

## v0.4.3 — 2026-11-24 — Bug fixes

The ten issues recorded in `BUG_REPORT.md` were reported fixed and validated at that time. The report is historical; this changelog does not claim that its old validation was rerun against the current repository state.

## Current architecture

- FastAPI API and Jinja2/static dashboard (`app/main.py`, `app/web/`).
- SQLite persistence for agents, tasks, steps, messages, and events (`app/db.py`).
- Sequential plan/delegate/final-summary flow (`app/agents/orchestrator.py`). There is no separate review-and-revise loop despite older design sketches.
- Mock and OpenAI-compatible providers (`app/providers/`). Anthropic and Gemini adapters are not implemented.
- Redis rate-limit attempt with in-memory fallback; see `README.md` for current caveats.
