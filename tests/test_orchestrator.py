import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.orchestrator import Orchestrator, TaskCancelledByUser
from app.agents.runtime import AgentRuntime
from app.bus import EventBus
from app.config import PROJECT_ROOT, Settings
from app.db import Database, default_agents
from app.main import create_app
from app.models import Event, Message, PlanStep, Task
from app.providers.base import LLMProvider
from app.providers.openai_compatible import OpenAICompatibleProvider


@pytest.mark.asyncio
async def test_mock_pipeline_persists_plan_messages_and_final_summary(tmp_path):
    database = Database(tmp_path / "workspace.db")
    await database.initialize()
    for agent in default_agents():
        await database.save_agent(agent)
    bus = EventBus()
    queue = bus.subscribe()
    task = Task(title="Test task", instruction="Prepare a project outline")
    await database.save_task(task)

    result = await Orchestrator(database, bus, Settings(database_path=database.path)).run(task)

    assert result.status == "done"
    assert result.summary
    assert len(await database.list_steps(task.id)) == 2
    messages = await database.list_messages(task.id)
    assert [message.kind for message in messages].count("assignment") == 2
    assert [message.kind for message in messages].count("response") == 2
    assert messages[-1].kind == "summary"
    assert queue.qsize() > len(messages)


@pytest.mark.asyncio
async def test_plan_cannot_assign_unknown_worker(tmp_path):
    database = Database(tmp_path / "workspace.db")
    await database.initialize()
    await database.save_agent(default_agents()[0])
    await database.save_agent(default_agents()[1])
    orchestrator = Orchestrator(database, EventBus(), Settings(database_path=database.path))
    task = Task(title="Invalid plan", instruction="Do something")
    await database.save_task(task)

    with pytest.raises(ValueError, match="unavailable worker"):
        await orchestrator.run(task)
    assert (await database.get_task(task.id)).status == "failed"
    assert (await database.list_messages(task.id))[-1].kind == "error"


def test_http_api_runs_mock_task_and_exposes_transcript(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "api.db"))
    with TestClient(app) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "YOUR AI TEAM, IN SYNC" in dashboard.text
        assert 'id="question-panel"' in dashboard.text
        assert 'id="agent-filters"' in dashboard.text
        assert 'id="cancel-task"' in dashboard.text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/style.css").status_code == 200
        assert client.get("/static/favicon.svg").status_code == 200
        response = client.post(
            "/api/tasks",
            json={"instruction": "Create a short project outline"},
        )
        assert response.status_code == 202
        task_id = response.json()["id"]

        for _ in range(1000):
            task_response = client.get(f"/api/tasks/{task_id}")
            if task_response.json()["task"]["status"] in {"done", "failed"}:
                break
            time.sleep(0.02)

        assert task_response.json()["task"]["status"] == "done"
        assert len(task_response.json()["steps"]) == 2
        messages = client.get(f"/api/tasks/{task_id}/messages").json()
        assert messages[-1]["kind"] == "summary"
        transcript = client.get(f"/api/tasks/{task_id}/export")
        assert transcript.status_code == 200
        assert "text/markdown" in transcript.headers["content-type"]
        assert transcript.headers["content-disposition"].endswith(f"agentwork-{task_id}.md\"")
        assert "# Create a short project outline" in transcript.text
        assert "Mock result:" in transcript.text


