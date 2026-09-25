"""Chat-model adapter. Token Factory inference is OpenAI-compatible."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

TOKEN_FACTORY_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
# NVIDIA Nemotron 3 Super: open-weights hybrid MoE tuned for tool calling and long-horizon agent work.
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, parsed by the tool layer


@dataclass
class Reply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls
            ]
        return msg


class ChatModel(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply: ...


class OpenAIChat:
    """Any OpenAI-compatible chat endpoint; defaults to Nebius Token Factory."""

    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> None:
        from openai import AsyncOpenAI

        self.model = model or os.environ.get("SANDCODER_MODEL", DEFAULT_MODEL)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = AsyncOpenAI(
            base_url=base_url or os.environ.get("TOKEN_FACTORY_BASE_URL", TOKEN_FACTORY_BASE_URL),
            api_key=api_key or os.environ.get("NEBIUS_API_KEY"),
        )

    async def list_models(self) -> list[str]:
        return sorted([m.id async for m in self._client.models.list()])

    def with_temperature(self, temperature: float) -> OpenAIChat:
        clone = object.__new__(OpenAIChat)
        clone.__dict__ = {**self.__dict__, "temperature": temperature}
        return clone

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            tool_choice="auto",
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
            for c in (msg.tool_calls or [])
            if c.type == "function"
        ]
        usage = {}
        if resp.usage:
            usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
        return Reply(content=msg.content, tool_calls=calls, usage=usage)
