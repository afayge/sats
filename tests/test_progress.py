from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from prompt_toolkit.utils import get_cwidth

from sats.analysis.opportunity_discovery import OpportunityDiscoveryResult
from sats.agent.progress import _event_detail, _event_label, agent_progress_event_sink
from sats.llm import LLMResponse
from sats.cli import main
from sats.progress import create_progress


def _strip_control(value: str) -> str:
    result = []
    in_escape = False
    for char in value:
        if char == "\033":
            in_escape = True
            continue
        if in_escape:
            if char.isalpha():
                in_escape = False
            continue
        result.append(char)
    return "".join(result)


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class ProgressReporterTest(unittest.TestCase):
    def test_agent_progress_labels_distinguish_chat_skill_and_knowledge(self) -> None:
        def event(tool_name: str) -> SimpleNamespace:
            return SimpleNamespace(
                event_type="runtime_iteration_started",
                item_type="agent_step",
                item_name="step",
                payload={"tool_name": tool_name},
            )

        self.assertEqual(_event_label(event("chat.answer")), "生成普通回答")
        self.assertEqual(_event_label(event("chat.load_skill")), "调用 skill")
        self.assertEqual(_event_label(event("chat.list_skills")), "调用 skill")
        self.assertEqual(_event_label(event("chat.knowledge_search")), "检索知识库")
        self.assertEqual(_event_label(event("research.knowledge_context")), "检索知识库")

        failed = event("chat.knowledge_search")
        failed.status = "error"
        failed.content = "unknown knowledge base: missing"
        failed.payload["result"] = {
            "status": "error",
            "content": "unknown knowledge base: missing",
            "payload": {},
        }
        self.assertIn("unknown knowledge base: missing", _event_detail(failed))

    def test_tty_progress_renders_codex_style_lines(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=10, request="sats screen --trade-date 20260520")

        with progress.step("Tushare 股票数据", total=100) as step:
            step.update(42)

        output = stream.getvalue()
        self.assertIn("→ Tushare 股票数据", output)
        self.assertIn("· Tushare 股票数据", output)
        self.assertIn("✓ Tushare 股票数据", output)
        self.assertIn("Tushare 股票数据", output)
        self.assertIn("42/100", output)
        self.assertIn("100/100", output)
        self.assertNotIn("Running agent", output)
        self.assertNotIn("Current", output)
        self.assertNotIn("State", output)
        self.assertNotIn("Tool", output)
        self.assertNotIn("Detail", output)
        self.assertNotIn("┌", output)
        self.assertNotIn("└", output)

    def test_non_tty_and_json_progress_are_silent(self) -> None:
        non_tty = io.StringIO()
        progress = create_progress(stream=non_tty)
        with progress.step("不会显示", total=1) as step:
            step.update(1)
        self.assertEqual(non_tty.getvalue(), "")

        json_stream = _TtyStringIO()
        progress = create_progress(stream=json_stream, json_mode=True, force=True)
        with progress.step("JSON 静默", total=1) as step:
            step.update(1)
        self.assertEqual(json_stream.getvalue(), "")

    def test_unknown_duration_step_shows_running_and_completes(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=8)

        step = progress.step("deepseek-v4-pro")
        step.update()
        step.complete()

        output = stream.getvalue()
        self.assertIn("→ deepseek-v4-pro", output)
        self.assertIn("✓ deepseek-v4-pro", output)
        self.assertNotIn("running", output)
        self.assertNotIn("ok", output)

    def test_failed_step_records_error_detail(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=8, request="demo")

        step = progress.step("bash")
        step.fail(message="exit_code=1")

        output = stream.getvalue()
        self.assertIn("→ bash", output)
        self.assertIn("✗ bash", output)
        self.assertIn("exit_code=1", output)

    def test_repeated_updates_append_lines_without_redraw(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=8, request="sats dsa --from-screened")

        with progress.step("Tushare 股票数据", total=100) as step:
            step.update(10)
            step.update(20)
            step.complete(message="done")

        output = stream.getvalue()
        self.assertIn("→ Tushare 股票数据", output)
        self.assertIn("10/100", output)
        self.assertIn("20/100", output)
        self.assertIn("✓ Tushare 股票数据", output)
        self.assertIn("done", output)
        self.assertNotIn("\033[J", output)
        self.assertNotIn("\033[A", output)

    def test_progress_lines_fit_chinese_text_in_narrow_terminal(self) -> None:
        stream = _TtyStringIO()
        with patch("sats.progress.shutil.get_terminal_size", return_value=os.terminal_size((54, 24))):
            progress = create_progress(
                stream=stream,
                force=True,
                width=8,
                request="sats discover 给出几个未来几天有上涨趋势的股票，优先热点板块",
            )
            step = progress.step("Tushare 股票数据和热点板块", total=100)
            step.update(42, message="正在获取同花顺行业板块和概念板块成分股")
            lines = progress._panel_lines()

        self.assertTrue(lines)
        widths = [get_cwidth(_strip_control(line)) for line in lines]
        self.assertLessEqual(max(widths), 53)

    def test_completed_steps_remain_as_lines(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=8, request="demo")

        progress.step("全市场数据").complete(message="5300 只")
        progress.step("机会发现").fail(message="Tushare timeout")
        progress.step("生成分析").complete(message="最终分析完成")

        output = _strip_control("\n".join(progress._panel_lines()))
        self.assertIn("✓ 全市场数据", output)
        self.assertIn("5300 只", output)
        self.assertIn("✗ 机会发现", output)
        self.assertIn("Tushare timeout", output)
        self.assertIn("✓ 生成分析", output)
        self.assertIn("最终分析完成", output)
        self.assertNotIn("Recent details", output)

    def test_many_steps_are_kept_as_append_only_history(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=8)

        for index in range(10):
            step = progress.step(f"step-{index}", total=1)
            step.complete(message="ok")

        lines = progress._panel_lines()
        output = "\n".join(lines)
        self.assertIn("step-0", output)
        self.assertIn("step-9", output)
        self.assertNotIn("older steps", output)
        self.assertNotIn("Recent details", output)
        self.assertEqual(len(lines), 20)

    def test_agent_progress_event_sink_summarizes_step_details(self) -> None:
        stream = _TtyStringIO()
        with patch("sats.progress.shutil.get_terminal_size", return_value=os.terminal_size((140, 24))):
            progress = create_progress(stream=stream, force=True, width=8, request="给出一些上涨机会")
            sink = agent_progress_event_sink(progress)
            assert sink is not None

            sink(
                SimpleNamespace(
                    event_type="runtime_iteration_started",
                    item_type="agent_step",
                    item_name="discover",
                    status="running",
                    content="",
                    payload={
                        "title": "发现具有上涨机会的股票",
                        "tool_name": "research.discover_opportunities",
                        "arguments": {"query": "给出一些上涨机会", "limit": 3},
                    },
                )
            )
            sink(
                SimpleNamespace(
                    event_type="tool_completed",
                    item_type="agent_step",
                    item_name="discover",
                    status="error",
                    content='{"status":"error","error":"Tushare timeout"}',
                    payload={
                        "tool_name": "research.discover_opportunities",
                        "arguments": {"query": "给出一些上涨机会", "limit": 3},
                        "result": {
                            "status": "error",
                            "content": '{"status":"error","error":"Tushare timeout"}',
                            "payload": {"status": "error", "error": "Tushare timeout"},
                        },
                    },
                )
            )
            sink(
                SimpleNamespace(
                    event_type="context_started",
                    item_type="agent_synthesis",
                    item_name="final_synthesis",
                    status="running",
                    content="",
                    payload={},
                )
            )
            sink(
                SimpleNamespace(
                    event_type="context_completed",
                    item_type="agent_synthesis",
                    item_name="final_synthesis",
                    status="done",
                    content="总结内容",
                    payload={"used_llm": True, "skills": ["hot-theme", "volume-breakout"]},
                )
            )

            output = _strip_control("\n".join(progress._panel_lines()))
        self.assertNotIn("Recent details", output)
        self.assertIn("research.discover_opportunities", output)
        self.assertIn("query=给出一些上涨机会", output)
        self.assertIn("Tushare timeout", output)
        self.assertIn("最终分析完成", output)
        self.assertIn("skills=2", output)


    def test_conversation_action_progress_shows_decision_detail(self) -> None:
        stream = _TtyStringIO()
        with patch("sats.progress.shutil.get_terminal_size", return_value=os.terminal_size((140, 24))):
            progress = create_progress(stream=stream, force=True, width=8, request="预测下周大盘走势")
            sink = agent_progress_event_sink(progress)
            assert sink is not None

            sink(
                SimpleNamespace(
                    event_type="runtime_iteration_started",
                    item_type="conversation_action",
                    item_name="iteration_1",
                    status="running",
                    content="",
                    payload={"iteration": 1, "max_iterations": 6, "title": "决定下一步"},
                )
            )
            sink(
                SimpleNamespace(
                    event_type="llm_completed",
                    item_type="conversation_action",
                    item_name="iteration_1",
                    status="done",
                    content="决定调用 research.market_context",
                    payload={
                        "iteration": 1,
                        "action": {
                            "action": "call_tool",
                            "tool_name": "research.market_context",
                            "arguments": {"horizons": ["next_week"], "dimensions": ["core_indices", "market_breadth"]},
                        },
                    },
                )
            )

            output = _strip_control("\n".join(progress._panel_lines()))

        self.assertIn("决定下一步", output)
        self.assertIn("research.market_context", output)
        self.assertIn("horizons=[next_week]", output)
        self.assertNotIn("iteration_1 0.0s  iteration_1", output)


    def test_cli_chat_can_show_forced_progress_lines(self) -> None:
        progress_stream = _TtyStringIO()
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")

        class FakeLLM:
            def chat(self, messages, tools=None):
                return LLMResponse(content="回答")

        def forced_progress(**kwargs):
            return create_progress(
                stream=progress_stream,
                force=True,
                width=6,
                json_mode=kwargs.get("json_mode", False),
                request=kwargs.get("request"),
            )

        with (
            patch("sats.cli.create_progress", side_effect=forced_progress),
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch("sats.chat.ChatLLM", return_value=FakeLLM()),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["chat", "--no-agent", "分析大盘"]), 0)

        self.assertIn("deepseek-v4-pro", progress_stream.getvalue())
        self.assertIn("→", progress_stream.getvalue())
        self.assertIn("✓", progress_stream.getvalue())
        self.assertNotIn("Running agent", progress_stream.getvalue())
        self.assertNotIn("Request: sats chat", progress_stream.getvalue())

    def test_discover_json_keeps_stdout_parseable_when_progress_requested(self) -> None:
        progress_stream = _TtyStringIO()
        result = OpportunityDiscoveryResult(
            trade_date="20260520",
            signals="short_up",
            candidates=[],
            candidate_count=0,
            scanned_count=0,
            message="无符合中短期上涨信号的候选股票",
        )
        settings = SimpleNamespace(project_root=Path(tempfile.gettempdir()), db_path=Path(":memory:"))

        def forced_progress(**kwargs):
            return create_progress(
                stream=progress_stream,
                force=True,
                width=6,
                json_mode=kwargs.get("json_mode", False),
                request=kwargs.get("request"),
            )

        stdout = io.StringIO()
        with (
            patch("sats.cli.create_progress", side_effect=forced_progress),
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.AStockDataProvider"),
            patch("sats.cli.run_opportunity_discovery", return_value=result),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["discover", "--trade-date", "20260520", "--json"]), 0)

        self.assertEqual(progress_stream.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["message"], result.message)
        with patch("sats.cli.run_opportunity_discovery", return_value=OpportunityDiscoveryResult(
            trade_date="20260520",
            signals="short_up",
            candidates=[],
            candidate_count=0,
            scanned_count=0,
        )):
            stdout = io.StringIO()
            with (
                patch("sats.cli.create_progress", side_effect=forced_progress),
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.AStockDataProvider"),
                redirect_stdout(stdout),
            ):
                self.assertEqual(main(["discover", "--trade-date", "20260520", "--json"]), 0)
            json.loads(stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
