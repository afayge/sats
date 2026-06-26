from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sats.agent.models import AgentExecutionPolicy, AgentPlan, AgentStep
from sats.agent.tools import AgentToolContext, AgentToolResult
from sats.agent.tools.research_tools import research_tool_specs
from sats.conversation.runtime import (
    confirm_pending_conversation_action,
    continue_conversation_after_clarification,
    format_conversation_plan,
    reject_pending_conversation_action,
    run_conversation_once,
)
from sats.conversation.threads import archive_thread, create_thread, fork_thread, list_threads, pin_thread, rename_thread
from sats.memory import ChatMemoryStore


class ConversationRuntimeTest(unittest.TestCase):
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
            patch("sats.conversation.runtime.build_agent_plan", return_value=AgentPlan(objective="hello")),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("loop final should not synthesize")),
        ):
            result = run_conversation_once("hello", settings=settings, llm_factory=LoopLLM)

        self.assertEqual(result.content, "loop 综合回答")
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(registry.executed, [("chat.answer", {"message": "hello"})])
        self.assertEqual([item.kind for item in result.observations], ["tool", "final"])

    def test_conversation_loop_requires_confirmation_for_side_effect_tool(self) -> None:
        class LoopLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content='{"action":"call_tool","tool_name":"data.stock_daily","arguments":{"symbols":["002436"]}}')

        settings = _settings(self)
        registry = _FakeRegistry(side_effect="write_db")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=AgentPlan(objective="刷新日线")),
        ):
            result = run_conversation_once("刷新日线", settings=settings, llm_factory=LoopLLM)

        self.assertTrue(result.requires_confirmation)
        self.assertTrue(result.pending_action_id)
        self.assertEqual(registry.executed, [])
        action = ChatMemoryStore(settings.db_path).get_pending_action(result.pending_action_id or "")
        self.assertEqual(action["action_type"], "conversation_tool")
        self.assertEqual(action["payload"]["tool_name"], "data.stock_daily")

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
            patch("sats.conversation.runtime.build_agent_plan", return_value=AgentPlan(objective="搜索")),
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
            patch("sats.conversation.runtime.build_agent_plan", return_value=AgentPlan(objective="recover")),
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
            patch("sats.conversation.runtime.build_agent_plan", return_value=AgentPlan(objective="bad")),
        ):
            result = run_conversation_once("bad", settings=settings, llm_factory=BadLLM)

        self.assertIn("conversation action must be a JSON object", result.content)
        invalid = [item for item in result.observations if item.step_id.endswith("invalid_action")]
        self.assertEqual(len(invalid), 2)

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
            patch("sats.conversation.runtime.build_agent_plan", return_value=AgentPlan(objective="取股票基础信息")),
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
        plan = _plan_with_arguments("research.market_context", {"horizons": ["next_week"]}, side_effect="readonly")
        synthesis = SimpleNamespace(content="# 大盘分析\n\n## 下周情景\n\n- 以真实市场上下文做情景推演。", skill_names=(), model_policy="standard", model_profile="default", model_name="m")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis) as synthesize,
        ):
            result = run_conversation_once("分析今天的大盘走势，预测下周走势情况", settings=settings, llm_factory=LoopLLM)

        self.assertIn("下周情景", result.content)
        self.assertNotEqual(result.content, "短回答")
        self.assertEqual(registry.executed, [("research.market_context", {"horizons": ["next_week"], "dimensions": ["core_indices", "market_breadth"]})])
        self.assertIn("loop_1_action_blocked", [item.step_id for item in result.observations])
        synthesize.assert_called_once()

    def test_conversation_loop_requires_next_week_horizon_for_next_week_market_forecast(self) -> None:
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
                if LoopLLM.calls == 2:
                    return SimpleNamespace(content='{"action":"final_answer","content":"今日数据够了"}')
                if LoopLLM.calls == 3:
                    return SimpleNamespace(
                        content='{"action":"call_tool","tool_name":"research.market_context","arguments":{"horizons":["next_week"]}}'
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"短回答"}')

        settings = _settings(self)
        registry = _MarketRegistry()
        plan = _plan_with_arguments("research.market_context", {"horizons": ["next_week"]}, side_effect="readonly")
        synthesis = SimpleNamespace(content="## 下周情景\n\n- 已使用 next_week horizon。", skill_names=(), model_policy="standard", model_profile="default", model_name="m")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            result = run_conversation_once("预测下周大盘走势", settings=settings, llm_factory=LoopLLM)

        self.assertIn("下周情景", result.content)
        self.assertEqual([args.get("horizons") for _name, args in registry.executed], [["today"], ["next_week"]])
        blocked = [item for item in result.observations if item.step_id.endswith("action_blocked")]
        self.assertEqual(len(blocked), 1)
        self.assertIn("next_week", blocked[0].content)

    def test_conversation_executes_readonly_tool_without_agent_runtime(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry(side_effect="readonly")
        plan = _plan("chat.answer", side_effect="readonly")
        synthesis = SimpleNamespace(content="综合回答", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            result = run_conversation_once("hello", settings=settings, llm_factory=None)

        self.assertEqual(result.content, "综合回答")
        self.assertEqual(result.tool_call_count, 1)
        self.assertFalse(result.requires_confirmation)
        self.assertEqual(registry.executed, [("chat.answer", {"message": "hello"})])

    def test_conversation_requires_confirmation_for_write_tools(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry(side_effect="write_db")
        plan = _plan("data.stock_daily", side_effect="write_db")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
        ):
            result = run_conversation_once("刷新日线", settings=settings, llm_factory=None)

        self.assertTrue(result.requires_confirmation)
        self.assertTrue(result.pending_action_id)
        self.assertEqual(registry.executed, [])
        action = ChatMemoryStore(settings.db_path).get_pending_action(result.pending_action_id or "")
        self.assertEqual(action["action_type"], "conversation_tool")
        self.assertEqual(action["payload"]["tool_name"], "data.stock_daily")

    def test_conversation_executes_write_artifact_without_confirmation(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry(side_effect="write_artifact")
        plan = _plan("research.discover_opportunities", side_effect="write_artifact")
        synthesis = SimpleNamespace(content="机会发现结果", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            result = run_conversation_once("选6支下周大概率上涨的股票", settings=settings, llm_factory=None)

        self.assertFalse(result.requires_confirmation)
        self.assertIsNone(result.pending_action_id)
        self.assertEqual(registry.executed, [("research.discover_opportunities", {"message": "hello"})])

    def test_conversation_request_confirmation_does_not_force_write_artifact(self) -> None:
        class LoopLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                LoopLLM.calls += 1
                if LoopLLM.calls == 1:
                    return SimpleNamespace(
                        content='{"action":"request_confirmation","tool_name":"research.discover_opportunities","arguments":{"query":"选股"}}'
                    )
                return SimpleNamespace(content='{"action":"final_answer","content":"已完成"}')

        settings = _settings(self)
        registry = _FakeRegistry(side_effect="write_artifact")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=AgentPlan(objective="选股")),
        ):
            result = run_conversation_once("选6支下周大概率上涨的股票", settings=settings, llm_factory=LoopLLM)

        self.assertFalse(result.requires_confirmation)
        self.assertEqual(registry.executed, [("research.discover_opportunities", {"query": "选股"})])

    def test_conversation_subagent_executes_only_readonly_tools(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry(side_effects={"chat.answer": "readonly", "data.stock_daily": "write_db"})
        parent_plan = AgentPlan(
            objective="复核",
            steps=(
                AgentStep(
                    step_id="sub_technical",
                    kind="subagent",
                    title="技术面复核",
                    arguments={"task": "技术面复核"},
                    side_effect="readonly",
                ),
                AgentStep(step_id="final", kind="final", title="总结结果"),
            ),
        )
        child_plan = AgentPlan(
            objective="技术面复核",
            steps=(
                AgentStep(
                    step_id="readonly_chat",
                    kind="tool",
                    title="只读问答",
                    tool_name="chat.answer",
                    arguments={"message": "hello"},
                    side_effect="readonly",
                ),
                AgentStep(
                    step_id="write_daily",
                    kind="tool",
                    title="写库日线",
                    tool_name="data.stock_daily",
                    arguments={"message": "hello"},
                    side_effect="write_db",
                ),
                AgentStep(step_id="final", kind="final", title="总结结果"),
            ),
        )
        synthesis = SimpleNamespace(content="子任务综合", skill_names=(), model_policy="none", model_profile="", model_name="")

        def build_plan(message, **_kwargs):
            return child_plan if message == "技术面复核" else parent_plan

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", side_effect=build_plan),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            result = run_conversation_once("复核 002436", settings=settings, llm_factory=None)

        self.assertEqual(result.content, "子任务综合")
        self.assertFalse(result.requires_confirmation)
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(registry.executed, [("chat.answer", {"message": "hello"})])
        subagent = next(item for item in result.observations if item.kind == "subagent")
        self.assertEqual(subagent.payload["skipped"], ["data.stock_daily"])

    def test_conversation_argument_guard_blocks_missing_schema_required_args(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry(input_schemas={"web.search": {"type": "object", "required": ["query"]}})
        plan = _plan_with_arguments("web.search", {}, side_effect="readonly")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            result = run_conversation_once("搜索", settings=settings, llm_factory=None)

        self.assertIn("web.search", result.content)
        self.assertIn("query", result.content)
        self.assertEqual(result.tool_call_count, 0)
        self.assertEqual(registry.executed, [])
        self.assertEqual(result.observations[0].kind, "clarification")

    def test_conversation_argument_guard_blocks_stock_context_without_symbols(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry()
        plan = _plan_with_arguments("research.stock_context", {"trade_date": "20260625"}, side_effect="readonly")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            result = run_conversation_once("分析走势", settings=settings, llm_factory=None)

        self.assertIn("symbols", result.content)
        self.assertEqual(registry.executed, [])
        self.assertTrue(result.requires_clarification)
        self.assertTrue(result.clarification_id)
        self.assertIn("symbols", result.missing_fields)
        action = ChatMemoryStore(settings.db_path).get_pending_action(result.clarification_id or "")
        self.assertEqual(action["action_type"], "conversation_clarification")

    def test_conversation_ambiguous_stock_analysis_asks_for_symbol(self) -> None:
        settings = _settings(self)

        result = run_conversation_once("分析走势", settings=settings, llm_factory=None)

        self.assertTrue(result.requires_clarification)
        self.assertIn("symbols", result.missing_fields)
        self.assertIn("sats chat --answer", result.content)

    def test_conversation_executes_historical_market_context_with_trade_date(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry()
        plan = _plan_with_arguments(
            "research.market_context",
            {"trade_date": "20260625", "dimensions": ["core_indices"]},
            side_effect="readonly",
        )
        synthesis = SimpleNamespace(content="昨天复盘", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            result = run_conversation_once("评价昨天的大盘走势", settings=settings, llm_factory=None)

        self.assertEqual(result.content, "昨天复盘")
        self.assertEqual(registry.executed, [("research.market_context", {"trade_date": "20260625", "dimensions": ["core_indices"]})])

    def test_conversation_plan_output_includes_tool_arguments(self) -> None:
        settings = _settings(self)
        plan = _plan_with_arguments(
            "research.market_context",
            {"trade_date": "20260625", "dimensions": ["core_indices"]},
            side_effect="readonly",
        )

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=_FakeRegistry()),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
        ):
            output = format_conversation_plan("评价昨天的大盘走势", settings=settings)

        self.assertIn('"trade_date": "20260625"', output)
        self.assertIn("research.market_context", output)

    def test_conversation_reroutes_sector_ranking_misplanned_theme_or_web_tools(self) -> None:
        settings = _settings(self)
        message = "过去一年A股跌幅最大的10个概念板块"
        synthesis = SimpleNamespace(content="板块排行完成", skill_names=(), model_policy="none", model_profile="", model_name="")

        for wrong_tool in ("research.theme_stock_returns", "web.search"):
            with self.subTest(wrong_tool=wrong_tool):
                registry = _FakeRegistry()
                plan = _plan_with_arguments(wrong_tool, {"query": message}, side_effect="readonly")

                with (
                    patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
                    patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
                    patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
                ):
                    result = run_conversation_once(message, settings=settings, llm_factory=None)

                self.assertEqual(result.content, "板块排行完成")
                self.assertEqual([name for name, _args in registry.executed], ["research.sector_return_ranking"])
                args = registry.executed[0][1]
                self.assertEqual(args["query"], message)
                self.assertEqual(args["sector_type"], "concept")
                self.assertEqual(args["period"], "1y")
                self.assertEqual(args["direction"], "bottom")
                self.assertEqual(args["limit"], 10)
                self.assertEqual(result.plan.steps[0].tool_name, "research.sector_return_ranking")

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
        settings = _settings(self)
        registry = _FakeRegistry(
            side_effect="write_db",
            input_schemas={"data.astock_fetch": {"type": "object", "required": ["operation"]}},
        )
        plan = _plan_with_arguments("data.astock_fetch", {"operation": "astock.stock_basic"}, side_effect="write_db")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=plan),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            result = run_conversation_once("取股票基础信息", settings=settings, llm_factory=None)

        self.assertIn("data.astock_catalog", result.content)
        self.assertFalse(result.requires_confirmation)
        self.assertEqual(registry.executed, [])

    def test_continue_conversation_after_clarification_merges_answer_and_executes(self) -> None:
        settings = _settings(self)
        registry = _FakeRegistry()
        blocked_plan = _plan_with_arguments("research.stock_context", {"trade_date": "20260625"}, side_effect="readonly")
        resumed_plan = _plan_with_arguments("research.stock_context", {"symbols": ["002436"], "trade_date": "20260625"}, side_effect="readonly")
        synthesis = SimpleNamespace(content="已继续分析", skill_names=(), model_policy="none", model_profile="", model_name="")

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=blocked_plan),
            patch("sats.conversation.runtime.synthesize_agent_result", side_effect=AssertionError("synthesis should not run")),
        ):
            first = run_conversation_once("分析走势", settings=settings, llm_factory=None)

        self.assertTrue(first.requires_clarification)
        self.assertEqual(registry.executed, [])
        events = []

        with (
            patch("sats.conversation.runtime.build_default_tool_registry", return_value=registry),
            patch("sats.conversation.runtime.build_agent_plan", return_value=resumed_plan),
            patch("sats.conversation.runtime.synthesize_agent_result", return_value=synthesis),
        ):
            resumed = continue_conversation_after_clarification(
                first.clarification_id or "",
                "002436",
                settings=settings,
                llm_factory=None,
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
    ) -> None:
        self.side_effect = side_effect
        self.side_effects = side_effects or {}
        self.input_schemas = input_schemas or {}
        self.executed: list[tuple[str, dict]] = []

    def get(self, name: str):
        return SimpleNamespace(
            side_effect=self.side_effects.get(name, self.side_effect),
            requires_confirmation=False,
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
