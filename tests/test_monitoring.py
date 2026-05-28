from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.cli import main
from sats.monitoring import MonitorConfig, MonitorDisplay, MonitorService, format_monitor_dashboard
from sats.screening.base import ScreeningResult
from sats.storage.duckdb import DuckDBStorage
from sats.trading.models import BrokerAsset, BrokerPosition, OrderResult
from sats.trading.monitor_provider import AutoTradeConfig, QmtTradingProvider
from sats.trading.sync import sync_positions_to_monitor
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
            storage.upsert_broker_positions([position.to_dict()], provider="qmt", account_id="acct")
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
            count = sync_positions_to_monitor(storage, [position], provider="qmt", account_id="acct")

            self.assertEqual(count, 1)
            self.assertEqual(storage.list_broker_positions(provider="qmt")[0]["ts_code"], "000001.SZ")
            self.assertEqual(storage.list_broker_orders()[0]["broker_order_id"], "qmt-1")
            self.assertEqual(storage.list_monitor_positions()[0]["note"], "qmt_sync:acct")


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
            service = MonitorService(settings=_settings(storage), storage=storage, provider=_FakeProvider())
            result = _screening_result(side="sell", label="一卖", signal_name="chan_first_sell")

            with patch("sats.screening.rules.chan_signals.ChanSignalsRule.evaluate", return_value=result):
                service.run_once(MonitorConfig(rules=("chan_signals",), lists=("positions",)))

            trade = storage.list_monitor_trade_events()[0]
            self.assertEqual(trade["action"], "sell")
            self.assertEqual(trade["quantity"], 100.0)

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
            for line in text.splitlines():
                self.assertLessEqual(len(line), 140)
            with storage.connect() as con:
                tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            self.assertNotIn("monitor_quotes", tables)


class MonitorCliTest(unittest.TestCase):
    def test_cli_watchlist_top_level_add_list_remove_and_non_tty_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"

            with patch("builtins.print") as printer:
                self.assertEqual(
                    main(["watchlist", "add", "--symbols", "000001,600519", "--db", str(db)]),
                    0,
                )
                self.assertEqual(main(["watchlist", "list", "--db", str(db)]), 0)
                with patch("sats.cli.sys.stdin.isatty", return_value=False), patch("sats.cli.sys.stdout.isatty", return_value=False):
                    self.assertEqual(main(["watchlist", "--db", str(db)]), 0)
                self.assertEqual(main(["watchlist", "remove", "--symbols", "000001", "--db", str(db)]), 0)

            printed = "\n".join(str(call.args[0]) for call in printer.call_args_list if call.args)
            self.assertIn("已加入关注列表 2 只股票", printed)
            self.assertIn("000001.SZ", printed)
            self.assertIn("600519.SH", printed)
            self.assertIn("已删除 1 只股票", printed)
            rows = DuckDBStorage(db).list_monitor_watchlist()
            self.assertEqual([row["ts_code"] for row in rows], ["600519.SH"])

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
            with patch("builtins.print") as printer:
                self.assertEqual(
                    main(
                        [
                            "monitor",
                            "positions",
                            "add",
                            "--symbol",
                            "000001",
                            "--name",
                            "平安银行",
                            "--buy-price",
                            "10.5",
                            "--quantity",
                            "100",
                            "--db",
                            str(db),
                        ]
                    ),
                    0,
                )
                self.assertEqual(main(["monitor", "positions", "list", "--db", str(db)]), 0)
            printed = "\n".join(str(call.args[0]) for call in printer.call_args_list if call.args)
            self.assertIn("已保存持仓", printed)
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

    def load_realtime_minute_klines(self, symbols, *, period="30m", count=None, storage=None):
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


class _FakeBrokerClient:
    provider = "qmt"
    account_id = "acct"

    def __init__(self) -> None:
        self.requests = []

    def asset(self) -> BrokerAsset:
        return BrokerAsset(available_cash=30000, total_asset=100000, account_id=self.account_id)

    def positions(self) -> list[BrokerPosition]:
        return [BrokerPosition(ts_code="000001.SZ", quantity=100, available_quantity=100, cost_price=10)]

    def place_order(self, request) -> OrderResult:
        self.requests.append(request)
        return OrderResult(sats_order_id="sats-1", broker_order_id="qmt-1", status="submitted", message="ok", request=request.to_dict())


if __name__ == "__main__":
    unittest.main()
