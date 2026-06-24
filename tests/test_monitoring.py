from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from sats.cli import main
from sats.monitoring import (
    MonitorConfig,
    MonitorDisplay,
    MonitorPlanValidationError,
    MonitorService,
    format_monitor_dashboard,
    import_monitor_plan,
    validate_monitor_plan,
)
from sats.screening.base import ScreeningResult
from sats.storage.duckdb import DuckDBStorage
from sats.trading.broker import BrokerError
from sats.trading.models import BrokerAsset, BrokerPosition, OrderResult
from sats.trading.monitor_provider import AutoTradeConfig, QmtTradingProvider
from sats.trading.sync import QMT_POSITION_SYNC_SERVICE, QmtPositionSyncError, QmtPositionSyncService
from sats.watchlist_editor import WATCHLIST_DIALOG_STYLE, run_watchlist_editor, select_stock_rows


class MonitoringStorageTest(unittest.TestCase):
    def test_monitor_tables_roundtrip_without_quotes_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_monitor_position(ts_code="000001.SZ", name="平安银行", quantity=100, buy_price=10.5)
            storage.upsert_monitor_watchlist(ts_code="600519.SH", name="贵州茅台")
            storage.upsert_monitor_buy_candidate(ts_code="000002.SZ", name="万科A", reason="一买")
            storage.upsert_monitor_runtime(service_name="monitor", status="running", pid=123, heartbeat=True)

            event = _event("000001.SZ", side="buy")
            self.assertTrue(storage.insert_monitor_event(event))
            self.assertFalse(storage.insert_monitor_event(event))
            self.assertTrue(
                storage.insert_monitor_trade_event(
                    {
                        "trade_event_id": "trade-1",
                        "event_id": event["event_id"],
                        "ts_code": "000001.SZ",
                        "action": "buy",
                        "side": "buy",
                        "status": "not_configured",
                    }
                )
            )

            self.assertEqual(storage.list_monitor_positions()[0]["ts_code"], "000001.SZ")
            self.assertEqual(storage.list_monitor_watchlist()[0]["ts_code"], "600519.SH")
            self.assertEqual(storage.list_monitor_buy_candidates()[0]["ts_code"], "000002.SZ")
            self.assertEqual(storage.list_monitor_events()[0]["event_id"], event["event_id"])
            self.assertEqual(storage.list_monitor_trade_events()[0]["trade_event_id"], "trade-1")
            self.assertEqual(storage.get_monitor_runtime("monitor")["pid"], 123)
            with storage.connect() as con:
                tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            self.assertNotIn("monitor_quotes", tables)

    def test_broker_tables_and_qmt_position_sync_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            position = BrokerPosition(ts_code="000001.SZ", name="平安银行", quantity=200, available_quantity=100, cost_price=10.0, price=11.0)

            storage.upsert_broker_account({"provider": "qmt", "account_id": "acct", "account_type": "STOCK", "available_cash": 1000})
            storage.insert_broker_order(
                {
                    "sats_order_id": "sats-1",
                    "provider": "qmt",
                    "account_id": "acct",
                    "broker_order_id": "qmt-1",
                    "ts_code": "000001.SZ",
                    "side": "buy",
                    "quantity": 100,
                    "status": "submitted",
                }
            )
            positions = QmtPositionSyncService(
                storage=storage,
                client=_FakeBrokerClient(positions=[position]),
            ).sync()

            self.assertEqual(len(positions), 1)
            self.assertEqual(storage.list_broker_positions(provider="qmt")[0]["ts_code"], "000001.SZ")
            self.assertEqual(storage.list_broker_orders()[0]["broker_order_id"], "qmt-1")
            self.assertEqual(storage.list_monitor_positions()[0]["note"], "qmt_sync:acct")
            self.assertEqual(storage.list_monitor_positions()[0]["buy_date"], "")
            self.assertEqual(storage.get_monitor_runtime(QMT_POSITION_SYNC_SERVICE)["status"], "ready")

    def test_qmt_position_sync_replaces_snapshot_and_accepts_explicit_empty_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_monitor_position(ts_code="600519.SH", quantity=100, buy_price=1000)
            position = BrokerPosition(
                ts_code="000001.SZ",
                name="平安银行",
                quantity=200,
                available_quantity=100,
                cost_price=10.5,
            )

            QmtPositionSyncService(storage=storage, client=_FakeBrokerClient(positions=[position])).sync()

            rows = storage.list_monitor_positions()
            self.assertEqual([row["ts_code"] for row in rows], ["000001.SZ"])
            self.assertEqual(rows[0]["quantity"], 200.0)
            self.assertEqual(rows[0]["buy_price"], 10.5)

            QmtPositionSyncService(storage=storage, client=_FakeBrokerClient(positions=[])).sync()

            self.assertEqual(storage.list_monitor_positions(), [])
            self.assertEqual(storage.list_broker_positions(provider="qmt", account_id="acct"), [])
            self.assertEqual(storage.get_monitor_runtime(QMT_POSITION_SYNC_SERVICE)["params"]["position_count"], 0)

    def test_qmt_position_sync_failure_preserves_last_successful_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            original = BrokerPosition(
                ts_code="000001.SZ",
                quantity=200,
                available_quantity=100,
                cost_price=10.5,
            )
            QmtPositionSyncService(storage=storage, client=_FakeBrokerClient(positions=[original])).sync()
            last_success = storage.get_monitor_runtime(QMT_POSITION_SYNC_SERVICE)["heartbeat_at"]
            duplicate = BrokerPosition(
                ts_code="000001.SZ",
                quantity=100,
                available_quantity=100,
                cost_price=11,
            )

            with self.assertRaisesRegex(QmtPositionSyncError, "重复股票代码"):
                QmtPositionSyncService(
                    storage=storage,
                    client=_FakeBrokerClient(positions=[duplicate, duplicate]),
                ).sync()

            rows = storage.list_monitor_positions()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["quantity"], 200.0)
            state = storage.get_monitor_runtime(QMT_POSITION_SYNC_SERVICE)
            self.assertEqual(state["status"], "stale")
            self.assertEqual(state["heartbeat_at"], last_success)
            self.assertIn("重复股票代码", state["last_error"])

    def test_monitor_plan_roundtrip_and_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            plan = import_monitor_plan(storage, _plan_payload())

            self.assertEqual(plan["status"], "draft")
            self.assertEqual(plan["items"][0]["symbol"], "000001.SZ")
            self.assertTrue(storage.set_monitor_plan_status(plan["plan_id"], "active"))
            active = storage.list_active_monitor_plan_groups(trade_date="20260619")
            self.assertEqual(len(active), 1)

            item_id = plan["items"][0]["item_id"]
            group_id = plan["items"][0]["trigger_groups"][0]["group_id"]
            self.assertTrue(storage.disable_monitor_plan_group(group_id))
            self.assertEqual(storage.list_active_monitor_plan_groups(trade_date="20260619"), [])
            self.assertTrue(storage.disable_monitor_plan_item(item_id))
            self.assertTrue(storage.delete_monitor_plan(plan["plan_id"]))
            self.assertEqual(storage.list_monitor_plans(), [])

    def test_monitor_plan_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_monitor_plan_trigger_state(
                group_id="group-1",
                trade_date="20260619",
                last_result="true",
                crossing_count=2,
                notification_count=2,
                trade_count=1,
                last_values=[{"actual": 12.3}],
                triggered=True,
            )

            state = storage.get_monitor_plan_trigger_state("group-1", "20260619")

            self.assertEqual(state["last_result"], "true")
            self.assertEqual(state["crossing_count"], 2)
            self.assertEqual(state["trade_count"], 1)
            self.assertEqual(state["last_values"][0]["actual"], 12.3)


