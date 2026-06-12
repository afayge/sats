from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sats.agent import AgentExecutionPolicy, AgentObservation, AgentPlan
from sats.agent.planner import build_agent_plan
from sats.agent.synthesis import synthesize_agent_result
from sats.agent.tools import build_default_tool_registry
from sats.chat import ChatSession
from sats.chat_components import (
    CHAT_ROUTE_GENERAL,
    CHAT_ROUTE_MARKET,
    CHAT_ROUTE_QUOTE,
    CHAT_ROUTE_STOCK,
    ChatEvidenceBundle,
    ChatRequestRoute,
    synthesize_chat_response,
)


class RecordingLLM:
    instances: list["RecordingLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        RecordingLLM.instances.append(self)

    def chat(self, messages, tools=None, timeout=None):
        prompt = "\n".join(str(item.get("content") or "") for item in messages)
        if "steps 每项字段" in prompt:
            return SimpleNamespace(
                content=(
                    '{"objective":"ok","steps":['
                    '{"step_id":"chat","kind":"tool","title":"answer","tool_name":"chat.answer","arguments":{"message":"ok"}},'
                    '{"step_id":"final","kind":"final","title":"summary"}]}'
                )
            )
        return SimpleNamespace(content="模型回答")


def _settings(tmp: str | None = None) -> SimpleNamespace:
    root = Path(tmp or ".")
    return SimpleNamespace(
        project_root=root,
        db_path=root / "sats.duckdb",
        openai_model="main-model",
        light_model_name="light-model",
        llm_timeout_seconds=33,
    )


class ModelRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        RecordingLLM.instances = []

    def test_plain_chat_synthesis_uses_light_model(self) -> None:
        route = ChatRequestRoute(route_kind=CHAT_ROUTE_GENERAL, intent="general_qa")
        result = synthesize_chat_response(
            "解释均线金叉",
            route=route,
            evidence=ChatEvidenceBundle(route=route),
            settings=_settings(),
            llm_factory=RecordingLLM,
        )

        self.assertEqual(result.model_policy, "light")
        self.assertEqual(result.model_profile, "light")
        self.assertEqual(result.model_name, "light-model")
        self.assertEqual(RecordingLLM.instances[0].kwargs["profile"], "light")

    def test_analysis_chat_synthesis_uses_standard_model(self) -> None:
        for route_kind in (CHAT_ROUTE_STOCK, CHAT_ROUTE_MARKET):
            RecordingLLM.instances = []
            route = ChatRequestRoute(route_kind=route_kind, intent="analysis")
            result = synthesize_chat_response(
                "分析 002436",
                route=route,
                evidence=ChatEvidenceBundle(route=route),
                settings=_settings(),
                llm_factory=RecordingLLM,
            )

            self.assertEqual(result.model_policy, "standard")
            self.assertEqual(result.model_profile, "default")
            self.assertEqual(result.model_name, "main-model")
            self.assertEqual(RecordingLLM.instances[0].kwargs["profile"], "default")
            self.assertEqual(RecordingLLM.instances[0].kwargs["model_name"], "main-model")

    def test_quote_only_synthesis_uses_light_model(self) -> None:
        route = ChatRequestRoute(route_kind=CHAT_ROUTE_QUOTE, intent="stock_quote")
        result = synthesize_chat_response(
            "看 002436 报价",
            route=route,
            evidence=ChatEvidenceBundle(route=route),
            settings=_settings(),
            llm_factory=RecordingLLM,
        )

        self.assertEqual(result.model_policy, "light")
        self.assertEqual(RecordingLLM.instances[0].kwargs["profile"], "light")

    def test_agent_planner_uses_light_for_chat_only_and_standard_for_analysis(self) -> None:
        registry = build_default_tool_registry()
        settings = _settings()

        build_agent_plan(
            "解释均线金叉",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=RecordingLLM,
            tool_registry=registry,
        )
        self.assertEqual(RecordingLLM.instances[-1].kwargs["profile"], "light")

        build_agent_plan(
            "分析 002436 下周走势",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=RecordingLLM,
            tool_registry=registry,
        )
        self.assertEqual(RecordingLLM.instances[-1].kwargs["profile"], "default")

    def test_agent_synthesis_uses_standard_model_and_skips_chat_answer_only(self) -> None:
        observations = (
            AgentObservation(
                step_id="market",
                kind="tool",
                status="done",
                payload={"tool_name": "research.market_context", "result": {"payload": {"market_context": {}}}},
            ),
        )
        result = synthesize_agent_result(
            message="分析大盘",
            plan=AgentPlan(objective="分析大盘"),
            observations=observations,
            skills=(),
            settings=_settings(),
            llm_factory=RecordingLLM,
        )
        self.assertTrue(result.used_llm)
        self.assertEqual(result.model_policy, "standard")
        self.assertEqual(RecordingLLM.instances[-1].kwargs["profile"], "default")

        class FailingLLM:
            def __init__(self, *args, **kwargs) -> None:
                raise AssertionError("chat.answer-only should not build synthesis LLM")

        chat_only = (
            AgentObservation(
                step_id="chat",
                kind="tool",
                status="done",
                content="普通回答",
                payload={"tool_name": "chat.answer"},
            ),
        )
        skipped = synthesize_agent_result(
            message="解释均线",
            plan=AgentPlan(objective="解释均线"),
            observations=chat_only,
            skills=(),
            settings=_settings(),
            llm_factory=FailingLLM,
        )
        self.assertFalse(skipped.used_llm)
        self.assertEqual(skipped.content, "普通回答")

    def test_runtime_tool_loop_uses_standard_model_and_emits_trace_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=RecordingLLM,
                memory_enabled=False,
                preprocess_enabled=False,
            )

            result = session.ask("生成一份市场研究报告并保存", event_sink=events.append)

        self.assertIn("报告:", result.content)
        self.assertEqual(RecordingLLM.instances[0].kwargs["profile"], "default")
        llm_events = [event for event in events if event.event_type == "llm_completed"]
        self.assertTrue(llm_events)
        self.assertEqual(llm_events[0].payload["model_policy"], "standard")
        turn_events = [event for event in events if event.event_type == "turn_completed"]
        self.assertEqual(turn_events[-1].payload["model_policy"], "standard")


if __name__ == "__main__":
    unittest.main()
