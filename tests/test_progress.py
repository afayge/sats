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
from sats.llm import LLMResponse
from sats.cli import main
from sats.progress import FILLED_BLOCK, EMPTY_BLOCK, create_progress


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
    def test_tty_progress_renders_vibe_style_panel_with_ascii_bar(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=10, request="sats screen --trade-date 20260520")

        with progress.step("Tushare 股票数据", total=100) as step:
            step.update(42)

        output = stream.getvalue()
        self.assertIn("SATS", output)
        self.assertIn("Running agent", output)
        self.assertIn("Request: sats screen --trade-date 20260520", output)
        self.assertIn("Current", output)
        self.assertIn("State", output)
        self.assertIn("Tool", output)
        self.assertIn("Time", output)
        self.assertIn("Detail", output)
        self.assertIn("running", output)
        self.assertIn("Tushare 股票数据", output)
        self.assertIn(FILLED_BLOCK, output)
        self.assertIn(EMPTY_BLOCK, output)
        self.assertNotIn("█", output)
        self.assertNotIn("░", output)

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
        self.assertIn("running", output)
        self.assertIn("ok", output)
        self.assertIn("1/1", output)
        self.assertIn(FILLED_BLOCK, output)
        self.assertIn(EMPTY_BLOCK, output)

    def test_failed_step_records_error_detail(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=8, request="demo")

        step = progress.step("bash")
        step.fail(message="exit_code=1")

        output = stream.getvalue()
        self.assertIn("error", output)
        self.assertIn("exit_code=1", output)

    def test_repeated_updates_clear_previous_panel_before_redraw(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=8, request="sats dsa --from-screened")

        with progress.step("Tushare 股票数据", total=100) as step:
            step.update(10)
            step.update(20)
            step.complete(message="done")

        output = stream.getvalue()
        self.assertIn("\033[J", output)
        self.assertIn("\033[", output)
        self.assertIn("A", output)

    def test_panel_lines_fit_chinese_text_in_narrow_terminal(self) -> None:
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
        widths = {get_cwidth(_strip_control(line)) for line in lines}
        self.assertEqual(len(widths), 1)
        self.assertLessEqual(max(widths), 53)

    def test_many_steps_are_folded_to_fixed_height(self) -> None:
        stream = _TtyStringIO()
        progress = create_progress(stream=stream, force=True, width=8)

        for index in range(10):
            step = progress.step(f"step-{index}", total=1)
            step.complete(message="ok")

        lines = progress._panel_lines()
        output = "\n".join(lines)
        self.assertIn("older steps", output)
        self.assertNotIn("step-0", output)
        self.assertIn("step-9", output)
        self.assertEqual(len(lines), 17)

    def test_cli_chat_can_show_forced_progress_blocks(self) -> None:
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
            self.assertEqual(main(["chat", "分析大盘"]), 0)

        self.assertIn(FILLED_BLOCK, progress_stream.getvalue())
        self.assertIn("deepseek-v4-pro", progress_stream.getvalue())
        self.assertIn("Request: sats chat", progress_stream.getvalue())

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