class MonitorPlanValidationTest(unittest.TestCase):
    def test_valid_plan_is_normalized(self) -> None:
        plan = validate_monitor_plan(_plan_payload())

        self.assertEqual(plan["items"][0]["symbol"], "000001.SZ")
        condition = plan["items"][0]["trigger_groups"][0]["conditions"][0]
        self.assertEqual(condition["subject"]["symbol"], "000001.SZ")
        self.assertEqual(plan["items"][0]["trigger_groups"][0]["sizing"]["mode"], "default")

    def test_invalid_plan_fields_are_rejected(self) -> None:
        cases = []
        missing_name = _plan_payload()
        missing_name.pop("name")
        cases.append(missing_name)
        invalid_symbol = _plan_payload()
        invalid_symbol["items"][0]["symbol"] = "ABC"
        cases.append(invalid_symbol)
        index_as_stock = _plan_payload()
        index_as_stock["items"][0]["symbol"] = "000001.SH"
        cases.append(index_as_stock)
        invalid_metric = _plan_payload()
        invalid_metric["items"][0]["trigger_groups"][0]["conditions"][0]["metric"] = "volume"
        cases.append(invalid_metric)
        invalid_operator = _plan_payload()
        invalid_operator["items"][0]["trigger_groups"][0]["conditions"][0]["operator"] = "=="
        cases.append(invalid_operator)
        invalid_date = _plan_payload()
        invalid_date["start_date"] = "20260230"
        cases.append(invalid_date)
        invalid_window = _plan_payload()
        invalid_window["active_windows"] = [{"start": "15:00", "end": "09:30"}]
        cases.append(invalid_window)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(MonitorPlanValidationError):
                    validate_monitor_plan(payload)

    def test_invalid_item_prevents_entire_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            payload = _plan_payload()
            payload["items"].append(
                {
                    "symbol": "BAD",
                    "trigger_groups": payload["items"][0]["trigger_groups"],
                }
            )

            with self.assertRaises(MonitorPlanValidationError):
                import_monitor_plan(storage, payload)

            self.assertEqual(storage.list_monitor_plans(), [])


