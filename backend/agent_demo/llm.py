"""A thin wrapper around the OpenAI-compatible chat-completions API.

The agent loop only depends on `LLMReply`, not on the `openai` SDK, which is
what lets the tests swap in a scripted fake LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from openai import AsyncOpenAI


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text, exactly as the model produced it


@dataclass
class LLMReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str | None = None  # e.g. DeepSeek's `reasoning_content`; shown, never sent back
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatModel(Protocol):
    """Anything with this method can drive the agent (real client or test fake)."""

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply: ...


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        kwargs = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        response = await self._client.chat.completions.create(**kwargs)

        message = response.choices[0].message
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
            for tc in (message.tool_calls or [])
        ]
        # Non-standard field: only some providers return it, so read it defensively.
        reasoning = getattr(message, "reasoning_content", None)
        usage = response.usage
        return LLMReply(
            content=message.content,
            tool_calls=tool_calls,
            reasoning=reasoning,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )
