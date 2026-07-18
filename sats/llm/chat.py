from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

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
        max_retries: int | None = None,
    ) -> None:
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self.profile = profile
        self.max_retries = max_retries
        build_kwargs = dict(
            model_name=model_name,
            timeout_seconds=timeout_seconds,
            profile=profile,
        )
        if max_retries is not None:
            build_kwargs["max_retries"] = max_retries
        self._llm = build_llm(**build_kwargs)
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
            return self.strict_stream_chat(
                messages,
                tools=tools,
                on_text_chunk=on_text_chunk,
                timeout=timeout,
            )
        except Exception:
            return self.chat(messages, tools=tools, timeout=timeout)

    def strict_stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text_chunk: Any | None = None,
        timeout: int | None = None,
    ) -> LLMResponse:
        """Stream once and propagate transport failures without a hidden chat retry."""

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

    def _llm_for_timeout(self, timeout: int | None) -> Any:
        if timeout is None or timeout == self.timeout_seconds:
            return self._llm
        cached = self._timeout_llms.get(timeout)
        if cached is None:
            build_kwargs = dict(
                model_name=self.model_name,
                timeout_seconds=timeout,
                profile=self.profile,
            )
            if self.max_retries is not None:
                build_kwargs["max_retries"] = self.max_retries
            cached = build_llm(**build_kwargs)
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


class LightFallbackChatLLM:
    def __init__(
        self,
        factory: Callable[..., Any],
        *,
        light_model_name: str,
        default_model_name: str,
        timeout_seconds: int | None = None,
    ) -> None:
        self.factory = factory
        self.light_model_name = light_model_name
        self.default_model_name = default_model_name
        self.timeout_seconds = timeout_seconds
        self.kwargs = {
            "model_name": light_model_name,
            "profile": "light",
            "timeout_seconds": timeout_seconds,
        }
        self._light_llm: Any | None = None
        self._default_llm: Any | None = None
        self._last_llm: Any | None = None
        self._last_profile = ""
        self._last_model_name = ""

    @property
    def last_profile(self) -> str:
        return self._last_profile

    @property
    def last_model_name(self) -> str:
        return self._last_model_name

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout: int | None = None,
    ) -> Any:
        return self._with_fallback(lambda llm: _call_chat_compatible(llm, messages, tools=tools, timeout=timeout))

    def chat_validated(
        self,
        messages: list[dict[str, Any]],
        validator: Callable[[Any], Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        timeout: int | None = None,
    ) -> Any:
        return self._with_fallback(lambda llm: validator(_call_chat_compatible(llm, messages, tools=tools, timeout=timeout)))

    def _with_fallback(self, call: Callable[[Any], Any]) -> Any:
        try:
            llm = self._light_client()
            self._mark_last(llm, profile="light", model_name=self.light_model_name)
            return call(llm)
        except Exception:
            llm = self._default_client()
            self._mark_last(llm, profile="default", model_name=self.default_model_name)
            return call(llm)

    def _light_client(self) -> Any:
        if self._light_llm is None:
            self._light_llm = _create_llm_client(
                self.factory,
                self.light_model_name,
                profile="light",
                timeout_seconds=self.timeout_seconds,
            )
        return self._light_llm

    def _default_client(self) -> Any:
        if self._default_llm is None:
            self._default_llm = _create_llm_client(
                self.factory,
                self.default_model_name,
                profile="default",
                timeout_seconds=self.timeout_seconds,
            )
        return self._default_llm

    def _mark_last(self, llm: Any, *, profile: str, model_name: str) -> None:
        self._last_llm = llm
        self._last_profile = profile
        self._last_model_name = model_name

    def __getattr__(self, name: str) -> Any:
        target = self._last_llm if self._last_llm is not None else self._light_client()
        return getattr(target, name)


def build_light_fallback_llm(
    factory: Callable[..., Any],
    *,
    light_model_name: str,
    default_model_name: str,
    timeout_seconds: int | None = None,
) -> LightFallbackChatLLM:
    return LightFallbackChatLLM(
        factory,
        light_model_name=light_model_name,
        default_model_name=default_model_name,
        timeout_seconds=timeout_seconds,
    )


def build_standard_llm(
    factory: Callable[..., Any],
    *,
    model_name: str,
    timeout_seconds: int | None = None,
    max_retries: int | None = None,
) -> Any:
    return _create_llm_client(
        factory,
        model_name,
        profile="default",
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


def _create_llm_client(
    factory: Callable[..., Any],
    model_name: str,
    *,
    profile: str,
    timeout_seconds: int | None = None,
    max_retries: int | None = None,
) -> Any:
    kwargs = {
        "model_name": model_name,
        "profile": profile,
        "timeout_seconds": timeout_seconds,
    }
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    try:
        return factory(**kwargs)
    except TypeError:
        if max_retries is not None:
            try:
                return factory(
                    model_name=model_name,
                    profile=profile,
                    timeout_seconds=timeout_seconds,
                )
            except TypeError:
                pass
        try:
            return factory(model_name=model_name, timeout_seconds=timeout_seconds)
        except TypeError:
            try:
                return factory(model_name=model_name, profile=profile)
            except TypeError:
                try:
                    return factory(model_name=model_name)
                except TypeError:
                    return factory()


def _call_chat_compatible(
    llm: Any,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    timeout: int | None = None,
) -> Any:
    if tools is not None and timeout is not None:
        try:
            return llm.chat(messages, tools=tools, timeout=timeout)
        except TypeError:
            try:
                return llm.chat(messages, tools=tools)
            except TypeError:
                try:
                    return llm.chat(messages, timeout=timeout)
                except TypeError:
                    return llm.chat(messages)
    if tools is not None:
        try:
            return llm.chat(messages, tools=tools)
        except TypeError:
            return llm.chat(messages)
    if timeout is not None:
        try:
            return llm.chat(messages, timeout=timeout)
        except TypeError:
            return llm.chat(messages)
    return llm.chat(messages)


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
