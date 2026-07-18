from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sats.llm.chat import ChatLLM, build_light_fallback_llm, build_standard_llm
from sats.llm.provider import _sync_provider_env, build_llm, extract_json_object
import sats.llm.provider as provider_mod


class LLMProviderEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        provider_mod._dotenv_loaded = True

    def _run_sync(self, env: dict[str, str], *, profile: str = "default") -> dict[str, str]:
        with patch.dict(os.environ, env, clear=True):
            provider = _sync_provider_env(profile=profile)
            return {
                "provider": provider,
                "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
                "OPENAI_API_BASE": os.environ.get("OPENAI_API_BASE", ""),
                "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", ""),
            }

    def test_deepseek_provider_maps_to_openai_compatible_env(self) -> None:
        result = self._run_sync(
            {
                "DEEPSEEK_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "ds-key",
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
                "DEEPSEEK_MODEL": "deepseek-chat",
                "DEFAULT_MODEL": "DEEPSEEK",
            }
        )

        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["OPENAI_API_KEY"], "ds-key")
        self.assertEqual(result["OPENAI_API_BASE"], "https://api.deepseek.com/v1")

    def test_openrouter_provider_maps_key_and_base_url(self) -> None:
        result = self._run_sync(
            {
                "OPENROUTER_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "or-key",
                "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
                "OPENROUTER_MODEL": "deepseek/deepseek-v3.2",
                "DEFAULT_MODEL": "OPENROUTER",
            }
        )

        self.assertEqual(result["OPENAI_API_KEY"], "or-key")
        self.assertIn("openrouter.ai", result["OPENAI_BASE_URL"])

    def test_qwen_alias_uses_dashscope_credentials(self) -> None:
        result = self._run_sync(
            {
                "QWEN_PROVIDER": "qwen",
                "QWEN_API_KEY": "qwen-key",
                "QWEN_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "QWEN_MODEL": "qwen-plus",
                "DEFAULT_MODEL": "QWEN",
            }
        )

        self.assertEqual(result["provider"], "qwen")
        self.assertEqual(result["OPENAI_API_KEY"], "qwen-key")

    def test_ollama_does_not_require_provider_api_key(self) -> None:
        result = self._run_sync(
            {
                "OLLAMA_PROVIDER": "ollama",
                "OLLAMA_BASE_URL": "http://localhost:11434/v1",
                "OLLAMA_MODEL": "qwen2.5:32b",
                "DEFAULT_MODEL": "OLLAMA",
            }
        )

        self.assertEqual(result["OPENAI_API_KEY"], "ollama")
        self.assertIn("localhost", result["OPENAI_API_BASE"])

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported LLM provider"):
            self._run_sync(
                {
                    "CUSTOM_PROVIDER": "unknown",
                    "CUSTOM_MODEL": "custom-model",
                    "DEFAULT_MODEL": "CUSTOM",
                }
            )

    def test_provider_key_does_not_fallback_to_legacy_openai_key(self) -> None:
        result = self._run_sync(
            {
                "DEEPSEEK_PROVIDER": "deepseek",
                "DEEPSEEK_MODEL": "deepseek-chat",
                "DEFAULT_MODEL": "DEEPSEEK",
                "OPENAI_API_KEY": "shared-key",
            }
        )

        self.assertEqual(result["OPENAI_API_KEY"], "")

    def test_light_provider_uses_light_profile_env(self) -> None:
        result = self._run_sync(
            {
                "OPENAI_PROVIDER": "openai",
                "OPENAI_API_KEY": "main-key",
                "OPENAI_MODEL": "gpt-4o-mini",
                "DEEPSEEK_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "light-key",
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
                "DEEPSEEK_MODEL": "deepseek-chat",
                "DEFAULT_MODEL": "OPENAI",
                "DEFAULT_LIGHT_MODEL": "DEEPSEEK",
            },
            profile="light",
        )

        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["OPENAI_API_KEY"], "light-key")
        self.assertEqual(result["OPENAI_API_BASE"], "https://api.deepseek.com/v1")


class LLMFactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        provider_mod._dotenv_loaded = True

    def _capture_build(
        self,
        env: dict[str, str],
        *,
        timeout_seconds: int | None = None,
        profile: str = "default",
        max_retries: int | None = None,
    ) -> dict:
        captured: dict = {}

        class FakeChatOpenAI:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

        with patch.dict(os.environ, env, clear=True):
            with patch.object(provider_mod, "ChatOpenAIWithReasoning", FakeChatOpenAI):
                build_llm(timeout_seconds=timeout_seconds, profile=profile, max_retries=max_retries)
        return captured

    def test_build_llm_forwards_model_generation_settings(self) -> None:
        captured = self._capture_build(
            {
                "OPENROUTER_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "or-key",
                "OPENROUTER_MODEL": "moonshotai/kimi-k2-thinking",
                "DEFAULT_MODEL": "OPENROUTER",
                "LLM_TEMPERATURE": "0.2",
                "LLM_TIMEOUT_SECONDS": "45",
                "LLM_MAX_RETRIES": "3",
                "LLM_REASONING_EFFORT": "HIGH",
            }
        )

        self.assertEqual(captured["model"], "moonshotai/kimi-k2-thinking")
        self.assertEqual(captured["temperature"], 0.2)
        self.assertEqual(captured["timeout"], 45)
        self.assertEqual(captured["max_retries"], 3)
        self.assertEqual(captured["extra_body"], {"reasoning": {"effort": "high"}})

    def test_build_llm_accepts_stage_retry_override(self) -> None:
        captured = self._capture_build(
            {
                "OPENAI_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-test",
                "OPENAI_MODEL": "gpt-4o-mini",
                "DEFAULT_MODEL": "OPENAI",
                "LLM_MAX_RETRIES": "3",
            },
            max_retries=0,
        )

        self.assertEqual(captured["max_retries"], 0)

    def test_build_llm_rejects_legacy_only_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "旧 LLM 配置已移除"):
            self._capture_build({"LLM_PROVIDER": "openai", "OPENAI_MODEL": "gpt-4o-mini"})

    def test_minimax_temperature_zero_is_clamped(self) -> None:
        captured = self._capture_build(
            {
                "MINIMAX_PROVIDER": "minimax",
                "MINIMAX_API_KEY": "minimax-key",
                "MINIMAX_MODEL": "MiniMax-M2.7",
                "DEFAULT_MODEL": "MINIMAX",
                "LLM_TEMPERATURE": "0.0",
            }
        )

        self.assertEqual(captured["temperature"], 0.01)

    def test_build_llm_accepts_timeout_override(self) -> None:
        captured = self._capture_build(
            {
                "OPENAI_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-test",
                "OPENAI_MODEL": "gpt-4o-mini",
                "DEFAULT_MODEL": "OPENAI",
                "LLM_TIMEOUT_SECONDS": "120",
            },
            timeout_seconds=5,
        )

        self.assertEqual(captured["timeout"], 5)

    def test_build_llm_uses_light_profile_model(self) -> None:
        captured = self._capture_build(
            {
                "OPENAI_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-main",
                "OPENAI_MODEL": "gpt-4o-mini",
                "DEEPSEEK_PROVIDER": "deepseek",
                "DEEPSEEK_MODEL": "deepseek-main",
                "DEEPSEEK_LIGHT_MODEL": "deepseek-chat",
                "DEEPSEEK_API_KEY": "ds-key",
                "DEFAULT_MODEL": "OPENAI",
                "DEFAULT_LIGHT_MODEL": "DEEPSEEK",
            },
            profile="light",
        )

        self.assertEqual(captured["model"], "deepseek-chat")

    def test_build_llm_light_profile_falls_back_to_main_model(self) -> None:
        captured = self._capture_build(
            {
                "MOONSHOT_PROVIDER": "moonshot",
                "MOONSHOT_MODEL": "kimi-k2.5",
                "MOONSHOT_API_KEY": "moonshot-key",
                "DEFAULT_MODEL": "MOONSHOT",
            },
            profile="light",
        )

        self.assertEqual(captured["model"], "kimi-k2.5")

    def test_chat_llm_uses_timeout_specific_client(self) -> None:
        calls: list[int | None] = []

        class FakeLLM:
            def __init__(self, timeout: int | None) -> None:
                self.timeout = timeout

            def invoke(self, messages, config=None):
                return SimpleNamespace(content=f"timeout={self.timeout}", response_metadata={})

        def fake_build(*, model_name=None, callbacks=None, timeout_seconds=None, profile="default"):
            calls.append(timeout_seconds)
            return FakeLLM(timeout_seconds)

        with patch("sats.llm.chat.build_llm", side_effect=fake_build):
            llm = ChatLLM(timeout_seconds=20)
            response = llm.chat([{"role": "user", "content": "hi"}], timeout=5)

        self.assertEqual(calls, [20, 5])
        self.assertEqual(response.content, "timeout=5")

    def test_strict_stream_chat_propagates_without_hidden_chat_retry(self) -> None:
        calls = {"stream": 0, "invoke": 0}

        class FakeLLM:
            def stream(self, messages, config=None):
                calls["stream"] += 1
                raise TimeoutError("stream timeout")

            def invoke(self, messages, config=None):
                calls["invoke"] += 1
                return SimpleNamespace(content="non-stream fallback", response_metadata={})

        with patch("sats.llm.chat.build_llm", return_value=FakeLLM()):
            llm = ChatLLM(timeout_seconds=20, max_retries=0)
            with self.assertRaisesRegex(TimeoutError, "stream timeout"):
                llm.strict_stream_chat([{"role": "user", "content": "hi"}], timeout=5)

        self.assertEqual(calls, {"stream": 1, "invoke": 0})

    def test_light_fallback_llm_uses_default_after_light_error(self) -> None:
        factory_calls = []
        chat_calls = []

        class FakeLLM:
            def __init__(self, *, model_name=None, profile="default", timeout_seconds=None) -> None:
                self.model_name = model_name
                self.profile = profile
                factory_calls.append({"model_name": model_name, "profile": profile, "timeout_seconds": timeout_seconds})

            def chat(self, messages, tools=None, timeout=None):
                chat_calls.append({"model_name": self.model_name, "profile": self.profile, "timeout": timeout})
                if self.profile == "light":
                    raise TimeoutError("light timeout")
                return SimpleNamespace(content=f"default:{self.model_name}", response_metadata={})

        llm = build_light_fallback_llm(
            FakeLLM,
            light_model_name="light-model",
            default_model_name="main-model",
            timeout_seconds=6,
        )
        response = llm.chat([{"role": "user", "content": "hi"}], timeout=3)

        self.assertEqual(response.content, "default:main-model")
        self.assertEqual([call["profile"] for call in factory_calls], ["light", "default"])
        self.assertEqual([call["profile"] for call in chat_calls], ["light", "default"])
        self.assertEqual(chat_calls[-1]["timeout"], 3)
        self.assertEqual(llm.last_profile, "default")
        self.assertEqual(llm.last_model_name, "main-model")

    def test_light_fallback_llm_does_not_build_default_when_light_succeeds(self) -> None:
        factory_calls = []

        class FakeLLM:
            def __init__(self, *, model_name=None, profile="default", timeout_seconds=None) -> None:
                self.profile = profile
                factory_calls.append(profile)

            def chat(self, messages, tools=None, timeout=None):
                return SimpleNamespace(content=self.profile, response_metadata={})

        llm = build_light_fallback_llm(FakeLLM, light_model_name="light-model", default_model_name="main-model")
        response = llm.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.content, "light")
        self.assertEqual(factory_calls, ["light"])
        self.assertEqual(llm.last_profile, "light")
        self.assertEqual(llm.last_model_name, "light-model")

    def test_build_standard_llm_uses_default_profile(self) -> None:
        factory_calls = []

        class FakeLLM:
            def __init__(self, *, model_name=None, profile="default", timeout_seconds=None) -> None:
                factory_calls.append({"model_name": model_name, "profile": profile, "timeout_seconds": timeout_seconds})

        build_standard_llm(FakeLLM, model_name="main-model", timeout_seconds=9)

        self.assertEqual(factory_calls, [{"model_name": "main-model", "profile": "default", "timeout_seconds": 9}])

    def test_build_standard_llm_forwards_stage_retry_override(self) -> None:
        factory_calls = []

        class FakeLLM:
            def __init__(self, *, model_name=None, profile="default", timeout_seconds=None, max_retries=None) -> None:
                factory_calls.append(
                    {
                        "model_name": model_name,
                        "profile": profile,
                        "timeout_seconds": timeout_seconds,
                        "max_retries": max_retries,
                    }
                )

        build_standard_llm(FakeLLM, model_name="main-model", timeout_seconds=110, max_retries=0)

        self.assertEqual(
            factory_calls,
            [{"model_name": "main-model", "profile": "default", "timeout_seconds": 110, "max_retries": 0}],
        )

    def test_light_fallback_llm_reraises_default_error(self) -> None:
        class FakeLLM:
            def __init__(self, *, model_name=None, profile="default", timeout_seconds=None) -> None:
                self.profile = profile

            def chat(self, messages, tools=None, timeout=None):
                raise RuntimeError(f"{self.profile} failed")

        llm = build_light_fallback_llm(FakeLLM, light_model_name="light-model", default_model_name="main-model")

        with self.assertRaisesRegex(RuntimeError, "default failed"):
            llm.chat([{"role": "user", "content": "hi"}])


