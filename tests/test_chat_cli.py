from __future__ import annotations

import unittest
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sats.chat import ChatResult
from sats.cli import main
from sats.skills import Skill


class ChatCliTest(unittest.TestCase):
    def test_cli_chat_prints_llm_response(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_chat_once", return_value=ChatResult("回答", ("sats-market-assistant",))) as chat,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "帮我", "解释筛选规则"]), 0)

        chat.assert_called_once_with("帮我 解释筛选规则", settings=settings, memory_enabled=True)
        self.assertEqual(stdout.getvalue().strip(), "使用 skill: sats-market-assistant\n回答")

    def test_cli_chat_can_disable_memory(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_chat_once", return_value=ChatResult("回答", ())) as chat,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "--no-memory", "临时问题"]), 0)

        chat.assert_called_once_with("临时问题", settings=settings, memory_enabled=False)
        self.assertEqual(stdout.getvalue().strip(), "回答")

    def test_cli_chat_allows_stock_analysis_through_real_data_context(self) -> None:
        stdout = StringIO()
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_chat_once", return_value=ChatResult("真实数据分析", ())) as chat,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["chat", "分析002436"]), 0)

        chat.assert_called_once_with("分析002436", settings=settings, memory_enabled=True)
        self.assertEqual(stdout.getvalue().strip(), "真实数据分析")

    def test_cli_chat_reports_stock_context_failure_before_llm_output(self) -> None:
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.run_chat_once", side_effect=ValueError("缺少真实分钟K数据")) as chat,
        ):
            with self.assertRaises(SystemExit) as raised:
                main(["chat", "分析002436"])

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


if __name__ == "__main__":
    unittest.main()
