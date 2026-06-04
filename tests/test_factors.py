from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from sats.cli import main
from sats.factors.analysis import analyze_factor_panel
from sats.factors.composite import compute_factor_panels, compose_scores, pick_top
from sats.factors.ml import predict_factor_ml_model, train_factor_ml_model
from sats.factors.panel import panel_from_screening_inputs
from sats.factors.registry import Registry, SkipAlpha
from sats.factors.service import compute_factor_snapshot, summarize_factor_exposure
from sats.screening.base import ScreeningInput
from sats.storage.duckdb import DuckDBStorage


class FactorRegistryTest(unittest.TestCase):
    def test_registry_loads_requested_zoos(self) -> None:
        registry = Registry()
        self.assertEqual(registry.health(), {"loaded": 308, "failed": 0, "errors": []})
        self.assertEqual(len(registry.list(zoo="alpha101")), 101)
        self.assertEqual(len(registry.list(zoo="gtja191")), 191)
        self.assertEqual(len(registry.list(zoo="barra_style")), 16)

    def test_representative_factors_compute_wide_output(self) -> None:
        panel = _sample_panel()
        registry = Registry()
        for factor_id in ("alpha101_001", "gtja191_001", "barra_style_value"):
            output = registry.compute(factor_id, panel)
            self.assertEqual(output.shape, panel["close"].shape)
            self.assertFalse(np.isinf(output.to_numpy(dtype=float, na_value=np.nan)).any())

    def test_missing_barra_data_is_unavailable_not_fabricated(self) -> None:
        panel = _sample_panel()
        with self.assertRaises(SkipAlpha):
            Registry().compute("barra_style_state_ownership_proxy", panel)

    def test_short_momentum_does_not_read_future_data(self) -> None:
        panel = _sample_panel()
        registry = Registry()
        trade_date = panel["close"].index[120]
        before = registry.compute("barra_style_short_momentum", panel).loc[trade_date].copy()
        changed = {key: value.copy() for key, value in panel.items()}
        changed["close"].iloc[121:] = changed["close"].iloc[121:] * 10.0
        after = registry.compute("barra_style_short_momentum", changed).loc[trade_date]
        pd.testing.assert_series_equal(before, after)


class FactorAnalysisAndPickTest(unittest.TestCase):
    def test_analysis_and_composite_pick_are_deterministic(self) -> None:
        panel = _sample_panel()
        registry = Registry()
        factor = registry.compute("barra_style_value", panel)
        result = analyze_factor_panel(
            "barra_style_value",
            factor,
            panel["close"],
            trade_date=panel["close"].index[-3],
            horizon=1,
        )
        self.assertGreater(result.coverage, 0.9)
        self.assertIn("Group_1", result.group_equity)

        panels, metas, warnings = compute_factor_panels(
            ["barra_style_value", "barra_style_short_momentum"],
            panel,
            registry=registry,
        )
        self.assertEqual(warnings, [])
        score = compose_scores(
            panels,
            metas,
            neutralize="industry",
            group_panel=panel["industry"],
        )
        picks = pick_top(score, panels, trade_date=panel["close"].index[-3], top=3)
        self.assertEqual([item.rank for item in picks], [1, 2, 3])
        self.assertEqual(len(picks), 3)

    def test_panel_from_screening_inputs_builds_factor_columns(self) -> None:
        inputs = _screening_inputs()
        result = panel_from_screening_inputs(inputs)
        for column in ("open", "high", "low", "close", "volume", "amount", "vwap", "pe", "pb", "ps", "industry"):
            self.assertIn(column, result.panel)
        self.assertEqual(len(result.symbols), 8)
        self.assertEqual(result.names["000001.SZ"], "股票1")

    def test_factor_snapshot_profile_summarizes_exposure(self) -> None:
        panel = _sample_panel()
        trade_date = panel["close"].index[-3]
        snapshot = compute_factor_snapshot(panel, trade_date=trade_date, profile="balanced")
        payload = summarize_factor_exposure(snapshot, ["000001.SZ"])
        self.assertEqual(payload["profile"], "balanced")
        self.assertGreater(payload["coverage"], 0)
        exposure = payload["exposures"][0]
        self.assertEqual(exposure["ts_code"], "000001.SZ")
        self.assertIn("barra_style_value", exposure["factor_values"])
        self.assertFalse(np.isinf(float(exposure["score"])))