def test_human_input_pauses_and_resumes_task(tmp_path, monkeypatch):
    class InteractiveProvider(LLMProvider):
        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            if "Return only valid JSON" in user_prompt:
                return '{"steps":[{"description":"Draft the proposal","agent_id":"researcher"}]}'
            if "final summary" in user_prompt.lower():
                return "Proposal completed with the requested audience."
            if "Human answer:" in user_prompt:
                return "Drafted proposal for the supplied audience."
            return "[NEEDS_INPUT]: Who is the intended audience?"

    monkeypatch.setattr(
        "app.agents.orchestrator.create_provider",
        lambda provider_name, *, model, settings: InteractiveProvider(),
    )
    app = create_app(Settings(database_path=tmp_path / "interactive.db"))
    with TestClient(app) as client:
        response = client.post("/api/tasks", json={"instruction": "Draft a proposal"})
        task_id = response.json()["id"]

        for _ in range(1000):
            detail = client.get(f"/api/tasks/{task_id}").json()
            if detail["task"]["status"] == "waiting_input":
                break
            time.sleep(0.02)

        assert detail["task"]["pending_question"] == "Who is the intended audience?"
        assert detail["steps"][0]["status"] == "needs_input"
        messages = client.get(f"/api/tasks/{task_id}/messages").json()
        assert messages[-1]["kind"] == "question"
        assert client.post(
            f"/api/tasks/{task_id}/answer",
            json={"answer": "  "},
        ).status_code == 422

        answer = client.post(
            f"/api/tasks/{task_id}/answer",
            json={"answer": "Local school teachers"},
        )
        assert answer.status_code == 200
        for _ in range(100):
            detail = client.get(f"/api/tasks/{task_id}").json()
            if detail["task"]["status"] in {"done", "failed"}:
                break
            time.sleep(0.02)

        assert detail["task"]["status"] == "done"
        messages = client.get(f"/api/tasks/{task_id}/messages").json()
        human_reply = next(message for message in messages if message["sender"] == "human" and message["kind"] == "response")
        assert human_reply["receiver"] == "researcher"
        assert "Local school teachers" in human_reply["content"]
        assert client.post(
            f"/api/tasks/{task_id}/answer",
            json={"answer": "Another answer"},
        ).status_code == 409


@pytest.mark.asyncio
async def test_agent_runtime_retries_network_errors_and_respects_attempt_limit():
    class FlakyProvider(LLMProvider):
        calls = 0

        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            if self.calls < 3:
                request = httpx.Request("POST", "https://provider.test")
                raise httpx.ConnectError("temporary network failure", request=request)
            return "Recovered"

    provider = FlakyProvider()
    runtime = AgentRuntime(default_agents()[0], provider, max_retries=2)

    assert await runtime.respond("Do the work") == "Recovered"
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_agent_runtime_retries_invalid_and_empty_provider_responses():
    class InvalidThenValidProvider(LLMProvider):
        calls = 0

        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            raise AssertionError("Streaming implementation should be used")

        async def stream(self, *, system_prompt: str, user_prompt: str):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("Provider returned malformed stream data")
            if self.calls == 2:
                yield ""
                return
            yield "Recovered"

    provider = InvalidThenValidProvider()
    runtime = AgentRuntime(default_agents()[0], provider, max_retries=2)

    assert await runtime.respond("Do the work") == "Recovered"
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_agent_runtime_resets_partial_output_before_retry():
    class InterruptedProvider(LLMProvider):
        calls = 0

        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            raise AssertionError("Streaming implementation should be used")

        async def stream(self, *, system_prompt: str, user_prompt: str):
            self.calls += 1
            if self.calls == 1:
                yield "partial"
                request = httpx.Request("POST", "https://provider.test")
                raise httpx.ReadError("connection interrupted", request=request)
            yield "complete response"

    streamed_chunks = []
    provider = InterruptedProvider()
    runtime = AgentRuntime(default_agents()[0], provider, max_retries=1)

    async def on_chunk(chunk: str) -> None:
        streamed_chunks.append(chunk)

    async def on_retry() -> None:
        streamed_chunks.append("<reset>")

    result = await runtime.respond(
        "Do the work",
        on_chunk=on_chunk,
        on_retry=on_retry,
    )

    assert result == "complete response"
    assert streamed_chunks == ["partial", "<reset>", "complete response"]


@pytest.mark.asyncio
async def test_streaming_message_is_persisted_before_stream_completes(tmp_path):
    class InterruptedProvider(LLMProvider):
        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            raise AssertionError("Streaming implementation should be used")

        async def stream(self, *, system_prompt: str, user_prompt: str):
            yield "partial output"
            raise RuntimeError("provider process interrupted")

    database = Database(tmp_path / "draft.db")
    await database.initialize()
    task = Task(title="Interrupted", instruction="Stream text")
    await database.save_task(task)
    orchestrator = Orchestrator(database, EventBus(), Settings(database_path=database.path))
    runtime = AgentRuntime(default_agents()[1], InterruptedProvider(), max_retries=0)

    with pytest.raises(RuntimeError, match="provider process interrupted"):
        await orchestrator._stream_message(
            task.id, "researcher", "orion", "response", runtime, "Do the work"
        )

    messages = await database.list_messages(task.id)
    assert len(messages) == 1
    assert messages[0].content == "partial output"


