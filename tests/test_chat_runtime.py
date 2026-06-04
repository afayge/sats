from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sats.chat import ChatSession
from sats.memory import ChatMemoryStore, MemoryExtractor


class RuntimeFakeLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, tools=None):
        return SimpleNamespace(content="这是 runtime 生成的研究报告。")


class NoopExtractor(MemoryExtractor):
    def extract(self, user_message, assistant_message, *, llm):
        return []

    def summarize(self, existing_summary, messages, *, llm):
        return existing_summary


def _settings(tmp: str):
    return SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")


class ChatRuntimeTest(unittest.TestCase):
    def test_report_request_enters_runtime_and_persists_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            store = ChatMemoryStore(Path(tmp) / "sats.duckdb")
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=RuntimeFakeLLM,
                memory_store=store,
                memory_extractor=NoopExtractor(),
                preprocess_enabled=False,
                session_id="chat_runtime",
            )

            result = session.ask("生成一份市场研究报告并保存", event_sink=events.append)

            self.assertIn("报告:", result.content)
            self.assertTrue(result.artifacts)
            self.assertIn("Runtime", result.data_names)
            self.assertIn("runtime_started", [event.event_type for event in events])
            self.assertIn("artifact_created", [event.event_type for event in events])
            artifact_path = Path(result.artifacts[0]["path"])
            self.assertTrue(artifact_path.exists())
            with store.storage.connect() as con:
                item_count = con.execute(
                    "SELECT COUNT(*) FROM chat_turn_items WHERE turn_id = ?",
                    [result.turn_id],
                ).fetchone()[0]
                artifact_count = con.execute(
                    "SELECT COUNT(*) FROM chat_artifacts WHERE turn_id = ?",
                    [result.turn_id],
                ).fetchone()[0]
            self.assertGreaterEqual(item_count, 1)
            self.assertEqual(artifact_count, 1)

    def test_plain_chat_does_not_enter_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=RuntimeFakeLLM,
                memory_enabled=False,
                preprocess_enabled=False,
            )

            result = session.ask("帮我解释筛选规则", event_sink=events.append)

            self.assertEqual(result.content, "这是 runtime 生成的研究报告。")
            self.assertFalse(result.artifacts)
            self.assertNotIn("runtime_started", [event.event_type for event in events])


if __name__ == "__main__":
    unittest.main()