class MonitorServiceTest(unittest.TestCase):
    def test_chan_buy_signal_writes_event_and_candidate_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_monitor_watchlist(ts_code="000001.SZ", name="平安银行")
            service = MonitorService(settings=_settings(storage), storage=storage, provider=_FakeProvider())
            result = _screening_result(side="buy", label="三买", signal_name="chan_third_buy")

            with patch("sats.screening.rules.chan_signals.ChanSignalsRule.evaluate", return_value=result):
                first = service.run_once(MonitorConfig(rules=("chan_signals",), lists=("watchlist",)))
                second = service.run_once(MonitorConfig(rules=("chan_signals",), lists=("watchlist",)))

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertEqual(storage.list_monitor_events()[0]["signal_label"], "三买")
            self.assertEqual(storage.list_monitor_buy_candidates()[0]["ts_code"], "000001.SZ")
            self.assertEqual(storage.list_monitor_trade_events()[0]["action"], "buy")

    def test_chan_sell_signal_from_position_writes_trade_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_monitor_position(ts_code="000001.SZ", name="平安银行", quantity=100, buy_price=10.5)
            service = MonitorService(
                settings=_settings(storage),
                storage=storage,
                provider=_FakeProvider(),
                position_sync=_FakePositionSync(),
            )
            result = _screening_result(side="sell", label="一卖", signal_name="chan_first_sell")

            with patch("sats.screening.rules.chan_signals.ChanSignalsRule.evaluate", return_value=result):
                service.run_once(MonitorConfig(rules=("chan_signals",), lists=("positions",)))

            trade = storage.list_monitor_trade_events()[0]
            self.assertEqual(trade["action"], "sell")
            self.assertEqual(trade["quantity"], 100.0)

    def test_position_sync_failure_aborts_monitor_cycle_before_rules_or_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_monitor_position(ts_code="000001.SZ", quantity=100, buy_price=10)
            service = MonitorService(
                settings=_settings(storage),
                storage=storage,
                provider=_FakeProvider(),
                position_sync=_FakePositionSync(error=QmtPositionSyncError("QMT 持仓同步失败")),
            )

            with patch("sats.screening.rules.chan_signals.ChanSignalsRule.evaluate") as evaluate:
                with self.assertRaises(QmtPositionSyncError):
                    service.run_once(MonitorConfig(rules=("chan_signals",), lists=("positions",)))

            evaluate.assert_not_called()
            self.assertEqual(storage.list_monitor_trade_events(), [])

    def test_llm_review_uses_light_profile(self) -> None:
        calls = []

        class FakeLLM:
            def __init__(self, *args, **kwargs) -> None:
                calls.append(kwargs)

            def chat(self, messages):
                return SimpleNamespace(content="两句话摘要")

        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            settings = SimpleNamespace(
                db_path=storage.db_path,
                openai_model="main-model",
                light_model_name="light-model",
                llm_timeout_seconds=180,
            )
            service = MonitorService(settings=settings, storage=storage, provider=_FakeProvider())

            with (
                patch("sats.monitoring.service.ChatLLM", FakeLLM),
                patch("sats.monitoring.service.search_chan_knowledge", return_value=[]),
            ):
                review = service._llm_review("000001.SZ", "平安银行", {"label": "三买"}, {"price": 11})

        self.assertEqual(review, "两句话摘要")
        self.assertEqual(calls[0]["model_name"], "light-model")
        self.assertEqual(calls[0]["profile"], "light")
        self.assertEqual(calls[0]["timeout_seconds"], 180)

    def test_llm_review_falls_back_to_default_when_light_times_out(self) -> None:
        calls = []

        class FakeLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.kwargs = kwargs
                calls.append(kwargs)

            def chat(self, messages):
                if self.kwargs.get("profile") == "light":
                    raise TimeoutError("light timeout")
                return SimpleNamespace(content="主模型摘要")

        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            settings = SimpleNamespace(
                db_path=storage.db_path,
                openai_model="main-model",
                light_model_name="light-model",
                llm_timeout_seconds=180,
            )
            service = MonitorService(settings=settings, storage=storage, provider=_FakeProvider())

            with (
                patch("sats.monitoring.service.ChatLLM", FakeLLM),
                patch("sats.monitoring.service.search_chan_knowledge", return_value=[]),
            ):
                review = service._llm_review("000001.SZ", "平安银行", {"label": "三买"}, {"price": 11})

        self.assertEqual(review, "主模型摘要")
        self.assertEqual([call["profile"] for call in calls], ["light", "default"])
        self.assertEqual([call["timeout_seconds"] for call in calls], [180, 180])
        self.assertEqual(calls[1]["model_name"], "main-model")

    def test_qmt_trading_provider_places_buy_order_with_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            broker = _FakeBrokerClient()
            provider = QmtTradingProvider(
                client=broker,
                storage=storage,
                config=AutoTradeConfig(enabled_actions={"buy"}, max_order_value=20000, max_position_pct=0.2),
            )

            trade_event = provider.build_trade_event(_event("000001.SZ", side="buy"), action="buy")

            self.assertEqual(trade_event["status"], "submitted")
            self.assertEqual(broker.requests[0].quantity, 1800)
            self.assertEqual(storage.list_broker_orders()[0]["ts_code"], "000001.SZ")

    def test_active_plan_runs_without_watchlist_and_draft_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            plan = import_monitor_plan(storage, _plan_payload())
            service = MonitorService(settings=_settings(storage), storage=storage, provider=_PlanProvider([12.0]))

            with patch("sats.monitoring.service._now", return_value=_plan_now()):
                self.assertEqual(service.run_once(MonitorConfig(lists=())), [])
                storage.set_monitor_plan_status(plan["plan_id"], "active")
                events = service.run_once(MonitorConfig(lists=()))

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["source_list"], "plan")
            self.assertEqual(events[0]["rule_name"], "monitor_plan")

    def test_plan_and_conditions_include_market_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            payload = _plan_payload()
            payload["items"][0]["trigger_groups"][0]["conditions"].append(
                {
                    "subject": {"type": "index", "symbol": "000001.SH"},
                    "metric": "pct_change",
                    "operator": ">=",
                    "value": 0.5,
                }
            )
            plan = import_monitor_plan(storage, payload)
            storage.set_monitor_plan_status(plan["plan_id"], "active")
            provider = _PlanProvider([12.0], index_pct=0.6)

            with patch("sats.monitoring.service._now", return_value=_plan_now()):
                events = MonitorService(settings=_settings(storage), storage=storage, provider=provider).run_once(
                    MonitorConfig(lists=())
                )

            self.assertEqual(len(events), 1)
            self.assertEqual(len(events[0]["metrics"]["conditions"]), 2)

    def test_change_points_metric_and_active_window_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            payload = _plan_payload()
            payload["items"][0]["trigger_groups"][0]["conditions"][0].update(
                {"metric": "change_points", "value": 1.5}
            )
            plan = import_monitor_plan(storage, payload)
            storage.set_monitor_plan_status(plan["plan_id"], "active")
            service = MonitorService(
                settings=_settings(storage),
                storage=storage,
                provider=_PlanProvider([12.0, 12.0]),
            )

            with patch(
                "sats.monitoring.service._now",
                side_effect=[
                    datetime(2026, 6, 19, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                    _plan_now(),
                ],
            ):
                outside = service.run_once(MonitorConfig(lists=()))
                inside = service.run_once(MonitorConfig(lists=()))

            self.assertEqual(outside, [])
            self.assertEqual(len(inside), 1)
            self.assertEqual(inside[0]["metrics"]["conditions"][0]["actual"], 2.0)

    def test_plan_triggers_on_false_to_true_crossings_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            plan = import_monitor_plan(storage, _plan_payload())
            storage.set_monitor_plan_status(plan["plan_id"], "active")
            service = MonitorService(
                settings=_settings(storage),
                storage=storage,
                provider=_PlanProvider([10.0, 12.0, 12.5, 10.5, 13.0]),
            )

            with patch("sats.monitoring.service._now", return_value=_plan_now()):
                results = [service.run_once(MonitorConfig(lists=())) for _ in range(5)]

            self.assertEqual([len(result) for result in results], [0, 1, 0, 0, 1])
            self.assertEqual(len(storage.list_monitor_events()), 2)

    def test_unknown_quote_does_not_reset_true_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            plan = import_monitor_plan(storage, _plan_payload())
            storage.set_monitor_plan_status(plan["plan_id"], "active")
            service = MonitorService(
                settings=_settings(storage),
                storage=storage,
                provider=_PlanProvider([12.0, None, 12.0]),
            )

            with patch("sats.monitoring.service._now", return_value=_plan_now()):
                results = [service.run_once(MonitorConfig(lists=())) for _ in range(3)]

            self.assertEqual([len(result) for result in results], [1, 0, 0])
            self.assertEqual(len(storage.list_monitor_events()), 1)

    def test_plan_state_survives_monitor_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            plan = import_monitor_plan(storage, _plan_payload())
            storage.set_monitor_plan_status(plan["plan_id"], "active")
            provider = _PlanProvider([12.0, 12.0])

            with patch("sats.monitoring.service._now", return_value=_plan_now()):
                first = MonitorService(settings=_settings(storage), storage=storage, provider=provider).run_once(
                    MonitorConfig(lists=())
                )
                second = MonitorService(settings=_settings(storage), storage=storage, provider=provider).run_once(
                    MonitorConfig(lists=())
                )

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])

    def test_repeated_crossings_notify_each_time_but_trade_only_once_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            payload = _plan_payload(action="buy")
            plan = import_monitor_plan(storage, payload)
            storage.set_monitor_plan_status(plan["plan_id"], "active")
            service = MonitorService(
                settings=_settings(storage),
                storage=storage,
                provider=_PlanProvider([12.0, 10.0, 13.0]),
            )

            with patch("sats.monitoring.service._now", return_value=_plan_now()):
                for _ in range(3):
                    service.run_once(MonitorConfig(lists=()))

            self.assertEqual(len(storage.list_monitor_events()), 2)
            self.assertEqual(len(storage.list_monitor_trade_events()), 1)
            group_id = plan["items"][0]["trigger_groups"][0]["group_id"]
            state = storage.get_monitor_plan_trigger_state(group_id, "20260619")
            self.assertEqual(state["crossing_count"], 2)
            self.assertEqual(state["trade_count"], 1)

    def test_plan_trade_allowance_resets_next_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            plan = import_monitor_plan(storage, _plan_payload(action="buy"))
            storage.set_monitor_plan_status(plan["plan_id"], "active")
            service = MonitorService(
                settings=_settings(storage),
                storage=storage,
                provider=_PlanProvider([12.0, 12.0]),
            )

            with patch(
                "sats.monitoring.service._now",
                side_effect=[_plan_now(), datetime(2026, 6, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))],
            ):
                service.run_once(MonitorConfig(lists=()))
                service.run_once(MonitorConfig(lists=()))

            self.assertEqual(len(storage.list_monitor_trade_events()), 2)

    def test_qmt_plan_amount_sizing_is_capped_by_global_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            broker = _FakeBrokerClient()
            provider = QmtTradingProvider(
                client=broker,
                storage=storage,
                config=AutoTradeConfig(enabled_actions={"buy"}, max_order_value=5000, max_position_pct=0.2),
            )

            provider.build_trade_event(
                _event("000001.SZ", side="buy"),
                action="buy",
                sizing={"mode": "amount", "value": 10000},
            )

            self.assertEqual(broker.requests[0].quantity, 400)

    def test_plan_sell_rejects_when_required_qmt_position_sync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            plan = import_monitor_plan(storage, _plan_payload(action="sell"))
            storage.set_monitor_plan_status(plan["plan_id"], "active")
            trading_provider = QmtTradingProvider(
                client=_FakeBrokerClient(),
                storage=storage,
                config=AutoTradeConfig(enabled_actions={"sell"}),
            )
            service = MonitorService(
                settings=_settings(storage),
                storage=storage,
                provider=_PlanProvider([12.0]),
                trading_provider=trading_provider,
                position_sync=_FakePositionSync(error=QmtPositionSyncError("bridge offline")),
            )

            with patch("sats.monitoring.service._now", return_value=_plan_now()):
                events = service.run_once(
                    MonitorConfig(lists=(), broker="qmt", auto_trade=("sell",))
                )

            self.assertEqual(len(events), 1)
            trade = storage.list_monitor_trade_events()[0]
            self.assertEqual(trade["status"], "rejected")
            self.assertIn("持仓同步失败", trade["message"])
            self.assertEqual(trading_provider.client.requests, [])