@pytest.mark.asyncio
async def test_save_message_upserts_existing_message_id(tmp_path):
    database = Database(tmp_path / "messages.db")
    await database.initialize()
    task = Task(title="Upsert", instruction="Test message persistence")
    await database.save_task(task)
    message = Message(
        task_id=task.id,
        sender="researcher",
        receiver="orion",
        kind="response",
        content="partial",
    )
    await database.save_message(message)
    message.content = "complete"
    await database.save_message(message)

    messages = await database.list_messages(task.id)
    assert len(messages) == 1
    assert messages[0].content == "complete"


@pytest.mark.asyncio
async def test_agent_runtime_does_not_retry_non_transient_http_errors():
    class UnauthorizedProvider(LLMProvider):
        calls = 0

        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            request = httpx.Request("POST", "https://provider.test")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    provider = UnauthorizedProvider()
    runtime = AgentRuntime(default_agents()[0], provider, max_retries=3)

    with pytest.raises(httpx.HTTPStatusError):
        await runtime.respond("Do the work")
    assert provider.calls == 1


def test_relative_database_path_is_resolved_from_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert Settings().database_path == PROJECT_ROOT / "data" / "workspace.db"
    assert Settings(database_path="data/custom.db").database_path == PROJECT_ROOT / "data" / "custom.db"


@pytest.mark.asyncio
async def test_event_bus_drops_oldest_event_when_subscriber_is_slow():
    bus = EventBus(max_queue_size=2)
    queue = bus.subscribe()

    for event_id in ("first", "second", "latest"):
        await bus.publish(Event(task_id="task", type=event_id, payload={}))

    assert queue.qsize() == 2
    assert (await queue.get()).type == "second"
    assert (await queue.get()).type == "latest"


@pytest.mark.asyncio
async def test_failure_persistence_error_does_not_replace_original_task_error(tmp_path):
    class FailingFailureStateDatabase(Database):
        async def save_task(self, task: Task) -> None:
            if task.status == "failed":
                raise RuntimeError("database unavailable while saving failure state")
            await super().save_task(task)

    database = FailingFailureStateDatabase(tmp_path / "failure.db")
    await database.initialize()
    task = Task(title="Original failure", instruction="Run without an orchestrator")
    await database.save_task(task)
    orchestrator = Orchestrator(database, EventBus(), Settings(database_path=database.path))

    with pytest.raises(ValueError, match="No enabled orchestrator"):
        await orchestrator.run(task)


@pytest.mark.asyncio
async def test_agent_runtime_enforces_request_timeout():
    class SlowProvider(LLMProvider):
        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            await asyncio.Event().wait()
            return "unreachable"

    runtime = AgentRuntime(
        default_agents()[0],
        SlowProvider(),
        timeout_seconds=0.01,
        max_retries=0,
    )

    with pytest.raises(TimeoutError, match="failed after 1 attempt"):
        await runtime.respond("Do the work")


@pytest.mark.asyncio
async def test_openai_compatible_provider_parses_server_sent_event_chunks(monkeypatch):
    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"Hello"}}]}'
            yield ""
            yield 'data: {"choices":[{"delta":{"content":" world"}}]}'
            yield "data: [DONE]"

    class FakeClient:
        request = None

        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, *, headers, json):
            self.request = {"method": method, "url": url, "json": json}
            return FakeResponse()

    monkeypatch.setattr("app.providers.openai_compatible.httpx.AsyncClient", FakeClient)
    provider = OpenAICompatibleProvider(
        model="test-model",
        base_url="https://provider.test/v1",
        api_key="test-key",
    )
    chunks = [
        chunk async for chunk in provider.stream(system_prompt="system", user_prompt="prompt")
    ]

    assert "".join(chunks) == "Hello world"


