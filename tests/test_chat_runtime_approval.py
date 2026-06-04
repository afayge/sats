from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sats.backtesting.service import BacktestResult
from sats.backtesting.strategy_spec import validate_strategy_spec
from sats.chat import ChatSession
from sats.chat_runtime import confirm_pending_runtime_action, reject_pending_runtime_action
from sats.memory import ChatMemoryStore, MemoryExtractor


class FakeLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, tools=None):
        return SimpleNamespace(content="不应调用")


class NoopExtractor(MemoryExtractor):
    def extract(self, user_message, assistant_message, *, llm):
        return []

    def summarize(self, existing_summary, messages, *, llm):
        return existing_summary


def _settings(tmp: str):
    return SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")


class ChatRuntimeApprovalTest(unittest.TestCase):
    def test_strategy_backtest_request_creates_pending_action_without_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatMemoryStore(Path(tmp) / "sats.duckdb")
            session = ChatSession(
                settings=_settings(tmp),
                skills=[],
                llm_factory=FakeLLM,
                memory_store=store,
                memory_extractor=NoopExtractor(),
                preprocess_enabled=False,
                session_id="chat_runtime",
            )

            result = session.ask("写一个5日和20日均线策略并回测000001")

            self.assertTrue(result.requires_confirmation)
            self.assertTrue(result.pending_action_id)
            self.assertFalse(result.artifacts)
            action = store.get_pending_action(result.pending_action_id or "")
            self.assertIsNotNone(action)
            self.assertEqual(action["status"], "pending")
            self.assertEqual(action["action_type"], "strategy_backtest")

    def test_confirm_pending_strategy_backtest_writes_artifacts_and_updates_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp)
            store = ChatMemoryStore(settings.db_path)
            store.start_chat_turn(turn_id="turn_runtime", session_id="chat_runtime", request="策略")
            spec = validate_strategy_spec(
                {
                    "name": "均线研究策略",
                    "strategy_type": "moving_average",
                    "symbols": ["000001.SZ"],
                    "start_date": "20260101",
                    "end_date": "20260520",
                    "short_window": 5,
                    "long_window": 20,
                    "top_n": 1,
                }
            )
            action_id = store.create_pending_action(
                session_id="chat_runtime",
                turn_id="turn_runtime",
                action_type="strategy_backtest",
                title="策略草稿与轻量回测",
                payload={"message": "策略", "session_id": "chat_runtime", "turn_id": "turn_runtime", "spec": spec.to_dict()},
            )
            fake_backtest = BacktestResult(
                spec=spec,
                metrics={"total_return": 0.1, "annual_return": 0.2, "max_drawdown": -0.05, "annual_volatility": 0.1, "win_rate": 0.5},
                equity_curve=[{"trade_date": "20260520", "nav": 1.1, "return": 0.01}],
                data_source="fake",
                message="ok",
            )

            with patch("sats.chat_runtime.run_strategy_backtest", return_value=fake_backtest):
                result = confirm_pending_runtime_action(action_id, settings=settings, store=store)

            self.assertIn("轻量回测", result.data_names)
            self.assertGreaterEqual(len(result.artifacts), 4)
            self.assertEqual(store.get_pending_action(action_id)["status"], "done")
            for artifact in result.artifacts:
                self.assertTrue(Path(artifact.path).exists())

    def test_reject_pending_action_marks_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp)
            store = ChatMemoryStore(settings.db_path)
            action_id = store.create_pending_action(
                session_id="chat_runtime",
                action_type="strategy_draft",
                title="策略草稿",
                payload={"spec": {}},
            )

            result = reject_pending_runtime_action(action_id, settings=settings, store=store)

            self.assertIn("已取消", result.content)
            self.assertEqual(store.get_pending_action(action_id)["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