class MonitorDisplayTest(unittest.TestCase):
    def test_display_uses_realtime_quote_for_position_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_monitor_position(ts_code="000001.SZ", name="平安银行", quantity=100, buy_price=10.0)
            storage.upsert_monitor_watchlist(ts_code="600519.SH", name="贵州茅台")
            storage.insert_scheduled_task_run(
                {
                    "run_id": "schedule-run-1",
                    "task_name": "daily-discover",
                    "task_type": "chat",
                    "text": "推荐股票",
                    "started_at": "2026-05-25 08:45:00",
                    "finished_at": "2026-05-25 08:45:02",
                    "status": "success",
                    "duration_seconds": 2.0,
                    "output_text": "完成",
                }
            )
            display = MonitorDisplay(settings=_settings(storage), storage=storage, provider=_FakeProvider())

            snapshot = display.snapshot()
            text = format_monitor_dashboard(snapshot, width=140, height=28)

            self.assertIn("monitor", text)
            self.assertIn("watchList", text)
            self.assertIn("positions", text)
            self.assertIn("Info", text)
            self.assertIn("NO 股票代码", text)
            self.assertIn("实时价格", text)
            self.assertIn("盈亏比", text)
            self.assertIn("600519.SH", text)
            self.assertIn("+10.00%", text)
            self.assertIn("+100.00", text)
            self.assertIn("定时任务", text)
            self.assertIn("daily-discover", text)
            self.assertIn("监控计划 active 0", text)
            for line in text.splitlines():
                self.assertLessEqual(len(line), 140)
            with storage.connect() as con:
                tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            self.assertNotIn("monitor_quotes", tables)

    def test_display_marks_last_snapshot_stale_when_qmt_sync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            position = BrokerPosition(
                ts_code="000001.SZ",
                name="平安银行",
                quantity=100,
                available_quantity=100,
                cost_price=10,
            )
            QmtPositionSyncService(storage=storage, client=_FakeBrokerClient(positions=[position])).sync()
            display = MonitorDisplay(
                settings=_settings(storage),
                storage=storage,
                provider=_FakeProvider(),
                position_sync=QmtPositionSyncService(
                    storage=storage,
                    client=_FakeBrokerClient(error=BrokerError("bridge offline")),
                ),
            )

            text = format_monitor_dashboard(display.snapshot(), width=140, height=28)

            self.assertIn("positions STALE", text)
            self.assertIn("最后成功", text)
            self.assertIn("bridge offline", text)
            self.assertIn("000001.SZ", text)


