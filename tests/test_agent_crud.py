import pytest
import pytest_asyncio
from pathlib import Path
from httpx import AsyncClient, ASGITransport

from app.main import create_app
from app.config import Settings
from app.db import Database
from app.providers.registry import create_provider

@pytest_asyncio.fixture
async def test_app(tmp_path):
    db_path = tmp_path / "test_agents.db"
    settings = Settings(database_path=db_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app

@pytest_asyncio.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture
async def database(test_app):
    return test_app.state.database

@pytest.mark.asyncio
async def test_create_agent_success(client, database):
    response = await client.post("/api/agents", json={
        "id": "tester",
        "name": "Tester Agent",
        "emoji": "🧪",
        "provider": "mock",
        "model": "mock-agent",
        "role": "worker",
        "persona": "You are a test specialist.",
        "color": "#ff6b6b",
        "enabled": True,
    })
    assert response.status_code == 201
    data = response.json()
    assert data["id"] == "tester"
    assert data["name"] == "Tester Agent"
    assert data["enabled"] is True
    saved = await database.get_agent("tester")
    assert saved is not None
    assert saved.name == "Tester Agent"

@pytest.mark.asyncio
async def test_create_agent_duplicate_id(client):
    payload = {
        "id": "orion",
        "name": "Duplicate",
        "provider": "mock",
        "model": "mock-agent",
        "role": "worker",
        "persona": "Test",
    }
    response = await client.post("/api/agents", json=payload)
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]

@pytest.mark.asyncio
async def test_update_agent(client, database):
    response = await client.put("/api/agents/orion", json={
        "name": "Orion Updated",
        "persona": "Updated persona",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Orion Updated"
    assert data["persona"] == "Updated persona"
    saved = await database.get_agent("orion")
    assert saved.name == "Orion Updated"

@pytest.mark.asyncio
async def test_update_nonexistent_agent(client):
    response = await client.put("/api/agents/nonexistent", json={"name": "New"})
    assert response.status_code == 404

@pytest.mark.asyncio
async def test_delete_agent_success(client, database):
    agents = await database.list_agents()
    worker_agent = next((a for a in agents if a.role == "worker"), None)
    assert worker_agent is not None
    test_agent = worker_agent.model_copy(update={"id": "deleteme"})
    await database.save_agent(test_agent)
    response = await client.delete("/api/agents/deleteme")
    assert response.status_code == 204
    deleted = await database.get_agent("deleteme")
    assert deleted is None

@pytest.mark.asyncio
async def test_delete_orchestrator_forbidden(client):
    response = await client.delete("/api/agents/orion")
    assert response.status_code == 403
    assert "orchestrator" in response.json()["detail"].lower()

@pytest.mark.asyncio
async def test_delete_agent_not_found(client):
    response = await client.delete("/api/agents/nonexistent")
    assert response.status_code == 404

@pytest.mark.asyncio
async def test_toggle_agent(client, database):
    agent = await database.get_agent("researcher")
    original = agent.enabled
    response = await client.patch("/api/agents/researcher/toggle")
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is not original
    toggled = await database.get_agent("researcher")
    assert toggled.enabled is not original

@pytest.mark.asyncio
async def test_list_providers(client):
    response = await client.get("/api/providers")
    assert response.status_code == 200
    data = response.json()
    assert "providers" in data
    assert "models" in data
    assert any(p["id"] == "mock" for p in data["providers"])
    assert any(p["id"] == "hemattoken" for p in data["providers"])
    assert "mock" in data["models"]


def test_hemattoken_provider_uses_configured_endpoint():
    provider = create_provider(
        "hemattoken",
        model="zrouter/gpt-6-luna",
        settings=Settings(
            base_url="https://provider.example/v1",
            api_key="test-token",
        ),
    )

    assert provider.base_url == "https://provider.example/v1"
    assert provider.api_key == "test-token"
    assert provider.model == "zrouter/gpt-6-luna"
