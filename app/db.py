import json
from pathlib import Path

import aiosqlite
from app.models import AgentConfig, Event, Message, PlanStep, Task

class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Initialize SQLite database
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_steps (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    step_index INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    UNIQUE(task_id, step_index)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    created_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_task_created
                    ON messages(task_id, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    created_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                """
            )
            await db.commit()


    async def save_agent(self, agent: AgentConfig) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO agents(id, data) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (agent.id, agent.model_dump_json()),
            )
            await db.commit()

    async def get_agent(self, agent_id: str) -> AgentConfig | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT data FROM agents WHERE id=?", (agent_id,))
            row = await cursor.fetchone()
        return AgentConfig.model_validate_json(row[0]) if row else None

    async def list_agents(self) -> list[AgentConfig]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT data FROM agents ORDER BY id")
            rows = await cursor.fetchall()
        return [AgentConfig.model_validate_json(row[0]) for row in rows]

    async def delete_agent(self, agent_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            await db.commit()

    async def count_active_tasks_for_agent(self, agent_id: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM plan_steps AS step "
                "JOIN tasks AS task ON task.id = step.task_id "
                "WHERE json_extract(step.data, '$.agent_id')=? "
                "AND json_extract(step.data, '$.status') IN ('pending', 'in_progress', 'needs_input') "
                "AND task.status IN ('queued', 'planning', 'running', 'review', 'waiting_input')",
                (agent_id,),
            )
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def save_task(self, task: Task) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO tasks(id, status, created_at, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (task.id, task.status, task.created_at.isoformat(), task.model_dump_json()),
            )
            await db.commit()

    async def get_task(self, task_id: str) -> Task | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT data FROM tasks WHERE id=?", (task_id,))
            row = await cursor.fetchone()
        return Task.model_validate_json(row[0]) if row else None

    async def list_tasks(self, limit: int = 50) -> list[Task]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT data FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
        return [Task.model_validate_json(row[0]) for row in rows]

    async def save_step(self, step: PlanStep) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO plan_steps(id, task_id, step_index, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (step.id, step.task_id, step.index, step.model_dump_json()),
            )
            await db.commit()

    async def list_steps(self, task_id: str) -> list[PlanStep]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT data FROM plan_steps WHERE task_id=? ORDER BY step_index",
                (task_id,),
            )
            rows = await cursor.fetchall()
        return [PlanStep.model_validate_json(row[0]) for row in rows]

    async def reset_plan_steps(self, task_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM plan_steps WHERE task_id=?", (task_id,))
            await db.commit()

    async def save_message(self, message: Message) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO messages(id, task_id, created_at, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "task_id=excluded.task_id, created_at=excluded.created_at, data=excluded.data",
                (message.id, message.task_id, message.created_at.isoformat(), message.model_dump_json()),
            )
            await db.commit()

    async def list_messages(self, task_id: str) -> list[Message]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT data FROM messages WHERE task_id=? ORDER BY created_at, rowid",
                (task_id,),
            )
            rows = await cursor.fetchall()
        return [Message.model_validate_json(row[0]) for row in rows]

    async def save_event(self, event: Event) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO events(id, task_id, created_at, data) VALUES (?, ?, ?, ?)",
                (
                    event.id,
                    event.task_id,
                    event.created_at.isoformat(),
                    json.dumps(event.model_dump(mode="json")),
                ),
            )
            await db.commit()

def default_agents(provider: str = "mock", model: str = "mock-agent") -> list[AgentConfig]:
    return [
        AgentConfig(
            id="orion",
            name="Orion",
            emoji="🎯",
            provider=provider,
            model=model,
            role="orchestrator",
            persona="You coordinate specialist agents, create clear plans, review results, and summarize.",
            color="#8b5cf6",
        ),
        AgentConfig(
            id="researcher",
            name="Nova",
            emoji="🔎",
            provider=provider,
            model=model,
            role="worker",
            persona="You analyze requests and produce concise, useful research and requirements.",
            color="#38bdf8",
        ),
        AgentConfig(
            id="writer",
            name="Lyra",
            emoji="✍️",
            provider=provider,
            model=model,
            role="worker",
            persona="You synthesize information into clear, actionable deliverables.",
            color="#34d399",
        ),
    ]
