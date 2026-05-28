from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sats.llm.provider import build_llm


def _dedupe_finish_reason(raw: str) -> str:
    for marker in ("tool_calls", "function_call", "content_filter", "length", "stop"):
        if raw.endswith(marker):
            return marker
    return raw


@dataclass(slots=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    reasoning_content: str | None = None
    finish_reason: str = "stop"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class ChatLLM:
    def __init__(
        self,
        model_name: str | None = None,
        timeout_seconds: int | None = None,
        profile: str = "default",
    ) -> None:
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self.profile = profile
        self._llm = build_llm(
            model_name=model_name,
            timeout_seconds=timeout_seconds,
            profile=profile,
        )
        self._timeout_llms: dict[int | None, Any] = {}

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout: int | None = None,
    ) -> LLMResponse:
        llm = self._llm_for_timeout(timeout)
        llm = llm.bind_tools(tools) if tools else llm
        config = {"timeout": timeout} if timeout else {}
        ai_message = llm.invoke(messages, config=config)
        return self._parse_response(ai_message)

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout: int | None = None,
    ) -> LLMResponse:
        llm = self._llm_for_timeout(timeout)
        llm = llm.bind_tools(tools) if tools else llm
        config = {"timeout": timeout} if timeout else {}
        ai_message = await llm.ainvoke(messages, config=config)
        return self._parse_response(ai_message)

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text_chunk: Any | None = None,
        timeout: int | None = None,
    ) -> LLMResponse:
        try:
            llm = self._llm_for_timeout(timeout)
            llm = llm.bind_tools(tools) if tools else llm
            config = {"timeout": timeout} if timeout else {}
            accumulated = None
            for chunk in llm.stream(messages, config=config):
                content = getattr(chunk, "content", None)
                if content and on_text_chunk:
                    on_text_chunk(content if isinstance(content, str) else str(content))
                accumulated = chunk if accumulated is None else accumulated + chunk
            if accumulated is None:
                return LLMResponse(content="", finish_reason="stop")
            return self._parse_response(accumulated)
        except Exception:
            return self.chat(messages, tools=tools, timeout=timeout)

    def _llm_for_timeout(self, timeout: int | None) -> Any:
        if timeout is None or timeout == self.timeout_seconds:
            return self._llm
        cached = self._timeout_llms.get(timeout)
        if cached is None:
            cached = build_llm(
                model_name=self.model_name,
                timeout_seconds=timeout,
                profile=self.profile,
            )
            self._timeout_llms[timeout] = cached
        return cached

    @staticmethod
    def _parse_response(ai_message: Any) -> LLMResponse:
        metadata = getattr(ai_message, "response_metadata", {}) or {}
        additional = getattr(ai_message, "additional_kwargs", {}) or {}
        finish_reason = metadata.get("finish_reason") or metadata.get("stop_reason") or "stop"
        return LLMResponse(
            content=getattr(ai_message, "content", None),
            tool_calls=[_parse_tool_call(item) for item in (getattr(ai_message, "tool_calls", None) or [])],
            reasoning_content=additional.get("reasoning_content"),
            finish_reason=_dedupe_finish_reason(str(finish_reason)),
        )


def _parse_tool_call(tool_call: Any) -> ToolCallRequest:
    if isinstance(tool_call, dict):
        return ToolCallRequest(
            id=str(tool_call.get("id") or ""),
            name=str(tool_call.get("name") or ""),
            arguments=dict(tool_call.get("args") or tool_call.get("arguments") or {}),
        )
    return ToolCallRequest(
        id=str(getattr(tool_call, "id", "")),
        name=str(getattr(tool_call, "name", "")),
        arguments=dict(getattr(tool_call, "args", {}) or getattr(tool_call, "arguments", {}) or {}),
    )