def test_websocket_receives_streaming_message_events(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "stream.db"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            response = client.post("/api/tasks", json={"instruction": "Stream a short result"})
            assert response.status_code == 202
            task_id = response.json()["id"]
            events = []
            while True:
                event = websocket.receive_json()
                if event["task_id"] != task_id:
                    continue
                events.append(event)
                if (
                    event["type"] == "message"
                    and event["payload"]["kind"] == "summary"
                ):
                    break

        assert any(event["type"] == "message_start" for event in events)
        assert sum(event["type"] == "message_delta" for event in events) > 3
        first_start = next(event for event in events if event["type"] == "message_start")
        assert first_start["payload"]["content"] == ""
        first_delta = next(event for event in events if event["type"] == "message_delta")
        assert first_delta["payload"]["sender"] in {"researcher", "writer", "orion"}
        assert first_delta["payload"]["kind"] in {"response", "summary"}


def test_task_creation_rate_limit_returns_retry_after(tmp_path):
    app = create_app(
        Settings(database_path=tmp_path / "rate-limit.db", task_rate_limit_per_minute=1)
    )
    with TestClient(app) as client:
        first = client.post("/api/tasks", json={"instruction": "First task"})
        second = client.post("/api/tasks", json={"instruction": "Second task"})

        assert first.status_code == 202
        assert second.status_code == 429
        assert int(second.headers["retry-after"]) > 0


def test_running_task_can_be_cancelled(tmp_path, monkeypatch):
    class SlowProvider(LLMProvider):
        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            if "Return only valid JSON" in user_prompt:
                return '{"steps":[{"description":"Do slow work","agent_id":"researcher"}]}'
            await asyncio.Event().wait()
            return "unreachable"

    monkeypatch.setattr(
        "app.agents.orchestrator.create_provider",
        lambda provider_name, *, model, settings: SlowProvider(),
    )
    app = create_app(Settings(database_path=tmp_path / "cancel.db"))
    with TestClient(app) as client:
        response = client.post("/api/tasks", json={"instruction": "A task to cancel"})
        task_id = response.json()["id"]
        for _ in range(100):
            detail = client.get(f"/api/tasks/{task_id}").json()
            if detail["task"]["status"] == "running":
                break
            time.sleep(0.02)

        cancelled = client.post(f"/api/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        messages = client.get(f"/api/tasks/{task_id}/messages").json()
        assert messages[-1]["kind"] == "system"
        assert "cancelled" in messages[-1]["content"].lower()
        assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 409


def test_explicitly_cancelled_task_is_not_resumed_after_startup(tmp_path):
    db_path = tmp_path / "explicit-cancel.db"

    async def seed():
        database = Database(db_path)
        await database.initialize()
        for agent in default_agents():
            await database.save_agent(agent)
        task = Task(
            title="Explicit cancellation",
            instruction="Do not resume this",
            status="cancelled",
            cancelled_by_user=True,
            cancelled_step_index=1,
        )
        await database.save_task(task)
        return task.id

    task_id = asyncio.run(seed())
    app = create_app(Settings(database_path=db_path))
    with TestClient(app):
        async def read():
            database = Database(db_path)
            await database.initialize()
            return await database.get_task(task_id)

        task = asyncio.run(read())
        assert task.status == "cancelled"
        assert task.cancelled_by_user is True


def test_interrupted_planning_task_is_recovered_after_startup(tmp_path):
    db_path = tmp_path / "planning-recovery.db"

    async def seed():
        database = Database(db_path)
        await database.initialize()
        for agent in default_agents():
            await database.save_agent(agent)
        task = Task(
            title="Interrupted planning",
            instruction="Prepare a short recovery plan",
            status="cancelled",
            interrupted_status="planning",
            cancelled_by_user=False,
        )
        await database.save_task(task)
        return task.id

    task_id = asyncio.run(seed())
    app = create_app(Settings(database_path=db_path))
    with TestClient(app):
        async def wait_for_done():
            database = Database(db_path)
            await database.initialize()
            for _ in range(100):
                task = await database.get_task(task_id)
                if task and task.status == "done":
                    return task
                await asyncio.sleep(0.02)
            return await database.get_task(task_id)

        task = asyncio.run(wait_for_done())
        assert task.status == "done"
        assert task.interrupted_status is None


def test_task_waiting_for_human_input_can_be_cancelled(tmp_path, monkeypatch):
    class ClarifyingProvider(LLMProvider):
        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            if "Return only valid JSON" in user_prompt:
                return '{"steps":[{"description":"Clarify details","agent_id":"researcher"}]}'
            return "[NEEDS_INPUT]: Please provide the missing detail."

    monkeypatch.setattr(
        "app.agents.orchestrator.create_provider",
        lambda provider_name, *, model, settings: ClarifyingProvider(),
    )
    app = create_app(Settings(database_path=tmp_path / "cancel-waiting.db"))
    with TestClient(app) as client:
        response = client.post("/api/tasks", json={"instruction": "A task needing clarification"})
        task_id = response.json()["id"]
        for _ in range(100):
            detail = client.get(f"/api/tasks/{task_id}").json()
            if detail["task"]["status"] == "waiting_input":
                break
            time.sleep(0.02)

        assert detail["task"]["status"] == "waiting_input"
        cancelled = client.post(f"/api/tasks/{task_id}/cancel")

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["pending_question"] is None

@pytest.mark.asyncio
async def test_waiting_task_persists_resume_metadata(tmp_path, monkeypatch):
    class ClarifyingProvider(LLMProvider):
        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            if "Return only valid JSON" in user_prompt:
                return '{"steps":[{"description":"Get answer","agent_id":"researcher"}]}'
            return "[NEEDS_INPUT]: What is the value?"

        async def stream(self, *, system_prompt: str, user_prompt: str):
            response = await self.complete(system_prompt=system_prompt, user_prompt=user_prompt)
            for offset in range(0, len(response), 10):
                yield response[offset : offset + 10]
                await asyncio.sleep(0.01)

    database = Database(tmp_path / "resume.db")
    await database.initialize()
    for agent in default_agents():
        await database.save_agent(agent)
    bus = EventBus()
    monkeypatch.setattr(
        "app.agents.orchestrator.create_provider",
        lambda provider_name, *, model, settings: ClarifyingProvider(),
    )
    orchestrator = Orchestrator(database, bus, Settings(database_path=database.path))
    task = Task(title="Resume test", instruction="Test resume flow")
    await database.save_task(task)

    run_task = asyncio.create_task(orchestrator.run(task))
    for _ in range(100):
        await asyncio.sleep(0.02)
        fresh = await database.get_task(task.id)
        if fresh and fresh.status == "waiting_input":
            break

    fresh = await database.get_task(task.id)
    assert fresh.status == "waiting_input"
    assert fresh.pending_question == "What is the value?"
    assert fresh.waiting_step_index == 1
    assert fresh.waiting_agent_id == "researcher"
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass

    orchestrator2 = Orchestrator(database, bus, Settings(database_path=database.path))
    orchestrator2._setup_waiter_for_task(fresh)
    assert task.id in orchestrator2._human_waiters


class _SlowWorkerProvider(LLMProvider):
    """Plans one step and then blocks, so a task stays in ``running``."""

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        if "Return only valid JSON" in user_prompt:
            return '{"steps":[{"description":"Do slow work","agent_id":"researcher"}]}'
        await asyncio.Event().wait()
        return "unreachable"


def test_user_cancelled_task_is_not_resumed_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.agents.orchestrator.create_provider",
        lambda provider_name, *, model, settings: _SlowWorkerProvider(),
    )
    db_path = tmp_path / "cancel-restart.db"
    app = create_app(Settings(database_path=db_path))
    with TestClient(app) as client:
        response = client.post("/api/tasks", json={"instruction": "A task to cancel"})
        task_id = response.json()["id"]
        for _ in range(200):
            detail = client.get(f"/api/tasks/{task_id}").json()
            if detail["task"]["status"] == "running":
                break
            time.sleep(0.02)
        assert detail["task"]["status"] == "running"

        cancelled = client.post(f"/api/tasks/{task_id}/cancel")

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["cancelled_by_user"] is True

    async def read_state():
        database = Database(db_path)
        await database.initialize()
        return await database.get_task(task_id), await database.list_messages(task_id)

    persisted, messages_before_restart = asyncio.run(read_state())
    assert persisted.cancelled_by_user is True
    assert persisted.interrupted_status is None

    restarted = create_app(Settings(database_path=db_path))
    with TestClient(restarted):
        time.sleep(0.3)
        after_restart, messages_after_restart = asyncio.run(read_state())

    assert after_restart.status == "cancelled"
    assert after_restart.cancelled_by_user is True
    assert len(messages_after_restart) == len(messages_before_restart)


def test_agent_with_running_task_cannot_be_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.agents.orchestrator.create_provider",
        lambda provider_name, *, model, settings: _SlowWorkerProvider(),
    )
    app = create_app(Settings(database_path=tmp_path / "delete-active.db"))
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"instruction": "Blocking task"}).json()["id"]
        for _ in range(200):
            if client.get(f"/api/tasks/{task_id}").json()["task"]["status"] == "running":
                break
            time.sleep(0.02)

        response = client.delete("/api/agents/researcher")

        assert response.status_code == 409
        assert "active task" in response.json()["detail"]