class MonitorCliTest(unittest.TestCase):
    def test_cli_watchlist_top_level_add_list_remove_and_non_tty_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"

            with patch("builtins.print") as printer:
                self.assertEqual(
                    main(["watchlist", "add", "--stocks", "000001,600519", "--db", str(db)]),
                    0,
                )
                self.assertEqual(main(["watchlist", "list", "--db", str(db)]), 0)
                with patch("sats.cli.sys.stdin.isatty", return_value=False), patch("sats.cli.sys.stdout.isatty", return_value=False):
                    self.assertEqual(main(["watchlist", "--db", str(db)]), 0)
                self.assertEqual(main(["watchlist", "remove", "--stocks", "000001", "--db", str(db)]), 0)

            printed = "\n".join(str(call.args[0]) for call in printer.call_args_list if call.args)
            self.assertIn("已加入关注列表 2 只股票", printed)
            self.assertIn("000001.SZ", printed)
            self.assertIn("600519.SH", printed)
            self.assertIn("已删除 1 只股票", printed)
            rows = DuckDBStorage(db).list_monitor_watchlist()
            self.assertEqual([row["ts_code"] for row in rows], ["600519.SH"])

    def test_cli_watchlist_clear_removes_all_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db)
            storage.upsert_monitor_watchlist(ts_code="000001.SZ", name="平安银行")
            storage.upsert_monitor_watchlist(ts_code="600519.SH", name="贵州茅台")

            with patch("builtins.print") as printer:
                self.assertEqual(main(["watchlist", "clear", "--db", str(db)]), 0)

            printed = "\n".join(str(call.args[0]) for call in printer.call_args_list if call.args)
            self.assertIn("已清空关注列表 2 只股票", printed)
            self.assertEqual(DuckDBStorage(db).list_monitor_watchlist(), [])

    def test_cli_watchlist_symbols_option_is_not_supported(self) -> None:
        with patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["watchlist", "add", "--symbols", "000001"])
            with self.assertRaises(SystemExit):
                main(["watchlist", "remove", "--symbols", "000001"])

    def test_cli_watchlist_import_screened_imports_selected_passed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db)
            storage.upsert_stock_basic(pd.DataFrame([{"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"}]))
            storage.upsert_screening_results(
                [
                    ScreeningResult(
                        trade_date="20260514",
                        ts_code="000001.SZ",
                        rule_name="chan_composite",
                        passed=True,
                        score=80,
                        matched_conditions=["chan_third_buy"],
                        failed_conditions=[],
                        metrics={"matched_chan_rules": ["三买"]},
                    )
                ]
            )

            with patch("sats.watchlist_editor.select_stock_rows", return_value=["000001.SZ"]), patch("builtins.print") as printer:
                self.assertEqual(
                    main(["watchlist", "import-screened", "--trade-date", "20260514", "--rule", "chan-composite", "--db", str(db)]),
                    0,
                )

            watchlist_row = DuckDBStorage(db).list_monitor_watchlist()[0]
            self.assertEqual(watchlist_row["ts_code"], "000001.SZ")
            self.assertEqual(watchlist_row["name"], "平安银行")
            self.assertIn("已加入关注列表 1 只股票", printer.call_args.args[0])

    def test_cli_watchlist_select_delete_uses_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db)
            storage.upsert_monitor_watchlist(ts_code="000001.SZ", name="平安银行")
            storage.upsert_monitor_watchlist(ts_code="600519.SH", name="贵州茅台")

            with patch("sats.watchlist_editor.select_stock_rows", return_value=["000001.SZ"]), patch("builtins.print"):
                self.assertEqual(main(["watchlist", "select-delete", "--db", str(db)]), 0)

            rows = DuckDBStorage(db).list_monitor_watchlist()
            self.assertEqual([row["ts_code"] for row in rows], ["600519.SH"])

    def test_watchlist_editor_adds_symbols_with_a_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            prompts = iter(["a", "000001,600519", "q"])

            with patch("sats.watchlist_editor.PromptSession") as prompt_session, patch("builtins.print"):
                prompt_session.return_value.prompt.side_effect = lambda *_args, **_kwargs: next(prompts)
                self.assertEqual(run_watchlist_editor(storage), 0)

            rows = storage.list_monitor_watchlist()
            self.assertEqual([row["ts_code"] for row in rows], ["000001.SZ", "600519.SH"])

    def test_watchlist_editor_deletes_selected_symbols_with_d_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_monitor_watchlist(ts_code="000001.SZ", name="平安银行")
            storage.upsert_monitor_watchlist(ts_code="600519.SH", name="贵州茅台")
            prompts = iter(["d", "q"])

            with (
                patch("sats.watchlist_editor.PromptSession") as prompt_session,
                patch("sats.watchlist_editor.select_stock_rows", return_value=["000001.SZ"]),
                patch("builtins.print"),
            ):
                prompt_session.return_value.prompt.side_effect = lambda *_args, **_kwargs: next(prompts)
                self.assertEqual(run_watchlist_editor(storage), 0)

            rows = storage.list_monitor_watchlist()
            self.assertEqual([row["ts_code"] for row in rows], ["600519.SH"])

    def test_watchlist_selection_dialog_uses_visible_current_row_style(self) -> None:
        fake_dialog = SimpleNamespace(run=lambda: ["000001.SZ"])

        with patch("sats.watchlist_editor.checkboxlist_dialog", return_value=fake_dialog) as dialog:
            result = select_stock_rows(
                [{"ts_code": "000001.SZ", "name": "平安银行", "matched_labels": ["三买"]}],
                title="加入关注列表",
                text="选择股票",
            )

        self.assertEqual(result, ["000001.SZ"])
        self.assertEqual(dialog.call_args.kwargs["values"], [("000001.SZ", "000001.SZ 平安银行 三买")])
        self.assertIs(dialog.call_args.kwargs["style"], WATCHLIST_DIALOG_STYLE)
        rules = dict(WATCHLIST_DIALOG_STYLE.style_rules)
        self.assertIn("bg:#2563eb", rules["checkbox-selected"])
        self.assertIn("#ffffff", rules["checkbox-selected"])
        self.assertEqual(rules["dialog frame.label"], "#9ca3af")
        self.assertIn("bg:#2563eb", rules["button.focused"])
        self.assertIn("#ffffff", rules["button.focused"])
        self.assertIn("bg:#2563eb", rules["button.focused button.text"])
        self.assertIn("#ffffff", rules["button.focused button.text"])
        self.assertIn("bg:#2563eb", rules["button.focused button.arrow"])
        self.assertIn("#ffffff", rules["button.focused button.arrow"])
        self.assertIn("bg:#374151", rules["button.text"])
        self.assertIn("bg:#374151", rules["button.arrow"])

    def test_cli_monitor_list_management_and_process_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            broker = _FakeBrokerClient()
            with patch("sats.trading.sync.broker_from_settings", return_value=broker), patch("builtins.print") as printer:
                self.assertEqual(main(["monitor", "positions", "list", "--db", str(db)]), 0)
            printed = "\n".join(str(call.args[0]) for call in printer.call_args_list if call.args)
            self.assertIn("000001.SZ", printed)

            with patch("builtins.print") as printer:
                self.assertEqual(main(["monitor", "watchlist", "add", "--symbol", "605300", "--db", str(db)]), 0)
                self.assertEqual(main(["monitor", "watchlist", "list", "--db", str(db)]), 0)
            printed = "\n".join(str(call.args[0]) for call in printer.call_args_list if call.args)
            self.assertIn("605300.SH", printed)

            fake_process = SimpleNamespace(pid=4321)
            with patch("subprocess.Popen", return_value=fake_process) as popen, patch("builtins.print") as printer:
                self.assertEqual(main(["monitor", "start", "--db", str(db)]), 0)
            popen.assert_called_once()
            self.assertIn("PID 4321", printer.call_args.args[0])

            with patch("os.kill") as kill, patch("builtins.print"):
                self.assertEqual(main(["monitor", "stop", "--db", str(db)]), 0)
            kill.assert_called_once()

    def test_removed_manual_position_and_qmt_sync_commands_are_rejected(self) -> None:
        with patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["monitor", "positions", "add", "--symbol", "000001", "--buy-price", "10", "--quantity", "100"])
            with self.assertRaises(SystemExit):
                main(["monitor", "positions", "remove", "--symbol", "000001"])
            with self.assertRaises(SystemExit):
                main(["qmt", "sync", "positions"])

    def test_qmt_positions_automatically_replaces_monitor_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db)
            storage.upsert_monitor_position(ts_code="600519.SH", quantity=100, buy_price=1000)
            broker = _FakeBrokerClient()

            with patch("sats.trading.sync.broker_from_settings", return_value=broker), patch("builtins.print") as printer:
                self.assertEqual(main(["qmt", "positions", "--db", str(db)]), 0)

            self.assertIn("000001.SZ", printer.call_args.args[0])
            rows = DuckDBStorage(db).list_monitor_positions()
            self.assertEqual([row["ts_code"] for row in rows], ["000001.SZ"])

    def test_position_query_failure_returns_nonzero_without_printing_stale_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db)
            QmtPositionSyncService(storage=storage, client=_FakeBrokerClient()).sync()
            broker = _FakeBrokerClient(error=BrokerError("bridge offline"))

            with patch("sats.trading.sync.broker_from_settings", return_value=broker), patch("builtins.print") as printer:
                with self.assertRaisesRegex(SystemExit, "bridge offline"):
                    main(["monitor", "positions", "list", "--db", str(db)])

            printer.assert_not_called()
            self.assertEqual(DuckDBStorage(db).list_monitor_positions()[0]["ts_code"], "000001.SZ")

    def test_cli_monitor_display_start_and_plain_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db)
            storage.upsert_monitor_position(ts_code="000001.SZ", name="平安银行", quantity=100, buy_price=10.0)
            fake_process = SimpleNamespace(pid=7654)
            with patch("sats.cli.MonitorDisplay") as display_class, patch("subprocess.Popen") as popen:
                self.assertEqual(main(["monitor-display", "start", "--db", str(db)]), 0)
            display_class.return_value.run.assert_called_once()
            popen.assert_not_called()

            with patch("subprocess.Popen", return_value=fake_process) as popen, patch("builtins.print"):
                self.assertEqual(main(["monitor-display", "start", "--new-terminal", "--db", str(db)]), 0)
            popen.assert_called_once()

            with (
                patch("sats.monitoring.display.AStockDataProvider", return_value=_FakeProvider()),
                patch("builtins.print") as printer,
            ):
                self.assertEqual(main(["monitor-display", "run", "--plain", "--db", str(db)]), 0)
            self.assertIn("+10.00%", printer.call_args.args[0])

    def test_qmt_cli_dry_run_writes_audit_without_bridge_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            with patch("builtins.print") as printer:
                self.assertEqual(main(["qmt", "buy", "--symbol", "000001", "--quantity", "100", "--dry-run", "--db", str(db)]), 0)

            self.assertIn("dry_run", printer.call_args.args[0])
            order = DuckDBStorage(db).list_broker_orders()[0]
            self.assertEqual(order["status"], "dry_run")
            self.assertEqual(order["ts_code"], "000001.SZ")

    def test_monitor_plan_cli_validate_import_activate_show_and_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(_plan_payload(), ensure_ascii=False), encoding="utf-8")

            with patch("builtins.print") as printer:
                self.assertEqual(main(["monitor", "plans", "validate", "--file", str(path)]), 0)
                self.assertEqual(
                    main(["monitor", "plans", "import", "--file", str(path), "--db", str(db)]),
                    0,
                )
            imported = DuckDBStorage(db).list_monitor_plans()[0]
            plan_id = imported["plan_id"]
            self.assertIn("计划有效", printer.call_args_list[0].args[0])
            self.assertIn("草稿", printer.call_args_list[1].args[0])

            with patch("builtins.print") as printer:
                self.assertEqual(main(["monitor", "plans", "activate", "--plan-id", plan_id, "--db", str(db)]), 0)
                self.assertEqual(main(["monitor", "plans", "show", "--plan-id", plan_id, "--db", str(db)]), 0)
                self.assertEqual(main(["monitor", "plans", "list", "--db", str(db)]), 0)
                self.assertEqual(main(["monitor", "plans", "disable", "--plan-id", plan_id, "--db", str(db)]), 0)

            self.assertIn('"status": "active"', printer.call_args_list[1].args[0])
            self.assertEqual(DuckDBStorage(db).get_monitor_plan(plan_id)["status"], "disabled")


