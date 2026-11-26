# Agent Workspace

A local multi-agent workspace built with FastAPI, SQLite, and a static HTML/CSS/JavaScript dashboard. An orchestrator agent creates a JSON plan, assigns its steps sequentially to enabled worker agents, and writes a final summary. Task state, steps, messages, and events are persisted in SQLite. The dashboard receives live updates over WebSocket and can fall back to polling.

## Run locally

Requires Python 3.11 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Open `http://localhost:8000` for the dashboard or `http://localhost:8000/docs` for the API. The default provider is deterministic `mock`, so no API key is required.

For OpenAI, Groq, Ollama, or HematToken, set `AGENTWORK_PROVIDER` and `AGENTWORK_MODEL`; set `AGENTWORK_API_KEY` and `AGENTWORK_BASE_URL` for HematToken. These adapters use the OpenAI-compatible chat completions API. Ollama defaults to `http://localhost:11434/v1`. Per-agent provider/model values are stored in the database and can be managed through the agent API, but secrets and endpoint URLs are supplied by server environment configuration.

## Main features

- Create, inspect, answer, cancel, and export tasks.
- Configure agents through CRUD/toggle endpoints.
- Stream agent messages and task/step status to the dashboard.
- Pause for up to three worker clarification rounds per step.
- Retry selected transient provider/network failures and empty/invalid responses.
- Limit task creation to a configured number per minute per client IP.
- Restore tasks waiting for human input after restart. Interrupted planning tasks restart from planning, running tasks resume saved execution state, review tasks rebuild their summary, and explicit cancellations remain stopped. Resumed work reuses deterministic message ids and adopts steps that already finished, so recovery does not repeat provider calls or duplicate transcript entries. A recovered workflow whose provider gives up is persisted as `failed` instead of being left in `running`/`review`.

## API

- `GET /api/health`
- `GET/POST /api/agents`, `PUT/DELETE /api/agents/{agent_id}`, `PATCH /api/agents/{agent_id}/toggle`
- `GET /api/providers`
- `GET /api/tasks`, `POST /api/tasks` with `{"instruction":"..."}`
- `GET /api/tasks/{task_id}` (task and steps)
- `GET /api/tasks/{task_id}/messages`
- `GET /api/tasks/{task_id}/export` (Markdown)
- `POST /api/tasks/{task_id}/answer` with `{"answer":"..."}`
- `POST /api/tasks/{task_id}/cancel`
- `WS /ws?task_id={task_id}` (optional task filter)

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENTWORK_DATABASE_PATH` | `data/workspace.db` | SQLite file; relative paths resolve from project root |
| `AGENTWORK_PROVIDER` | `mock` | Provider used for seeded agents |
| `AGENTWORK_MODEL` | `mock-agent` | Model used for seeded agents |
| `AGENTWORK_API_KEY` | unset | API key for compatible remote provider |
| `AGENTWORK_BASE_URL` | provider default | Compatible API base URL |
| `AGENTWORK_TIMEOUT_SECONDS` | `60` | Provider call timeout |
| `AGENTWORK_MAX_STEPS` | `8` | Maximum steps in a generated plan (1–50) |
| `AGENTWORK_MAX_RETRIES` | `2` | Retries after selected provider errors (0–5) |
| `AGENTWORK_TASKS_PER_MINUTE` | `10` | Task-creation limit per IP |
| `AGENTWORK_REDIS_URL` | `redis://localhost:6379/0` | Redis URL for limiter; in-memory fallback is used if unavailable |
| `AGENTWORK_CORS_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | Comma-separated allowed browser origins |
| `AGENTWORK_TRUSTED_PROXY_IPS` | unset | Comma-separated proxy IPs allowed to supply `X-Forwarded-For` |
| `AGENTWORK_WEBSOCKET_MAX_CONNECTIONS` | `100` | Maximum concurrent WebSocket connections |

## Important implementation status

Authentication is **not implemented**: the application has no registration/login/profile routes or user ownership model and removes the previous JWT/SQLAlchemy scaffolding.

Rate limiting uses Redis when available and otherwise uses a process-local sliding window. The Redis 429 response is not swallowed; memory fallback is not shared across processes. `X-Forwarded-For` is trusted only for configured proxy IPs.

Startup restores `waiting_input` task waiters. Interrupted `planning` tasks restart from planning, interrupted `running` tasks resume saved step state, and interrupted `review` tasks rebuild the final summary. Explicit user cancellations remain stopped, and a recovered workflow that raises ends as `failed` instead of staying `running`/`review`. Recovery is best-effort and not a durable distributed job queue: it only inspects the 50 most recent tasks, finished steps are adopted instead of re-run, messages use deterministic ids so replays overwrite rather than duplicate, and a workflow that is still finishing cannot overwrite an explicit cancellation.

## Tests

Run `pytest` from the project root.

See [CHANGELOG.md](CHANGELOG.md) for the implementation history, [PLAN.md](PLAN.md) for current scope and backlog, and [AUDIT_FINAL.md](AUDIT_FINAL.md) for the latest documentation/code review notes. Historical findings in [BUG_REPORT.md](BUG_REPORT.md) describe bugs reported and fixed in v0.4.3; verify current behavior against code before treating that report as a current issue list.
