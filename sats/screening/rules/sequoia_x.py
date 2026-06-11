from __future__ import annotations

from typing import Any

import pandas as pd

from sats.screening.base import ScreeningInput, ScreeningResult, ScreeningRule


class TurtleTradeRule(ScreeningRule):
    name = "turtle_trade"

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        daily = _prepare_daily(data.daily, trade_date=data.trade_date)
        metrics = _base_metrics(data, daily)
        if len(daily) < 21:
            return _build_result(
                data,
                self.name,
                checks={"data_window_21": False},
                metrics={**metrics, "reason": "need at least 21 daily bars"},
            )
        if metrics["latest_daily_trade_date"] != data.trade_date:
            return _build_result(
                data,
                self.name,
                checks={"daily_trade_date_current": False},
                metrics={**metrics, "reason": "latest daily trade_date is not requested trade_date"},
            )

        latest = daily.iloc[-1]
        previous = daily.iloc[-2]
        high_20 = _num(daily.iloc[-21:-1]["high"].max())
        close = _num(latest.get("close"))
        amount = _num(latest.get("amount"))
        circ_mv = _latest_basic_value(data.daily_basic, "circ_mv", trade_date=data.trade_date)
        pct_chg = _pct_change(daily)

        checks = {
            "close_breaks_prior_20d_high": close > high_20,
            "amount_gte_100m": amount >= 100_000.0,
            "bullish_body": close > _num(latest.get("open")),
            "close_gt_previous_close": close > _num(previous.get("close")),
        }
        metrics.update(
            {
                "close": close,
                "prior_20d_high": high_20,
                "amount": amount,
                "amount_unit": "thousand_yuan",
                "circ_mv": circ_mv,
                "pct_chg": pct_chg,
            }
        )
        score = 100.0 + circ_mv / 1_000_000.0 if all(checks.values()) else None
        return _build_result(data, self.name, checks=checks, metrics=metrics, score=score)


class MaVolumeRule(ScreeningRule):
    name = "ma_volume"

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        daily = _prepare_daily(data.daily, trade_date=data.trade_date)
        metrics = _base_metrics(data, daily)
        if len(daily) < 20:
            return _build_result(
                data,
                self.name,
                checks={"data_window_20": False},
                metrics={**metrics, "reason": "need at least 20 daily bars"},
            )
        if metrics["latest_daily_trade_date"] != data.trade_date:
            return _build_result(
                data,
                self.name,
                checks={"daily_trade_date_current": False},
                metrics={**metrics, "reason": "latest daily trade_date is not requested trade_date"},
            )

        prepared = daily.copy()
        prepared["ma5"] = prepared["close"].rolling(5).mean()
        prepared["ma20"] = prepared["close"].rolling(20).mean()
        prepared["vol_ma20"] = prepared["vol"].rolling(20).mean()
        latest = prepared.iloc[-1]
        previous = prepared.iloc[-2]
        volume_ratio = _ratio(_num(latest.get("vol")), _num(latest.get("vol_ma20")))
        checks = {
            "ma5_crosses_above_ma20": _num(previous.get("ma5")) < _num(previous.get("ma20"))
            and _num(latest.get("ma5")) > _num(latest.get("ma20")),
            "volume_gt_1p5x_ma20": _num(latest.get("vol")) > _num(latest.get("vol_ma20")) * 1.5,
        }
        metrics.update(
            {
                "close": _num(latest.get("close")),
                "ma5": _num(latest.get("ma5")),
                "ma20": _num(latest.get("ma20")),
                "vol": _num(latest.get("vol")),
                "vol_ma20": _num(latest.get("vol_ma20")),
                "volume_ratio_20d": volume_ratio,
            }
        )
        return _build_result(data, self.name, checks=checks, metrics=metrics)


