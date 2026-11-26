from functools import lru_cache
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

class Settings(BaseModel):
    database_path: Path = Field(default_factory=lambda: PROJECT_ROOT / "data" / "workspace.db")
    provider: str = "mock"
    model: str = "mock-agent"
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=60, gt=0)
    max_steps: int = Field(default=8, ge=1, le=50)
    max_retries: int = Field(default=2, ge=0, le=5)
    task_rate_limit_per_minute: int = Field(default=10, ge=1, le=600)
    redis_url: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000", "http://127.0.0.1:8000"])
    trusted_proxy_ips: list[str] = Field(default_factory=list)
    websocket_max_connections: int = Field(default=100, ge=1, le=10_000)

    @field_validator("database_path")
    @classmethod
    def resolve_database_path(cls, path: Path) -> Path:
        return path if path.is_absolute() else PROJECT_ROOT / path

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv()
        values: dict[str, object] = {}
        env_map = {
            "database_path": "AGENTWORK_DATABASE_PATH",
            "provider": "AGENTWORK_PROVIDER",
            "model": "AGENTWORK_MODEL",
            "api_key": "AGENTWORK_API_KEY",
            "base_url": "AGENTWORK_BASE_URL",
            "timeout_seconds": "AGENTWORK_TIMEOUT_SECONDS",
            "max_steps": "AGENTWORK_MAX_STEPS",
            "max_retries": "AGENTWORK_MAX_RETRIES",
            "task_rate_limit_per_minute": "AGENTWORK_TASKS_PER_MINUTE",
            "redis_url": "AGENTWORK_REDIS_URL",
            "cors_origins": "AGENTWORK_CORS_ORIGINS",
            "trusted_proxy_ips": "AGENTWORK_TRUSTED_PROXY_IPS",
            "websocket_max_connections": "AGENTWORK_WEBSOCKET_MAX_CONNECTIONS",
        }
        for field, env_name in env_map.items():
            value = os.getenv(env_name)
            if value is not None:
                if field in {"cors_origins", "trusted_proxy_ips"}:
                    values[field] = [item.strip() for item in value.split(",") if item.strip()]
                else:
                    values[field] = value
        return cls.model_validate(values)

@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()
