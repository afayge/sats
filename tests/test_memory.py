from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sats.chat import ChatSession, build_chat_messages
from sats.history import InteractionHistoryStore, format_history_detail, format_history_list
from sats.memory import ChatMemoryStore, MemoryCandidate, MemoryExtractor


class FakeLLM:
    instances: list["FakeLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.messages: list[list[dict[str, str]]] = []
        FakeLLM.instances.append(self)

    def chat(self, messages):
        self.messages.append(messages)
        return SimpleNamespace(content="回答")


class NoopExtractor(MemoryExtractor):
    def extract(self, user_message, assistant_message, *, llm):
        return []

    def summarize(self, existing_summary, messages, *, llm):
        return "会话摘要"


class CandidateExtractor(MemoryExtractor):
    def extract(self, user_message, assistant_message, *, llm):
        return [
            MemoryCandidate(
                memory_type="preference",
                content="用户偏好使用 price_volume_ma 策略",
                tags=("price_volume_ma", "股票"),
                importance=0.8,
            )
        ]

    def summarize(self, existing_summary, messages, *, llm):
        return "会话摘要"


class FailingExtractor(MemoryExtractor):
    def extract(self, user_message, assistant_message, *, llm):
        raise RuntimeError("extract failed")

    def summarize(self, existing_summary, messages, *, llm):
        raise RuntimeError("summarize failed")


class MemoryStoreTest(unittest.TestCase):
    def test_memory_store_writes_searches_archives_and_clears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatMemoryStore(Path(tmp) / "sats.duckdb")
            store.ensure_session("default", model_name="deepseek-v4-pro")
            store.add_message("default", "user", "hello")
            memory_id = store.add_memory(
                content="用户偏好使用 price_volume_ma 策略",
                memory_type="preference",
                tags=("price_volume_ma", "股票"),
                importance=0.8,
                source_session_id="default",
            )

            rows = store.search_memories("股票 策略", limit=5)

            self.assertEqual(rows[0].memory_id, memory_id)
            self.assertEqual(rows[0].tags, ("price_volume_ma", "股票"))
            self.assertEqual(store.count_session_messages("default"), 1)
            self.assertTrue(store.archive_memory(memory_id))
            self.assertEqual(store.search_memories("股票"), [])
            self.assertGreaterEqual(store.clear_all_memory(), 1)


class InteractionHistoryStoreTest(unittest.TestCase):
    def test_history_store_writes_searches_shows_and_soft_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InteractionHistoryStore(Path(tmp) / "sats.duckdb")

            chat_id = store.add_record(
                kind="chat",
                request="分析 000001",
                source="chat",
                output="回答: 平安银行",
                duration_seconds=1.25,
                session_id="default",
            )
            command_id = store.add_record(
                kind="command",
                request="/results --passed",
                source="/results",
                output="1. 000001.SZ",
                status="done",
            )

            all_records = store.list_records(limit=10)
            chat_records = store.list_records(kind="chat", limit=10)
            search_records = store.search_records("平安银行", limit=10)
            detail = store.get_record(chat_id)

            self.assertEqual({record.history_id for record in all_records}, {chat_id, command_id})
            self.assertEqual([record.history_id for record in chat_records], [chat_id])
            self.assertEqual([record.history_id for record in search_records], [chat_id])
            self.assertIsNotNone(detail)
            self.assertEqual(detail.output, "回答: 平安银行")
            self.assertIn(chat_id, format_history_list(all_records))
            self.assertIn("请求:", format_history_detail(detail))
            self.assertTrue(store.delete_record(chat_id))
            self.assertFalse(store.delete_record(chat_id))
            self.assertIsNone(store.get_record(chat_id))
            self.assertEqual(store.search_records("平安银行", limit=10), [])


class ChatMemoryTest(unittest.TestCase):
    def test_chat_session_injects_relevant_memory_and_persists_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            FakeLLM.instances = []
            store = ChatMemoryStore(Path(tmp) / "sats.duckdb")
            store.add_memory(
                content="用户偏好使用 price_volume_ma 策略",
                memory_type="preference",
                tags=("price_volume_ma",),
                importance=0.9,
            )
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            session = ChatSession(
                settings=settings,
                skills=[],
                llm_factory=FakeLLM,
                memory_store=store,
                memory_extractor=NoopExtractor(),
            )

            result = session.ask("price_volume_ma 怎么用")

            self.assertEqual(result.content, "回答")
            self.assertEqual(result.memory_count, 1)
            sent = FakeLLM.instances[0].messages[0]
            self.assertTrue(any("本地长期记忆" in item["content"] for item in sent))
            self.assertEqual(store.count_session_messages("default"), 2)

    def test_chat_session_no_memory_skips_retrieval_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            FakeLLM.instances = []
            store = ChatMemoryStore(Path(tmp) / "sats.duckdb")
            store.add_memory(content="应该不会注入", tags=("hello",))
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            session = ChatSession(
                settings=settings,
                skills=[],
                llm_factory=FakeLLM,
                memory_store=store,
                memory_extractor=NoopExtractor(),
            )

            result = session.ask("hello", use_memory=False)

            self.assertEqual(result.memory_count, 0)
            sent = FakeLLM.instances[0].messages[0]
            self.assertFalse(any("本地长期记忆" in item["content"] for item in sent))
            self.assertEqual(store.count_session_messages("default"), 0)

    def test_memory_extraction_failure_does_not_break_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            store = ChatMemoryStore(settings.db_path)
            session = ChatSession(
                settings=settings,
                skills=[],
                llm_factory=FakeLLM,
                memory_store=store,
                memory_extractor=FailingExtractor(),
                summary_threshold_messages=2,
                summary_refresh_messages=2,
            )

            result = session.ask("hello")

            self.assertEqual(result.content, "回答")
            self.assertEqual(store.list_memories(), [])

    def test_summary_is_created_and_used_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            FakeLLM.instances = []
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            store = ChatMemoryStore(settings.db_path)
            session = ChatSession(
                settings=settings,
                skills=[],
                llm_factory=FakeLLM,
                memory_store=store,
                memory_extractor=NoopExtractor(),
                summary_threshold_messages=2,
                summary_refresh_messages=2,
            )

            session.ask("第一轮")
            session.ask("第二轮")

            self.assertEqual(store.get_session_summary("default"), "会话摘要")
            second_call = FakeLLM.instances[0].messages[1]
            self.assertTrue(any("当前会话摘要" in item["content"] for item in second_call))

    def test_build_chat_messages_can_include_memory_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatMemoryStore(Path(tmp) / "sats.duckdb")
            store.add_memory(content="用户偏好中文回答", memory_type="preference", tags=("中文",))
            memory = store.search_memories("中文", limit=1)

            messages = build_chat_messages("hello", memories=memory, session_summary="已有摘要")

            self.assertTrue(any("本地长期记忆" in item["content"] for item in messages))
            self.assertTrue(any("当前会话摘要" in item["content"] for item in messages))


if __name__ == "__main__":
    unittest.main()
