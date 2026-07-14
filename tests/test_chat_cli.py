from __future__ import annotations

import unittest
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.analysis.dsa_native import DsaAnalysisRanking, DsaAnalysisRunResult
from sats.chat import ChatResult
from sats.cli import main
from sats.storage.duckdb import DuckDBStorage
from sats.skills import Skill
from sats.skillhub import SkillHubSyncResult


class _FakeTTYStdout:
    encoding = "utf-8"
    errors = "strict"

    def __init__(self) -> None:
        self.values: list[str] = []

    def write(self, text: str) -> int:
        value = str(text)
        self.values.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 1

    def writable(self) -> bool:
        return True

    def getvalue(self) -> str:
        return "".join(self.values)


class ChatCliTest(unittest.TestCase):
    def test_cli_chat_prints_llm_response(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        fake = SimpleNamespace(
            content="回答 [S1]",
            tool_call_count=1,
            data_names=("Conversation",),
            artifacts=(),
            sources=({"id": "S1", "title": "来源", "url": "https://example.com/a"},),
            turn_id="turn",
            session_id="conversation",
        )

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_conversation_once", return_value=fake) as conversation,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "帮我", "解释筛选规则"]), 0)

        conversation.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("# SATS 自然对话输出", output)
        self.assertIn("## 来源", output)
        self.assertIn("https://example.com/a", output)
        self.assertIn("`数据: Conversation`", output)
        self.assertIn("> 回答", output)

    def test_cli_chat_returns_nonzero_and_plain_error_for_failed_conversation(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"))
        failed = SimpleNamespace(content="受限 Python 分析程序执行失败：NameError", status="error")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_conversation_once", return_value=failed),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "做一个只读计算"]), 1)

        self.assertEqual(stdout.getvalue().strip(), "错误: 受限 Python 分析程序执行失败：NameError")
        self.assertNotIn("SATS 自然对话输出", stdout.getvalue())

    def test_cli_tty_human_text_uses_natural_text_output(self) -> None:
        stdout = _FakeTTYStdout()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"))
        created: list[object] = []

        class _RecordingNaturalTextOutput:
            def __init__(self, target, *, db_path=None, width=100) -> None:
                self.target = target
                self.db_path = db_path
                self.width = width
                created.append(self)

            def write(self, text: str) -> int:
                self.target.write(f"rendered:{text}")
                return len(text)

            def flush(self) -> None:
                self.target.flush()

            def isatty(self) -> bool:
                return True

            def __getattr__(self, name: str):
                return getattr(self.target, name)

        def dispatch(args, parser) -> int:
            print("状态: done +1.33%")
            return 0

        with (
            patch("sys.stdout", stdout),
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.NaturalTextOutput", _RecordingNaturalTextOutput),
            patch("sats.cli._dispatch_command", side_effect=dispatch),
        ):
            self.assertEqual(main(["result-rules"]), 0)

        self.assertEqual(len(created), 1)
        self.assertIn("rendered:状态: done +1.33%", stdout.getvalue())

    def test_cli_json_output_skips_natural_text_output(self) -> None:
        stdout = _FakeTTYStdout()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"))

        def dispatch(args, parser) -> int:
            print('{"status":"ok"}')
            return 0

        with (
            patch("sys.stdout", stdout),
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.NaturalTextOutput", side_effect=AssertionError("JSON output should not be rendered")),
            patch("sats.cli._dispatch_command", side_effect=dispatch),
        ):
            self.assertEqual(main(["catalog", "--json"]), 0)

        self.assertIn('{"status":"ok"}', stdout.getvalue())

    def test_cli_chat_can_disable_memory(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        fake = SimpleNamespace(content="回答", tool_call_count=1, data_names=("Conversation",), artifacts=(), turn_id="turn", session_id="conversation")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_conversation_once", return_value=fake) as conversation,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "--no-memory", "临时问题"]), 0)

        conversation.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("# SATS 自然对话输出", output)
        self.assertIn("`数据: Conversation`", output)
        self.assertIn("> 回答", output)

    def test_cli_chat_plan_only_prints_plan_mode_without_execution(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro", llm_timeout_seconds=10)
        plan_output = "# SATS Plan Mode\n\n## 目标\n- 规划筛选和交易计划\n\n## 建议步骤\n- 后续执行由 conversation loop 选择工具。"

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.format_plan_mode_result", return_value=plan_output) as plan_mode,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "--plan-only", "用", "price_volume_ma", "筛选，并对筛选股票制定明天交易计划"]), 0)

        output = stdout.getvalue()
        self.assertIn("# SATS Plan Mode", output)
        self.assertIn("conversation loop", output)
        plan_mode.assert_called_once()

    def test_cli_chat_agent_flag_is_obsolete(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["chat", "--agent", "帮我", "解释筛选规则"])

        self.assertIn("OBSOLETE: sats chat --agent", str(raised.exception))

    def test_cli_chat_no_agent_flag_is_obsolete(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["chat", "--no-agent", "临时问题"])

        self.assertIn("OBSOLETE: sats chat --no-agent", str(raised.exception))

    def test_cli_chat_legacy_engine_can_disable_memory(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_chat_once", return_value=ChatResult("回答", ())) as chat,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "--engine", "legacy", "--no-memory", "临时问题"]), 0)

        chat.assert_called_once_with("临时问题", settings=settings, memory_enabled=False)
        output = stdout.getvalue()
        self.assertIn("# SATS 自然对话输出", output)
        self.assertIn("> 回答", output)

    def test_cli_chat_can_confirm_runtime_action(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        runtime_result = SimpleNamespace(content="已执行", tool_call_count=0, data_names=("Runtime",), artifacts=(), pending_action=None)

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.confirm_pending_runtime_action", return_value=runtime_result) as confirm,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "--confirm", "act_123"]), 0)

        confirm.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("# SATS 执行结果", output)
        self.assertIn("`数据: Runtime`", output)
        self.assertIn("> 已执行", output)

    def test_cli_chat_can_show_runtime_trace(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.format_runtime_trace", return_value="Turn: turn_123") as trace,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "--trace", "turn_123"]), 0)

        trace.assert_called_once()
        self.assertEqual(stdout.getvalue().strip(), "Turn: turn_123")

    def test_cli_chat_answer_continues_conversation_clarification(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        fake = SimpleNamespace(
            content="已继续分析",
            tool_call_count=1,
            data_names=("Conversation",),
            artifacts=(),
            sources=(),
            requires_confirmation=False,
            pending_action_id="",
            requires_clarification=False,
            clarification_id="",
            clarification_prompt="",
            missing_fields=(),
            turn_id="turn",
            session_id="conversation",
        )
        closed: list[str] = []
        progress = SimpleNamespace(enabled=True, close=lambda: closed.append("answer"))
        sink = object()

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.continue_conversation_after_clarification", return_value=fake) as resume,
            patch("sats.cli.create_progress", return_value=progress),
            patch("sats.cli.agent_progress_event_sink", return_value=sink),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "--answer", "act_clarify", "002436"]), 0)

        resume.assert_called_once_with("act_clarify", "002436", settings=settings, event_sink=sink)
        self.assertEqual(closed, ["answer"])
        self.assertIn("> 已继续分析", stdout.getvalue())

    def test_cli_chat_allows_stock_analysis_through_real_data_context(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        fake = SimpleNamespace(content="真实数据分析", tool_call_count=1, data_names=("Conversation",), artifacts=(), turn_id="turn", session_id="conversation")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_conversation_once", return_value=fake) as conversation,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "分析002436"]), 0)

        conversation.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("# SATS 自然对话输出", output)
        self.assertIn("`数据: Conversation`", output)
        self.assertIn("> 真实数据分析", output)

    def test_cli_chat_reports_stock_context_failure_before_llm_output(self) -> None:
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_chat_once", side_effect=ValueError("缺少真实分钟K数据")) as chat,
        ):
            with self.assertRaises(SystemExit) as raised:
                main(["chat", "--engine", "legacy", "分析002436"])

        chat.assert_called_once()
        self.assertEqual(str(raised.exception), "缺少真实分钟K数据")

    def test_cli_chat_requires_message(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["chat"])

        self.assertEqual(str(raised.exception), "chat message is required")

    def test_cli_skills_prints_local_skills(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("/tmp/sats"), openai_model="deepseek-v4-pro")
        skills = [Skill("a", "alpha", "描述", ("股票",), "body", Path("SKILL.md"))]

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.load_skills", return_value=skills) as load_skills,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["skills"]), 0)

        load_skills.assert_called_once_with(Path("/tmp/sats") / "skills")
        self.assertIn("[other]", stdout.getvalue())
        self.assertIn("1. alpha - 描述 触发: 股票", stdout.getvalue())

    def test_cli_skillhub_install_all_dry_run_calls_sync(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("/tmp/sats"), openai_model="deepseek-v4-pro")
        result = SkillHubSyncResult(
            total=160,
            official=26,
            community=134,
            installed=160,
            dry_run=True,
            manifest_path="/tmp/sats/skills/.skillhub_manifest.json",
            skills_dir="/tmp/sats/skills",
        )

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.sync_skillhub_skills", return_value=result) as sync,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["skillhub", "install", "--all", "--dry-run"]), 0)

        sync.assert_called_once_with(settings.project_root, dry_run=True, prune_generated=False)
        self.assertIn("SkillHub 同步预览", stdout.getvalue())

    def test_cli_memory_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from sats.memory import ChatMemoryStore

            db_path = Path(tmp) / "sats.duckdb"
            store = ChatMemoryStore(db_path)
            memory_id = store.add_memory(content="用户关注股票筛选", tags=("股票",), importance=0.7)
            settings = SimpleNamespace(project_root=Path(tmp), db_path=db_path, openai_model="deepseek-v4-pro")
            stdout = StringIO()

            with patch("sats.cli.load_settings", return_value=settings), redirect_stdout(stdout):
                self.assertEqual(main(["memory", "list"]), 0)
                self.assertEqual(main(["memory", "search", "股票"]), 0)
                self.assertEqual(main(["memory", "forget", memory_id]), 0)
                self.assertEqual(main(["memory", "clear", "--yes"]), 0)

            output = stdout.getvalue()
            self.assertIn("用户关注股票筛选", output)
            self.assertIn(f"已删除记忆 {memory_id}", output)

    def test_cli_history_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from sats.history import InteractionHistoryStore

            db_path = Path(tmp) / "sats.duckdb"
            store = InteractionHistoryStore(db_path)
            history_id = store.add_record(kind="chat", request="分析股票", source="chat", output="聊天回答")
            stdout = StringIO()

            with redirect_stdout(stdout):
                self.assertEqual(main(["history", "list", "--db", str(db_path)]), 0)
                self.assertEqual(main(["history", "search", "聊天", "--kind", "chat", "--db", str(db_path)]), 0)
                self.assertEqual(main(["history", "show", history_id, "--db", str(db_path)]), 0)
                self.assertEqual(main(["history", "delete", history_id, "--db", str(db_path)]), 0)
                self.assertEqual(main(["history", "delete", history_id, "--db", str(db_path)]), 0)

            output = stdout.getvalue()
            self.assertIn("分析股票", output)
            self.assertIn("聊天回答", output)
            self.assertIn(f"已删除历史记录 {history_id}", output)
            self.assertIn(f"未找到历史记录 {history_id}", output)

    def test_cli_knowledge_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "sats.duckdb"
            doc = root / "chan.md"
            doc.write_text("# 三买\n第三类买点 回试 中枢。\n", encoding="utf-8")
            settings = SimpleNamespace(project_root=root, db_path=db_path, openai_model="deepseek-v4-pro")
            stdout = StringIO()

            with patch("sats.cli.load_settings", return_value=settings), redirect_stdout(stdout):
                self.assertEqual(main(["knowledge", "add", "--name", "chan", "--description", "缠论"]), 0)
                self.assertEqual(main(["knowledge", "ingest", "--knowledge", "chan", "--path", str(doc)]), 0)
                self.assertEqual(main(["knowledge", "search", "--query", "三买", "--knowledge", "chan"]), 0)
                self.assertEqual(main(["knowledge", "list"]), 0)

            output = stdout.getvalue()
            self.assertIn("已保存知识库 chan", output)
            self.assertIn("已入库 1 个知识块", output)
            self.assertIn("第三类买点", output)
            self.assertIn("chan", output)

    def test_cli_knowledge_sync_stock_basic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_basic(pd.DataFrame([{"ts_code": "000938.SZ", "symbol": "000938", "name": "紫光股份"}]))
            settings = SimpleNamespace(project_root=root, db_path=db_path, openai_model="deepseek-v4-pro")
            stdout = StringIO()

            with patch("sats.cli.load_settings", return_value=settings), redirect_stdout(stdout):
                self.assertEqual(main(["knowledge", "sync-stock-basic"]), 0)
                self.assertEqual(main(["knowledge", "search", "--query", "紫光股份 股票代码", "--knowledge", "stock-basic"]), 0)

            output = stdout.getvalue()
            self.assertIn("已同步 1 条 stock_basic 股票知识", output)
            self.assertIn("000938.SZ", output)

    def test_cli_chat_can_force_knowledge(self) -> None:
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        stdout = StringIO()

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_chat_once", return_value=ChatResult("回答", ())) as chat,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "--engine", "legacy", "--knowledge", "chan", "解释三买"]), 0)

        chat.assert_called_once_with("解释三买", settings=settings, memory_enabled=True, knowledge="chan")

    def test_cli_dsa_accepts_stock_name_at_input_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_basic(pd.DataFrame([{"ts_code": "000938.SZ", "symbol": "000938", "name": "紫光股份"}]))
            settings = SimpleNamespace(project_root=root, env_path=root / ".env", db_path=db_path, openai_model="deepseek-v4-pro")
            fake_result = DsaAnalysisRunResult(
                analyzed_codes=["000938.SZ"],
                skipped_codes=[],
                rankings=[DsaAnalysisRanking("000938.SZ", "紫光股份", 76, "买入", "偏多")],
                source_report=None,
                archived_report=None,
            )
            stdout = StringIO()

            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.run_dsa_analysis", return_value=fake_result) as run_dsa,
                redirect_stdout(stdout),
            ):
                self.assertEqual(main(["dsa", "--stocks", "紫光股份", "--trade-date", "20260514", "--no-llm"]), 0)

            self.assertEqual(run_dsa.call_args.args[0], ["000938.SZ"])
            self.assertIn("000938.SZ 紫光股份", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
