from __future__ import annotations

import io
import sys
import unittest
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.utils import get_cwidth

from sats import __version__
from sats.chat import ChatResult, ChatSession
from sats.cli import main
from sats.history import InteractionHistoryStore
from sats.memory import ChatMemoryStore
from sats.output_saver import CapturedOutput
from sats.repl import (
    CLI_COMMANDS,
    COMPLETION_DESCRIPTIONS,
    COMPLETION_LIST_MIN_WIDTH,
    COMPLETION_MENU_SELECTED_STYLE,
    COMPLETION_MENU_STYLE,
    COMPLETION_WORDS,
    COMMAND_STYLE,
    DESC_STYLE,
    INTERRUPT_MESSAGE,
    MUTED_STYLE,
    PROMPT_MESSAGE,
    REPL_STYLE,
    SEPARATOR_STYLE,
    ReplState,
    SlashCommandFileHistory,
    build_repl_completer,
    handle_repl_line,
    help_text,
    render_help_box,
    render_startup_banner,
    repl_command_to_argv,
    run_repl,
    _status_toolbar,
    _TeeStdout,
)


class RecordingChatSession(ChatSession):
    def __init__(self) -> None:
        self.calls = []
        self.settings = SimpleNamespace(db_path=Path("missing.duckdb"))

    def ask(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return ChatResult(content="回答", skill_names=())


class ReplFakeLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, tools=None):
        return SimpleNamespace(content="回答")


def _completion_map(text: str):
    completer = build_repl_completer()
    completions = completer.get_completions(Document(text), CompleteEvent(completion_requested=True))
    return {completion.text: completion for completion in completions}


def _completion_meta(completion) -> str:
    return fragment_list_to_text(completion.display_meta)


