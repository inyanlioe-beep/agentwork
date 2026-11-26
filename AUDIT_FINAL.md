# Audit Notes — Agent Workspace

**Scope:** documentation reconciliation against the repository source on 2026-11-25. This is not a new runtime test report. Historical v0.4.3 validation details are retained in `BUG_REPORT.md`; they were not rerun for this review.

## Findings confirmed in source

### Authentication is explicitly absent

The application has no registration, login, profile, bearer-protected route, or user ownership model. JWT and SQLAlchemy scaffolding have been removed; CORS origins are configurable and credential mode is disabled.

### Redis limiter exception handling

Redis 429 responses now bypass the fallback exception path and are returned directly. If Redis is unavailable, process-local memory is used. `X-Forwarded-For` is accepted only from configured trusted proxy IPs.

### Recovery is partial, idempotent, and cancellation-safe

`main.py` restores `waiting_input` waiters and separates explicit cancellation from process interruption. Interrupted `planning` tasks restart planning, interrupted `running` tasks resume saved step state, interrupted `review` tasks rebuild the summary, and explicit user cancellations are not resumed. This is enforced by `Orchestrator.cancel_by_user()`, which stamps `cancelled_by_user=True` even when the cancelled workflow already persisted its own interruption record with `cancelled_by_user=False`.

Replays no longer duplicate provider calls or transcript rows. Messages carry deterministic ids (`{task_id}:instruction`, `{task_id}:step:{index}:assignment|response`, `{task_id}:summary`), so a recovered workflow overwrites its own rows instead of appending second copies; steps that already stored a `result` are adopted without invoking a provider again; and `Orchestrator._set_status()` reads the persisted task and raises `TaskCancelledByUser` instead of writing `planning`/`running`/`review`/`done` over an explicit cancellation. Recovered workflows are registered in `app.state.task_jobs` through `Orchestrator._spawn()`, so `POST /api/tasks/{task_id}/cancel` stops them exactly like a freshly created task.

Recovery remains best-effort: only the 50 most recent tasks are inspected at startup, there is no durable distributed job queue, and idempotency covers this application's own rows and provider calls, not arbitrary external side effects of a provider. A recovered workflow that raises is persisted as `failed` with an `error` transcript message instead of being left in `running`/`review` with an `in_progress` step, and a task that starts running again clears its stale `finished_at`.

### Provider and workflow scope

The registry implements mock and an OpenAI-compatible adapter (usable with configured compatible endpoints). No native Anthropic or Gemini adapter is present. The orchestrator runs plan steps sequentially and then produces a summary; there is no independent review/revision loop described in older design sketches.

### WebSocket and event delivery

The WebSocket endpoint has no authentication or connection limit. Event bus queues are bounded (see `app/bus.py`), and slow subscribers may lose older queued events. Persisted event records are not exposed through a replay endpoint, so the browser's task/message refresh is not a full event replay mechanism.

## Historical audit status

The v0.4.3 report stated that ten findings had been fixed and that tests/browser checks passed at that time. The source and test files have since evolved. Do not interpret those historical results as a test run for the present tree, and do not treat all old findings as current without rechecking the relevant code.

## Follow-up priorities

1. Add a durable task backend if multi-instance coordination becomes required.
2. Re-run and refresh the verified checks below whenever the recovery paths change.

## Verified checks — 2026-11-26

- `python -m pytest tests -q` → **42 passed** (the 35 pre-change tests plus the recovery regressions added in these passes; the three newest cover a recovered step whose provider keeps failing, a recovered `review` restart whose summary provider fails, and `finished_at` cleanup while a task runs again). The remaining warning is a dependency deprecation from Starlette's `TestClient`/`httpx` pairing.
- `python -m compileall app` → no syntax errors.
- Recovery regression tests added: `test_recovered_running_task_does_not_replay_completed_steps`, `test_recovery_replaces_partial_summary_instead_of_duplicating_it`, `test_cancelling_a_recovered_workflow_stops_its_execution`, and `test_set_status_refuses_to_overwrite_user_cancellation`.
- Test strength was checked by mutation: forcing recovery to re-run an already-finished step fails the first test (`assert 'Mock result: ...' == 'Step one result'`), detaching startup recovery from `app.state.task_jobs` fails the third (`assert '<task id>' in {}`), and replacing the `_record_recovery_failure()` call in the recovery wrapper with `raise` fails `test_recovery_marks_task_failed_when_provider_keeps_failing` and `test_recovery_marks_task_failed_when_review_restart_fails`. All of them pass with the fix in place.
- Redis was not reachable in this environment, so the limiter exercised its in-memory fallback during the API tests.
