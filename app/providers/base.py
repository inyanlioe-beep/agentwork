from abc import ABC, abstractmethod
import asyncio
from collections.abc import AsyncIterator


class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        """Return one assistant response for the given prompts."""

    async def stream(
        self, *, system_prompt: str, user_prompt: str
    ) -> AsyncIterator[str]:
        response = await self.complete(system_prompt=system_prompt, user_prompt=user_prompt)
        for offset in range(0, len(response), 12):
            yield response[offset : offset + 12]
            await asyncio.sleep(0.01)