def test_agent_can_be_deleted_after_its_task_is_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.agents.orchestrator.create_provider",
        lambda provider_name, *, model, settings: _SlowWorkerProvider(),
    )
    app = create_app(Settings(database_path=tmp_path / "delete-cancelled.db"))
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"instruction": "Blocking task"}).json()["id"]
        for _ in range(200):
            if client.get(f"/api/tasks/{task_id}").json()["task"]["status"] == "running":
                break
            time.sleep(0.02)

        assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200

        response = client.delete("/api/agents/researcher")

        assert response.status_code == 204
        agents = client.get("/api/agents").json()
        assert all(agent["id"] != "researcher" for agent in agents)



@pytest.mark.asyncio
async def test_set_status_refuses_to_overwrite_user_cancellation(tmp_path):
    database = Database(tmp_path / "cancel-guard.db")
    await database.initialize()
    task = Task(
        title="Cancelled task",
        instruction="Do not resume this task",
        status="cancelled",
        cancelled_by_user=True,
    )
    await database.save_task(task)
    orchestrator = Orchestrator(database, EventBus(), Settings(database_path=database.path))

    with pytest.raises(TaskCancelledByUser):
        await orchestrator._set_status(task, "running")

    assert (await database.get_task(task.id)).status == "cancelled"