class HighTightFlagRule(ScreeningRule):
    name = "high_tight_flag"

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        daily = _prepare_daily(data.daily, trade_date=data.trade_date)
        metrics = _base_metrics(data, daily)
        if len(daily) < 40:
            return _build_result(
                data,
                self.name,
                checks={"data_window_40": False},
                metrics={**metrics, "reason": "need at least 40 daily bars"},
            )
        if metrics["latest_daily_trade_date"] != data.trade_date:
            return _build_result(
                data,
                self.name,
                checks={"daily_trade_date_current": False},
                metrics={**metrics, "reason": "latest daily trade_date is not requested trade_date"},
            )

        tail40 = daily.tail(40)
        tail10 = daily.tail(10)
        high40 = _num(tail40["high"].max())
        low40 = _num(tail40["low"].min())
        high10 = _num(tail10["high"].max())
        low10 = _num(tail10["low"].min())
        vol_ma20_prior = _num(daily["vol"].iloc[-21:-1].mean())
        latest_vol = _num(daily.iloc[-1].get("vol"))
        momentum_ratio = _ratio(high40, low40)
        consolidation_ratio = _ratio(high10, low10)
        volume_ratio = _ratio(latest_vol, vol_ma20_prior)
        checks = {
            "momentum_40d_gt_60pct": momentum_ratio > 1.6,
            "consolidation_10d_lt_15pct": 0 < consolidation_ratio < 1.15,
            "high_level_low10_gte_80pct_high40": low10 >= high40 * 0.8,
            "volume_shrink_lt_0p6x_prior20": latest_vol < vol_ma20_prior * 0.6,
        }
        metrics.update(
            {
                "high40": high40,
                "low40": low40,
                "high10": high10,
                "low10": low10,
                "momentum_ratio_40d": momentum_ratio,
                "consolidation_ratio_10d": consolidation_ratio,
                "vol": latest_vol,
                "prior20_vol_ma": vol_ma20_prior,
                "volume_ratio_prior20": volume_ratio,
            }
        )
        return _build_result(data, self.name, checks=checks, metrics=metrics)


class LimitUpShakeoutRule(ScreeningRule):
    name = "limit_up_shakeout"

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        daily = _prepare_daily(data.daily, trade_date=data.trade_date)
        metrics = _base_metrics(data, daily)
        if len(daily) < 3:
            return _build_result(
                data,
                self.name,
                checks={"data_window_3": False},
                metrics={**metrics, "reason": "need at least 3 daily bars"},
            )
        if metrics["latest_daily_trade_date"] != data.trade_date:
            return _build_result(
                data,
                self.name,
                checks={"daily_trade_date_current": False},
                metrics={**metrics, "reason": "latest daily trade_date is not requested trade_date"},
            )

        prev2 = daily.iloc[-3]
        prev1 = daily.iloc[-2]
        today = daily.iloc[-1]
        checks = {
            "yesterday_limit_up": _num(prev1.get("close")) >= _num(prev2.get("close")) * 1.095,
            "bearish_today": _num(today.get("close")) < _num(today.get("open")),
            "volume_gt_2x_yesterday": _num(today.get("vol")) > _num(prev1.get("vol")) * 2.0,
            "support_low_gte_yesterday_close": _num(today.get("low")) >= _num(prev1.get("close")),
        }
        metrics.update(
            {
                "prev2_close": _num(prev2.get("close")),
                "yesterday_close": _num(prev1.get("close")),
                "today_open": _num(today.get("open")),
                "today_low": _num(today.get("low")),
                "today_close": _num(today.get("close")),
                "yesterday_vol": _num(prev1.get("vol")),
                "today_vol": _num(today.get("vol")),
                "volume_ratio_yesterday": _ratio(_num(today.get("vol")), _num(prev1.get("vol"))),
            }
        )
        return _build_result(data, self.name, checks=checks, metrics=metrics)


class UptrendLimitDownRule(ScreeningRule):
    name = "uptrend_limit_down"

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        daily = _prepare_daily(data.daily, trade_date=data.trade_date)
        metrics = _base_metrics(data, daily)
        if len(daily) < 60:
            return _build_result(
                data,
                self.name,
                checks={"data_window_60": False},
                metrics={**metrics, "reason": "need at least 60 daily bars"},
            )
        if metrics["latest_daily_trade_date"] != data.trade_date:
            return _build_result(
                data,
                self.name,
                checks={"daily_trade_date_current": False},
                metrics={**metrics, "reason": "latest daily trade_date is not requested trade_date"},
            )

        prepared = daily.copy()
        prepared["ma20"] = prepared["close"].rolling(20).mean()
        prepared["ma60"] = prepared["close"].rolling(60).mean()
        prepared["vol_ma20"] = prepared["vol"].rolling(20).mean()
        previous = prepared.iloc[-2]
        today = prepared.iloc[-1]
        ma20 = _num(previous.get("ma20"))
        ma60 = _num(previous.get("ma60"))
        vol_ma20 = _num(today.get("vol_ma20"))
        checks = {
            "previous_ma20_gt_ma60": ma20 > 0 and ma60 > 0 and ma20 > ma60,
            "close_lte_90p5pct_previous_close": _num(today.get("close")) <= _num(previous.get("close")) * 0.905,
            "volume_gt_2x_ma20": vol_ma20 > 0 and _num(today.get("vol")) > vol_ma20 * 2.0,
        }
        metrics.update(
            {
                "previous_close": _num(previous.get("close")),
                "today_close": _num(today.get("close")),
                "previous_ma20": ma20,
                "previous_ma60": ma60,
                "today_vol": _num(today.get("vol")),
                "vol_ma20": vol_ma20,
                "volume_ratio_20d": _ratio(_num(today.get("vol")), vol_ma20),
            }
        )
        return _build_result(data, self.name, checks=checks, metrics=metrics)