class LLMResponseParsingTest(unittest.TestCase):
    def test_parse_response_keeps_tool_calls_reasoning_and_finish_reason(self) -> None:
        message = SimpleNamespace(
            content="",
            tool_calls=[{"id": "call-1", "name": "lookup", "args": {"ts_code": "000001.SZ"}}],
            additional_kwargs={"reasoning_content": "thinking"},
            response_metadata={"finish_reason": "tool_callstool_calls"},
        )

        response = ChatLLM._parse_response(message)

        self.assertEqual(response.reasoning_content, "thinking")
        self.assertEqual(response.finish_reason, "tool_calls")
        self.assertTrue(response.has_tool_calls)
        self.assertEqual(response.tool_calls[0].arguments["ts_code"], "000001.SZ")


class ReasoningPayloadTest(unittest.TestCase):
    def _llm(self):
        if provider_mod.ChatOpenAIWithReasoning is None:
            self.skipTest("langchain-openai is not installed")
        return provider_mod.ChatOpenAIWithReasoning(
            model="gpt-test",
            api_key="sk-test",
            base_url="https://example.com/v1",
        )

    def test_request_payload_keeps_additional_kwargs_reasoning(self) -> None:
        payload = self._llm()._get_request_payload(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "additional_kwargs": {"reasoning_content": "thinking"},
                }
            ]
        )

        self.assertEqual(payload["messages"][0]["reasoning_content"], "thinking")

    def test_request_payload_keeps_top_level_reasoning(self) -> None:
        payload = self._llm()._get_request_payload(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "thinking",
                }
            ]
        )

        self.assertEqual(payload["messages"][0]["reasoning_content"], "thinking")

    def test_request_payload_does_not_add_empty_reasoning(self) -> None:
        payload = self._llm()._get_request_payload([{"role": "assistant", "content": "hello"}])

        self.assertNotIn("reasoning_content", payload["messages"][0])


class ExtractJsonObjectTest(unittest.TestCase):
    def test_extracts_plain_json_object(self) -> None:
        self.assertEqual(extract_json_object('{"score": 88}'), {"score": 88})

    def test_extracts_embedded_json_object(self) -> None:
        self.assertEqual(extract_json_object('结果如下：{"rating": "Buy"}。'), {"rating": "Buy"})

    def test_extracts_nested_json_object(self) -> None:
        result = extract_json_object('{"outer": {"inner": [1, 2, 3]}}')
        self.assertEqual(result, {"outer": {"inner": [1, 2, 3]}})

    def test_ignores_braces_inside_strings(self) -> None:
        result = extract_json_object('{"note": "if (x) { keep }"}')
        self.assertEqual(result, {"note": "if (x) { keep }"})

    def test_returns_none_when_no_json_exists(self) -> None:
        self.assertIsNone(extract_json_object("no json here"))


if __name__ == "__main__":
    unittest.main()
