import os

from app.config import Settings
from app.providers.base import LLMProvider
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider


def create_provider(
    provider_name: str,
    *,
    model: str,
    settings: Settings,
) -> LLMProvider:
    name = provider_name.lower()
    if name == "mock":
        return MockProvider()
    if name in {"openai", "groq", "ollama", "openai-compatible", "hemattoken"}:
        provider_env = name.upper().replace("-", "_")
        base_url = os.getenv(f"{provider_env}_BASE_URL") or settings.base_url
        if not base_url:
            defaults = {
                "openai": "https://api.openai.com/v1",
                "groq": "https://api.groq.com/openai/v1",
                "ollama": "http://localhost:11434/v1",
            }
            base_url = defaults.get(name)
        if not base_url:
            raise ValueError(f"AGENTWORK_BASE_URL is required for provider '{name}'")
        api_key = os.getenv(f"{provider_env}_API_KEY") or settings.api_key
        if name != "ollama" and not api_key:
            raise ValueError(f"AGENTWORK_API_KEY is required for provider '{name}'")
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=settings.timeout_seconds,
        )
    raise ValueError(f"Unsupported provider: {provider_name}")