def _event(ts_code: str, *, side: str) -> dict:
    return {
        "event_id": f"event-{ts_code}-{side}",
        "event_key": f"key-{ts_code}-{side}",
        "ts_code": ts_code,
        "name": "平安银行",
        "source_list": "watchlist",
        "rule_name": "chan_signals",
        "signal_name": "chan_third_buy",
        "signal_label": "三买",
        "side": side,
        "score": 80,
        "price": 11,
        "trade_time": "2026-05-14 10:00:00",
        "message": "三买",
    }


def _screening_result(*, side: str, label: str, signal_name: str) -> ScreeningResult:
    signal = {
        "signal_name": signal_name,
        "label": label,
        "side": side,
        "passed": True,
        "score": 80.0,
        "watch_levels": {"support": 10.0},
        "risk_flags": [],
    }
    return ScreeningResult(
        trade_date="20260514",
        ts_code="000001.SZ",
        rule_name="chan_signals",
        passed=True,
        score=80,
        matched_conditions=[signal_name],
        failed_conditions=[],
        metrics={"chan_signals": [signal]},
    )


def _settings(storage: DuckDBStorage):
    return SimpleNamespace(
        db_path=storage.db_path,
        tickflow_api_key="key",
        tickflow_base_url="https://api.tickflow.org",
        tickflow_timeout_seconds=1,
        tickflow_max_retries=0,
    )