class ReplCliTest(unittest.TestCase):
    def test_main_without_args_enters_repl(self) -> None:
        with patch("sats.repl.run_repl", return_value=0) as run_repl:
            self.assertEqual(main([]), 0)

        run_repl.assert_called_once_with()

    def test_main_with_args_uses_existing_command_path(self) -> None:
        with patch("sats.cli.cmd_result_rules", return_value=0) as command:
            self.assertEqual(main(["result-rules"]), 0)

        command.assert_called_once()

    def test_startup_banner_contains_version_help_commands_and_db(self) -> None:
        output: list[str] = []
        formatted: list[list[tuple[str, str]]] = []

        render_startup_banner(
            Path("data/sats.duckdb"),
            printer=output.append,
            formatted_printer=lambda fragments: formatted.append(list(fragments))
            or output.append("".join(text for _, text in fragments)),
            terminal_width=80,
        )

        text = "\n".join(output)
        self.assertTrue(all(get_cwidth(line) == 80 for line in output))
        self.assertTrue(output[0].startswith("┌"))
        self.assertTrue(output[-1].startswith("└"))
        self.assertEqual(sum(1 for line in output if line.startswith("┌")), 2)
        self.assertEqual(sum(1 for line in output if line.startswith("└")), 2)
        self.assertTrue(output[2].startswith("└"))
        self.assertTrue(output[3].startswith("┌"))
        self.assertIn("SATS", text)
        self.assertIn(f"v{__version__}", text)
        self.assertIn("输入 /commands 开始", text)
        self.assertIn("/help", text)
        self.assertIn("查看命令", text)
        self.assertIn("/exit", text)
        self.assertIn("退出", text)
        self.assertIn("/clear", text)
        self.assertIn("清屏", text)
        self.assertIn("/save", text)
        self.assertIn("保存上一条输出", text)
        self.assertIn("Ctrl+C", text)
        self.assertIn("中断当前执行", text)
        self.assertIn("/screen --trade-date YYYYMMDD", text)
        self.assertIn("全 A 股筛选", text)
        self.assertIn("/results --passed", text)
        self.assertIn("查询筛选结果", text)
        self.assertIn("/analyze-dsa", text)
        self.assertIn("/dsa --stocks 000001,600519", text)
        self.assertIn("分析股票", text)
        self.assertIn("DB: data/sats.duckdb", text)
        self.assertTrue(any(style == MUTED_STYLE and "输入 /commands 开始" in value for row in formatted for style, value in row))
        self.assertTrue(any(style == COMMAND_STYLE and "/help" in value for row in formatted for style, value in row))
        self.assertTrue(any(style == COMMAND_STYLE and "/screen" in value for row in formatted for style, value in row))
        self.assertTrue(any(style == DESC_STYLE and "查询筛选结果" in value for row in formatted for style, value in row))
        self.assertEqual(COMMAND_STYLE, DESC_STYLE)
        screen_line = next(line for line in output if "/screen --trade-date YYYYMMDD" in line)
        results_line = next(line for line in output if "/results --passed" in line)
        analyze_line = next(line for line in output if "/analyze-dsa" in line)
        self.assertEqual(screen_line.index("/screen"), results_line.index("/results"))
        self.assertEqual(screen_line.index("/screen"), analyze_line.index("/analyze-dsa"))
        self.assertEqual(screen_line.index("全 A 股筛选"), results_line.index("查询筛选结果"))
        self.assertEqual(screen_line.index("全 A 股筛选"), analyze_line.index("分析股票"))

    def test_startup_banner_truncates_long_db_path_to_terminal_width(self) -> None:
        output: list[str] = []
        long_db_path = Path("data") / ("very-long-directory-name-" * 8) / "sats.duckdb"

        render_startup_banner(
            long_db_path,
            printer=output.append,
            formatted_printer=lambda fragments: output.append("".join(text for _, text in fragments)),
            terminal_width=50,
        )

        self.assertTrue(all(get_cwidth(line) == 50 for line in output))
        self.assertTrue(any("..." in line for line in output))

    def test_help_box_contains_aligned_commands_examples_and_styles(self) -> None:
        output: list[str] = []
        formatted: list[list[tuple[str, str]]] = []

        def rich_print(fragments):
            row = list(fragments)
            formatted.append(row)
            output.append("".join(text for _, text in row))

        render_help_box(printer=output.append, formatted_printer=rich_print, terminal_width=80)

        text = "\n".join(output)
        self.assertTrue(all(get_cwidth(line) == 80 for line in output))
        self.assertEqual(sum(1 for line in output if line.startswith("┌")), 1)
        self.assertEqual(sum(1 for line in output if line.startswith("└")), 1)
        self.assertIn("可用命令", text)
        self.assertIn("示例", text)
        self.assertIn("/screen", text)
        self.assertIn("/screen --trade-date 20260514 --rule monthly_base_breakout", text)
        self.assertIn("/dsa", text)
        self.assertIn("全 A 股筛选", text)
        self.assertIn("/save", text)
        self.assertIn("保存上一条输出", text)
        self.assertIn("快捷键", text)
        self.assertIn("Ctrl+C", text)
        self.assertIn("中断当前执行", text)
        self.assertIn("/indicators --symbols 000001.SZ --trade-date 20260514", text)
        self.assertTrue(any(style == MUTED_STYLE and "可用命令" in value for row in formatted for style, value in row))
        self.assertTrue(any(style == MUTED_STYLE and "示例" in value for row in formatted for style, value in row))
        self.assertTrue(any(style == MUTED_STYLE and "快捷键" in value for row in formatted for style, value in row))
        self.assertTrue(any(style == COMMAND_STYLE and "/help" in value for row in formatted for style, value in row))
        self.assertTrue(any(style == DESC_STYLE and "查看命令" in value for row in formatted for style, value in row))
        self.assertEqual(COMMAND_STYLE, DESC_STYLE)

        help_line = next(line for line in output if "/help" in line)
        analyze_line = next(line for line in output if "/analyze-dsa" in line)
        self.assertEqual(help_line.index("/help"), analyze_line.index("/analyze-dsa"))
        self.assertEqual(help_line.index("查看命令"), analyze_line.index("分析 DSA 股票"))

    def test_help_box_truncates_to_narrow_terminal_width(self) -> None:
        output: list[str] = []

        render_help_box(
            printer=output.append,
            formatted_printer=lambda fragments: output.append("".join(text for _, text in fragments)),
            terminal_width=42,
        )

        self.assertTrue(all(get_cwidth(line) == 42 for line in output))
        self.assertTrue(any("..." in line for line in output))

    def test_run_repl_prints_input_separators_and_llm_status_bar(self) -> None:
        settings = SimpleNamespace(db_path=Path("data/sats.duckdb"), llm_provider="openai", openai_model="GPT-5.5")
        formatted: list[list[tuple[str, str]]] = []
        clock = [100.0]

        with (
            patch("sats.repl.load_settings", return_value=settings),
            patch("sats.repl.time.monotonic", side_effect=lambda: clock[0]),
            patch("sats.repl.PromptSession") as prompt_session,
            patch("sats.repl.print_formatted_text", side_effect=lambda fragments: formatted.append(list(fragments))),
            patch("sats.repl.shutil.get_terminal_size", return_value=SimpleNamespace(columns=40)),
            patch("builtins.print") as printer,
        ):
            prompt_session.return_value.prompt.return_value = "/exit"

            self.assertEqual(run_repl(), 0)
            bottom_toolbar = prompt_session.call_args.kwargs["bottom_toolbar"]
            bottom_toolbar_text = fragment_list_to_text(bottom_toolbar())

        printed = [call.args[0] for call in printer.call_args_list if call.args]
        prompt_session.return_value.prompt.assert_called_once_with(PROMPT_MESSAGE)
        self.assertEqual(PROMPT_MESSAGE[0], ("", "sats> "))
        self.assertNotIn("─" * 72, printed)
        separators = [
            row for row in formatted
            if len(row) == 1 and row[0][0] == SEPARATOR_STYLE and set(row[0][1]) == {"─"}
        ]
        self.assertEqual(len(separators), 2)
        self.assertTrue(all(get_cwidth(row[0][1]) == 40 for row in separators))
        self.assertTrue(callable(bottom_toolbar))
        self.assertEqual(
            bottom_toolbar_text,
            " OpenAI:GPT-5.5  ｜  0.0s  ｜  Last：--",
        )
        completer = prompt_session.call_args.kwargs["completer"]
        self.assertEqual(completer.meta_dict["/discover"], "短线机会发现")
        self.assertNotIn("input_processors", prompt_session.call_args.kwargs)

    def test_run_repl_ctrl_c_at_prompt_keeps_existing_input_behavior(self) -> None:
        settings = SimpleNamespace(db_path=Path("data/sats.duckdb"), llm_provider="openai", openai_model="GPT-5.5")

        with (
            patch("sats.repl.load_settings", return_value=settings),
            patch("sats.repl.time.monotonic", return_value=100.0),
            patch("sats.repl.PromptSession") as prompt_session,
            patch("sats.repl.print_formatted_text"),
            patch("sats.repl.shutil.get_terminal_size", return_value=SimpleNamespace(columns=40)),
            patch("builtins.print") as printer,
        ):
            prompt_session.return_value.prompt.side_effect = [KeyboardInterrupt, "/exit"]

            self.assertEqual(run_repl(), 0)

        self.assertEqual(prompt_session.return_value.prompt.call_count, 2)
        prompt_session.return_value.prompt.assert_called_with(PROMPT_MESSAGE)
        printed = [call.args[0] for call in printer.call_args_list if call.args]
        self.assertIn("^C", printed)
        self.assertIn("bye", printed)

    def test_repl_status_toolbar_tracks_elapsed_and_last_duration(self) -> None:
        settings = SimpleNamespace(llm_provider="deepseek", openai_model="deepseek-v4-pro")
        output: list[str] = []
        clock = [0.0]

        with patch("sats.repl.time.monotonic", side_effect=lambda: clock[0]):
            state = ReplState()
            toolbar = _status_toolbar(settings, state)
            self.assertEqual(
                fragment_list_to_text(toolbar()),
                " DeepSeek:deepseek-v4-pro  ｜  0.0s  ｜  Last：--",
            )

            clock[0] = 10.0

            def runner(argv):
                output.append(" ".join(argv))
                clock[0] = 53.2
                return 0

            self.assertTrue(handle_repl_line("/results", runner=runner, printer=output.append, state=state))

            self.assertEqual(output, ["results"])
            self.assertEqual(
                fragment_list_to_text(toolbar()),
                " DeepSeek:deepseek-v4-pro  ｜  53.2s  ｜  Last：43.2s",
            )

    def test_repl_command_keyboard_interrupt_returns_to_prompt_without_saving_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InteractionHistoryStore(Path(tmp) / "sats.duckdb")
            output: list[str] = []
            clock = [10.0]
            state = ReplState(
                last_output=CapturedOutput(content="previous", request="/results", source="/results"),
                last_stock_output=CapturedOutput(content="1. 000001.SZ", request="/results", source="/results"),
                history_store=store,
            )

            def runner(_argv):
                clock[0] = 14.5
                raise KeyboardInterrupt

            with patch("sats.repl.time.monotonic", side_effect=lambda: clock[0]):
                keep_running = handle_repl_line("/results", runner=runner, printer=output.append, state=state)

            records = store.list_records(limit=10)
            self.assertTrue(keep_running)
            self.assertEqual(output, [INTERRUPT_MESSAGE])
            self.assertEqual(state.last_output.content, "previous")
            self.assertEqual(state.last_stock_output.content, "1. 000001.SZ")
            self.assertEqual(state.last_duration_seconds, 4.5)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].status, "interrupted")
            self.assertEqual(records[0].output, INTERRUPT_MESSAGE)

    def test_repl_status_toolbar_keeps_last_for_empty_and_exit(self) -> None:
        settings = SimpleNamespace(llm_provider="openai", openai_model="gpt-4o-mini")
        output: list[str] = []
        clock = [0.0]

        with patch("sats.repl.time.monotonic", side_effect=lambda: clock[0]):
            state = ReplState()
            toolbar = _status_toolbar(settings, state)
            state.last_duration_seconds = 5.0
            clock[0] = 20.0

            self.assertTrue(handle_repl_line("  ", printer=output.append, state=state))
            self.assertFalse(handle_repl_line("/exit", printer=output.append, state=state))

            self.assertEqual(output, ["bye"])
            self.assertEqual(
                fragment_list_to_text(toolbar()),
                " OpenAI:gpt-4o-mini  ｜  20.0s  ｜  Last：5.0s",
            )

    def test_repl_completer_shows_command_descriptions(self) -> None:
        completions = _completion_map("/d")
        save_completions = _completion_map("/s")

        self.assertIn("/discover", completions)
        self.assertEqual(completions["/discover"].text, "/discover")
        self.assertEqual(_completion_meta(completions["/discover"]), "短线机会发现")
        self.assertGreaterEqual(get_cwidth(completions["/discover"].display_text), COMPLETION_LIST_MIN_WIDTH)
        self.assertEqual(_completion_meta(save_completions["/save"]), "保存上一条输出")

    def test_repl_completion_menu_uses_unified_command_and_meta_colors(self) -> None:
        rules = dict(REPL_STYLE.style_rules)

        self.assertEqual(rules["completion-menu.completion"], COMPLETION_MENU_STYLE)
        self.assertEqual(rules["completion-menu.meta.completion"], COMPLETION_MENU_STYLE)
        self.assertEqual(rules["completion-menu.completion.current"], COMPLETION_MENU_SELECTED_STYLE)
        self.assertEqual(rules["completion-menu.meta.completion.current"], COMPLETION_MENU_SELECTED_STYLE)

    def test_repl_completer_shows_parameter_and_subcommand_descriptions(self) -> None:
        param_completions = _completion_map("--s")
        hot_completions = _completion_map("--hot")
        subcommand_completions = _completion_map("ad")

        self.assertEqual(_completion_meta(param_completions["--signals"]), "信号策略列表")
        self.assertEqual(_completion_meta(hot_completions["--hot-sector-days"]), "热点板块天数")
        self.assertEqual(_completion_meta(_completion_map("--fo")["--format"]), "保存格式")
        self.assertEqual(_completion_meta(subcommand_completions["add"]), "新增记录")

    def test_completion_words_are_derived_from_descriptions(self) -> None:
        self.assertEqual(COMPLETION_WORDS, list(COMPLETION_DESCRIPTIONS))
        self.assertIn("/screen", COMPLETION_WORDS)
        self.assertIn("/save", COMPLETION_WORDS)
        self.assertIn("--trade-date", COMPLETION_WORDS)
        self.assertIn("--format", COMPLETION_WORDS)
        self.assertIn("--no-hot-sector", COMPLETION_WORDS)
        self.assertIn("--knowledge", COMPLETION_WORDS)
        self.assertIn("watchlist", COMPLETION_WORDS)
        self.assertIn("ingest", COMPLETION_WORDS)
        self.assertIn("sync-stock-basic", COMPLETION_WORDS)

    def test_repl_command_to_argv_strips_slash_and_preserves_args(self) -> None:
        argv = repl_command_to_argv("/results --trade-date 20260514 --passed")

        self.assertEqual(argv, ["results", "--trade-date", "20260514", "--passed"])

    def test_repl_line_runs_existing_command(self) -> None:
        calls: list[list[str]] = []
        output: list[str] = []

        keep_running = handle_repl_line(
            "/results --passed",
            runner=lambda argv: calls.append(argv) or 0,
            printer=output.append,
        )

        self.assertTrue(keep_running)
        self.assertEqual(calls, [["results", "--passed"]])
        self.assertEqual(output, [])

    def test_repl_allows_analyze_dsa_command(self) -> None:
        calls: list[list[str]] = []

        keep_running = handle_repl_line(
            "/analyze-dsa --trade-date 20260514",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertIn("analyze-dsa", CLI_COMMANDS)
        self.assertNotIn("analyze-screened", CLI_COMMANDS)
        self.assertIn("/analyze-dsa", help_text())
        self.assertNotIn("/analyze-screened", help_text())
        self.assertEqual(calls, [["analyze-dsa", "--trade-date", "20260514"]])

    def test_repl_allows_analyze_commands_with_stocks(self) -> None:
        calls: list[list[str]] = []

        keep_running = handle_repl_line(
            "/analyze-chan --stocks 000001 --chan-rule chan-signals",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertIn("/analyze-dsa --stocks 000001,600519", help_text())
        self.assertEqual(calls, [["analyze-chan", "--stocks", "000001", "--chan-rule", "chan-signals"]])

    def test_repl_allows_unified_analyze_command(self) -> None:
        calls: list[list[str]] = []

        keep_running = handle_repl_line(
            "/analyze --stocks 000938 --signals ma_kline,chan --noreport",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertIn("analyze", CLI_COMMANDS)
        self.assertIn("/analyze --stocks 000938 --signals ma_kline,chan", help_text())
        self.assertEqual(calls, [["analyze", "--stocks", "000938", "--signals", "ma_kline,chan", "--noreport"]])

    def test_repl_allows_discover_command(self) -> None:
        calls: list[list[str]] = []

        keep_running = handle_repl_line(
            "/discover --signals short_up --limit 5",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertIn("discover", CLI_COMMANDS)
        self.assertIn("/discover --limit 5", help_text())
        self.assertEqual(calls, [["discover", "--signals", "short_up", "--limit", "5"]])

    def test_repl_allows_dsa_command(self) -> None:
        calls: list[list[str]] = []

        keep_running = handle_repl_line(
            "/dsa --stocks 000001 --trade-date 20260514",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertIn("dsa", CLI_COMMANDS)
        self.assertIn("/dsa", help_text())
        self.assertEqual(calls, [["dsa", "--stocks", "000001", "--trade-date", "20260514"]])

    def test_repl_allows_dsa_from_screened_command(self) -> None:
        calls: list[list[str]] = []

        keep_running = handle_repl_line(
            "/dsa --from-screened --trade-date 20260518 --rule chan-composite",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertIn("/dsa --from-screened", help_text())
        self.assertIn("--explain-rating", help_text())
        self.assertEqual(
            calls,
            [["dsa", "--from-screened", "--trade-date", "20260518", "--rule", "chan-composite"]],
        )

    def test_repl_allows_chan_kb_command(self) -> None:
        calls: list[list[str]] = []
        output: list[str] = []

        keep_running = handle_repl_line(
            "/chan-kb search 三买",
            runner=lambda argv: calls.append(argv) or 0,
            printer=output.append,
        )

        self.assertTrue(keep_running)
        self.assertIn("chan-kb", CLI_COMMANDS)
        self.assertIn("/chan-kb", help_text())
        self.assertEqual(calls, [["chan-kb", "search", "三买"]])
        self.assertEqual(output, [])

    def test_repl_non_slash_input_calls_llm_chat(self) -> None:
        output: list[str] = []
        chat_session = SimpleNamespace(
            ask=lambda message: ChatResult(content=f"回答: {message}", skill_names=("sats-market-assistant",))
        )

        keep_running = handle_repl_line("帮我解释筛选规则", chat_session=chat_session, printer=output.append)

        self.assertTrue(keep_running)
        self.assertEqual(len(output), 1)
        self.assertIn("`skill: sats-market-assistant`", output[0])
        self.assertIn("> 回答: 帮我解释筛选规则", output[0])

    def test_repl_records_non_slash_chat_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InteractionHistoryStore(Path(tmp) / "sats.duckdb")
            state = ReplState(history_store=store)
            output: list[str] = []
            chat_session = SimpleNamespace(ask=lambda message: ChatResult(content=f"回答: {message}", skill_names=()))

            keep_running = handle_repl_line("帮我解释筛选规则", chat_session=chat_session, printer=output.append, state=state)

            records = store.list_records(limit=10)
            self.assertTrue(keep_running)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].kind, "chat")
            self.assertEqual(records[0].status, "done")
            self.assertEqual(records[0].request, "帮我解释筛选规则")
            self.assertIn("回答: 帮我解释筛选规则", records[0].output)

    def test_repl_natural_chan_stock_question_uses_chat_context(self) -> None:
        calls: list[list[str]] = []
        output: list[str] = []
        chat_calls: list[str] = []
        chat_session = SimpleNamespace(
            ask=lambda message: chat_calls.append(message) or ChatResult(content="真实数据分析", skill_names=("chan-theory",))
        )

        keep_running = handle_repl_line(
            "用缠论分析002436",
            runner=lambda argv: calls.append(argv) or 0,
            chat_session=chat_session,
            printer=output.append,
        )

        self.assertTrue(keep_running)
        self.assertEqual(calls, [])
        self.assertEqual(chat_calls, ["用缠论分析002436"])
        self.assertEqual(len(output), 1)
        self.assertIn("`skill: chan-theory`", output[0])
        self.assertIn("> 真实数据分析", output[0])

    def test_repl_stock_question_with_date_uses_chat_context(self) -> None:
        calls: list[list[str]] = []
        chat_calls: list[str] = []

        keep_running = handle_repl_line(
            "分析002436 2026-05-15",
            runner=lambda argv: calls.append(argv) or 0,
            chat_session=SimpleNamespace(
                ask=lambda message: chat_calls.append(message) or ChatResult(content="回答", skill_names=())
            ),
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertEqual(calls, [])
        self.assertEqual(chat_calls, ["分析002436 2026-05-15"])

    def test_repl_indicator_stock_question_with_date_uses_chat_context(self) -> None:
        calls: list[list[str]] = []
        chat_calls: list[str] = []

        keep_running = handle_repl_line(
            "看002436 MACD 20260515",
            runner=lambda argv: calls.append(argv) or 0,
            chat_session=SimpleNamespace(
                ask=lambda message: chat_calls.append(message) or ChatResult(content="回答", skill_names=())
            ),
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertEqual(calls, [])
        self.assertEqual(chat_calls, ["看002436 MACD 20260515"])

    def test_repl_intraday_natural_stock_analysis_uses_chat_context(self) -> None:
        calls: list[list[str]] = []
        output: list[str] = []
        chat_calls: list[str] = []

        keep_running = handle_repl_line(
            "分析002436 20260515 10:30",
            runner=lambda argv: calls.append(argv) or 0,
            chat_session=SimpleNamespace(
                ask=lambda message: chat_calls.append(message) or ChatResult(content="回答", skill_names=())
            ),
            printer=output.append,
        )

        self.assertTrue(keep_running)
        self.assertEqual(calls, [])
        self.assertEqual(chat_calls, ["分析002436 20260515 10:30"])
        self.assertEqual(len(output), 1)
        self.assertIn("# SATS 自然对话输出", output[0])
        self.assertIn("> 回答", output[0])

    def test_repl_chat_slash_command_calls_llm_chat(self) -> None:
        output: list[str] = []
        calls: list[str] = []
        chat_session = SimpleNamespace(
            ask=lambda message: calls.append(message) or ChatResult(content="回答", skill_names=())
        )

        keep_running = handle_repl_line("/chat hello world", chat_session=chat_session, printer=output.append)

        self.assertTrue(keep_running)
        self.assertEqual(calls, ["hello world"])
        self.assertEqual(len(output), 1)
        self.assertIn("# SATS 自然对话输出", output[0])
        self.assertIn("> 回答", output[0])

    def test_repl_new_command_creates_chat_session_and_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            current = ChatSession(
                settings=settings,
                skills=[],
                llm_factory=ReplFakeLLM,
                memory_extractor=SimpleNamespace(extract=lambda *args, **kwargs: []),
                preprocess_enabled=False,
            )
            state = ReplState(
                session_id=current.session_id,
                chat_session=current,
                history_store=InteractionHistoryStore(settings.db_path),
            )
            output: list[str] = []

            keep_running = handle_repl_line("/new 茅台估值复盘", chat_session=current, printer=output.append, state=state)

            self.assertTrue(keep_running)
            self.assertIsInstance(state.chat_session, ChatSession)
            self.assertNotEqual(state.chat_session.session_id, current.session_id)
            self.assertEqual(state.session_id, state.chat_session.session_id)
            self.assertIn("茅台估值复盘", output[0])
            store = ChatMemoryStore(settings.db_path)
            with store.storage.connect() as con:
                row = con.execute(
                    "SELECT title FROM chat_sessions WHERE session_id = ?",
                    [state.chat_session.session_id],
                ).fetchone()
            self.assertEqual(row[0], "茅台估值复盘")

    def test_repl_new_session_resets_stock_followup_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            current = ChatSession(
                settings=settings,
                skills=[],
                llm_factory=ReplFakeLLM,
                memory_enabled=False,
                preprocess_enabled=False,
            )
            current._last_stock_question = SimpleNamespace(
                symbols=["000001.SZ"],
                trade_date="20260514",
                as_of_time=None,
                has_stock_question=True,
            )
            state = ReplState(session_id=current.session_id, chat_session=current)
            output: list[str] = []

            self.assertTrue(handle_repl_line("/new", chat_session=current, printer=output.append, state=state))
            self.assertTrue(handle_repl_line("继续分析它", chat_session=current, printer=output.append, state=state))

            self.assertNotIn("000001.SZ", output[-1])
            self.assertIn("#", output[-1])

    def test_repl_records_chat_slash_command_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InteractionHistoryStore(Path(tmp) / "sats.duckdb")
            state = ReplState(history_store=store)
            output: list[str] = []
            chat_session = SimpleNamespace(ask=lambda message: ChatResult(content="回答", skill_names=()))

            keep_running = handle_repl_line("/chat hello world", chat_session=chat_session, printer=output.append, state=state)

            records = store.list_records(kind="chat", limit=10)
            self.assertTrue(keep_running)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].request, "hello world")
            self.assertEqual(records[0].source, "chat")
            self.assertIn("# SATS 自然对话输出", records[0].output)
            self.assertIn("> 回答", records[0].output)

    def test_repl_chat_keyboard_interrupt_returns_to_prompt_without_saving_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InteractionHistoryStore(Path(tmp) / "sats.duckdb")
            output: list[str] = []
            clock = [20.0]
            state = ReplState(
                last_output=CapturedOutput(content="previous chat", request="hello", source="chat"),
                last_stock_output=CapturedOutput(content="1. 000001.SZ", request="/results", source="/results"),
                history_store=store,
            )

            class InterruptingChatSession:
                def ask(self, message, **kwargs):
                    clock[0] = 23.25
                    raise KeyboardInterrupt

            with patch("sats.repl.time.monotonic", side_effect=lambda: clock[0]):
                keep_running = handle_repl_line(
                    "hello world",
                    chat_session=InterruptingChatSession(),
                    printer=output.append,
                    state=state,
                )

            records = store.list_records(limit=10)
            self.assertTrue(keep_running)
            self.assertEqual(output, [INTERRUPT_MESSAGE])
            self.assertEqual(state.last_output.content, "previous chat")
            self.assertEqual(state.last_stock_output.content, "1. 000001.SZ")
            self.assertEqual(state.last_duration_seconds, 3.25)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].kind, "chat")
            self.assertEqual(records[0].status, "interrupted")

    def test_repl_defers_memory_updates_for_real_chat_session(self) -> None:
        output: list[str] = []
        chat_session = RecordingChatSession()

        keep_running = handle_repl_line("hello world", chat_session=chat_session, printer=output.append)

        self.assertTrue(keep_running)
        self.assertEqual(len(output), 1)
        self.assertIn("# SATS 自然对话输出", output[0])
        self.assertIn("> 回答", output[0])
        self.assertEqual(chat_session.calls[0][0], "hello world")
        self.assertTrue(chat_session.calls[0][1]["defer_memory_updates"])

    def test_repl_chat_slash_command_allows_stock_analysis_with_context(self) -> None:
        output: list[str] = []
        calls: list[str] = []
        chat_session = SimpleNamespace(
            ask=lambda message, **kwargs: calls.append(message) or ChatResult(content="回答", skill_names=())
        )

        keep_running = handle_repl_line("/chat 分析002436", chat_session=chat_session, printer=output.append)

        self.assertTrue(keep_running)
        self.assertEqual(calls, ["分析002436"])
        self.assertEqual(len(output), 1)
        self.assertIn("# SATS 自然对话输出", output[0])
        self.assertIn("> 回答", output[0])

    def test_repl_chat_slash_command_can_disable_memory(self) -> None:
        output: list[str] = []
        calls: list[tuple[str, bool | None]] = []

        class FakeChatSession:
            def ask(self, message, *, use_memory=None):
                calls.append((message, use_memory))
                return ChatResult(content="回答", skill_names=())

        keep_running = handle_repl_line(
            "/chat --no-memory hello world",
            chat_session=FakeChatSession(),
            printer=output.append,
        )

        self.assertTrue(keep_running)
        self.assertEqual(calls, [("hello world", False)])
        self.assertEqual(len(output), 1)
        self.assertIn("# SATS 自然对话输出", output[0])
        self.assertIn("> 回答", output[0])

    def test_repl_trace_builtin_prints_latest_turn_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            store = ChatMemoryStore(settings.db_path)
            store.start_chat_turn(turn_id="turn_trace", session_id="repl", request="hello")
            state = ReplState(session_id="repl", history_store=InteractionHistoryStore(settings.db_path))
            output: list[str] = []

            with patch("sats.repl.load_settings", return_value=settings):
                keep_running = handle_repl_line("/trace", printer=output.append, state=state)

            self.assertTrue(keep_running)
            self.assertIn("Turn: turn_trace", output[-1])

    def test_repl_confirm_builtin_executes_pending_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            state = ReplState(session_id="repl", history_store=InteractionHistoryStore(settings.db_path))
            output: list[str] = []
            runtime_result = SimpleNamespace(
                content="已执行",
                skill_names=(),
                tool_call_count=0,
                data_names=("Runtime",),
                artifacts=(),
                pending_action=None,
            )

            with patch("sats.repl.load_settings", return_value=settings), patch(
                "sats.repl.confirm_pending_runtime_action", return_value=runtime_result
            ) as confirm:
                keep_running = handle_repl_line("/confirm act_123", printer=output.append, state=state)

            self.assertTrue(keep_running)
            confirm.assert_called_once()
            self.assertIn("# SATS 执行结果", output[-1])
            self.assertIn("`数据: Runtime`", output[-1])
            self.assertIn("> 已执行", output[-1])

    def test_repl_chat_passes_reference_context_from_last_output(self) -> None:
        state = ReplState()
        state.last_output = CapturedOutput(
            content="1. 000001.SZ 平安银行 price_volume_ma",
            request="/results --trade-date 20260522 --passed",
            source="/results",
        )
        output: list[str] = []
        calls: list[tuple[str, object | None]] = []
        reference_context = SimpleNamespace(
            system_message="上一条筛选结果",
            symbols=["000001.SZ"],
            trade_date="20260522",
            source="/results",
            data_name="筛选结果",
        )

        class FakeChatSession:
            settings = SimpleNamespace(db_path=Path("test.duckdb"))

            def ask(self, message, **kwargs):
                calls.append((message, kwargs.get("reference_context")))
                return ChatResult(content="回答", skill_names=(), data_names=("筛选结果",))

        with patch("sats.repl.build_chat_reference_context", return_value=reference_context) as reference_builder:
            self.assertTrue(
                handle_repl_line(
                    "分析上面列表，选出最高评分的5支",
                    chat_session=FakeChatSession(),
                    printer=output.append,
                    state=state,
                )
            )

        reference_builder.assert_called_once()
        self.assertEqual(calls, [("分析上面列表，选出最高评分的5支", reference_context)])
        self.assertEqual(len(output), 1)
        self.assertIn("`数据: 筛选结果`", output[0])
        self.assertIn("> 回答", output[0])

    def test_repl_chat_falls_back_to_recent_stock_output_context(self) -> None:
        state = ReplState()
        output: list[str] = []

        def results_runner(argv):
            print("1. 000001.SZ 平安银行 price_volume_ma")
            return 0

        self.assertTrue(handle_repl_line("/results --passed", runner=results_runner, state=state, printer=output.append))
        self.assertIsNotNone(state.last_stock_output)

        calls: list[object | None] = []

        class FakeChatSession:
            settings = SimpleNamespace(db_path=Path("missing.duckdb"))

            def ask(self, message, **kwargs):
                calls.append(kwargs.get("reference_context"))
                return ChatResult(content="没有股票" if len(calls) == 1 else "报价", skill_names=())

        session = FakeChatSession()
        self.assertTrue(handle_repl_line("普通问答", chat_session=session, printer=output.append, state=state))
        self.assertTrue(handle_repl_line("查看刚才股票实时报价", chat_session=session, printer=output.append, state=state))

        self.assertIsNone(calls[0])
        self.assertIsNotNone(calls[1])
        self.assertEqual(calls[1].symbols, ["000001.SZ"])
        self.assertEqual(calls[1].data_name, "筛选结果")

    def test_repl_chat_uses_screen_output_for_stock_followup_phrase(self) -> None:
        state = ReplState()
        output: list[str] = []

        def screen_runner(argv):
            print("1. 000001.SZ 平安银行 突破")
            print("2. 600519.SH 贵州茅台 放量")
            return 0

        self.assertTrue(
            handle_repl_line(
                "/screen --trade-date 20260527 --rule price_volume_ma",
                runner=screen_runner,
                state=state,
                printer=output.append,
            )
        )
        self.assertIsNotNone(state.last_stock_output)

        calls: list[object | None] = []

        class FakeChatSession:
            settings = SimpleNamespace(db_path=Path("missing.duckdb"))

            def ask(self, message, **kwargs):
                calls.append(kwargs.get("reference_context"))
                return ChatResult(content="分析完成", skill_names=(), data_names=("筛选结果", "个股", "大盘"))

        self.assertTrue(
            handle_repl_line(
                "分析上面的股票，预测短期走势",
                chat_session=FakeChatSession(),
                printer=output.append,
                state=state,
            )
        )

        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0])
        self.assertEqual(calls[0].symbols, ["000001.SZ", "600519.SH"])

    def test_repl_agent_passes_reference_context_from_last_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            state = ReplState(
                chat_session=ChatSession(settings=settings, skills=[], llm_factory=ReplFakeLLM, memory_enabled=False),
                last_output=CapturedOutput(
                    content="1. 002436.SZ 兴森科技\n2. 300276.SZ 三丰智能",
                    request="分析兴森科技和三丰智能",
                    source="agent",
                ),
            )
            reference_context = SimpleNamespace(
                system_message="上一条输出包含两只股票",
                symbols=["002436.SZ", "300276.SZ"],
                trade_date="20260605",
                source="agent",
                data_name="上条输出",
            )
            fake_result = SimpleNamespace(
                content="DSA完成",
                tool_call_count=1,
                data_names=("Agent",),
                skill_names=(),
                artifacts=(),
                turn_id="turn",
                session_id="agent",
            )
            output: list[str] = []

            with (
                patch("sats.repl.build_chat_reference_context", return_value=reference_context) as reference_builder,
                patch("sats.repl.run_agent_once", return_value=fake_result) as run_agent,
            ):
                self.assertTrue(handle_repl_line("上面2个股票用DSA进行分析", printer=output.append, state=state))

            reference_builder.assert_called_once()
            run_agent.assert_called_once()
            self.assertIs(run_agent.call_args.kwargs.get("reference_context"), reference_context)
            self.assertIn("DSA完成", output[-1])

    def test_repl_new_clears_agent_reference_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            current = ChatSession(settings=settings, skills=[], llm_factory=ReplFakeLLM, memory_enabled=False)
            state = ReplState(
                session_id=current.session_id,
                chat_session=current,
                last_output=CapturedOutput(
                    content="1. 002436.SZ 兴森科技\n2. 300276.SZ 三丰智能",
                    request="分析兴森科技和三丰智能",
                    source="agent",
                ),
                last_stock_output=CapturedOutput(
                    content="1. 002436.SZ 兴森科技\n2. 300276.SZ 三丰智能",
                    request="分析兴森科技和三丰智能",
                    source="agent",
                ),
            )
            output: list[str] = []
            fake_result = SimpleNamespace(
                content="需要明确股票",
                tool_call_count=0,
                data_names=("Agent",),
                skill_names=(),
                artifacts=(),
                turn_id="turn",
                session_id="agent",
            )

            self.assertTrue(handle_repl_line("/new", chat_session=current, printer=output.append, state=state))
            self.assertIsNone(state.last_output)
            self.assertIsNone(state.last_stock_output)

            with (
                patch("sats.repl.build_chat_reference_context", return_value=None) as reference_builder,
                patch("sats.repl.run_agent_once", return_value=fake_result) as run_agent,
            ):
                self.assertTrue(handle_repl_line("上面2个股票用DSA进行分析", printer=output.append, state=state))

            reference_builder.assert_called_once()
            self.assertIsNone(reference_builder.call_args.args[1])
            self.assertIsNone(run_agent.call_args.kwargs.get("reference_context"))

    def test_repl_allows_memory_command(self) -> None:
        calls: list[list[str]] = []
        output: list[str] = []

        keep_running = handle_repl_line(
            "/memory search 股票",
            runner=lambda argv: calls.append(argv) or 0,
            printer=output.append,
        )

        self.assertTrue(keep_running)
        self.assertIn("memory", CLI_COMMANDS)
        self.assertIn("/memory search 股票", help_text())
        self.assertEqual(calls, [["memory", "search", "股票"]])
        self.assertEqual(output, [])

    def test_repl_records_slash_command_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InteractionHistoryStore(Path(tmp) / "sats.duckdb")
            state = ReplState(history_store=store)
            output: list[str] = []

            def runner(argv):
                print("1. 000001.SZ")
                return 0

            keep_running = handle_repl_line("/results --passed", runner=runner, printer=output.append, state=state)

            records = store.list_records(kind="command", limit=10)
            self.assertTrue(keep_running)
            self.assertEqual(output, ["1. 000001.SZ"])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].request, "/results --passed")
            self.assertEqual(records[0].source, "/results")
            self.assertEqual(records[0].status, "done")
            self.assertEqual(records[0].output, "1. 000001.SZ")

    def test_repl_records_slash_command_error_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InteractionHistoryStore(Path(tmp) / "sats.duckdb")
            state = ReplState(history_store=store)
            output: list[str] = []

            def runner(_argv):
                raise ValueError("bad input")

            keep_running = handle_repl_line("/results --bad", runner=runner, printer=output.append, state=state)

            records = store.list_records(kind="command", limit=10)
            self.assertTrue(keep_running)
            self.assertEqual(output, ["错误: bad input"])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].status, "error")
            self.assertEqual(records[0].output, "错误: bad input")

    def test_repl_allows_history_command_without_recording_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InteractionHistoryStore(Path(tmp) / "sats.duckdb")
            state = ReplState(history_store=store)
            calls: list[list[str]] = []
            output: list[str] = []

            keep_running = handle_repl_line(
                "/history list",
                runner=lambda argv: calls.append(argv) or print("无历史记录"),
                printer=output.append,
                state=state,
            )

            self.assertTrue(keep_running)
            self.assertIn("history", CLI_COMMANDS)
            self.assertIn("/history list", help_text())
            self.assertEqual(calls, [["history", "list"]])
            self.assertEqual(store.list_records(limit=10), [])

    def test_repl_allows_knowledge_command(self) -> None:
        calls: list[list[str]] = []
        output: list[str] = []

        keep_running = handle_repl_line(
            "/knowledge search --query 三买 --knowledge chan",
            runner=lambda argv: calls.append(argv) or 0,
            printer=output.append,
        )

        self.assertTrue(keep_running)
        self.assertIn("knowledge", CLI_COMMANDS)
        self.assertIn("/knowledge search --query 三买 --knowledge chan", help_text())
        self.assertEqual(calls, [["knowledge", "search", "--query", "三买", "--knowledge", "chan"]])
        self.assertEqual(output, [])

    def test_repl_allows_knowledge_sync_stock_basic_command(self) -> None:
        calls: list[list[str]] = []

        keep_running = handle_repl_line(
            "/knowledge sync-stock-basic",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertIn("/knowledge sync-stock-basic", help_text())
        self.assertEqual(calls, [["knowledge", "sync-stock-basic"]])

    def test_repl_allows_indicators_command(self) -> None:
        calls: list[list[str]] = []
        output: list[str] = []

        keep_running = handle_repl_line(
            "/indicators --symbols 000001.SZ --trade-date 20260514",
            runner=lambda argv: calls.append(argv) or 0,
            printer=output.append,
        )

        self.assertTrue(keep_running)
        self.assertIn("indicators", CLI_COMMANDS)
        self.assertIn("/indicators --symbols 000001.SZ --trade-date 20260514", help_text())
        self.assertEqual(calls, [["indicators", "--symbols", "000001.SZ", "--trade-date", "20260514"]])
        self.assertEqual(output, [])

    def test_repl_allows_factor_command(self) -> None:
        calls: list[list[str]] = []

        keep_running = handle_repl_line(
            "/factor list --zoo barra_style",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertIn("factor", CLI_COMMANDS)
        self.assertIn("/factor list --zoo barra_style", help_text())
        self.assertIn("/factor pick --profile balanced --trade-date 20260514 --top 20", help_text())
        self.assertIn("/factor ml status", help_text())
        self.assertIn("/factor ml train --profile balanced --model lightgbm", help_text())
        self.assertIn("/factor ml predict --model-run factor_ml_xxxxxxxx", help_text())
        self.assertIn("/chat 分析000001的因子暴露", help_text())

        keep_running = handle_repl_line(
            "/factor ml status",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertEqual(calls, [["factor", "list", "--zoo", "barra_style"], ["factor", "ml", "status"]])

    def test_repl_allows_monitor_commands(self) -> None:
        calls: list[list[str]] = []

        self.assertTrue(
            handle_repl_line(
                "/monitor start --rules chan_signals",
                runner=lambda argv: calls.append(argv) or 0,
                printer=lambda _: None,
            )
        )
        self.assertTrue(
            handle_repl_line(
                "/monitor-display start",
                runner=lambda argv: calls.append(argv) or 0,
                printer=lambda _: None,
            )
        )

        self.assertIn("monitor", CLI_COMMANDS)
        self.assertIn("monitor-display", CLI_COMMANDS)
        self.assertIn("/monitor start --rules chan_signals", help_text())
        self.assertIn("/monitor-display start", help_text())
        self.assertEqual(
            calls,
            [
                ["monitor", "start", "--rules", "chan_signals"],
                ["monitor-display", "start"],
            ],
        )

    def test_repl_allows_schedule_commands(self) -> None:
        calls: list[list[str]] = []

        self.assertTrue(
            handle_repl_line(
                "/schedule list",
                runner=lambda argv: calls.append(argv) or 0,
                printer=lambda _: None,
            )
        )
        self.assertTrue(
            handle_repl_line(
                '/schedule add --name daily-discover --type chat --text "推荐股票" --daily --time 08:45',
                runner=lambda argv: calls.append(argv) or 0,
                printer=lambda _: None,
            )
        )

        self.assertIn("schedule", CLI_COMMANDS)
        self.assertIn("/schedule list", help_text())
        self.assertIn("--daily", COMPLETION_DESCRIPTIONS)
        self.assertIn("--weekly", COMPLETION_DESCRIPTIONS)
        self.assertEqual(
            calls,
            [
                ["schedule", "list"],
                ["schedule", "add", "--name", "daily-discover", "--type", "chat", "--text", "推荐股票", "--daily", "--time", "08:45"],
            ],
        )

    def test_repl_allows_model_commands(self) -> None:
        calls: list[list[str]] = []

        self.assertTrue(
            handle_repl_line(
                "/model status",
                runner=lambda argv: calls.append(argv) or 0,
                printer=lambda _: None,
            )
        )
        self.assertTrue(
            handle_repl_line(
                "/model use XIAOMIMIMO --target light",
                runner=lambda argv: calls.append(argv) or 0,
                printer=lambda _: None,
            )
        )

        self.assertIn("model", CLI_COMMANDS)
        self.assertIn("/model status", help_text())
        self.assertIn("--target", COMPLETION_DESCRIPTIONS)
        self.assertEqual(calls, [["model", "status"], ["model", "use", "XIAOMIMIMO", "--target", "light"]])

    def test_repl_allows_watchlist_command(self) -> None:
        calls: list[list[str]] = []
        output: list[str] = []

        self.assertTrue(
            handle_repl_line(
                "/watchlist",
                runner=lambda argv: calls.append(argv) or 0,
                printer=output.append,
            )
        )
        self.assertTrue(
            handle_repl_line(
                "/watchlist add --stocks 000001.SZ",
                runner=lambda argv: calls.append(argv) or 0,
                printer=output.append,
            )
        )
        self.assertTrue(
            handle_repl_line(
                "/watchlist clear",
                runner=lambda argv: calls.append(argv) or 0,
                printer=output.append,
            )
        )

        self.assertIn("watchlist", CLI_COMMANDS)
        self.assertIn("/watchlist", help_text())
        self.assertEqual(
            calls,
            [
                ["watchlist"],
                ["watchlist", "add", "--stocks", "000001.SZ"],
                ["watchlist", "clear"],
            ],
        )
        self.assertEqual(output, [])

    def test_repl_allows_quote_command(self) -> None:
        calls: list[list[str]] = []

        self.assertTrue(
            handle_repl_line(
                "/quote --stocks 000001.SZ",
                runner=lambda argv: calls.append(argv) or 0,
                printer=lambda _: None,
            )
        )

        self.assertIn("quote", CLI_COMMANDS)
        self.assertIn("/quote --stocks 000001,600519", help_text())
        self.assertEqual(_completion_meta(_completion_map("/q")["/quote"]), "实时价格")
        self.assertEqual(calls, [["quote", "--stocks", "000001.SZ"]])

    def test_repl_chat_error_does_not_crash_loop(self) -> None:
        output: list[str] = []

        def fail(_: str):
            raise RuntimeError("llm down")

        keep_running = handle_repl_line(
            "hello",
            chat_session=SimpleNamespace(ask=fail),
            printer=output.append,
        )

        self.assertTrue(keep_running)
        self.assertEqual(output, ["LLM错误: llm down"])

    def test_repl_help_and_unknown_commands_do_not_execute(self) -> None:
        output: list[str] = []
        calls: list[list[str]] = []

        self.assertTrue(
            handle_repl_line("/help", runner=lambda argv: calls.append(argv) or 0, printer=output.append)
        )
        self.assertTrue(
            handle_repl_line("/unknown", runner=lambda argv: calls.append(argv) or 0, printer=output.append)
        )
        self.assertTrue(
            handle_repl_line(
                "/minute-k --symbols 000001.SZ",
                runner=lambda argv: calls.append(argv) or 0,
                printer=output.append,
            )
        )

        self.assertEqual(calls, [])
        self.assertTrue(output[0].startswith("┌"))
        self.assertTrue(any("/screen" in line for line in output))
        self.assertTrue(any("/chat" in line for line in output))
        self.assertFalse(any("/minute-k" in line for line in output[0].splitlines()))
        self.assertIn("未知命令", output[-1])

    def test_repl_exit_and_quit_stop_loop(self) -> None:
        output: list[str] = []

        self.assertFalse(handle_repl_line("/exit", printer=output.append))
        self.assertFalse(handle_repl_line("/quit", printer=output.append))

        self.assertEqual(output, ["bye", "bye"])

    def test_repl_system_exit_does_not_crash_loop(self) -> None:
        output: list[str] = []

        def fail(_: list[str]) -> int:
            raise SystemExit("boom")

        keep_running = handle_repl_line("/results", runner=fail, printer=output.append)

        self.assertTrue(keep_running)
        self.assertEqual(output, ["错误: boom"])

    def test_repl_natural_language_saves_current_chat_output_as_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ReplState()
            output: list[str] = []
            settings = SimpleNamespace(project_root=Path(tmp))
            chat_session = SimpleNamespace(
                ask=lambda message: ChatResult(content=f"回答: {message}", skill_names=())
            )

            with patch("sats.repl.load_settings", return_value=settings):
                keep_running = handle_repl_line(
                    "帮我解释 price_volume_ma，并保存结果为MD",
                    chat_session=chat_session,
                    printer=output.append,
                    state=state,
                )

            self.assertTrue(keep_running)
            self.assertIn("> 回答: 帮我解释 price_volume_ma", output[0])
            self.assertTrue(output[-1].startswith("已保存: "))
            saved_path = Path(output[-1].split("已保存: ", 1)[1])
            self.assertEqual(saved_path.suffix, ".md")
            saved_text = saved_path.read_text(encoding="utf-8")
            self.assertIn("回答: 帮我解释 price_volume_ma", saved_text)
            self.assertNotIn("保存结果", saved_text)

    def test_repl_pure_natural_language_save_uses_last_output_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ReplState()
            state.last_output = None
            output: list[str] = []
            settings = SimpleNamespace(project_root=Path(tmp))
            calls: list[str] = []
            chat_session = SimpleNamespace(
                ask=lambda message: calls.append(message) or ChatResult(content="不应调用", skill_names=())
            )

            with patch("sats.repl.load_settings", return_value=settings):
                self.assertTrue(
                    handle_repl_line(
                        "上一个输出到出到markdown文件",
                        chat_session=chat_session,
                        printer=output.append,
                        state=state,
                    )
                )

            self.assertEqual(output, ["没有可保存的上一条输出。"])
            self.assertEqual(calls, [])

            state.last_output = CapturedOutput(
                content="上一条回答",
                request="上一条问题",
                source="chat",
                report_path=None,
            )
            output.clear()
            with patch("sats.repl.load_settings", return_value=settings):
                self.assertTrue(
                    handle_repl_line(
                        "上一个输出到出到markdown文件",
                        chat_session=chat_session,
                        printer=output.append,
                        state=state,
                    )
                )

            self.assertEqual(calls, [])
            self.assertTrue(output[-1].startswith("已保存: "))
            saved_path = Path(output[-1].split("已保存: ", 1)[1])
            self.assertEqual(saved_path.suffix, ".md")
            self.assertTrue(saved_path.exists())
            self.assertIn("上一条回答", saved_path.read_text(encoding="utf-8"))

    def test_repl_output_as_pdf_uses_last_output_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ReplState()
            output: list[str] = []
            settings = SimpleNamespace(project_root=Path(tmp))
            calls: list[str] = []
            chat_session = SimpleNamespace(
                ask=lambda message: calls.append(message) or ChatResult(content="不应调用", skill_names=())
            )

            with patch("sats.repl.load_settings", return_value=settings):
                self.assertTrue(
                    handle_repl_line(
                        "上面内容输出为PDF",
                        chat_session=chat_session,
                        printer=output.append,
                        state=state,
                    )
                )

            self.assertEqual(output, ["没有可保存的上一条输出。"])
            self.assertEqual(calls, [])

            state.last_output = CapturedOutput(
                content="上一条回答",
                request="上一条问题",
                source="chat",
                report_path=None,
            )
            output.clear()
            with patch("sats.repl.load_settings", return_value=settings):
                self.assertTrue(
                    handle_repl_line(
                        "上面内容输出为PDF",
                        chat_session=chat_session,
                        printer=output.append,
                        state=state,
                    )
                )

            self.assertEqual(calls, [])
            self.assertTrue(output[-1].startswith("已保存: "))
            saved_path = Path(output[-1].split("已保存: ", 1)[1])
            self.assertEqual(saved_path.suffix, ".pdf")
            self.assertTrue(saved_path.exists())

    def test_repl_compound_output_as_pdf_saves_current_chat_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ReplState()
            output: list[str] = []
            settings = SimpleNamespace(project_root=Path(tmp))
            calls: list[str] = []
            chat_session = SimpleNamespace(
                ask=lambda message: calls.append(message) or ChatResult(content=f"回答: {message}", skill_names=())
            )

            with patch("sats.repl.load_settings", return_value=settings):
                self.assertTrue(
                    handle_repl_line(
                        "分析 000938 技术面，输出为PDF",
                        chat_session=chat_session,
                        printer=output.append,
                        state=state,
                    )
                )

            self.assertEqual(calls, ["分析 000938 技术面"])
            self.assertIn("> 回答: 分析 000938 技术面", output[0])
            self.assertTrue(output[-1].startswith("已保存: "))
            saved_path = Path(output[-1].split("已保存: ", 1)[1])
            self.assertEqual(saved_path.suffix, ".pdf")
            self.assertTrue(saved_path.exists())

    def test_repl_save_command_saves_last_slash_output_and_does_not_overwrite_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ReplState()
            output: list[str] = []
            settings = SimpleNamespace(project_root=Path(tmp))

            def runner(argv: list[str]) -> int:
                print("1. 000001.SZ 平安银行")
                return 0

            self.assertTrue(handle_repl_line("/results --passed", runner=runner, printer=output.append, state=state))
            with patch("sats.repl.load_settings", return_value=settings):
                self.assertTrue(handle_repl_line("/save --format md", printer=output.append, state=state))
                self.assertTrue(handle_repl_line("/save --format md", printer=output.append, state=state))

            saved_paths = [Path(line.split("已保存: ", 1)[1]) for line in output if line.startswith("已保存: ")]
            self.assertEqual(len(saved_paths), 2)
            self.assertIn("1. 000001.SZ 平安银行", saved_paths[0].read_text(encoding="utf-8"))
            self.assertNotIn("已保存:", saved_paths[1].read_text(encoding="utf-8"))

    def test_repl_slash_output_with_default_print_does_not_recurse_and_is_captured(self) -> None:
        state = ReplState()

        def runner(argv: list[str]) -> int:
            print("1. 000001.SZ 平安银行")
            return 0

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertTrue(handle_repl_line("/results --passed", runner=runner, state=state))

        self.assertIn("1. 000001.SZ 平安银行", stdout.getvalue())
        self.assertIsNotNone(state.last_output)
        self.assertIn("1. 000001.SZ 平安银行", state.last_output.content)

    def test_repl_slash_output_supports_isatty_checks(self) -> None:
        state = ReplState()

        def runner(argv: list[str]) -> int:
            if sys.stdout.isatty():
                print("TTY")
            else:
                print("NOTTY")
            return 0

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertTrue(handle_repl_line("/screen --trade-date 20260526 --rule price_volume_ma", runner=runner, state=state))

        self.assertIn("NOTTY", stdout.getvalue())
        self.assertIsNotNone(state.last_output)
        self.assertIn("NOTTY", state.last_output.content)

    def test_tee_stdout_proxies_terminal_methods_and_attributes(self) -> None:
        class _FakeStdout:
            encoding = "utf-8"
            errors = "strict"

            def __init__(self) -> None:
                self.buffer = object()

            def write(self, text: str) -> int:
                return len(text)

            def flush(self) -> None:
                return None

            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 7

            def writable(self) -> bool:
                return False

        tee = _TeeStdout(lambda line: None, io.StringIO(), original_stdout=_FakeStdout())

        self.assertTrue(tee.isatty())
        self.assertEqual(tee.fileno(), 7)
        self.assertFalse(tee.writable())
        self.assertEqual(tee.encoding, "utf-8")
        self.assertEqual(tee.errors, "strict")
        self.assertIs(tee.buffer, tee.original_stdout.buffer)

    def test_repl_save_report_source_reads_report_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "source_report.md"
            report.write_text("# 原始报告\n\n报告正文\n", encoding="utf-8")
            state = ReplState()
            output: list[str] = []
            settings = SimpleNamespace(project_root=root)

            def runner(argv: list[str]) -> int:
                print("终端摘要")
                print(f"报告: {report}")
                return 0

            self.assertTrue(handle_repl_line("/discover", runner=runner, printer=output.append, state=state))
            with patch("sats.repl.load_settings", return_value=settings):
                self.assertTrue(handle_repl_line("/save --source report --format md", printer=output.append, state=state))

            saved_path = Path(output[-1].split("已保存: ", 1)[1])
            saved_text = saved_path.read_text(encoding="utf-8")
            self.assertIn("# 原始报告", saved_text)
            self.assertIn("报告正文", saved_text)
            self.assertNotIn("终端摘要", saved_text)

    def test_history_file_stores_only_slash_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".sats_history"
            history = SlashCommandFileHistory(str(path))

            history.store_string("帮我解释筛选规则")
            history.store_string("/results --passed")

            text = path.read_text(encoding="utf-8")

        self.assertNotIn("帮我解释筛选规则", text)
        self.assertIn("/results --passed", text)


if __name__ == "__main__":
    unittest.main()
