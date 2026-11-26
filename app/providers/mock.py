import asyncio
import json
from collections.abc import AsyncIterator

from app.providers.base import LLMProvider


class MockProvider(LLMProvider):
    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        if "Return only valid JSON" in user_prompt:
            return json.dumps(
                {
                    "steps": [
                        {
                            "description": "Analyze the request and identify the key requirements.",
                            "agent_id": "researcher",
                        },
                        {
                            "description": "Prepare a concise, actionable result from the analysis.",
                            "agent_id": "writer",
                        },
                    ]
                }
            )
        if "final summary" in user_prompt.lower():
            return f"Task completed. Results:\n\n{user_prompt}"
        return f"Mock result: {user_prompt[:500]}"

    async def stream(
        self, *, system_prompt: str, user_prompt: str
    ) -> AsyncIterator[str]:
        response = await self.complete(system_prompt=system_prompt, user_prompt=user_prompt)
        for offset in range(0, len(response), 18):
            yield response[offset : offset + 18]
            await asyncio.sleep(0.025)