def test_recovered_running_task_does_not_replay_completed_steps(tmp_path):
    db_path = tmp_path / "resume-idempotent.db"

    async def seed():
        database = Database(db_path)
        await database.initialize()
        for agent in default_agents():
            await database.save_agent(agent)
        task = Task(
            title="Interrupted after a finished step",
            instruction="Finish the remaining work",
            status="running",
            cancelled_step_index=1,
            cancelled_context=(
                "Original request: Finish the remaining work\n"
                "Earlier results:\nresearcher: Step one result"
            ),
        )
        await database.save_task(task)
        await database.save_step(
            PlanStep(
                task_id=task.id,
                index=1,
                description="Analyse the request",
                agent_id="researcher",
                status="done",
                result="Step one result",
            )
        )
        await database.save_step(
            PlanStep(task_id=task.id, index=2, description="Write the outcome", agent_id="writer")
        )
        return task.id

    task_id = asyncio.run(seed())
    app = create_app(Settings(database_path=db_path))
    with TestClient(app):
        async def read_state():
            database = Database(db_path)
            await database.initialize()
            for _ in range(200):
                task = await database.get_task(task_id)
                if task and task.status in {"done", "failed"}:
                    break
                await asyncio.sleep(0.02)
            return task, await database.list_steps(task_id), await database.list_messages(task_id)

        task, steps, messages = asyncio.run(read_state())

    kinds = [message.kind for message in messages]
    message_ids = [message.id for message in messages]
    assert task.status == "done"
    # The step that finished before the restart keeps its recorded result and is never
    # handed to a provider again.
    assert steps[0].result == "Step one result"
    assert steps[1].result.startswith("Mock result:")
    assert kinds.count("assignment") == 1
    assert kinds.count("response") == 1
    assert kinds.count("summary") == 1
    assert f"{task_id}:step:1:response" not in message_ids
    assert f"{task_id}:step:2:response" in message_ids
    assert f"{task_id}:summary" in message_ids