class FactorCliTest(unittest.TestCase):
    def test_cli_factor_list_and_show(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["factor", "list", "--zoo", "barra_style"]), 0)
        self.assertIn("barra_style_value", stdout.getvalue())

        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["factor", "show", "--factor", "barra_style_value", "--json"]), 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["id"], "barra_style_value")
        self.assertEqual(payload["theme"], ["value"])

    def test_cli_factor_analyze_and_pick_write_screening(self) -> None:
        inputs = _screening_inputs(days=120)
        symbols = ",".join(item.ts_code for item in inputs)
        trade_date = inputs[0].trade_date
        fake_provider = _FakeFactorProvider(inputs)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            settings = SimpleNamespace(db_path=db_path, project_root=Path(tmp), openai_model="test")
            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.AStockDataProvider", return_value=fake_provider),
            ):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(
                        main(
                            [
                                "factor",
                                "analyze",
                                "--factor",
                                "barra_style_value",
                                "--trade-date",
                                trade_date,
                                "--symbols",
                                symbols,
                                "--noreport",
                            ]
                        ),
                        0,
                    )
                self.assertIn("RankIC", stdout.getvalue())

                stdout = StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(
                        main(
                            [
                                "factor",
                                "pick",
                                "--factors",
                                "barra_style_value,barra_style_short_momentum",
                                "--trade-date",
                                trade_date,
                                "--symbols",
                                symbols,
                                "--top",
                                "3",
                                "--neutralize",
                                "industry",
                                "--write-screening",
                                "--noreport",
                            ]
                        ),
                        0,
                    )
                self.assertIn("run_id:", stdout.getvalue())
            rows = DuckDBStorage(db_path).list_screening_results(
                trade_date=trade_date,
                rule_name="factor:multi_factor",
                passed=True,
            )
            self.assertEqual(len(rows), 3)
            self.assertIn("factor_values", rows[0]["metrics"])

    def test_cli_factor_pick_uses_profile_when_factors_omitted(self) -> None:
        inputs = _screening_inputs(days=260)
        symbols = ",".join(item.ts_code for item in inputs)
        fake_provider = _FakeFactorProvider(inputs)
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", project_root=Path(tmp), openai_model="test")
            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.AStockDataProvider", return_value=fake_provider),
            ):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(
                        main(
                            [
                                "factor",
                                "pick",
                                "--profile",
                                "balanced",
                                "--trade-date",
                                inputs[0].trade_date,
                                "--symbols",
                                symbols,
                                "--top",
                                "2",
                                "--noreport",
                            ]
                        ),
                        0,
                    )
        self.assertIn("run_id:", stdout.getvalue())


class _MeanFactorModel:
    def fit(self, x, y):
        self.feature_columns = list(x.columns)
        self.mean = float(y.mean())
        return self

    def predict(self, x):
        return x.sum(axis=1).to_numpy(dtype=float) * 0.01 + self.mean


