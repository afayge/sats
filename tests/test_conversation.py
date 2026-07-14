from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.agent.models import AgentExecutionPolicy, AgentObservation, AgentPlan, AgentStep
from sats.agent.tools import AgentToolContext, AgentToolResult
from sats.agent.tools.research_tools import research_tool_specs
from sats.conversation.runtime import (
    _chan_structured_error_fallback,
    _compact_observations,
    _conversation_action_messages,
    confirm_pending_conversation_action,
    continue_conversation_after_clarification,
    format_plan_mode_result,
    reject_pending_conversation_action,
    run_conversation_once,
)
from sats.conversation.threads import archive_thread, create_thread, fork_thread, list_threads, pin_thread, rename_thread
from sats.memory import ChatMemoryStore


MARKET_DIMENSIONS = ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors", "fund_flow", "catalysts"]


class ConversationRuntimeTest(unittest.TestCase):
    def test_conversation_action_prompt_documents_python_program_observation_access(self) -> None:
        observation = AgentObservation(
            step_id="loop_1_research_market_context",
            kind="tool",
            status="done",
            content="{}",
            payload={
                "tool_name": "research.market_context",
                "result": {
                    "status": "done",
                    "content": "{}",
                    "payload": {"market_context": {"hot_sectors": [{"name": "AI算力"}]}},
                },
            },
        )

        with patch("sats.conversation.runtime.agent_today", return_value="20260701"):
            messages = _conversation_action_messages(
                message="从市场上下文提取热点板块",
                plan=AgentPlan(objective="从市场上下文提取热点板块"),
                registry=_FakeRegistry(),
                policy=AgentExecutionPolicy(),
                observations=(observation,),
                reference_context=None,
            )
        compact = _compact_observations((observation,))

        self.assertIn("允许导入已安装模块", messages[0]["content"])
        self.assertIn("禁止文件、进程、网络和动态执行", messages[0]["content"])
        self.assertIn("def run(context)", messages[0]["content"])
        self.assertIn("不要在顶层直接访问 context", messages[0]["content"])
        self.assertIn("observations_by_step", messages[0]["content"])
        self.assertIn("payload']['result']['payload", messages[0]["content"])
        self.assertIn("昨天/yesterday 必须写成 20260630", messages[0]["content"])
        self.assertIn("今天/today/current 必须写成 20260701", messages[0]["content"])
        self.assertIn("result_payload_keys", messages[1]["content"])
        self.assertIn("market_context", compact[0]["result_payload_keys"])
        self.assertEqual(compact[0]["result_status"], "done")
        self.assertIn("支持哪些 skills", messages[0]["content"])
        self.assertIn("catalog.capabilities", messages[0]["content"])

    def test_conversation_loop_executes_readonly_tool_and_final_answer(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"chat.answer","arguments":{"message":"hello"}}')
                return SimpleNamespace(content='{"action":"final_answer","content":"loop 综合回答"}')

        settings = _settings(self)
        registry = _FakeRegistry(side_effect="readonly")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("loop final should not synthesize")),
        ):
            result = run_conversation_once("hello", settings=settings, llm_factory=LoopLLM)

        self.assertEqual(result.content, "loop 综合回答")
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(registry.executed, [("chat.answer", {"message": "hello"})])
        self.assertEqual([item.kind for item in result.observations], ["tool", "final"])

    def test_conversation_recovers_safe_content_only_json_as_final_answer(self) -> None:
        class ContentOnlyLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"content":"OK"}')

        settings = _settings(self)
        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=_FakeRegistry()),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("simple final should not synthesize")),
        ):
            result = run_conversation_once("用一句话回答：OK", settings=settings, llm_factory=ContentOnlyLLM)

        trace = ChatMemoryStore(settings.db_path).get_chat_turn_trace(result.turn_id or "")
        self.assertEqual(result.content, "OK")
        self.assertTrue(any(item.step_id.endswith("_action_recovered") for item in result.observations))
        self.assertEqual(trace["turn"]["status"], "done")
        self.assertIn("action_recovered", [event["event_type"] for event in trace["events"]])

    def test_conversation_market_content_only_json_uses_evidence_synthesis(self) -> None:
        class ContentOnlyMarketLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                ContentOnlyMarketLLM.calls += 1
                if ContentOnlyMarketLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.market_context","arguments":{}}')
                return SimpleNamespace(content='{"content":"模型已生成的市场回答"}')

        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(
            content="基于真实市场 observation 的最终综合",
            skill_names=(),
            model_policy="standard",
            model_profile="default",
            model_name="m",
        )
        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260710"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis) as synthesize,
        ):
            ContentOnlyMarketLLM.calls = 0
            result = run_conversation_once("分析今天的A股走势，预测下走走势", settings=settings, llm_factory=ContentOnlyMarketLLM)

        self.assertEqual(result.content, "基于真实市场 observation 的最终综合")
        self.assertEqual([name for name, _args in registry.executed], ["research.market_context"])
        self.assertNotIn("conversation action must be one of", result.content)
        self.assertTrue(any(item.step_id.endswith("_action_recovered") for item in result.observations))
        synthesize.assert_called_once()

    def test_conversation_normalizes_short_term_typo_and_reuses_market_context(self) -> None:
        class DuplicateMarketLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                DuplicateMarketLLM.calls += 1
                horizons = '["today","tomorrow","day_after_tomorrow"]' if DuplicateMarketLLM.calls == 1 else '["next_week"]'
                return SimpleNamespace(
                    content=(
                        '{"action":"call_tool","tool_name":"research.market_context","arguments":'
                        f'{{"trade_date":"20260710","horizons":{horizons}}}}}'
                    )
                )

        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(
            content="短线市场综合",
            skill_names=(),
            model_policy="standard",
            model_profile="default",
            model_name="m",
        )
        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260710"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            DuplicateMarketLLM.calls = 0
            result = run_conversation_once("分析今天的A股走势，预测下走走势", settings=settings, llm_factory=DuplicateMarketLLM)

        self.assertEqual(result.content, "短线市场综合")
        self.assertEqual(len(registry.executed), 1)
        self.assertEqual(registry.executed[0][1]["horizons"], ["today", "tomorrow", "day_after_tomorrow"])
        self.assertTrue(any(item.step_id.endswith("_duplicate_tool_result") for item in result.observations))

    def test_conversation_preseeds_capability_catalog_for_skill_inventory(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                return SimpleNamespace(content='{"action":"final_answer","content":"支持的 skills 包括："}')

        settings = _settings(self)
        registry = _CapabilityRegistry()
        synthesis = SimpleNamespace(
            content="# SATS 支持的 Skills 与 Agent 能力总览\n\n## 能力总览\n\n- 已综合能力目录。",
            skill_names=(),
            used_llm=True,
            phase="synthesis",
            model_policy="standard",
            model_profile="default",
            model_name="m",
            prompt_chars=100,
            prompt_budget_chars=0,
            compact_mode="full",
            retry_count=0,
            base_url_class="unknown",
        )

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis) as synthesize,
        ):
            result = run_conversation_once("列出支持的 skills", settings=settings, llm_factory=LoopLLM)

        self.assertIn("能力总览", result.content)
        self.assertNotEqual(result.content, "支持的 skills 包括：")
        self.assertEqual(
            registry.executed,
            [
                ("catalog.capabilities", {"section": "summary", "limit": 50}),
                ("catalog.capabilities", {"section": "skills", "limit": 12}),
            ],
        )
        self.assertIn("SATS capabilities", result.data_names)
        self.assertIn("Skills", result.data_names)
        self.assertEqual([item.step_id for item in result.observations[:2]], ["capability_catalog_summary", "capability_catalog_skills"])
        synthesize.assert_called_once()

    def test_conversation_loop_executes_write_db_tool_without_confirmation(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"data.stock_daily","arguments":{"symbols":["002436"]}}')
                return SimpleNamespace(content='{"action":"final_answer","content":"日线已刷新"}')

        settings = _settings(self)
        registry = _FakeRegistry(side_effect="write_db")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once("刷新日线", settings=settings, llm_factory=LoopLLM)

        self.assertFalse(result.requires_confirmation)
        self.assertIsNone(result.pending_action_id)
        self.assertEqual(registry.executed, [("data.stock_daily", {"symbols": ["002436"]})])

    def test_conversation_loop_requires_confirmation_for_live_trade_tool(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"trade.submit_intent","arguments":{"symbols":["002436"]}}')

        settings = _settings(self)
        registry = _FakeRegistry(side_effect="live_trade")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
        ):
            result = run_conversation_once("买入 002436", settings=settings, llm_factory=LoopLLM)

        self.assertTrue(result.requires_confirmation)
        self.assertTrue(result.pending_action_id)
        self.assertEqual(registry.executed, [])
        action = ChatMemoryStore(settings.db_path).get_pending_action(result.pending_action_id or "")
        self.assertEqual(action["action_type"], "conversation_tool")
        self.assertEqual(action["payload"]["tool_name"], "trade.submit_intent")

    def test_conversation_loop_requires_confirmation_for_trade_permission_tool(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"trade.submit_intent","arguments":{"symbols":["002436"]}}')

        settings = _settings(self)
        registry = _FakeRegistry(requires_trade_permission=True)

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
        ):
            result = run_conversation_once("买入 002436", settings=settings, llm_factory=LoopLLM)

        self.assertTrue(result.requires_confirmation)
        self.assertTrue(result.pending_action_id)
        self.assertEqual(registry.executed, [])
        action = ChatMemoryStore(settings.db_path).get_pending_action(result.pending_action_id or "")
        self.assertEqual(action["action_type"], "conversation_tool")
        self.assertEqual(action["payload"]["tool_name"], "trade.submit_intent")

    def test_conversation_loop_request_confirmation_still_validates_arguments(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"request_confirmation","tool_name":"web.search","arguments":{}}')

        settings = _settings(self)
        registry = _FakeRegistry(input_schemas={"web.search": {"type": "object", "required": ["query"]}})

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
        ):
            result = run_conversation_once("搜索", settings=settings, llm_factory=LoopLLM)

        self.assertTrue(result.requires_clarification)
        self.assertFalse(result.requires_confirmation)
        self.assertIn("query", result.content)
        self.assertEqual(registry.executed, [])

    def test_conversation_loop_tool_error_can_continue_to_next_action(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"bad.tool","arguments":{}}')
                if LoopLLM.calls == 2:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"chat.answer","arguments":{"message":"recover"}}')
                return SimpleNamespace(content='{"action":"final_answer","content":"已恢复"}')

        settings = _settings(self)
        registry = _StatusRegistry({"bad.tool": "error", "chat.answer": "done"})

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
        ):
            result = run_conversation_once("recover", settings=settings, llm_factory=LoopLLM)

        self.assertEqual(result.content, "已恢复")
        self.assertEqual([name for name, _args in registry.executed], ["bad.tool", "chat.answer"])
        self.assertEqual([item.status for item in result.observations if item.kind == "tool"], ["error", "done"])

    def test_conversation_loop_repeated_invalid_json_stops(self) -> None:
        class BadLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content="not json")

        settings = _settings(self)

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=_FakeRegistry()),
        ):
            result = run_conversation_once("bad", settings=settings, llm_factory=BadLLM)

        self.assertIn("conversation action must be a JSON object", result.content)
        invalid = [item for item in result.observations if item.step_id.endswith("invalid_action")]
        self.assertEqual(len(invalid), 2)
        trace = ChatMemoryStore(settings.db_path).get_chat_turn_trace(result.turn_id or "")
        self.assertEqual(trace["turn"]["status"], "error")

    def test_conversation_repeated_invalid_action_with_evidence_uses_synthesis(self) -> None:
        class InvalidAfterEvidenceLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                InvalidAfterEvidenceLLM.calls += 1
                if InvalidAfterEvidenceLLM.calls == 1:
                    return SimpleNamespace(
                        content=(
                            '{"action":"call_tool","tool_name":"research.market_context",'
                            '"arguments":{"trade_date":"20260710","dimensions":["core_indices"]}}'
                        )
                    )
                return SimpleNamespace(content="not json")

        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(
            content="已用成功工具证据生成回答",
            skill_names=(),
            model_policy="standard",
            model_profile="default",
            model_name="m",
        )
        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            InvalidAfterEvidenceLLM.calls = 0
            result = run_conversation_once("基于市场数据给出结论", settings=settings, llm_factory=InvalidAfterEvidenceLLM)

        trace = ChatMemoryStore(settings.db_path).get_chat_turn_trace(result.turn_id or "")
        self.assertEqual(result.content, "已用成功工具证据生成回答")
        self.assertEqual(trace["turn"]["status"], "done")
        self.assertIn("action_recovered", [event["event_type"] for event in trace["events"]])

    def test_conversation_does_not_infer_missing_action_for_tool_request(self) -> None:
        class MissingToolActionLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(
                    content='{"tool_name":"trade.submit_intent","arguments":{"symbols":["002436.SZ"]},"content":"执行买入"}'
                )

        settings = _settings(self)
        registry = _FakeRegistry(side_effect="live_trade")
        with patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry):
            result = run_conversation_once("买入 002436", settings=settings, llm_factory=MissingToolActionLLM)

        trace = ChatMemoryStore(settings.db_path).get_chat_turn_trace(result.turn_id or "")
        self.assertEqual(registry.executed, [])
        self.assertIn("conversation action must be one of", result.content)
        self.assertEqual(trace["turn"]["status"], "error")

    def test_conversation_loop_argument_guard_requires_catalog_before_astock_fetch(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"data.astock_fetch","arguments":{"operation":"astock.stock_basic"}}')

        settings = _settings(self)
        registry = _FakeRegistry(input_schemas={"data.astock_fetch": {"type": "object", "required": ["operation"]}})

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
        ):
            result = run_conversation_once("取股票基础信息", settings=settings, llm_factory=LoopLLM)

        self.assertTrue(result.requires_clarification)
        self.assertIn("data.astock_catalog", result.content)
        self.assertEqual(registry.executed, [])

    def test_conversation_loop_blocks_market_forecast_final_until_market_context(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"final_answer","content":"短回答"}')
                if LoopLLM.calls == 2:
                    return SimpleNamespace(
                        content=(
                            '{"action":"call_tool","tool_name":"research.market_context",'
                            '"arguments":{"horizons":["next_week"],"dimensions":["core_indices","market_breadth"]}}'
                        )
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"短回答"}')

        settings = _settings(self)
        registry = _MarketRegistry()
        synthesis = SimpleNamespace(content="# 大盘分析\n\n## 下周情景\n\n- 以真实市场上下文做情景推演。", skill_names=(), model_policy="standard", model_profile="default", model_name="m")

        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260626"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis) as synthesize,
        ):
            result = run_conversation_once("分析今天的大盘走势，预测下周走势情况", settings=settings, llm_factory=LoopLLM)

        self.assertIn("下周情景", result.content)
        self.assertNotEqual(result.content, "短回答")
        self.assertEqual(
            registry.executed,
            [
                (
                    "research.market_context",
                    {"horizons": ["today", "next_week"], "dimensions": MARKET_DIMENSIONS, "trade_date": "20260626"},
                )
            ],
        )
        self.assertIn("loop_1_action_blocked", [item.step_id for item in result.observations])
        synthesize.assert_called_once()

    def test_conversation_emits_final_synthesis_progress_events(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content=(
                            '{"action":"call_tool","tool_name":"research.market_context",'
                            '"arguments":{"horizons":["today"],"dimensions":["core_indices","market_breadth"]}}'
                        )
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"大盘完成"}')

        settings = _settings(self)
        registry = _MarketRegistry()
        events = []
        synthesis = SimpleNamespace(
            content="最终综合",
            skill_names=("sats-market-assistant",),
            used_llm=True,
            phase="synthesis",
            model_policy="standard",
            model_profile="default",
            model_name="m",
            prompt_chars=123,
            prompt_budget_chars=456,
            compact_mode="gateway_compact",
            retry_count=0,
            base_url_class="openai_compatible_third_party",
        )

        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260630"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            result = run_conversation_once(
                "分析今天大盘",
                settings=settings,
                llm_factory=LoopLLM,
                event_sink=events.append,
            )

        synthesis_events = [
            event
            for event in events
            if event.item_type == "agent_synthesis" and event.item_name == "final_synthesis"
        ]
        self.assertEqual(result.content, "最终综合")
        self.assertEqual([event.event_type for event in synthesis_events], ["context_started", "context_completed"])
        self.assertEqual(synthesis_events[0].status, "running")
        self.assertEqual(synthesis_events[1].status, "done")
        self.assertEqual(synthesis_events[1].payload["used_llm"], True)
        self.assertEqual(synthesis_events[1].payload["prompt_chars"], 123)
        self.assertEqual(synthesis_events[1].payload["compact_mode"], "gateway_compact")
        self.assertIsNotNone(synthesis_events[1].duration_seconds)

    def test_conversation_records_final_synthesis_llm_error(self) -> None:
        class LoopThenFailSynthesisLLM:
            instances = 0

            def __init__(self, *args, **kwargs) -> None:
                LoopThenFailSynthesisLLM.instances += 1
                self.instance = LoopThenFailSynthesisLLM.instances
                self.calls = 0
                self.model_name = kwargs.get("model_name", "")
                self.profile = kwargs.get("profile", "")

            def chat(self, messages, timeout=None):
                self.calls += 1
                if self.instance == 1 and self.calls == 1:
                    return SimpleNamespace(
                        content=(
                            '{"action":"call_tool","tool_name":"research.market_context",'
                            '"arguments":{"horizons":["today"],"dimensions":["core_indices"]}}'
                        )
                    )
                if self.instance == 1:
                    return SimpleNamespace(content='{"action":"final_answer","content":"本地前置结论"}')
                raise RuntimeError("status_code=500, upstream error: do request failed")

        settings = _settings(self)
        registry = _FakeRegistry()
        events = []

        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260630"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
        ):
            result = run_conversation_once(
                "分析今天大盘",
                settings=settings,
                llm_factory=LoopThenFailSynthesisLLM,
                event_sink=events.append,
            )

        error_events = [
            event
            for event in events
            if event.event_type == "llm_completed"
            and event.item_type == "agent_synthesis"
            and event.item_name == "final_synthesis"
        ]
        self.assertEqual(result.model_policy, "standard")
        self.assertTrue(any(event.event_type == "turn_completed" for event in events))
        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0].status, "error")
        self.assertIn("do request failed", error_events[0].content)
        self.assertEqual(error_events[0].payload["error_type"], "RuntimeError")
        self.assertGreater(error_events[0].payload["prompt_chars"], 0)
        self.assertEqual(error_events[0].payload["compact_mode"], "ultra_compact")
        self.assertEqual(error_events[0].payload["retry_count"], 1)
        self.assertEqual(len(error_events[0].payload["attempt_errors"]), 2)
        self.assertEqual(error_events[0].payload["fallback"], "local_summary")

    def test_conversation_injects_market_relative_time_arguments(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.market_context","arguments":{}}')
                return SimpleNamespace(content='{"action":"final_answer","content":"大盘完成"}')

        cases = (
            ("评价今天的大盘走势，以及热点板块", {"trade_date": "20260626", "dimensions": MARKET_DIMENSIONS}),
            ("评价昨天的大盘走势", {"trade_date": "20260625", "dimensions": MARKET_DIMENSIONS}),
            ("预测明天大盘走势", {"horizons": ["tomorrow"], "dimensions": MARKET_DIMENSIONS}),
            ("预测未来几日大盘走势", {"horizons": ["tomorrow", "day_after_tomorrow"], "dimensions": MARKET_DIMENSIONS}),
            ("预测未来3天大盘走势", {"horizons": ["tomorrow", "day_after_tomorrow"], "dimensions": MARKET_DIMENSIONS}),
            ("分析今天大盘走势，预测明天走势", {"trade_date": "20260626", "horizons": ["today", "tomorrow"], "dimensions": MARKET_DIMENSIONS}),
            ("过去几天大盘和热点板块", {"trade_date": "20260626", "dimensions": MARKET_DIMENSIONS}),
            ("过去3天大盘和热点板块", {"trade_date": "20260626", "dimensions": MARKET_DIMENSIONS}),
            ("2026-06-25 不是昨天的大盘走势", {"trade_date": "20260625", "dimensions": MARKET_DIMENSIONS}),
            ("评价大盘走势和热点板块", {"horizons": ["today"], "dimensions": MARKET_DIMENSIONS}),
        )
        settings = _settings(self)
        synthesis = SimpleNamespace(content="大盘综合", skill_names=(), model_policy="standard", model_profile="default", model_name="m")

        for message, expected_arguments in cases:
            with self.subTest(message=message):
                registry = _MarketRegistry()
                with (
                    patch("sats.conversation.runtime.agent_today", return_value="20260626"),
                    patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
                    patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
                ):
                    LoopLLM.calls = 0
                    result = run_conversation_once(message, settings=settings, llm_factory=LoopLLM)

                self.assertFalse(result.requires_clarification)
                self.assertEqual(registry.executed, [("research.market_context", expected_arguments)])
                tool_observation = next(item for item in result.observations if item.kind == "tool")
                self.assertEqual(tool_observation.payload["arguments"], expected_arguments)

    def test_conversation_normalizes_model_relative_trade_date_literal(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content=(
                            '{"action":"call_tool","tool_name":"research.market_context",'
                            '"arguments":{"trade_date":"yesterday","horizons":["today"],"dimensions":["core_indices"]}}'
                        )
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"大盘完成"}')

        settings = _settings(self)
        registry = _MarketRegistry()
        synthesis = SimpleNamespace(content="大盘综合", skill_names=(), model_policy="standard", model_profile="default", model_name="m")

        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260701"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once("评价A股昨天走势，预测今天走势", settings=settings, llm_factory=LoopLLM)

        expected_arguments = {"trade_date": "20260630", "horizons": ["today"], "dimensions": MARKET_DIMENSIONS}
        self.assertFalse(result.requires_clarification)
        self.assertEqual(registry.executed, [("research.market_context", expected_arguments)])
        tool_observation = next(item for item in result.observations if item.kind == "tool")
        self.assertEqual(tool_observation.payload["arguments"], expected_arguments)

    def test_conversation_injects_research_forecast_as_of_arguments(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.stock_context","arguments":{"symbols":["002436"]}}')
                return SimpleNamespace(content='{"action":"final_answer","content":"个股完成"}')

        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(content="个股综合", skill_names=(), model_policy="standard", model_profile="default", model_name="m")

        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260626"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once("分析 002436 明天走势", settings=settings, llm_factory=LoopLLM)

        self.assertFalse(result.requires_clarification)
        self.assertEqual(
            registry.executed,
            [("research.stock_context", {"symbols": ["002436"], "trade_date": "20260626", "horizons": ["tomorrow"]})],
        )

    def test_conversation_normalizes_today_sector_ranking_to_single_day(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content=(
                            '{"action":"call_tool","tool_name":"research.sector_return_ranking",'
                            '"arguments":{"query":"今天 最大跌幅板块","period":"1y"}}'
                        )
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"板块完成"}')

        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(content="板块综合", skill_names=(), model_policy="standard", model_profile="default", model_name="m")

        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260702"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once("今天 最大跌幅板块", settings=settings, llm_factory=LoopLLM)

        self.assertFalse(result.requires_clarification)
        self.assertEqual(
            registry.executed,
            [("research.sector_return_ranking", {"query": "今天 最大跌幅板块", "period": "1d", "trade_date": "20260702"})],
        )

    def test_conversation_normalizes_recent_trading_days_sector_ranking_and_skips_duplicate(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                return SimpleNamespace(
                    content=(
                        '{"action":"call_tool","tool_name":"research.sector_return_ranking",'
                        '"arguments":{"query":"近5个交易日的热点板块，给出20个列表",'
                        '"period":"近5个交易日","direction":"top","limit":20}}'
                    )
                )

        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(content="板块综合", skill_names=(), model_policy="standard", model_profile="default", model_name="m")

        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260708"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once("近5个交易日的热点板块，给出20个列表", settings=settings, llm_factory=LoopLLM)

        self.assertFalse(result.requires_clarification)
        self.assertEqual(
            registry.executed,
            [
                (
                    "research.sector_return_ranking",
                    {
                        "query": "近5个交易日的热点板块，给出20个列表",
                        "period": "5d",
                        "direction": "top",
                        "limit": 20,
                        "trade_date": "20260708",
                    },
                )
            ],
        )
        self.assertEqual(result.content, "板块综合")
        self.assertEqual(LoopLLM.calls, 2)

    def test_conversation_marks_max_iterations_without_data_as_error(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"chat.answer","arguments":{"message":"hello"}}')

        events = []
        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(content="本地兜底", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            result = run_conversation_once(
                "闲聊一下",
                settings=settings,
                policy=AgentExecutionPolicy(max_iterations=1),
                llm_factory=LoopLLM,
                event_sink=events.append,
            )

        self.assertEqual(result.content, "本地兜底")
        turn_completed = [event for event in events if event.event_type == "turn_completed"]
        self.assertEqual(turn_completed[-1].status, "error")

    def test_conversation_marks_max_iterations_with_data_as_partial_done(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(
                    content=(
                        '{"action":"call_tool","tool_name":"research.sector_return_ranking",'
                        '"arguments":{"query":"近5个交易日热点板块","period":"5d","direction":"top","limit":20}}'
                    )
                )

        events = []
        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(content="板块综合", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.agent_today", return_value="20260708"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            result = run_conversation_once(
                "近5个交易日热点板块",
                settings=settings,
                policy=AgentExecutionPolicy(max_iterations=1),
                llm_factory=LoopLLM,
                event_sink=events.append,
            )

        self.assertIn("部分完成", result.content)
        self.assertIn("板块综合", result.content)
        turn_completed = [event for event in events if event.event_type == "turn_completed"]
        self.assertEqual(turn_completed[-1].status, "done")

    def test_python_program_failure_uses_successful_market_data_for_partial_answer(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                actions = (
                    {"action": "call_tool", "tool_name": "data.index_daily", "arguments": {"index_codes": ["000688.SH"]}},
                    {
                        "action": "call_tool",
                        "tool_name": "research.market_context",
                        "arguments": {"indices": ["000688.SH"], "horizons": ["tomorrow", "day_after_tomorrow"]},
                    },
                    {
                        "action": "call_tool",
                        "tool_name": "analysis.python_program",
                        "arguments": {"task": "计算近10日表现", "code": "def run(context):\n    return missing_name"},
                    },
                )
                return SimpleNamespace(content=json.dumps(actions[LoopLLM.calls - 1], ensure_ascii=False))

        failure = {
            "failure_id": "fail_python",
            "category": "python_code_error",
            "stage": "python_runtime",
            "tool": "analysis.python_program",
            "exception_type": "NameError",
            "message": "NameError: name 'isinstance' is not defined",
            "frames": [],
            "fingerprint": "python-isinstance",
            "retryable": False,
            "repair_level": "runtime",
            "attempt": 1,
            "metadata": {},
        }

        class Registry(_FakeRegistry):
            def execute(self, name: str, arguments, context):
                args = dict(arguments or {})
                self.executed.append((name, args))
                if name == "analysis.python_program":
                    return AgentToolResult(
                        status="error",
                        content=json.dumps({"status": "error", "failure": failure}, ensure_ascii=False),
                        payload={"failure": failure, "python_program": {"failure": failure}},
                        data_names=("python_program",),
                    )
                return AgentToolResult(status="done", content=f"{name} ok", payload={"rows": 10}, data_names=(name,))

        settings = _settings(self)
        registry = Registry()
        synthesis = SimpleNamespace(content="科创50证据综合", skill_names=(), model_policy="none", model_profile="", model_name="")
        events = []

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once(
                "科创50近10个交易日表现以及未来几天走势预测",
                settings=settings,
                llm_factory=LoopLLM,
                event_sink=events.append,
            )

        self.assertEqual(LoopLLM.calls, 3)
        self.assertEqual([name for name, _args in registry.executed], ["data.index_daily", "research.market_context", "analysis.python_program"])
        self.assertEqual(result.status, "done")
        self.assertTrue(result.partial)
        self.assertIn("部分完成", result.content)
        self.assertIn("科创50证据综合", result.content)
        self.assertNotIn("conversation loop stopped", result.content)
        self.assertNotIn('"result": null', result.content)
        self.assertTrue(any(item.step_id == "python_program_recovery_exhausted" for item in result.observations))
        self.assertEqual([event for event in events if event.event_type == "turn_completed"][-1].status, "done")

    def test_python_program_failure_without_data_returns_error_status(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "action": "call_tool",
                            "tool_name": "analysis.python_program",
                            "arguments": {"task": "计算", "code": "def run(context):\n    return missing_name"},
                        }
                    )
                )

        failure = {
            "category": "python_code_error",
            "message": "NameError: missing_name is not defined",
            "fingerprint": "python-missing-name",
        }

        class Registry(_FakeRegistry):
            def execute(self, name: str, arguments, context):
                self.executed.append((name, dict(arguments or {})))
                return AgentToolResult(
                    status="error",
                    content="internal wrapped JSON",
                    payload={"failure": failure, "python_program": {"failure": failure}},
                    data_names=("python_program",),
                )

        settings = _settings(self)
        events = []
        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=Registry()),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            result = run_conversation_once("做一个只读计算", settings=settings, llm_factory=LoopLLM, event_sink=events.append)

        self.assertEqual(result.status, "error")
        self.assertFalse(result.partial)
        self.assertIn("受限 Python 分析程序执行失败", result.content)
        self.assertNotIn("internal wrapped JSON", result.content)
        self.assertEqual([event for event in events if event.event_type == "turn_completed"][-1].status, "error")

    def test_conversation_invalid_date_normalization_requests_clarification(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.market_context","arguments":{"trade_date":"2026-13-40"}}')

        settings = _settings(self)
        registry = _MarketRegistry()

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            result = run_conversation_once("评价大盘走势", settings=settings, llm_factory=LoopLLM)

        self.assertTrue(result.requires_clarification)
        self.assertIn("日期参数无效", result.content)
        self.assertEqual(registry.executed, [])

    def test_conversation_loop_normalizes_next_week_horizon_for_next_week_market_forecast(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content='{"action":"call_tool","tool_name":"research.market_context","arguments":{"horizons":["today"]}}'
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"短回答"}')

        settings = _settings(self)
        registry = _MarketRegistry()
        synthesis = SimpleNamespace(content="## 下周情景\n\n- 已使用 next_week horizon。", skill_names=(), model_policy="standard", model_profile="default", model_name="m")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            result = run_conversation_once("预测下周大盘走势", settings=settings, llm_factory=LoopLLM)

        self.assertIn("下周情景", result.content)
        self.assertEqual([args.get("horizons") for _name, args in registry.executed], [["next_week"]])
        blocked = [item for item in result.observations if item.step_id.endswith("action_blocked")]
        self.assertEqual(blocked, [])

    def test_conversation_executes_readonly_tool_without_agent_runtime(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry(side_effect="readonly")

        with patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry):
            result = run_conversation_once("hello", settings=settings, llm_factory=None)

        self.assertIn("requires an LLM action model", result.content)
        self.assertEqual(result.tool_call_count, 0)
        self.assertFalse(result.requires_confirmation)
        self.assertEqual(registry.executed, [])
        self.assertEqual(result.plan.phase, "conversation_loop")
        self.assertEqual(result.plan.steps, ())

    def test_conversation_executes_stock_basic_lookup_without_confirmation(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"data.stock_basic","arguments":{"query":"通富微电"}}')
                return SimpleNamespace(content='{"action":"final_answer","content":"通富微电已查询"}')

        settings = _settings(self)
        registry = _FakeRegistry(side_effect="write_db")

        with patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry):
            LoopLLM.calls = 0
            result = run_conversation_once("怎么评价 通富微电，预测明天走势", settings=settings, llm_factory=LoopLLM)

        self.assertFalse(result.requires_confirmation)
        self.assertIsNone(result.pending_action_id)
        self.assertEqual(registry.executed, [("data.stock_basic", {"query": "通富微电"})])

    def test_conversation_prompt_injects_resolved_stock_name_symbols(self) -> None:
        class LoopLLM:
            calls = 0
            prompts: list[str] = []

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                LoopLLM.prompts.append(messages[1]["content"])
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content='{"action":"call_tool","tool_name":"research.stock_context","arguments":{"symbols":["002156.SZ"]}}'
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"通富微电研究完成"}')

        settings = _settings(self)
        registry = _FakeRegistry()
        stock_basic = pd.DataFrame([{"ts_code": "002156.SZ", "symbol": "002156", "name": "通富微电"}])
        synthesis = SimpleNamespace(content="通富微电综合", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.load_stock_basic_frame", return_value=stock_basic),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            LoopLLM.prompts = []
            result = run_conversation_once("怎么评价通富微电，预测明天走势", settings=settings, llm_factory=LoopLLM)

        self.assertEqual(result.content, "通富微电综合")
        self.assertIn('"name": "通富微电"', LoopLLM.prompts[0])
        self.assertIn('"ts_code": "002156.SZ"', LoopLLM.prompts[0])
        self.assertEqual(registry.executed[0][0], "research.stock_context")
        self.assertEqual(registry.executed[0][1]["symbols"], ["002156.SZ"])

    def test_chan_daily_buy_sell_request_redirects_stock_daily_and_python_program_to_structured_tools(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content=json.dumps(
                            {
                                "action": "call_tool",
                                "tool_name": "research.chan_context",
                                "arguments": {"message": "国瓷材料 和 洁美科技 缠论日线买卖点"},
                            },
                            ensure_ascii=False,
                        )
                    )
                if LoopLLM.calls == 2:
                    return SimpleNamespace(
                        content=json.dumps(
                            {
                                "action": "call_tool",
                                "tool_name": "data.stock_daily",
                                "arguments": {
                                    "symbols": ["300285.SZ", "002859.SZ"],
                                    "start_date": "20250709",
                                    "end_date": "20260709",
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                if LoopLLM.calls in {3, 4}:
                    return SimpleNamespace(
                        content=json.dumps(
                            {
                                "action": "call_tool",
                                "tool_name": "analysis.python_program",
                                "arguments": {
                                    "task": "错误地迭代 data.stock_daily payload rows",
                                    "code": "def run(context):\n    rows = context['observations_by_step']['loop_2_data_stock_daily']['payload']['result']['payload']['rows']\n    return {'n': len(rows)}",
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"缠论完成"}')

        settings = _settings(self)
        registry = _FakeRegistry(side_effects={"data.stock_daily": "write_db"})
        stock_basic = pd.DataFrame(
            [
                {"ts_code": "300285.SZ", "symbol": "300285", "name": "国瓷材料"},
                {"ts_code": "002859.SZ", "symbol": "002859", "name": "洁美科技"},
            ]
        )
        synthesis = SimpleNamespace(content="缠论结构化综合", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.load_stock_basic_frame", return_value=stock_basic),
            patch("sats.conversation.runtime.agent_today", return_value="20260709"),
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once(
                "国瓷材料 和 洁美科技使用 缠论分析 2者的买卖点，日线",
                settings=settings,
                llm_factory=LoopLLM,
            )

        self.assertEqual(result.content, "缠论结构化综合")
        self.assertEqual(
            [name for name, _args in registry.executed],
            ["research.chan_context", "research.stock_context", "research.internal_analysis", "research.internal_analysis"],
        )
        self.assertNotIn("data.stock_daily", [name for name, _args in registry.executed])
        self.assertNotIn("analysis.python_program", [name for name, _args in registry.executed])
        self.assertEqual(registry.executed[1][1]["symbols"], ["300285.SZ", "002859.SZ"])
        self.assertEqual(registry.executed[2][1]["kind"], "indicators")
        self.assertEqual(registry.executed[3][1]["kind"], "analyze_signals")
        self.assertEqual(registry.executed[3][1]["signals"], "chan")
        self.assertTrue(any(item.step_id.endswith("chan_structured_redirect") for item in result.observations))

    def test_stock_basic_observation_compaction_exposes_stock_matches(self) -> None:
        observation = AgentObservation(
            step_id="loop_1_data_stock_basic",
            kind="tool",
            status="done",
            content="stock_basic: 1 rows",
            payload={
                "tool_name": "data.stock_basic",
                "arguments": {"query": "通富微电"},
                "result": {
                    "status": "done",
                    "content": "stock_basic: 1 rows",
                    "payload": {
                        "rows": 1,
                        "columns": ["ts_code", "symbol", "name"],
                        "sample": [{"ts_code": "002156.SZ", "symbol": "002156", "name": "通富微电"}],
                    },
                },
            },
        )

        compact = _compact_observations((observation,))

        self.assertEqual(compact[0]["stock_matches"][0]["display"], "通富微电(002156.SZ)")

    def test_chan_rows_count_python_error_falls_back_to_analyze_signals(self) -> None:
        observations = (
            AgentObservation(
                step_id="loop_1_research_chan_context",
                kind="tool",
                status="done",
                content="chan context",
                payload={"tool_name": "research.chan_context", "arguments": {}, "result": {"payload": {}}},
            ),
            AgentObservation(
                step_id="loop_2_data_stock_daily",
                kind="tool",
                status="done",
                content="stock_daily: 422 rows",
                payload={
                    "tool_name": "data.stock_daily",
                    "arguments": {"symbols": ["300285.SZ", "002859.SZ"]},
                    "result": {"payload": {"rows": 422, "columns": ["ts_code", "trade_date", "close"]}},
                },
            ),
            AgentObservation(
                step_id="loop_3_research_stock_context",
                kind="tool",
                status="done",
                content="stock context",
                payload={"tool_name": "research.stock_context", "arguments": {"symbols": ["300285.SZ", "002859.SZ"]}, "result": {"payload": {}}},
            ),
            AgentObservation(
                step_id="loop_4_research_internal_analysis",
                kind="tool",
                status="done",
                content="indicators",
                payload={
                    "tool_name": "research.internal_analysis",
                    "arguments": {"kind": "indicators", "symbols": ["300285.SZ", "002859.SZ"]},
                    "result": {"payload": {"analysis": {"kind": "indicators"}}},
                },
            ),
            AgentObservation(
                step_id="loop_5_analysis_python_program",
                kind="tool",
                status="error",
                content='{"status":"error","error":"\'int\' object is not iterable"}',
                payload={"tool_name": "analysis.python_program", "arguments": {}, "result": {"payload": {"status": "error"}}},
            ),
        )

        with patch("sats.conversation.runtime.agent_today", return_value="20260709"):
            step, content = _chan_structured_error_fallback(
                message="国瓷材料 和 洁美科技使用 缠论分析 2者的买卖点，日线",
                observations=observations,
                resolved_stock_mentions=(
                    {"ts_code": "300285.SZ", "name": "国瓷材料"},
                    {"ts_code": "002859.SZ", "name": "洁美科技"},
                ),
                iteration=6,
            )

        self.assertEqual(content, "")
        self.assertIsNotNone(step)
        self.assertEqual(step.tool_name, "research.internal_analysis")
        self.assertEqual(step.arguments["kind"], "analyze_signals")
        self.assertEqual(step.arguments["signals"], "chan")
        self.assertEqual(step.arguments["symbols"], ["300285.SZ", "002859.SZ"])
        self.assertEqual(step.arguments["trade_date"], "20260709")

    def test_conversation_blocks_repeated_stock_basic_lookup_after_match(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"data.stock_basic","arguments":{"query":"通富微电"}}')
                if LoopLLM.calls == 2:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"data.stock_basic","arguments":{"query":"通富微电"}}')
                if LoopLLM.calls == 3:
                    return SimpleNamespace(
                        content='{"action":"call_tool","tool_name":"research.stock_context","arguments":{"symbols":["002156.SZ"]}}'
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"通富微电研究完成"}')

        settings = _settings(self)
        registry = _StockBasicRegistry()
        synthesis = SimpleNamespace(content="通富微电综合", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once("怎么评价通富微电，预测明天走势", settings=settings, llm_factory=LoopLLM)

        self.assertEqual(result.content, "通富微电综合")
        self.assertEqual([name for name, _args in registry.executed], ["data.stock_basic", "research.stock_context"])
        self.assertTrue(any("已经查询过" in item.content for item in result.observations if item.kind == "runtime"))

    def test_hot_sector_opportunity_request_executes_existing_tools_without_llm(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry()

        with patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry):
            result = run_conversation_once(
                "根据热点板块，选取6支 下周大概率上涨的股票",
                settings=settings,
                llm_factory=None,
            )

        self.assertIn("requires an LLM action model", result.content)
        self.assertEqual(result.tool_call_count, 0)
        self.assertEqual(registry.executed, [])
        self.assertEqual(result.plan.steps, ())

    def test_hot_sector_opportunity_request_llm_loop_uses_existing_tools_before_final(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.market_context","arguments":{"dimensions":["hot_sectors"],"horizons":["next_week"]}}')
                if LoopLLM.calls == 2:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"analysis.python_program","arguments":{"task":"热点候选","code":"def run(context):\\n    return {\\"kind\\": \\"hot_sector_candidates\\", \\"rows\\": []}"}}')
                return SimpleNamespace(content='{"action":"final_answer","content":"热点候选完成"}')

        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(content="热点候选综合", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once(
                "根据热点板块，选取6支 下周大概率上涨的股票",
                settings=settings,
                llm_factory=LoopLLM,
            )

        self.assertEqual(result.content, "热点候选综合")
        self.assertNotIn("工具列表移除", result.content)
        self.assertEqual(result.tool_call_count, 2)
        self.assertEqual(
            [name for name, _args in registry.executed],
            ["research.market_context", "analysis.python_program"],
        )
        self.assertEqual([item.kind for item in result.observations[-1:]], ["final"])

    def test_conversation_executes_write_artifact_without_confirmation(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.write_report","arguments":{"message":"hello"}}')
                return SimpleNamespace(content='{"action":"final_answer","content":"机会发现结果"}')

        settings = _settings(self)
        registry = _FakeRegistry(side_effect="write_artifact")
        synthesis = SimpleNamespace(content="机会发现结果", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once("选6支下周大概率上涨的股票", settings=settings, llm_factory=LoopLLM)

        self.assertFalse(result.requires_confirmation)
        self.assertIsNone(result.pending_action_id)
        self.assertEqual(registry.executed, [("research.write_report", {"message": "hello"})])

    def test_conversation_request_confirmation_does_not_force_write_artifact(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content='{"action":"request_confirmation","tool_name":"research.write_report","arguments":{"title":"选股"}}'
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"已完成"}')

        settings = _settings(self)
        registry = _FakeRegistry(side_effect="write_artifact")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
        ):
            result = run_conversation_once("选6支下周大概率上涨的股票", settings=settings, llm_factory=LoopLLM)

        self.assertFalse(result.requires_confirmation)
        self.assertEqual(registry.executed, [("research.write_report", {"title": "选股"})])

    def test_conversation_default_loop_does_not_call_legacy_planner(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"final_answer","content":"直接回答"}')

        settings = _settings(self)

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=_FakeRegistry()),
            patch("sats.agent.planner.build_agent_plan", side_effect=AssertionError("legacy planner should not run")) as planner,
        ):
            result = run_conversation_once("复核 002436", settings=settings, llm_factory=LoopLLM)

        self.assertEqual(result.content, "直接回答")
        self.assertEqual(result.plan.phase, "conversation_loop")
        self.assertEqual(result.plan.steps, ())
        planner.assert_not_called()

    def test_conversation_argument_guard_blocks_missing_schema_required_args(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"web.search","arguments":{}}')

        settings = _settings(self)
        registry = _FakeRegistry(input_schemas={"web.search": {"type": "object", "required": ["query"]}})

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            result = run_conversation_once("搜索", settings=settings, llm_factory=LoopLLM)

        self.assertIn("web.search", result.content)
        self.assertIn("query", result.content)
        self.assertEqual(result.tool_call_count, 0)
        self.assertEqual(registry.executed, [])
        self.assertEqual(result.observations[0].kind, "clarification")

    def test_conversation_argument_guard_blocks_stock_context_without_symbols(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.stock_context","arguments":{"trade_date":"20260625"}}')

        settings = _settings(self)
        registry = _FakeRegistry()

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            result = run_conversation_once("分析走势", settings=settings, llm_factory=LoopLLM)

        self.assertIn("symbols", result.content)
        self.assertEqual(registry.executed, [])
        self.assertTrue(result.requires_clarification)
        self.assertTrue(result.clarification_id)
        self.assertIn("symbols", result.missing_fields)
        action = ChatMemoryStore(settings.db_path).get_pending_action(result.clarification_id or "")
        self.assertEqual(action["action_type"], "conversation_clarification")

    def test_conversation_argument_guard_allows_sector_ranking_with_query_only(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content=(
                            '{"action":"call_tool","tool_name":"research.sector_return_ranking",'
                            '"arguments":{"query":"过去一年A股跌幅最大的10个概念板块"}}'
                        )
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"板块排行完成"}')

        settings = _settings(self)
        registry = _FakeRegistry(
            input_schemas={"research.sector_return_ranking": {"type": "object", "required": ["query"]}},
        )
        synthesis = SimpleNamespace(content="板块排行完成", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once("过去一年A股跌幅最大的10个概念板块", settings=settings, llm_factory=LoopLLM)

        self.assertEqual(result.content, "板块排行完成")
        self.assertFalse(result.requires_clarification)
        self.assertEqual(
            registry.executed,
            [("research.sector_return_ranking", {"query": "过去一年A股跌幅最大的10个概念板块"})],
        )

    def test_conversation_argument_guard_still_blocks_sector_ranking_without_query(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.sector_return_ranking","arguments":{"period":"1y"}}')

        settings = _settings(self)
        registry = _FakeRegistry(
            input_schemas={"research.sector_return_ranking": {"type": "object", "required": ["query"]}},
        )

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            result = run_conversation_once("过去一年A股跌幅最大的10个概念板块", settings=settings, llm_factory=LoopLLM)

        self.assertTrue(result.requires_clarification)
        self.assertIn("缺少明确参数: query", result.content)
        self.assertEqual(registry.executed, [])
        self.assertIn("query", result.missing_fields)

    def test_conversation_ambiguous_stock_analysis_asks_for_symbol(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.stock_context","arguments":{}}')

        settings = _settings(self)

        result = run_conversation_once("分析走势", settings=settings, llm_factory=LoopLLM)

        self.assertTrue(result.requires_clarification)
        self.assertIn("symbols", result.missing_fields)
        self.assertIn("sats chat --answer", result.content)

    def test_conversation_executes_historical_market_context_with_trade_date(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content=(
                            '{"action":"call_tool","tool_name":"research.market_context",'
                            '"arguments":{"trade_date":"20260625","dimensions":["core_indices"]}}'
                        )
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"昨天复盘"}')

        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(content="昨天复盘", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once("评价昨天的大盘走势", settings=settings, llm_factory=LoopLLM)

        self.assertEqual(result.content, "昨天复盘")
        self.assertEqual(registry.executed, [("research.market_context", {"trade_date": "20260625", "dimensions": MARKET_DIMENSIONS})])

    def test_plan_mode_output_uses_plan_mode_structure_without_tool_execution(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry()

        with patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry):
            output = format_plan_mode_result("评价昨天的大盘走势", settings=settings, llm_factory=None)

        self.assertIn("# SATS Plan Mode", output)
        self.assertIn("## 目标", output)
        self.assertIn("## 建议步骤", output)
        self.assertEqual(registry.executed, [])

    def test_conversation_loop_does_not_rewrite_model_selected_tool_with_planner_rules(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(content='{"action":"call_tool","tool_name":"web.search","arguments":{"query":"过去一年A股跌幅最大的10个概念板块"}}')
                return SimpleNamespace(content='{"action":"final_answer","content":"搜索完成"}')

        settings = _settings(self)
        message = "过去一年A股跌幅最大的10个概念板块"
        synthesis = SimpleNamespace(content="板块排行完成", skill_names=(), model_policy="none", model_profile="", model_name="")

        registry = _FakeRegistry()

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            LoopLLM.calls = 0
            result = run_conversation_once(message, settings=settings, llm_factory=LoopLLM)

        self.assertEqual(result.content, "板块排行完成")
        self.assertEqual([name for name, _args in registry.executed], ["web.search"])
        self.assertEqual(result.plan.steps, ())

    def test_research_market_context_tool_preserves_freshness_payload(self) -> None:
        settings = _settings(self)
        spec = next(item for item in research_tool_specs() if item.name == "research.market_context")
        context = AgentToolContext(
            settings=settings,
            storage=None,
            resolver=None,
            policy=AgentExecutionPolicy(),
            command_runner=None,
            trader=None,
            message="今天大盘",
        )
        fake_context = SimpleNamespace(
            payload={
                "trade_date": "20260521",
                "freshness": {
                    "current_day_refresh_requested": True,
                    "index_daily": {"source": "astock_provider_cached", "cache_hit": False},
                },
            }
        )

        with patch("sats.agent.tools.research_tools.build_market_context_component", return_value=fake_context):
            result = spec.executor(context, {"dimensions": ["core_indices"]})

        self.assertTrue(result.payload["market_context"]["freshness"]["current_day_refresh_requested"])
        self.assertFalse(result.payload["market_context"]["freshness"]["index_daily"]["cache_hit"])

    def test_conversation_argument_guard_requires_catalog_before_astock_fetch(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"data.astock_fetch","arguments":{"operation":"astock.stock_basic"}}')

        settings = _settings(self)
        registry = _FakeRegistry(
            side_effect="write_db",
            input_schemas={"data.astock_fetch": {"type": "object", "required": ["operation"]}},
        )

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            result = run_conversation_once("取股票基础信息", settings=settings, llm_factory=LoopLLM)

        self.assertIn("data.astock_catalog", result.content)
        self.assertFalse(result.requires_confirmation)
        self.assertEqual(registry.executed, [])

    def test_continue_conversation_after_clarification_merges_answer_and_executes(self) -> None:
        class FirstLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"research.stock_context","arguments":{"trade_date":"20260625"}}')

        class ResumedLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                ResumedLLM.calls += 1
                if ResumedLLM.calls == 1:
                    return SimpleNamespace(
                        content='{"action":"call_tool","tool_name":"research.stock_context","arguments":{"symbols":["002436"],"trade_date":"20260625"}}'
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"已继续分析"}')

        settings = _settings(self)
        registry = _FakeRegistry()
        synthesis = SimpleNamespace(content="已继续分析", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            first = run_conversation_once("分析走势", settings=settings, llm_factory=FirstLLM)

        self.assertTrue(first.requires_clarification)
        self.assertEqual(registry.executed, [])
        events = []

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            ResumedLLM.calls = 0
            resumed = continue_conversation_after_clarification(
                first.clarification_id or "",
                "002436",
                settings=settings,
                llm_factory=ResumedLLM,
                event_sink=events.append,
            )

        self.assertEqual(resumed.content, "已继续分析")
        self.assertFalse(resumed.requires_clarification)
        self.assertEqual(registry.executed, [("research.stock_context", {"symbols": ["002436"], "trade_date": "20260625"})])
        self.assertIn("plan_ready", [event.event_type for event in events])
        action = ChatMemoryStore(settings.db_path).get_pending_action(first.clarification_id or "")
        self.assertEqual(action["status"], "done")

    def test_reject_conversation_clarification_marks_rejected(self) -> None:
        settings = _settings(self)
        store = ChatMemoryStore(settings.db_path)
        action_id = store.create_pending_action(
            session_id="conversation",
            turn_id="turn_1",
            action_type="conversation_clarification",
            title="补充股票",
            payload={"message": "分析走势"},
        )

        result = reject_pending_conversation_action(action_id, settings=settings, store=store)

        self.assertIn("已取消澄清问题", result.content)
        self.assertEqual(store.get_pending_action(action_id)["status"], "rejected")

    def test_confirm_pending_conversation_action_executes_stored_tool(self) -> None:
        settings = _settings(self)
        store = ChatMemoryStore(settings.db_path)
        store.create_session("conversation")
        action_id = store.create_pending_action(
            session_id="conversation",
            turn_id="turn_1",
            action_type="conversation_tool",
            title="刷新日线",
            payload={
                "message": "刷新日线",
                "session_id": "conversation",
                "policy": {},
                "plan": _plan("data.stock_daily", side_effect="write_db").to_dict(),
                "step": _plan("data.stock_daily", side_effect="write_db").steps[0].to_dict(),
            },
        )
        registry = _FakeRegistry(side_effect="write_db")

        with patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry):
            result = confirm_pending_conversation_action(action_id, settings=settings, store=store, llm_factory=None)

        self.assertEqual(result.content, "tool ok")
        self.assertEqual(registry.executed, [("data.stock_daily", {"message": "hello"})])
        self.assertEqual(store.get_pending_action(action_id)["status"], "done")


class ConversationThreadTest(unittest.TestCase):
    def test_thread_lifecycle(self) -> None:
        settings = _settings(self)
        created = create_thread(settings, title="茅台复盘")
        renamed = rename_thread(settings, created.session_id, "贵州茅台复盘")
        pinned = pin_thread(settings, created.session_id, pinned=True)
        forked = fork_thread(settings, created.session_id, title="分支")
        archived = archive_thread(settings, created.session_id, archived=True)
        visible = list_threads(settings)
        all_threads = list_threads(settings, include_archived=True)

        self.assertEqual(renamed.title, "贵州茅台复盘")
        self.assertTrue(pinned.pinned)
        self.assertEqual(forked.meta["parent_session_id"], created.session_id)
        self.assertTrue(archived.archived)
        self.assertNotIn(created.session_id, {item.session_id for item in visible})
        self.assertIn(created.session_id, {item.session_id for item in all_threads})


class _FakeRegistry:
    def __init__(
        self,
        *,
        side_effect: str = "readonly",
        side_effects: dict[str, str] | None = None,
        input_schemas: dict[str, dict] | None = None,
        requires_trade_permission: bool = False,
        requires_trade_permissions: dict[str, bool] | None = None,
    ) -> None:
        self.side_effect = side_effect
        self.side_effects = side_effects or {}
        self.input_schemas = input_schemas or {}
        self.requires_trade_permission = requires_trade_permission
        self.requires_trade_permissions = requires_trade_permissions or {}
        self.executed: list[tuple[str, dict]] = []

    def get(self, name: str):
        return SimpleNamespace(
            side_effect=self.side_effects.get(name, self.side_effect),
            requires_confirmation=False,
            requires_trade_permission=self.requires_trade_permissions.get(name, self.requires_trade_permission),
            input_schema=self.input_schemas.get(name, {"type": "object", "properties": {}, "required": []}),
        )

    def execute(self, name: str, arguments, context):
        args = dict(arguments or {})
        self.executed.append((name, args))
        return AgentToolResult(status="done", content="tool ok", payload={"args": args}, data_names=("Fake",))


class _StatusRegistry(_FakeRegistry):
    def __init__(self, statuses: dict[str, str]) -> None:
        super().__init__()
        self.statuses = statuses

    def execute(self, name: str, arguments, context):
        args = dict(arguments or {})
        self.executed.append((name, args))
        status = self.statuses.get(name, "done")
        return AgentToolResult(status=status, content=f"{name} {status}", payload={"args": args}, data_names=("Fake",))


class _CapabilityRegistry(_FakeRegistry):
    def execute(self, name: str, arguments, context):
        args = dict(arguments or {})
        self.executed.append((name, args))
        section = str(args.get("section") or "summary")
        if section == "skills":
            catalog = {
                "section": "skills",
                "counts": {"skills": 3},
                "data": {
                    "skills": {
                        "total": 3,
                        "returned": 2,
                        "offset": 0,
                        "limit": 12,
                        "truncated": True,
                        "by_category": {"strategy": 2, "data-source": 1},
                        "items": [
                            {"id": "chan-theory", "name": "chan-theory", "category": "strategy", "description": "缠论方法论"},
                            {"id": "tickflow", "name": "tickflow", "category": "data-source", "description": "实时行情数据源"},
                        ],
                    }
                },
                "consistency": {"warnings": []},
            }
            return AgentToolResult(status="done", content="catalog section skills", payload={"catalog": catalog}, data_names=("Skills",))
        catalog = {
            "section": "summary",
            "counts": {
                "commands": 34,
                "agent-tools": 58,
                "skills": 3,
                "knowledge": 2,
                "providers": 20,
                "screening-rules": 4,
                "signals": 10,
                "factors": 8,
                "api": 5,
            },
            "data": {
                "summary": {
                    "commands": {"total": 34, "top_level_count": 10},
                    "agent-tools": {"total": 58, "by_category": {"web": 4, "analysis": 1, "data_catalog": 2}},
                    "skills": {"total": 3, "by_category": {"strategy": 2, "data-source": 1}},
                    "knowledge": {"total": 2},
                    "providers": {"total": 20, "by_provider": {"astock": 5, "tushare": 10, "akshare": 5}},
                    "screening-rules": {"total": 4},
                    "signals": {"total": 10},
                    "factors": {"total": 8},
                    "api": {"total": 5},
                }
            },
            "consistency": {"warnings": []},
        }
        return AgentToolResult(status="done", content="catalog section summary", payload={"catalog": catalog}, data_names=("SATS capabilities",))


class _MarketRegistry(_FakeRegistry):
    def execute(self, name: str, arguments, context):
        args = dict(arguments or {})
        self.executed.append((name, args))
        return AgentToolResult(
            status="done",
            content="market_context ready",
            payload={
                "status": "ok",
                "market_context": {
                    "requested_horizons": list(args.get("horizons") or []),
                    "requested_dimensions": list(args.get("dimensions") or []),
                    "core_indices": [],
                },
            },
            data_names=("market_context",),
        )


class _StockBasicRegistry(_FakeRegistry):
    def execute(self, name: str, arguments, context):
        args = dict(arguments or {})
        self.executed.append((name, args))
        if name == "data.stock_basic":
            return AgentToolResult(
                status="done",
                content="stock_basic: 1 rows",
                payload={
                    "rows": 1,
                    "columns": ["ts_code", "symbol", "name"],
                    "sample": [{"ts_code": "002156.SZ", "symbol": "002156", "name": "通富微电"}],
                },
                data_names=("stock_basic",),
            )
        return AgentToolResult(status="done", content="tool ok", payload={"args": args}, data_names=("Fake",))


def _plan(tool_name: str, *, side_effect: str) -> AgentPlan:
    return _plan_with_arguments(tool_name, {"message": "hello"}, side_effect=side_effect)


def _plan_with_arguments(tool_name: str, arguments: dict, *, side_effect: str) -> AgentPlan:
    return AgentPlan(
        objective="hello",
        steps=(
            AgentStep(
                step_id="tool_1",
                kind="tool",
                title="工具",
                tool_name=tool_name,
                arguments=arguments,
                side_effect=side_effect,
            ),
            AgentStep(step_id="final", kind="final", title="总结结果"),
        ),
    )


def _settings(testcase: unittest.TestCase):
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    path = Path(tmp.name)
    return SimpleNamespace(project_root=path, db_path=path / "sats.duckdb", openai_model="m", llm_timeout_seconds=10)


if __name__ == "__main__":
    unittest.main()