class RpsBreakoutRule(ScreeningRule):
    name = "rps_breakout"
    required_trade_days = 121
    rps_period = 120
    rps_threshold = 90.0

    def prepare_inputs(self, inputs: list[ScreeningInput]) -> list[ScreeningInput]:
        records: list[dict[str, Any]] = []
        payloads: dict[str, dict[str, Any]] = {}
        for item in inputs:
            daily = _prepare_daily(item.daily, trade_date=item.trade_date)
            payloads[item.ts_code] = {
                "rps_available": False,
                "eligible_count": 0,
                "reason": "insufficient data",
            }
            if len(daily) <= self.rps_period:
                continue
            latest_trade_date = _latest_trade_date(daily)
            if latest_trade_date != item.trade_date:
                payloads[item.ts_code]["reason"] = "latest daily trade_date is not requested trade_date"
                payloads[item.ts_code]["latest_daily_trade_date"] = latest_trade_date
                continue
            base_close = _num(daily.iloc[-self.rps_period - 1].get("close"))
            close = _num(daily.iloc[-1].get("close"))
            if base_close <= 0 or close <= 0:
                payloads[item.ts_code]["reason"] = "invalid close"
                continue
            roll_high = _num(
                daily["high"]
                .rolling(window=self.rps_period, min_periods=self.rps_period // 2)
                .max()
                .iloc[-1]
            )
            records.append(
                {
                    "ts_code": item.ts_code,
                    "pct_change_120d": close / base_close - 1.0,
                    "roll_high_120d": roll_high,
                    "close": close,
                    "eligible_count": len(daily),
                }
            )

        if records:
            frame = pd.DataFrame(records)
            frame["rps"] = frame["pct_change_120d"].rank(pct=True) * 100.0
            for row in frame.to_dict("records"):
                payloads[str(row["ts_code"])] = {
                    "rps_available": True,
                    "eligible_count": int(row["eligible_count"]),
                    "pct_change_120d": float(row["pct_change_120d"]),
                    "roll_high_120d": float(row["roll_high_120d"]),
                    "close": float(row["close"]),
                    "rps": float(row["rps"]),
                }

        for item in inputs:
            item.metadata[self.name] = payloads.get(
                item.ts_code,
                {"rps_available": False, "eligible_count": 0, "reason": "not in ranking universe"},
            )
        return inputs

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        daily = _prepare_daily(data.daily, trade_date=data.trade_date)
        metrics = _base_metrics(data, daily)
        if len(daily) <= self.rps_period:
            return _build_result(
                data,
                self.name,
                checks={"data_window_121": False},
                metrics={**metrics, "reason": "need at least 121 daily bars"},
            )
        if metrics["latest_daily_trade_date"] != data.trade_date:
            return _build_result(
                data,
                self.name,
                checks={"daily_trade_date_current": False},
                metrics={**metrics, "reason": "latest daily trade_date is not requested trade_date"},
            )

        payload = data.metadata.get(self.name)
        precomputed = payload if isinstance(payload, dict) else {}
        close = _num(precomputed.get("close"), _num(daily.iloc[-1].get("close")))
        roll_high = _num(precomputed.get("roll_high_120d"))
        if roll_high <= 0:
            roll_high = _num(
                daily["high"]
                .rolling(window=self.rps_period, min_periods=self.rps_period // 2)
                .max()
                .iloc[-1]
            )
        rps = _num(precomputed.get("rps"))
        checks = {
            "rps_precomputed_available": bool(precomputed.get("rps_available")),
            "rps_gte_90": rps >= self.rps_threshold,
            "close_gte_90pct_120d_high": roll_high > 0 and close >= roll_high * 0.90,
        }
        metrics.update(
            {
                "rps": rps,
                "rps_threshold": self.rps_threshold,
                "pct_change_120d": _num(precomputed.get("pct_change_120d")),
                "roll_high_120d": roll_high,
                "close": close,
                "eligible_count": int(_num(precomputed.get("eligible_count"))),
                "rps_reason": str(precomputed.get("reason") or ""),
            }
        )
        score = rps if all(checks.values()) else None
        return _build_result(data, self.name, checks=checks, metrics=metrics, score=score)


def _base_metrics(data: ScreeningInput, daily: pd.DataFrame) -> dict[str, Any]:
    return {
        "daily_rows": len(daily),
        "latest_daily_trade_date": _latest_trade_date(daily),
        "data_source": data.metadata.get("data_source", "unknown"),
    }


def _build_result(
    data: ScreeningInput,
    rule_name: str,
    *,
    checks: dict[str, bool],
    metrics: dict[str, Any],
    score: float | None = None,
) -> ScreeningResult:
    matched = [name for name, passed in checks.items() if passed]
    failed = [name for name, passed in checks.items() if not passed]
    passed = bool(checks) and not failed
    resolved_score = float(score) if score is not None else _score_from_checks(matched, failed)
    return ScreeningResult(
        trade_date=data.trade_date,
        ts_code=data.ts_code,
        rule_name=rule_name,
        passed=passed,
        score=resolved_score,
        matched_conditions=matched,
        failed_conditions=failed,
        metrics=metrics,
    )


def _score_from_checks(matched: list[str], failed: list[str]) -> float:
    total = len(matched) + len(failed)
    if total <= 0:
        return 0.0
    return round(len(matched) / total * 100.0, 2)


def _prepare_daily(frame: pd.DataFrame, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = _rename_columns(frame.copy())
    required = ["trade_date", "open", "high", "low", "close"]
    for column in required:
        if column not in data.columns:
            raise ValueError(f"daily data missing required column: {column}")
    if "vol" not in data.columns:
        data["vol"] = 0.0
    if "amount" not in data.columns:
        data["amount"] = 0.0
    data["trade_date"] = data["trade_date"].astype(str).str.replace("-", "", regex=False)
    data = data[data["trade_date"] <= str(trade_date)]
    for column in ["open", "high", "low", "close", "vol", "amount", "pct_chg"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "high", "low", "close"])
    return data.sort_values("trade_date").reset_index(drop=True)


def _prepare_daily_basic(frame: pd.DataFrame, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if "trade_date" in data.columns:
        data["trade_date"] = data["trade_date"].astype(str).str.replace("-", "", regex=False)
        data = data[data["trade_date"] <= str(trade_date)]
    for column in ["circ_mv", "turnover_rate", "turnover_rate_f"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    if "trade_date" in data.columns:
        return data.sort_values("trade_date").reset_index(drop=True)
    return data.reset_index(drop=True)


def _rename_columns(data: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "date": "trade_date",
        "日期": "trade_date",
        "volume": "vol",
        "成交量": "vol",
        "turnover": "amount",
        "成交额": "amount",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "涨跌幅": "pct_chg",
    }
    existing = {str(column) for column in data.columns}
    rename: dict[Any, str] = {}
    for column in data.columns:
        target = aliases.get(str(column))
        if target and target not in existing:
            rename[column] = target
    return data.rename(columns=rename)


def _latest_trade_date(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return ""
    return str(frame["trade_date"].astype(str).max())


def _latest_basic_value(frame: pd.DataFrame, column: str, *, trade_date: str) -> float:
    data = _prepare_daily_basic(frame, trade_date=trade_date)
    if data.empty or column not in data.columns:
        return 0.0
    return _num(data.iloc[-1].get(column))


def _pct_change(data: pd.DataFrame) -> float:
    latest = data.iloc[-1]
    if "pct_chg" in data.columns and not pd.isna(latest.get("pct_chg")):
        return _num(latest.get("pct_chg"))
    if len(data) < 2:
        return 0.0
    previous_close = _num(data.iloc[-2].get("close"))
    close = _num(latest.get("close"))
    return (close / previous_close - 1.0) * 100.0 if previous_close > 0 else 0.0


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
