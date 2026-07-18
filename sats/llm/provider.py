from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sats.llm.model_config import resolve_model_selection

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency guard
    load_dotenv = None  # type: ignore[assignment]

try:
    from langchain_openai import ChatOpenAI
except ImportError:  # pragma: no cover - dependency guard
    ChatOpenAI = None  # type: ignore[assignment]


if ChatOpenAI is not None:

    class ChatOpenAIWithReasoning(ChatOpenAI):  # type: ignore[misc,valid-type]
        """ChatOpenAI wrapper that preserves provider reasoning content."""

        @staticmethod
        def _capture(src: dict[str, Any], msg: Any) -> None:
            value = src.get("reasoning_content") or src.get("reasoning")
            if value:
                msg.additional_kwargs["reasoning_content"] = value

        def _create_chat_result(self, response: Any, generation_info: dict | None = None):  # type: ignore[override]
            result = super()._create_chat_result(response, generation_info)
            raw = response if isinstance(response, dict) else response.model_dump()
            for gen, choice in zip(result.generations, raw.get("choices", [])):
                message = choice.get("message", {})
                self._capture(message, gen.message)
            return result

        def _convert_chunk_to_generation_chunk(  # type: ignore[override]
            self,
            chunk: dict,
            default_chunk_class: type,
            base_generation_info: dict | None,
        ):
            gen = super()._convert_chunk_to_generation_chunk(
                chunk,
                default_chunk_class,
                base_generation_info,
            )
            if gen is None:
                return None
            choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices")
            if choices:
                self._capture(choices[0].get("delta", {}), gen.message)
            return gen

        def _get_request_payload(
            self,
            input_: Any,
            *,
            stop: list[str] | None = None,
            **kwargs: Any,
        ) -> dict:
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            messages = super()._convert_input(input_).to_messages()
            raw_messages = _raw_input_messages(input_)
            for index, message in enumerate(payload.get("messages", [])):
                if message.get("role") != "assistant" or index >= len(messages):
                    continue
                if message.get("content") is None:
                    message["content"] = ""
                reasoning = messages[index].additional_kwargs.get("reasoning_content")
                if not reasoning and index < len(raw_messages):
                    reasoning = _reasoning_from_raw_message(raw_messages[index])
                if reasoning:
                    message["reasoning_content"] = reasoning
            return payload

else:
    ChatOpenAIWithReasoning = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ProviderSpec:
    api_key_env: str | None
    base_url_env: str
    default_model: str
    default_base_url: str


_PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec("OPENAI_API_KEY", "OPENAI_BASE_URL", "gpt-4o-mini", "https://api.openai.com/v1"),
    "openrouter": ProviderSpec("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "deepseek/deepseek-v3.2", "https://openrouter.ai/api/v1"),
    "deepseek": ProviderSpec("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "deepseek-chat", "https://api.deepseek.com/v1"),
    "gemini": ProviderSpec("GEMINI_API_KEY", "GEMINI_BASE_URL", "gemini-2.5-flash", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    "groq": ProviderSpec("GROQ_API_KEY", "GROQ_BASE_URL", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1"),
    "dashscope": ProviderSpec("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "qwen": ProviderSpec("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "zhipu": ProviderSpec("ZHIPU_API_KEY", "ZHIPU_BASE_URL", "glm-4-plus", "https://open.bigmodel.cn/api/paas/v4"),
    "moonshot": ProviderSpec("MOONSHOT_API_KEY", "MOONSHOT_BASE_URL", "kimi-k2.5", "https://api.moonshot.ai/v1"),
    "minimax": ProviderSpec("MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MiniMax-M2.7", "https://api.minimax.io/v1"),
    "mimo": ProviderSpec("MIMO_API_KEY", "MIMO_BASE_URL", "MiMo-72B-A27B", "https://api.xiaomimimo.com/v1"),
    "zai": ProviderSpec("ZAI_API_KEY", "ZAI_BASE_URL", "glm-5.1", "https://api.z.ai/api/coding/paas/v4"),
    "ollama": ProviderSpec(None, "OLLAMA_BASE_URL", "qwen2.5:32b", "http://localhost:11434/v1"),
}

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_CANDIDATES = [Path.cwd() / ".env", _PROJECT_ROOT / ".env"]
_dotenv_loaded = False


def build_llm(
    *,
    model_name: str | None = None,
    callbacks: Any = None,
    timeout_seconds: int | None = None,
    profile: str = "default",
    max_retries: int | None = None,
) -> Any:
    provider = _sync_provider_env(profile=profile)
    if ChatOpenAIWithReasoning is None:
        raise RuntimeError("langchain-openai is not installed; install requirements.txt")

    model = model_name or _resolve_model_name(provider, profile=profile)
    temperature = _float_env("LLM_TEMPERATURE", 0.0)
    if provider == "minimax" and temperature <= 0.0:
        temperature = 0.01

    effort = os.getenv("LLM_REASONING_EFFORT", "").strip().lower()
    return ChatOpenAIWithReasoning(
        model=model,
        temperature=temperature,
        timeout=timeout_seconds if timeout_seconds is not None else _int_env("LLM_TIMEOUT_SECONDS", 120),
        max_retries=max_retries if max_retries is not None else _int_env("LLM_MAX_RETRIES", 2),
        callbacks=callbacks,
        extra_body={"reasoning": {"effort": effort}} if effort else None,
    )


def extract_json_object(text: str) -> dict[str, Any] | None:
    start = -1
    depth = 0
    in_string = False
    escape = False

    for index, char in enumerate(text):
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    start = -1
                    continue
                return parsed if isinstance(parsed, dict) else None
    return None


def _raw_input_messages(input_: Any) -> list[Any]:
    if isinstance(input_, (list, tuple)):
        return list(input_)
    if isinstance(input_, dict):
        messages = input_.get("messages")
        if isinstance(messages, (list, tuple)):
            return list(messages)
    return []


def _reasoning_from_raw_message(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    direct = message.get("reasoning_content") or message.get("reasoning")
    if direct:
        return str(direct)
    additional = message.get("additional_kwargs")
    if isinstance(additional, dict):
        value = additional.get("reasoning_content") or additional.get("reasoning")
        if value:
            return str(value)
    return ""


def _sync_provider_env(*, profile: str = "default") -> str:
    _ensure_dotenv()
    selection = resolve_model_selection(profile=profile)
    provider = selection.provider
    if provider in {"openai-codex", "openai_codex"}:
        raise RuntimeError("openai-codex OAuth provider is not supported in SATS LLM provider v1")

    spec = _PROVIDERS.get(provider)
    if spec is None:
        raise RuntimeError(f"unsupported LLM provider: {provider}")

    api_key = selection.api_key or ("ollama" if spec.api_key_env is None else "")
    base_url = selection.base_url or spec.default_base_url

    os.environ["OPENAI_API_KEY"] = api_key
    if base_url:
        os.environ["OPENAI_API_BASE"] = base_url
        os.environ["OPENAI_BASE_URL"] = base_url
    return provider


def _resolve_provider_name(*, profile: str = "default") -> str:
    return resolve_model_selection(profile=profile).provider


def _resolve_model_name(provider: str, *, profile: str = "default") -> str:
    return resolve_model_selection(profile=profile).model


def _ensure_dotenv() -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    for candidate in _ENV_CANDIDATES:
        if candidate.exists():
            _load_env_file(candidate)
            break
    _dotenv_loaded = True


def _load_env_file(path: Path) -> None:
    if load_dotenv is not None:
        load_dotenv(dotenv_path=path, override=False)
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)