def _plan_payload(*, action: str = "notify") -> dict:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    end_date = (now + timedelta(days=1)).strftime("%Y%m%d")
    return {
        "schema_version": 1,
        "name": "测试计划",
        "start_date": "20260619",
        "end_date": end_date,
        "active_windows": [{"start": "09:30", "end": "11:30"}],
        "items": [
            {
                "symbol": "000001",
                "name": "平安银行",
                "summary": "等待突破",
                "risk_note": "跌破支撑",
                "trigger_groups": [
                    {
                        "action": action,
                        "message": "价格突破",
                        "conditions": [
                            {
                                "subject": {"type": "stock"},
                                "metric": "latest_price",
                                "operator": ">=",
                                "value": 11.0,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _plan_now() -> datetime:
    return datetime(2026, 6, 19, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class _FakeProvider:
    def load_historical_daily_klines(self, symbols, *, start_date, end_date, storage=None):
        return pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "trade_date": "20260513",
                    "open": 10.0,
                    "high": 10.6,
                    "low": 9.8,
                    "close": 10.2,
                    "vol": 1000.0,
                    "amount": 10000.0,
                    "pct_chg": 1.0,
                }
                for symbol in symbols
            ]
        )

    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        return pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "trade_date": "20260514",
                    "trade_time": "2026-05-14 10:00:00",
                    "open": 10.2,
                    "high": 11.2,
                    "low": 10.1,
                    "close": 11.0,
                    "pre_close": 10.0,
                    "vol": 2000.0,
                    "amount": 22000.0,
                    "pct_chg": 10.0,
                    "data_source": "tickflow_quote",
                }
                for symbol in (symbols or [])
            ]
        )

    def load_realtime_minute_klines(self, symbols, *, period="30m", count=None):
        return pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "period": period,
                    "trade_date": "20260514",
                    "trade_time": "2026-05-14 10:00:00",
                    "open": 10.5,
                    "high": 11.2,
                    "low": 10.4,
                    "close": 11.0,
                    "vol": 100,
                    "amount": 1100,
                    "data_source": "tickflow",
                }
                for symbol in symbols
            ]
        )


