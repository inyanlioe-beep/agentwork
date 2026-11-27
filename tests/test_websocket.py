"""Regression tests for connection accounting on the /ws endpoint.

Browser-facing symptom they cover: after a tab was closed or reloaded the server kept
counting that connection, so once enough dashboards had come and gone a fresh one was
refused with HTTP 403 / close code 1013 until some event happened to be published to the
abandoned queue.

The clients below talk to the app over raw ASGI messages instead of
``starlette.testclient``, because ``WebSocketTestSession.__exit__`` cancels the app task
and would therefore run the endpoint's cleanup block for the wrong reason. uvicorn never
cancels that task on disconnect; it only delivers a ``websocket.disconnect`` message.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from app.models import Event


class ASGIWebSocketClient:
    """Minimal websocket client: feeds ASGI events in, records the events sent back."""

    def __init__(self, query_string: bytes = b"") -> None:
        self.query_string = query_string
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[dict] = []

    def scope(self) -> dict:
        return {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": "/ws",
            "raw_path": b"/ws",
            "query_string": self.query_string,
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    def open(self) -> None:
        self.incoming.put_nowait({"type": "websocket.connect"})

    def close(self, code: int = 1000) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": code})

    def sent_payloads(self) -> list[dict]:
        return [json.loads(message["text"]) for message in self.sent if message["type"] == "websocket.send"]


async def wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not met before the timeout")
        await asyncio.sleep(0.01)


async def start_session(app: FastAPI, client: ASGIWebSocketClient) -> asyncio.Task:
    client.open()
    return asyncio.create_task(app(client.scope(), client.receive, client.send))


async def stop_session(handler: asyncio.Task) -> None:
    handler.cancel()
    await asyncio.gather(handler, return_exceptions=True)


@pytest.mark.asyncio
async def test_abandoned_websocket_frees_its_slot_for_the_next_client(tmp_path):
    settings = Settings(database_path=tmp_path / "workspace.db", websocket_max_connections=1)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        first = ASGIWebSocketClient()
        first_handler = await start_session(app, first)
        try:
            await wait_until(lambda: len(app.state.websocket_connections) == 1)
            assert first.sent[0]["type"] == "websocket.accept"

            # The browser goes away while the endpoint sits idle on the event queue, so no
            # event is ever published to tell it that the socket is dead.
            first.close()
            await asyncio.wait_for(first_handler, timeout=5)
        finally:
            await stop_session(first_handler)

        assert app.state.websocket_connections == set()

        second = ASGIWebSocketClient()
        second_handler = await start_session(app, second)
        try:
            await wait_until(lambda: second.sent)
            # With the slot leaked this is a close (1013) instead of an accept.
            assert second.sent[0]["type"] == "websocket.accept"
            assert len(app.state.websocket_connections) == 1
        finally:
            await stop_session(second_handler)


@pytest.mark.asyncio
async def test_events_still_reach_the_matching_task_filter(tmp_path):
    settings = Settings(database_path=tmp_path / "workspace.db")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        matching = ASGIWebSocketClient(query_string=b"task_id=task-1")
        other = ASGIWebSocketClient(query_string=b"task_id=task-2")
        matching_handler = await start_session(app, matching)
        other_handler = await start_session(app, other)
        try:
            await wait_until(lambda: len(app.state.websocket_connections) == 2)

            await app.state.bus.publish(Event(task_id="task-1", type="step_started", payload={"step": 1}))
            await wait_until(lambda: matching.sent_payloads())

            assert matching.sent_payloads()[0]["type"] == "step_started"
            assert matching.sent_payloads()[0]["task_id"] == "task-1"
            assert other.sent_payloads() == []
        finally:
            await stop_session(matching_handler)
            await stop_session(other_handler)
