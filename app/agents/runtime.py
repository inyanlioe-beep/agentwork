import asyncio
from collections.abc import Awaitable, Callable
import logging

import httpx

from app.models import AgentConfig
from app.providers.base import LLMProvider

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        config: AgentConfig,
        provider: LLMProvider,
        *,
        timeout_seconds: float = 60,
        max_retries: int = 2,
    ) -> None:
        self.config = config
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    async def respond(
        self,
        instruction: str,
        *,
        context: str = "",
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
        on_retry: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        user_prompt = instruction
        if context:
            user_prompt = f"Task context:\n{context}\n\nAssignment:\n{instruction}"
        for attempt in range(self.max_retries + 1):
            try:
                chunks: list[str] = []
                async with asyncio.timeout(self.timeout_seconds):
                    async for chunk in self.provider.stream(
                        system_prompt=self.config.persona,
                        user_prompt=user_prompt,
                    ):
                        chunks.append(chunk)
                        if on_chunk is not None:
                            await on_chunk(chunk)
                response = "".join(chunks).strip()
                if not response:
                    raise ValueError("Provider returned an empty chat completion")
                return response
            except asyncio.CancelledError:
                raise
            except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self.max_retries:
                    raise TimeoutError(
                        f"Provider '{self.config.provider}' failed after {attempt + 1} attempt(s)"
                    ) from exc
                if on_retry is not None:
                    await on_retry()
                self._log_retry(attempt + 1, exc)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {429, 500, 502, 503, 504}:
                    raise
                if attempt >= self.max_retries:
                    raise
                if on_retry is not None:
                    await on_retry()
                self._log_retry(attempt + 1, exc)
            except ValueError as exc:
                if attempt >= self.max_retries:
                    raise
                if on_retry is not None:
                    await on_retry()
                self._log_retry(attempt + 1, exc)
            await asyncio.sleep(min(0.25 * (2**attempt), 2))
        raise RuntimeError("Provider retry loop exited unexpectedly")

    def _log_retry(self, attempt: int, error: Exception) -> None:
        logger.warning(
            "Retrying provider %s for agent %s after attempt %s: %s",
            self.config.provider,
            self.config.id,
            attempt,
            error,
        )
