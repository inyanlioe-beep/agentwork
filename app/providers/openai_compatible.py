import json
from collections.abc import AsyncIterator

import httpx

from app.providers.base import LLMProvider


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float = 60,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                },
            )
            response.raise_for_status()
            payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Provider returned an invalid chat completion response") from exc
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Provider returned an empty chat completion")
        return content.strip()

    async def stream(
        self, *, system_prompt: str, user_prompt: str
    ) -> AsyncIterator[str]:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                received_content = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    payload = json.loads(data)
                    choices = payload.get("choices")
                    if not isinstance(choices, list):
                        raise ValueError("Provider returned an invalid streaming response")
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    if not isinstance(delta, dict):
                        raise ValueError("Provider returned an invalid streaming response")
                    content = delta.get("content")
                    if content is None:
                        continue
                    if not isinstance(content, str):
                        raise ValueError("Provider returned invalid streamed content")
                    if content:
                        received_content = True
                        yield content
                if not received_content:
                    raise ValueError("Provider returned an empty chat completion")
