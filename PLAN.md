# Plan: Agent Workspace

## Current product

A locally hosted multi-agent workspace. A person submits one instruction; the enabled orchestrator asks its provider for a JSON plan, workers execute steps sequentially, and the orchestrator produces a final summary. FastAPI serves the REST API and dashboard; SQLite stores tasks, plans, messages, and events; WebSocket streams updates to connected browsers.

## Implemented today

- FastAPI app and static dashboard (Jinja2, vanilla JavaScript, CSS).
- SQLite persistence for agents, tasks, plan steps, messages, and events.
- Agent CRUD, enable/disable, task list/detail, transcript export, answer, cancellation, health and provider-list endpoints.
- Mock and OpenAI-compatible provider adapters. The adapter supports OpenAI, Groq, Ollama, and configured compatible APIs.
- Sequential planner → worker steps → final summary. Each plan is validated against enabled worker IDs and configured step limits.
- Streaming UI updates, with polling fallback; clarification requests pause a step for up to three answer rounds.
- Provider timeout, selected retries, and task-create rate limiting.
- Startup restoration for tasks waiting on human input and a best-effort resume path for interrupted work. Steps that already stored a result are adopted and messages use deterministic ids, so a replay does not repeat provider calls or duplicate transcript rows.

## Architecture

```text
Browser dashboard
  ├─ REST calls ───────────────┐
  └─ WebSocket /ws ────────────┤
                               v
                         FastAPI app
                    ┌──────────┴─────────┐
                    v                    v
              Orchestrator          Event bus
                ├ planner                └─ in-process queues
                ├ sequential workers
                └ final summary
                    │
                    ├─ Provider interface
                    │   ├─ Mock
                    │   └─ OpenAI-compatible HTTP
                    └─ SQLite persistence
```

## Repository map

| Path | Responsibility |
|---|---|
| `app/main.py` | Application factory/lifespan, REST and WebSocket routes |
| `app/config.py` | Environment configuration |
| `app/models.py` | Pydantic agent/task/step/message/event schemas |
| `app/db.py` | SQLite persistence and default agents |
| `app/agents/orchestrator.py` | Planning, worker execution, clarification, summary, recovery |
| `app/agents/runtime.py` | Provider invocation, timeout, retry |
| `app/providers/` | Provider interface, mock, compatible HTTP adapter, registry |
| `app/rate_limit.py` | Redis attempt and process-local fallback limiter |
| `app/web/` | Dashboard template, JavaScript and CSS |
| `tests/` | Agent CRUD, orchestrator, and WebSocket tests |

## Current scope boundaries and known defects

- Authentication scaffolding has been removed; no registration/login/profile endpoints or user ownership model is implemented.
- CORS is configurable and credentials are disabled. WebSocket connections are connection-limited but not authenticated, and a client that disconnects releases its slot immediately.
- Redis 429 responses are enforced correctly; unavailable Redis falls back to process-local memory.
- Startup distinguishes explicit cancellation from interruption, restores waiting-input workflows, restarts interrupted planning, resumes interrupted running steps, rebuilds interrupted summaries, and persists `failed` when a recovered workflow gives up instead of leaving it stuck in `running`/`review`. Recovery is idempotent for this application's own writes: finished steps are not re-run, messages use deterministic ids, and a workflow that is still finishing cannot write a non-cancelled status over an explicit cancellation.
- Agent calls execute sequentially. There is no separate quality-review/revision cycle, task token budget, or native Anthropic/Gemini adapter.
- There is no durable distributed task queue or multi-instance coordination for running workflows.

## Backlog (proposed)

1. Authentication remains out of scope until a real multi-user/security model is requested.
2. Recovery is idempotent for persisted rows and provider calls since v0.8.2; external side effects performed by a provider remain outside that guarantee.
3. Consider a durable task backend if multi-worker/multi-instance operation is required.
4. Add Anthropic/Gemini adapters only when needed; current supported adapters are mock and OpenAI-compatible.

## Run and inspect

See `README.md` for setup, configuration, endpoints, and practical caveats. Run `pytest` from the repository root to execute the current test suite.