def test_cancelling_a_recovered_workflow_stops_its_execution(tmp_path, monkeypatch):
    # The provider blocks until the test releases it, so the recovered workflow is
    # guaranteed to be in flight when the cancel request arrives.
    release = threading.Event()

    class BlockingWorkerProvider(LLMProvider):
        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            if "Return only valid JSON" in user_prompt:
                return '{"steps":[{"description":"Do slow work","agent_id":"researcher"}]}'
            if "final summary" in user_prompt.lower():
                return "Late summary"
            while not release.is_set():
                await asyncio.sleep(0.05)
            return "Late result"

    monkeypatch.setattr(
        "app.agents.orchestrator.create_provider",
        lambda provider_name, *, model, settings: BlockingWorkerProvider(),
    )
    db_path = tmp_path / "cancel-recovered.db"

    async def seed():
        database = Database(db_path)
        await database.initialize()
        for agent in default_agents():
            await database.save_agent(agent)
        task = Task(
            title="Interrupted planning",
            instruction="Recover and stop me",
            status="planning",
        )
        await database.save_task(task)
        return task.id

    task_id = asyncio.run(seed())
    app = create_app(Settings(database_path=db_path))
    with TestClient(app) as client:
        for _ in range(200):
            detail = client.get(f"/api/tasks/{task_id}").json()
            if detail["task"]["status"] == "running":
                break
            time.sleep(0.02)
        assert detail["task"]["status"] == "running"
        # A recovered workflow is tracked like a new one so it can be cancelled.
        assert task_id in app.state.task_jobs

        cancelled = client.post(f"/api/tasks/{task_id}/cancel")

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["cancelled_by_user"] is True

        # Let the in-flight provider call finish: the cancelled workflow must stay
        # stopped instead of completing and overwriting the cancellation.
        release.set()
        time.sleep(0.5)
        detail = client.get(f"/api/tasks/{task_id}").json()
        messages = client.get(f"/api/tasks/{task_id}/messages").json()

    assert task_id not in app.state.task_jobs
    assert detail["task"]["status"] == "cancelled"
    assert detail["task"]["cancelled_by_user"] is True
    assert all(message["kind"] != "summary" for message in messages)


def test_recovery_replaces_partial_summary_instead_of_duplicating_it(tmp_path):
    db_path = tmp_path / "review-recovery.db"

    async def seed():
        database = Database(db_path)
        await database.initialize()
        for agent in default_agents():
            await database.save_agent(agent)
        task = Task(
            title="Interrupted summary",
            instruction="Summarise the finished work",
            status="review",
        )
        await database.save_task(task)
        await database.save_step(
            PlanStep(
                task_id=task.id,
                index=1,
                description="Prepare the deliverables",
                agent_id="researcher",
                status="done",
                result="Prepared deliverables",
            )
        )
        await database.save_message(
            Message(
                id=f"{task.id}:summary",
                task_id=task.id,
                sender="orion",
                receiver="human",
                kind="summary",
                content="partial summary",
            )
        )
        return task.id

    task_id = asyncio.run(seed())
    app = create_app(Settings(database_path=db_path))
    with TestClient(app):
        async def read_state():
            database = Database(db_path)
            await database.initialize()
            for _ in range(200):
                task = await database.get_task(task_id)
                if task and task.status == "done":
                    break
                await asyncio.sleep(0.02)
            return task, await database.list_messages(task_id)

        task, messages = asyncio.run(read_state())

    summaries = [message for message in messages if message.kind == "summary"]
    assert task.status == "done"
    assert len(summaries) == 1
    assert summaries[0].id == f"{task_id}:summary"
    assert summaries[0].content != "partial summary"

