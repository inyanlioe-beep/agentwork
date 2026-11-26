from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentConfig(BaseModel):
    id: str
    name: str
    emoji: str = "🤖"
    provider: str = "mock"
    model: str = "mock-agent"
    role: Literal["orchestrator", "worker"]
    persona: str
    color: str = "#8b5cf6"
    enabled: bool = True


class PlanStep(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    index: int
    description: str
    agent_id: str
    status: Literal["pending", "in_progress", "needs_input", "done", "failed"] = "pending"
    result: str | None = None


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    instruction: str
    status: Literal[
        "queued", "planning", "running", "review", "waiting_input", "done", "failed", "cancelled"
    ] = "queued"
    summary: str | None = None
    pending_question: str | None = None
    waiting_step_index: int | None = None
    waiting_agent_id: str | None = None
    cancelled_step_index: int | None = None
    cancelled_context: str | None = None
    cancelled_by_user: bool = False
    interrupted_status: Literal["planning", "running", "review"] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    sender: str
    receiver: str
    kind: Literal[
        "instruction", "assignment", "response", "question", "review",
        "summary", "system", "error",
    ]
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class TaskRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=20_000)


class TaskAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=4_000)

    @field_validator("answer", mode="before")
    @classmethod
    def answer_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Answer cannot be blank")
        return normalized


class TaskPlan(BaseModel):
    steps: list["PlannedStep"] = Field(min_length=1)

class PlannedStep(BaseModel):
    description: str = Field(min_length=1, max_length=2_000)
    agent_id: str

class AgentCreateRequest(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$", min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    emoji: str = Field(default="🤖", max_length=10)
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    role: Literal["orchestrator", "worker"] = "worker"
    persona: str = Field(min_length=1, max_length=5_000)
    color: str = Field(default="#8b5cf6", pattern=r"^#[0-9a-fA-F]{6}$")
    enabled: bool = True

class AgentUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    emoji: str | None = Field(None, max_length=10)
    provider: str | None = Field(None, min_length=1, max_length=50)
    model: str | None = Field(None, min_length=1, max_length=200)
    persona: str | None = Field(None, min_length=1, max_length=5_000)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    enabled: bool | None = None

class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    type: str
    payload: dict[str, object]
    created_at: datetime = Field(default_factory=utc_now)