class FactorMLTest(unittest.TestCase):
    def test_train_and_predict_factor_ml_with_mock_model(self) -> None:
        inputs = _screening_inputs(days=260)
        fake_provider = _FakeFactorProvider(inputs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = DuckDBStorage(root / "sats.duckdb")
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            train = train_factor_ml_model(
                settings=settings,
                storage=storage,
                provider=fake_provider,
                model_type="lightgbm",
                profile="balanced",
                train_start=inputs[0].daily["trade_date"].iloc[20],
                train_end=inputs[0].daily["trade_date"].iloc[-20],
                valid_end=inputs[0].trade_date,
                lookback_days=260,
                horizon=1,
                symbols=[item.ts_code for item in inputs],
                model_factory=lambda model_type: _MeanFactorModel(),
            )
            self.assertTrue(Path(train.model_path).exists())
            self.assertEqual(storage.get_factor_run(train.run_id)["kind"], "ml_train")

            result = predict_factor_ml_model(
                settings=settings,
                storage=storage,
                provider=fake_provider,
                model_run=train.run_id,
                trade_date=inputs[0].trade_date,
                profile="balanced",
                factor_ids=train.factor_ids,
                top=3,
                lookback_days=260,
                symbols=[item.ts_code for item in inputs],
            )

            self.assertEqual(len(result.candidates), 3)
            self.assertEqual(storage.get_factor_run(result.run_id)["kind"], "ml_predict")
            self.assertIn("model_run", result.candidates[0].metrics)
            with self.assertRaisesRegex(ValueError, "profile mismatch"):
                predict_factor_ml_model(
                    settings=settings,
                    storage=storage,
                    provider=fake_provider,
                    model_run=train.run_id,
                    trade_date=inputs[0].trade_date,
                    profile="short_term",
                    top=3,
                    lookback_days=260,
                    symbols=[item.ts_code for item in inputs],
                )


class _FakeFactorProvider:
    def __init__(self, inputs: list[ScreeningInput]) -> None:
        self.inputs = inputs

    def load_screening_inputs(self, symbols, trade_date, *, storage, trade_days, rule_name=None):
        wanted = set(symbols)
        return [item for item in self.inputs if item.ts_code in wanted]

    def load_all_screening_inputs(self, trade_date, *, storage, trade_days, rule_name=None):
        return list(self.inputs)


def _sample_panel(days: int = 320, symbol_count: int = 8) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2025-01-01", periods=days).strftime("%Y%m%d")
    symbols = [f"{index:06d}.SZ" for index in range(1, symbol_count + 1)]
    steps = rng.normal(0.02, 0.12, size=(len(dates), len(symbols)))
    close = pd.DataFrame(10 + np.cumsum(steps, axis=0) + np.arange(len(symbols))[None, :], index=dates, columns=symbols)
    open_ = close * (1 + rng.normal(0, 0.01, size=close.shape))
    high = pd.DataFrame(
        np.maximum(open_.to_numpy(), close.to_numpy()) * (1 + rng.random(close.shape) * 0.02),
        index=dates,
        columns=symbols,
    )
    low = pd.DataFrame(
        np.minimum(open_.to_numpy(), close.to_numpy()) * (1 - rng.random(close.shape) * 0.02),
        index=dates,
        columns=symbols,
    )
    volume = pd.DataFrame(rng.lognormal(10, 0.25, size=close.shape), index=dates, columns=symbols)
    amount = volume * close * 100 / 1000
    industry_labels = ["银行", "银行", "科技", "科技", "医药", "医药", "消费", "消费"]
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
        "vwap": close,
        "pe": close + 10,
        "pb": close / 10,
        "ps": close / 5,
        "turnover_rate": volume / 10000,
        "total_mv": close * 1_000_000,
        "industry": pd.DataFrame([industry_labels[:symbol_count]] * len(dates), index=dates, columns=symbols),
    }


def _screening_inputs(days: int = 90) -> list[ScreeningInput]:
    panel = _sample_panel(days=days)
    trade_date = panel["close"].index[-1]
    inputs: list[ScreeningInput] = []
    for index, ts_code in enumerate(panel["close"].columns, start=1):
        daily = pd.DataFrame(
            {
                "trade_date": panel["close"].index,
                "open": panel["open"][ts_code].to_numpy(),
                "high": panel["high"][ts_code].to_numpy(),
                "low": panel["low"][ts_code].to_numpy(),
                "close": panel["close"][ts_code].to_numpy(),
                "vol": panel["volume"][ts_code].to_numpy(),
                "amount": panel["amount"][ts_code].to_numpy(),
            }
        )
        daily_basic = pd.DataFrame(
            {
                "trade_date": panel["close"].index,
                "pe": panel["pe"][ts_code].to_numpy(),
                "pb": panel["pb"][ts_code].to_numpy(),
                "ps": panel["ps"][ts_code].to_numpy(),
                "turnover_rate": panel["turnover_rate"][ts_code].to_numpy(),
                "total_mv": panel["total_mv"][ts_code].to_numpy(),
            }
        )
        inputs.append(
            ScreeningInput(
                ts_code=ts_code,
                trade_date=trade_date,
                daily=daily,
                daily_basic=daily_basic,
                stock_basic={"name": f"股票{index}", "industry": str(panel["industry"][ts_code].iloc[-1])},
            )
        )
    return inputs


if __name__ == "__main__":
    unittest.main()
