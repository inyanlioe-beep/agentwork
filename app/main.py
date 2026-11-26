import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from app.bus import EventBus
from app.config import Settings, get_settings
from app.db import Database, default_agents
from app.models import AgentConfig, AgentCreateRequest, AgentUpdateRequest, Event, Task, TaskAnswer, TaskRequest
from app.agents.orchestrator import Orchestrator
from app.rate_limit import rate_limiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    database = Database(config.database_path)
    bus = EventBus()


    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await database.initialize()
        if not await database.list_agents():
            for agent in default_agents(config.provider, config.model):
                await database.save_agent(agent)
        app.state.database = database
        app.state.bus = bus
        app.state.background_tasks = set()
        app.state.task_jobs = {}
        app.state.websocket_connections = set()

        def spawn_background(task_id: str, coro) -> None:
            """Track workflow work per task so shutdown and /cancel can stop it."""
            background_task = asyncio.create_task(coro)
            app.state.background_tasks.add(background_task)
            app.state.task_jobs[task_id] = background_task

            def finish(completed: asyncio.Task, task_id: str = task_id) -> None:
                app.state.background_tasks.discard(completed)
                if app.state.task_jobs.get(task_id) is completed:
                    del app.state.task_jobs[task_id]
                if not completed.cancelled() and completed.exception():
                    logger.error("Task execution failed: %s", completed.exception())

            background_task.add_done_callback(finish)

        app.state.spawn_task = spawn_background
        orchestrator = Orchestrator(database, bus, config, spawn_task=spawn_background)
        app.state.orchestrator = orchestrator

        for task in await database.list_tasks():
            if task.status == "waiting_input" and task.pending_question:
                logger.info("Resuming waiting task %s", task.id)
                orchestrator.restore_waiting_waiter(task)
            elif task.status == "cancelled" and not task.cancelled_by_user:
                logger.info("Resuming interrupted cancelled task %s", task.id)
                orchestrator.start_recovery(task)
            elif task.status in {"planning", "running", "review"}:
                logger.warning("Marking interrupted task %s as cancelled", task.id)
                task = await orchestrator.mark_cancelled(
                    task,
                    "Task interrupted by application restart",
                    cancelled_by_user=False,
                )
                orchestrator.start_recovery(task)
        
        # Initialize rate limiter
        await rate_limiter.initialize(config)
        
        yield
        pending = list(app.state.background_tasks)
        for background_task in pending:
            background_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(title="Agent Workspace", version="0.8.2", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def dashboard(request: Request):
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/agents")
    async def agents():
        return await database.list_agents()

    @app.post("/api/agents", status_code=201)
    async def create_agent(agent_request: AgentCreateRequest):
        existing = await database.get_agent(agent_request.id)
        if existing:
            raise HTTPException(status_code=409, detail=f"Agent '{agent_request.id}' already exists")
        agent = AgentConfig(**agent_request.model_dump())
        await database.save_agent(agent)
        await bus.publish(Event(task_id="system", type="agent_created", payload={"agent_id": agent.id, "agent": agent.model_dump(mode="json")}))
        return agent

    @app.put("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, agent_request: AgentUpdateRequest):
        existing = await database.get_agent(agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Agent not found")
        updates = agent_request.model_dump(exclude_unset=True)
        updated = existing.model_copy(update=updates)
        await database.save_agent(updated)
        await bus.publish(Event(task_id="system", type="agent_updated", payload={"agent_id": agent_id, "agent": updated.model_dump(mode="json")}))
        return updated

    @app.delete("/api/agents/{agent_id}", status_code=204)
    async def delete_agent(agent_id: str):
        existing = await database.get_agent(agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Agent not found")
        if existing.role == "orchestrator":
            raise HTTPException(status_code=403, detail="Cannot delete orchestrator agent")
        active_count = await database.count_active_tasks_for_agent(agent_id)
        if active_count > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete agent with {active_count} active task(s). Wait for tasks to complete or cancel them first.",
            )
        await database.delete_agent(agent_id)
        await bus.publish(Event(task_id="system", type="agent_deleted", payload={"agent_id": agent_id}))
        return Response(status_code=204)

    @app.patch("/api/agents/{agent_id}/toggle")
    async def toggle_agent(agent_id: str):
        existing = await database.get_agent(agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Agent not found")
        updated = existing.model_copy(update={"enabled": not existing.enabled})
        await database.save_agent(updated)
        await bus.publish(Event(task_id="system", type="agent_updated", payload={"agent_id": agent_id, "agent": updated.model_dump(mode="json")}))
        return updated

    @app.get("/api/providers")
    async def providers():
        return {
            "providers": [
                {"id": "mock", "name": "Mock Provider (Testing)", "requires_key": False},
                {"id": "openai", "name": "OpenAI", "requires_key": True},
                {"id": "groq", "name": "Groq", "requires_key": True},
                {"id": "ollama", "name": "Ollama (Local)", "requires_key": False},
                {"id": "openai-compatible", "name": "OpenAI-Compatible", "requires_key": True},
                {"id": "hemattoken", "name": "HematToken (OpenAI-Compatible)", "requires_key": True},
            ],
            "models": {
                "mock": ["mock-agent"],
                "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
                "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
                "ollama": ["llama3.2", "mistral", "codellama", "qwen2.5"],
                "openai-compatible": [],
                "hemattoken": [],
            },
        }

    @app.get("/api/tasks")
    async def tasks():
        return await database.list_tasks()

    @app.post("/api/tasks", status_code=202)
    async def create_task(task_request: TaskRequest, request: Request):
        # Apply rate limiting
        await rate_limiter.check_rate_limit(request, "task_create:")
        
        instruction = task_request.instruction.strip()
        if not instruction:
            raise HTTPException(status_code=422, detail="Instruction cannot be blank")
        task = Task(
            title=instruction.splitlines()[0][:120],
            instruction=instruction,
        )
        await database.save_task(task)
        app.state.spawn_task(task.id, app.state.orchestrator.run(task))
        return task

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        task = await database.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status in {"done", "failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="Task is already complete")

        background_task = app.state.task_jobs.get(task_id)
        if background_task is not None and not background_task.done():
            background_task.cancel()
            await asyncio.gather(background_task, return_exceptions=True)

        task = await database.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status in {"done", "failed"}:
            raise HTTPException(status_code=409, detail="Task is already complete")
        # The cancelled workflow may have persisted its own interruption record, so
        # stamping the explicit cancellation has to happen even when the task already
        # reports "cancelled".
        return await app.state.orchestrator.cancel_by_user(task)

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str):
        task = await database.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"task": task, "steps": await database.list_steps(task_id)}

    @app.get("/api/tasks/{task_id}/messages")
    async def task_messages(task_id: str):
        if await database.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return await database.list_messages(task_id)

    @app.get("/api/tasks/{task_id}/export")
    async def export_task_transcript(task_id: str):
        task = await database.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        agents = {agent.id: agent.name for agent in await database.list_agents()}
        lines = [
            f"# {task.title}",
            "",
            f"- Status: {task.status}",
            f"- Dibuat: {task.created_at.isoformat()}",
            "",
            "## Transcript",
            "",
        ]
        for message in await database.list_messages(task_id):
            sender = "Kamu" if message.sender == "human" else (
                "Workspace" if message.sender == "system" else agents.get(message.sender, message.sender)
            )
            receiver = "Kamu" if message.receiver == "human" else (
                "Workspace" if message.receiver == "system" else agents.get(message.receiver, message.receiver)
            )
            lines.extend(
                [
                    f"### {message.created_at.isoformat()} — {sender} → {receiver} ({message.kind})",
                    "",
                    message.content,
                    "",
                ]
            )
        return Response(
            content="\n".join(lines),
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="agentwork-{task.id}.md"'
            },
        )

    @app.post("/api/tasks/{task_id}/answer")
    async def answer_task(task_id: str, request: TaskAnswer):
        try:
            return await app.state.orchestrator.submit_answer(task_id, request.answer)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket):
        if len(app.state.websocket_connections) >= config.websocket_max_connections:
            await websocket.close(code=1013)
            return
        await websocket.accept()
        app.state.websocket_connections.add(websocket)
        queue = bus.subscribe()
        task_filter = websocket.query_params.get("task_id")
        try:
            while True:
                event = await queue.get()
                if task_filter is None or event.task_id == task_filter:
                    await websocket.send_json(event.model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        finally:
            app.state.websocket_connections.discard(websocket)
            bus.unsubscribe(queue)

    return app

app = create_app()