class _PlanProvider:
    def __init__(self, prices: list[float | None], *, index_pct: float = 0.6) -> None:
        self.prices = list(prices)
        self.index_pct = index_pct
        self.calls = 0

    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        price = self.prices[min(self.calls, len(self.prices) - 1)]
        self.calls += 1
        rows = []
        for symbol in symbols or []:
            if symbol == "000001.SH":
                rows.append(
                    {
                        "ts_code": symbol,
                        "close": 3200.0,
                        "pre_close": 3180.0,
                        "pct_chg": self.index_pct,
                        "trade_time": "2026-06-19 10:00:00",
                        "data_source": "plan-test-index",
                    }
                )
            elif price is not None:
                rows.append(
                    {
                        "ts_code": symbol,
                        "close": price,
                        "pre_close": 10.0,
                        "pct_chg": (price / 10.0 - 1.0) * 100.0,
                        "trade_time": "2026-06-19 10:00:00",
                        "data_source": "plan-test-stock",
                    }
                )
        return pd.DataFrame(rows)


class _FakeBrokerClient:
    provider = "qmt"
    account_id = "acct"

    def __init__(
        self,
        *,
        positions: list[BrokerPosition] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.requests = []
        self.position_rows = positions if positions is not None else [
            BrokerPosition(ts_code="000001.SZ", quantity=100, available_quantity=100, cost_price=10)
        ]
        self.position_error = error

    def asset(self) -> BrokerAsset:
        return BrokerAsset(available_cash=30000, total_asset=100000, account_id=self.account_id)

    def positions(self) -> list[BrokerPosition]:
        if self.position_error is not None:
            raise self.position_error
        return list(self.position_rows)

    def place_order(self, request) -> OrderResult:
        self.requests.append(request)
        return OrderResult(sats_order_id="sats-1", broker_order_id="qmt-1", status="submitted", message="ok", request=request.to_dict())


class _FakePositionSync:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def sync(self) -> list[BrokerPosition]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return []


if __name__ == "__main__":
    unittest.main()
