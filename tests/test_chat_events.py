from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sats.chat import ChatSession
from sats.llm import LLMResponse, ToolCallRequest
from sats.memory import ChatMemoryStore, MemoryExtractor
from sats.stock_question import StockQuestion


class FakeLLM:
    def __init__(self, *args, **kwargs) -> None:
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        return SimpleNamespace(content="回答")


class ToolLLM:
    def __init__(self, *args, **kwargs) -> None:
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1 and tools:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call_1", name="list_skills", arguments={})],
            )
        return LLMResponse(content="工具回答")


class FailingLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, tools=None):
        raise RuntimeError("boom")


class NoopExtractor(MemoryExtractor):
    def extract(self, user_message, assistant_message, *, llm):
        return []

    def summarize(self, existing_summary, messages, *, llm):
        return existing_summary


def _settings(tmp: str):
    return SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")


class ChatEventsTest(unittest.TestCase):
    def test_chat_session_emits_turn_events_without_memory_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=FakeLLM,
                memory_enabled=False,
                preprocess_enabled=False,
            )

            result = session.ask("hello", event_sink=events.append)

            self.assertEqual(result.content, "回答")
            self.assertEqual(result.session_id, "default")
            self.assertIsNotNone(result.turn_id)
            event_types = [event.event_type for event in events]
            self.assertIn("turn_started", event_types)
            self.assertIn("plan_ready", event_types)
            self.assertIn("assistant_completed", event_types)
            self.assertEqual(event_types[-1], "turn_completed")

    def test_chat_session_persists_turn_and_events_by_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatMemoryStore(Path(tmp) / "sats.duckdb")
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=FakeLLM,
                memory_store=store,
                memory_extractor=NoopExtractor(),
                preprocess_enabled=False,
                session_id="chat_a",
            )

            result = session.ask("hello")

            with store.storage.connect() as con:
                turn_row = con.execute(
                    "SELECT session_id, request, status, user_message_id, assistant_message_id FROM chat_turns WHERE turn_id = ?",
                    [result.turn_id],
                ).fetchone()
                event_rows = con.execute(
                    "SELECT event_type FROM chat_turn_events WHERE turn_id = ? ORDER BY seq",
                    [result.turn_id],
                ).fetchall()
                message_count = con.execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE session_id = 'chat_a'",
                ).fetchone()[0]

            self.assertEqual(turn_row[0], "chat_a")
            self.assertEqual(turn_row[1], "hello")
            self.assertEqual(turn_row[2], "done")
            self.assertTrue(turn_row[3])
            self.assertTrue(turn_row[4])
            self.assertEqual(message_count, 2)
            self.assertIn(("turn_started",), event_rows)
            self.assertIn(("turn_completed",), event_rows)

    def test_stock_question_emits_stock_and_market_context_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            question = StockQuestion(symbols=["000001.SZ"], trade_date="20260514", has_stock_question=True)
            stock_context = SimpleNamespace(system_message="个股上下文", trade_date="20260514", question=question)
            market_context = SimpleNamespace(system_message="大盘上下文")
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=FakeLLM,
                memory_enabled=False,
                preprocess_enabled=False,
            )

            with patch("sats.chat.build_stock_llm_context", return_value=stock_context), patch(
                "sats.chat.build_market_llm_context", return_value=market_context
            ):
                result = session.ask("分析000001", event_sink=events.append)

            self.assertIn("个股", result.data_names)
            self.assertIn("大盘", result.data_names)
            completed_contexts = [
                event.item_name
                for event in events
                if event.event_type == "context_completed" and event.item_type == "context"
            ]
            self.assertIn("stock_context", completed_contexts)
            self.assertIn("market_context", completed_contexts)

    def test_tool_loop_emits_tool_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=ToolLLM,
                memory_enabled=False,
                preprocess_enabled=False,
            )

            result = session.ask("列出 skills", event_sink=events.append)

            self.assertEqual(result.content, "工具回答")
            self.assertEqual(result.tool_call_count, 1)
            tool_events = [(event.event_type, event.item_name, event.status) for event in events if event.item_type == "tool"]
            self.assertIn(("tool_started", "list_skills", "running"), tool_events)
            self.assertIn(("tool_completed", "list_skills", "done"), tool_events)

    def test_runtime_report_emits_runtime_and_artifact_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=FakeLLM,
                memory_enabled=False,
                preprocess_enabled=False,
            )

            result = session.ask("生成一份研究报告并保存", event_sink=events.append)

            self.assertTrue(result.artifacts)
            event_types = [event.event_type for event in events]
            self.assertIn("runtime_started", event_types)
            self.assertIn("artifact_created", event_types)
            self.assertIn("runtime_completed", event_types)

    def test_clarification_event_for_missing_stock_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=FakeLLM,
                memory_enabled=False,
                preprocess_enabled=False,
            )

            result = session.ask("继续分析它", event_sink=events.append)

            self.assertIn("请先提供明确股票代码", result.content)
            self.assertIn("clarification_required", [event.event_type for event in events])

    def test_failure_event_when_llm_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=FailingLLM,
                memory_enabled=False,
                preprocess_enabled=False,
                tools_enabled=False,
            )

            with self.assertRaises(RuntimeError):
                session.ask("hello", event_sink=events.append)

            self.assertEqual(events[-1].event_type, "turn_failed")
            self.assertEqual(events[-1].status, "error")


if __name__ == "__main__":
    unittest.main()
