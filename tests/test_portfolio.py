from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from sats.agent.models import AgentExecutionPolicy
from sats.agent.planner import build_agent_plan
from sats.agent.tools import build_default_tool_registry
from sats.cli import main
from sats.monitoring.plans import validate_monitor_plan
from sats.monitoring.service import MonitorConfig, MonitorService
from sats.portfolio import DailyPortfolioAgent, PortfolioConfig, PortfolioStore
from sats.scheduler import compute_next_run
from sats.storage import DuckDBStorage
from sats.trading.models import BrokerAsset, OrderResult


class PortfolioWorkflowTest(unittest.TestCase):
    def test_paper_afternoon_buy_selects_five_and_trades_within_seventy_percent_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            _seed_stock_basic(storage)
            provider = _FakePortfolioProvider()
            agent = _agent(storage, provider=provider, now=datetime(2026, 6, 23, 14, 25, tzinfo=ZoneInfo("Asia/Shanghai")))

            result = agent.run(
                phase="afternoon-buy",
                trade_date="20260623",
                config=PortfolioConfig(trading_mode="paper", llm_enabled=False),
            )

            selected = [item for item in result.candidates if item.selected]
            self.assertEqual(len(result.candidates), 10)
            self.assertEqual(len(selected), 5)
            self.assertLessEqual(max(_industry_counts(selected).values()), 2)
            self.assertTrue(all(item.effective_trade_date == "20260624" for item in selected))
            store = PortfolioStore(storage)
            account = store.paper_account("default")
            self.assertEqual(len(store.paper_positions("default")), 5)
            self.assertEqual(len(store.paper_trades("default")), 5)
            self.assertLessEqual(account["market_value"], account["total_asset"] * 0.70 + 1)
            self.assertTrue(all(row["name"] for row in store.paper_positions("default")))

    def test_afternoon_scan_and_intraday_reviews_do_not_open_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            _seed_stock_basic(storage)
            provider = _FakePortfolioProvider()
            agent = _agent(storage, provider=provider, now=datetime(2026, 6, 23, 14, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
            config = PortfolioConfig(trading_mode="paper", llm_enabled=False)

            scan = agent.run(phase="afternoon-scan", trade_date="20260623", config=config)
            review = agent.run(phase="review", trade_date="20260623", config=config)

            self.assertEqual(len(scan.candidates), 10)
            self.assertEqual(len(PortfolioStore(storage).paper_trades("default")), 0)
            self.assertEqual(len(PortfolioStore(storage).paper_positions("default")), 0)
            self.assertEqual([row for row in review.actions if row.get("side") == "buy"], [])

    def test_paper_stop_loss_obeys_t_plus_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            _seed_stock_basic(storage)
            provider = _FakePortfolioProvider(price=10.0)
            current = [datetime(2026, 6, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            agent = _agent(storage, provider=provider, now_fn=lambda: current[0])
            config = PortfolioConfig(trading_mode="paper", llm_enabled=False)

            agent.run(phase="afternoon-buy", trade_date="20260623", config=config)
            provider.price = 9.0
            current[0] = datetime(2026, 6, 23, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
            same_day = agent.run(phase="plan-finalize", trade_date="20260623", config=config)
            self.assertEqual([row for row in same_day.actions if row.get("side") == "sell"], [])

            current[0] = datetime(2026, 6, 24, 9, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
            next_day = agent.run(phase="morning", trade_date="20260624", config=config)

            self.assertEqual(len([row for row in next_day.actions if row.get("status") == "filled"]), 5)
            self.assertEqual(PortfolioStore(storage).paper_positions("default"), [])

    def test_live_scan_creates_intents_and_only_approval_calls_broker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            _seed_stock_basic(storage)
            provider = _FakePortfolioProvider()
            agent = _agent(storage, provider=provider, now=datetime(2026, 6, 23, 14, 25, tzinfo=ZoneInfo("Asia/Shanghai")))

            result = agent.run(
                phase="afternoon-buy",
                trade_date="20260623",
                config=PortfolioConfig(trading_mode="live", llm_enabled=False),
            )

            self.assertEqual(len(result.actions), 5)
            self.assertTrue(all(row["status"] == "pending" for row in result.actions))
            broker = _FakeBroker()
            intent_id = result.actions[0]["intent_id"]
            approved = agent.approve_live_intent(intent_id, client=broker)
            duplicate = PortfolioStore(storage).get_pending_intent(intent_id)

            self.assertEqual(approved["status"], "submitted")
            self.assertEqual(len(broker.requests), 1)
            self.assertEqual(duplicate["status"], "submitted")
            with self.assertRaisesRegex(ValueError, "不是 pending"):
                agent.approve_live_intent(intent_id, client=broker)

    def test_report_phase_writes_markdown_daily_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            _seed_stock_basic(storage)
            provider = _FakePortfolioProvider()
            current = [datetime(2026, 6, 23, 14, 25, tzinfo=ZoneInfo("Asia/Shanghai"))]
            agent = _agent(storage, provider=provider, now_fn=lambda: current[0])
            config = PortfolioConfig(trading_mode="paper", llm_enabled=False)

            agent.run(phase="afternoon-buy", trade_date="20260623", config=config)
            current[0] = datetime(2026, 6, 23, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            report = agent.run(phase="report", trade_date="20260623", config=config)

            report_path = Path(report.report_path)
            self.assertTrue(report_path.exists())
            text = report_path.read_text(encoding="utf-8")
            self.assertIn("# SATS Portfolio 每日总结 20260623", text)
            self.assertIn("## 当日成交", text)
            self.assertIn("000001.SZ", text)
            self.assertIn("股票1", text)
            self.assertIn("报告", report.message)

    def test_monitor_plan_v2_accepts_market_and_position_metrics(self) -> None:
        payload = {
            "schema_version": 2,
            "name": "组合风控",
            "start_date": "20260623",
            "end_date": "20260630",
            "active_windows": [{"start": "09:30", "end": "14:55"}],
            "items": [
                {
                    "symbol": "000001",
                    "name": "平安银行",
                    "trigger_groups": [
                        {
                            "action": "sell",
                            "conditions": [
                                {
                                    "subject": {"type": "market"},
                                    "metric": "market_regime_score",
                                    "operator": "<",
                                    "value": 45,
                                },
                                {
                                    "subject": {"type": "position"},
                                    "metric": "position_pnl_pct",
                                    "operator": "<=",
                                    "value": -5,
                                },
                            ],
                        }
                    ],
                }
            ],
        }

        normalized = validate_monitor_plan(payload)

        self.assertEqual(normalized["schema_version"], 2)
        self.assertEqual(normalized["items"][0]["name"], "平安银行")

    def test_trading_day_schedule_skips_weekends(self) -> None:
        current = datetime(2026, 6, 26, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(
            compute_next_run(current, schedule_kind="trading_day", days=[], time_of_day="09:40"),
            "2026-06-29 09:40:00",
        )

    def test_agent_routes_portfolio_request_to_dedicated_workflow(self) -> None:
        settings = SimpleNamespace(
            project_root=Path("."),
            db_path=Path("/tmp/sats-agent-portfolio-test.duckdb"),
            openai_model="test",
            light_model_name="test",
        )

        plan = build_agent_plan(
            "运行盘中10选5自动模拟交易",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=build_default_tool_registry(),
        )

        self.assertEqual(plan.steps[0].tool_name, "workflow.daily_portfolio")
        self.assertEqual(plan.steps[0].arguments["trading_mode"], "paper")
        self.assertEqual(plan.steps[0].arguments["phase"], "afternoon-buy")

    def test_portfolio_schedule_install_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"

            self.assertEqual(main(["portfolio", "schedule", "install", "--mode", "paper", "--db", str(db)]), 0)
            self.assertEqual(main(["portfolio", "schedule", "install", "--mode", "paper", "--db", str(db)]), 0)

            tasks = DuckDBStorage(db).list_scheduled_tasks()
            self.assertEqual(len(tasks), 8)
            self.assertTrue(all(row["schedule_kind"] == "trading_day" for row in tasks))
            self.assertTrue(all("--db" in row["text"] for row in tasks))
            phases = {row["text"].split("--phase ", 1)[1].split(" ", 1)[0] for row in tasks}
            self.assertIn("morning", phases)
            self.assertIn("afternoon-buy", phases)
            self.assertIn("report", phases)

    def test_monitor_hard_stop_sells_paper_position_and_dedupes_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            _seed_stock_basic(storage)
            provider = _FakePortfolioProvider(price=10.0)
            agent = _agent(
                storage,
                provider=provider,
                now=datetime(2026, 6, 23, 14, 25, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            config = PortfolioConfig(trading_mode="paper", llm_enabled=False)
            agent.run(phase="afternoon-buy", trade_date="20260623", config=config)
            with storage.connect() as con:
                con.execute(
                    "UPDATE paper_positions SET last_buy_trade_date = '20260622', available_quantity = quantity"
                )
            provider.price = 9.0
            monitor = MonitorService(
                settings=agent.settings,
                storage=storage,
                provider=provider,
                position_sync=_NoopPositionSync(),
            )

            first = monitor.run_once(MonitorConfig(lists=(), portfolio_recheck=True))
            first_request_count = len(
                [
                    row
                    for row in PortfolioStore(storage).list_review_requests(limit=10)
                    if row["trigger_type"] == "hard_stop_loss"
                ]
            )
            second = monitor.run_once(MonitorConfig(lists=(), portfolio_recheck=True))

            self.assertTrue(any(row.get("signal_name") == "hard_stop_loss" for row in first))
            sells = [row for row in PortfolioStore(storage).paper_trades("default") if row.get("side") == "sell"]
            self.assertGreaterEqual(len(sells), 1)
            requests = PortfolioStore(storage).list_review_requests(limit=10)
            self.assertGreaterEqual(first_request_count, 1)
            self.assertEqual(
                len([row for row in requests if row["trigger_type"] == "hard_stop_loss"]),
                first_request_count,
            )
            self.assertFalse(any(row.get("signal_name") == "hard_stop_loss" for row in second))


def _agent(
    storage: DuckDBStorage,
    *,
    provider: "_FakePortfolioProvider",
    now: datetime | None = None,
    now_fn=None,
) -> DailyPortfolioAgent:
    settings = SimpleNamespace(
        db_path=storage.db_path,
        project_root=storage.db_path.parent,
        openai_model="test-model",
        light_model_name="test-model",
        llm_timeout_seconds=1,
    )
    clock = now_fn or (lambda: now)
    return DailyPortfolioAgent(
        settings=settings,
        storage=storage,
        provider=provider,
        discovery_runner=_fake_discovery,
        committee_runner=_fake_committee,
        market_loader=_fake_market,
        now=clock,
    )


def _fake_discovery(**kwargs):
    candidates = []
    for index in range(10):
        code = f"{index + 1:06d}.SZ"
        candidates.append(
            SimpleNamespace(
                ts_code=code,
                name=f"股票{index + 1}",
                local_score=90 - index,
                ranking_score=90 - index,
                close=10.0,
                decision="buy",
                trend="看多",
                events=[{"confidence": 0.8, "score": 5}],
                key_levels={"support": [9.6]},
                indicator={"technical": {"atr": {"atr14": 0.2}}, "factor": {"score": 1.0}},
                hot_sectors=[],
                chan_signals=[],
                missing_fields=[],
            )
        )
    return SimpleNamespace(candidates=candidates)


def _fake_committee(symbols, **kwargs):
    return SimpleNamespace(
        reports=tuple(
            SimpleNamespace(
                ts_code=symbol,
                final_rating="Buy" if index < 5 else "Overweight",
                final_decision="看多但按计划执行",
                risk_debate="严格止损",
            )
            for index, symbol in enumerate(symbols)
        )
    )


def _fake_market(**kwargs):
    trade_date = kwargs["trade_date"]
    return {
        "trade_date": trade_date,
        "indices": [{"latest": {"pct_chg": 1.0}}],
        "market_breadth": {
            "advancing_count": 3000,
            "declining_count": 1000,
            "median_pct_chg": 0.5,
        },
        "limit_sentiment": {"limit_up_count": 50, "limit_down_count": 5},
        "hot_sector_context": {},
        "data_sources": {"index_quote": "fake", "market_breadth": "fake"},
        "missing_fields": [],
    }


class _FakePortfolioProvider:
    def __init__(self, *, price: float = 10.0) -> None:
        self.price = price

    def fetch_data_operation(self, operation, params, **kwargs):
        start = datetime.strptime(params["params"]["start_date"], "%Y%m%d")
        end = datetime.strptime(params["params"]["end_date"], "%Y%m%d")
        rows = []
        while start <= end:
            rows.append(
                {
                    "cal_date": start.strftime("%Y%m%d"),
                    "is_open": 1 if start.weekday() < 5 else 0,
                }
            )
            start += timedelta(days=1)
        return {"data": rows, "rows": rows}

    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        symbols = symbols or [f"{index + 1:06d}.SZ" for index in range(10)]
        return pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "name": f"股票{int(symbol[:6])}",
                    "price": self.price,
                    "close": self.price,
                    "pre_close": 10.0,
                    "pct_chg": (self.price / 10.0 - 1.0) * 100.0,
                    "trade_time": "2026-06-23 10:00:00",
                    "data_source": "fake",
                }
                for symbol in symbols
            ]
        )


class _FakeBroker:
    provider = "qmt"
    account_id = "live-test"

    def __init__(self) -> None:
        self.requests = []

    def asset(self):
        return BrokerAsset(cash=1_000_000, available_cash=1_000_000, total_asset=1_000_000)

    def positions(self):
        return []

    def place_order(self, request):
        self.requests.append(request)
        return OrderResult(
            sats_order_id="live-order-1",
            broker_order_id="broker-order-1",
            status="submitted",
            message="ok",
            request=request.to_dict(),
        )


class _NoopPositionSync:
    def sync(self):
        return None


def _seed_stock_basic(storage: DuckDBStorage) -> None:
    storage.upsert_stock_basic(
        pd.DataFrame(
            [
                {
                    "ts_code": f"{index + 1:06d}.SZ",
                    "symbol": f"{index + 1:06d}",
                    "name": f"股票{index + 1}",
                    "industry": f"行业{index // 2 + 1}",
                    "market": "主板",
                    "exchange": "SZSE",
                    "list_date": "20200101",
                }
                for index in range(10)
            ]
        )
    )


def _industry_counts(rows) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        result[row.industry] = result.get(row.industry, 0) + 1
    return result
