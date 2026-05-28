from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from sats.screening.base import ScreeningInput, ScreeningResult, ScreeningRule


@dataclass(frozen=True)
class PriceVolumeMaThresholds:
    pct_chg_min: float = 3.0
    pct_chg_max: float = 5.0
    volume_ratio_min: float = 1.0
    turnover_min: float = 5.0
    turnover_max: float = 10.0
    circ_mv_min: float = 500_000.0
    circ_mv_max: float = 2_000_000.0


class PriceVolumeMaRule(ScreeningRule):
    name = "price_volume_ma"

    def __init__(self, thresholds: PriceVolumeMaThresholds | None = None) -> None:
        self.thresholds = thresholds or PriceVolumeMaThresholds()

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        precomputed = data.metadata.get("price_volume_ma")
        if isinstance(precomputed, dict) and "selected" in precomputed:
            return self._build_precomputed_result(data, precomputed)

        checks: dict[str, bool] = {}
        metrics: dict[str, Any] = {}

        checks["not_st"] = not _is_st_stock(data.stock_basic)
        checks["not_bse"] = not _is_bse_stock(data.ts_code, data.stock_basic)

        daily = _prepare_daily(data.daily, trade_date=data.trade_date)
        daily_basic = _prepare_daily_basic(data.daily_basic, trade_date=data.trade_date)
        metrics["daily_rows"] = len(daily)
        metrics["daily_basic_rows"] = len(daily_basic)
        metrics["data_source"] = data.metadata.get("data_source", "unknown")
        metrics["latest_daily_trade_date"] = _latest_trade_date(daily)
        metrics["latest_basic_trade_date"] = _latest_trade_date(daily_basic)

        if len(daily) < 60:
            checks["data_window_60"] = False
            metrics["reason"] = "需要至少60个交易日以计算MA60"
            return self._build_result(data, checks=checks, metrics=metrics)
        checks["daily_trade_date_current"] = metrics["latest_daily_trade_date"] == data.trade_date
        if not checks["daily_trade_date_current"]:
            metrics["reason"] = "最新日线数据不是请求交易日"
            return self._build_result(data, checks=checks, metrics=metrics)
        if daily_basic.empty:
            checks["daily_basic_available"] = False
            metrics["reason"] = "缺少当日每日指标数据"
            return self._build_result(data, checks=checks, metrics=metrics)
        checks["daily_basic_trade_date_current"] = metrics["latest_basic_trade_date"] == data.trade_date
        if not checks["daily_basic_trade_date_current"]:
            metrics["reason"] = "最新每日指标数据不是请求交易日"
            return self._build_result(data, checks=checks, metrics=metrics)

        daily = _with_moving_averages(daily)
        latest = daily.iloc[-1]
        latest_basic = daily_basic.iloc[-1]

        close = _num(latest.get("close"))
        ma5 = _num(latest.get("ma5"))
        ma10 = _num(latest.get("ma10"))
        ma20 = _num(latest.get("ma20"))
        ma60 = _num(latest.get("ma60"))
        pct_chg = _pct_change(daily)
        volume_ratio = _volume_ratio(daily)
        turnover_rate = _turnover_rate(latest_basic)
        circ_mv = _num(latest_basic.get("circ_mv"))

        metrics.update(
            {
                "close": close,
                "pct_chg": pct_chg,
                "volume_ratio_5d": volume_ratio,
                "turnover_rate": turnover_rate,
                "circ_mv": circ_mv,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20,
                "ma60": ma60,
            }
        )

        checks["pct_chg_3_to_5"] = self.thresholds.pct_chg_min <= pct_chg <= self.thresholds.pct_chg_max
        checks["volume_ratio_gt_1"] = volume_ratio > self.thresholds.volume_ratio_min
        checks["turnover_rate_5_to_10"] = self.thresholds.turnover_min <= turnover_rate <= self.thresholds.turnover_max
        checks["circ_mv_50_to_200_yi"] = self.thresholds.circ_mv_min <= circ_mv <= self.thresholds.circ_mv_max
        checks["ma_bull_stack_5_10_20_60"] = ma5 > ma10 > ma20 > ma60

        return self._build_result(data, checks=checks, metrics=metrics)

    def _build_precomputed_result(self, data: ScreeningInput, precomputed: dict[str, Any]) -> ScreeningResult:
        selected = bool(precomputed.get("selected"))
        checks = {"price_volume_ma_union_selected": selected}
        raw_metrics = precomputed.get("raw_metrics") if isinstance(precomputed.get("raw_metrics"), dict) else {}
        metrics = {
            "data_source": data.metadata.get("data_source", "unknown"),
            "selection_source": str(precomputed.get("selection_source") or ""),
            "current_logic_passed": bool(precomputed.get("current_logic_passed")),
            "legacy_logic_passed": bool(precomputed.get("legacy_logic_passed")),
            "raw_metrics": raw_metrics,
        }
        score_metrics = _score_metrics_from_precomputed(raw_metrics)
        if score_metrics:
            metrics.update(score_metrics)
        return self._build_result(data, checks=checks, metrics=metrics)

    def _build_result(
        self,
        data: ScreeningInput,
        *,
        checks: dict[str, bool],
        metrics: dict[str, Any],
    ) -> ScreeningResult:
        matched = [name for name, passed in checks.items() if passed]
        failed = [name for name, passed in checks.items() if not passed]
        passed = bool(checks) and not failed
        score = _score(metrics, matched, failed)
        return ScreeningResult(
            trade_date=data.trade_date,
            ts_code=data.ts_code,
            rule_name=self.name,
            passed=passed,
            score=score,
            matched_conditions=matched,
            failed_conditions=failed,
            metrics=metrics,
        )


def _prepare_daily(frame: pd.DataFrame, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = _rename_columns(frame.copy())
    required = ["trade_date", "open", "high", "low", "close"]
    for column in required:
        if column not in data.columns:
            raise ValueError(f"daily data missing required column: {column}")
    if "vol" not in data.columns:
        if "volume" in data.columns:
            data["vol"] = data["volume"]
        else:
            raise ValueError("daily data missing required column: vol")
    data["trade_date"] = data["trade_date"].astype(str)
    data = data[data["trade_date"] <= str(trade_date)]
    for column in ["open", "high", "low", "close", "vol", "pct_chg"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "high", "low", "close", "vol"])
    return data.sort_values("trade_date").reset_index(drop=True)


def _prepare_daily_basic(frame: pd.DataFrame, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if "trade_date" in data.columns:
        data["trade_date"] = data["trade_date"].astype(str)
        data = data[data["trade_date"] <= str(trade_date)]
    for column in ["turnover_rate", "turnover_rate_f", "circ_mv"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    if "trade_date" in data.columns:
        return data.sort_values("trade_date").reset_index(drop=True)
    return data.reset_index(drop=True)


def _rename_columns(data: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "日期": "trade_date",
        "成交量": "vol",
        "成交额": "amount",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "涨跌幅": "pct_chg",
    }
    return data.rename(columns={col: aliases.get(str(col), col) for col in data.columns})


def _with_moving_averages(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    for window in (5, 10, 20, 60):
        result[f"ma{window}"] = close.rolling(window=window, min_periods=window).mean()
    return result.reset_index(drop=True)


def _pct_change(data: pd.DataFrame) -> float:
    latest = data.iloc[-1]
    if "pct_chg" in data.columns and not pd.isna(latest.get("pct_chg")):
        return _num(latest.get("pct_chg"))
    if len(data) < 2:
        return 0.0
    prev_close = _num(data.iloc[-2]["close"])
    close = _num(latest["close"])
    if prev_close <= 0:
        return 0.0
    return (close / prev_close - 1.0) * 100


def _volume_ratio(data: pd.DataFrame) -> float:
    if len(data) < 6:
        return 0.0
    latest = _num(data.iloc[-1]["vol"])
    base = _num(data.iloc[-6:-1]["vol"].mean())
    if base <= 0:
        return 0.0
    return latest / base


def _turnover_rate(row: pd.Series) -> float:
    turnover_rate_f = _optional_num(row.get("turnover_rate_f"))
    if turnover_rate_f is not None:
        return turnover_rate_f
    return _num(row.get("turnover_rate"))


def _latest_trade_date(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return ""
    return str(frame["trade_date"].astype(str).max())


def _is_st_stock(stock_basic: dict[str, Any]) -> bool:
    name = str(stock_basic.get("name") or "").upper()
    return "ST" in name


def _is_bse_stock(ts_code: str, stock_basic: dict[str, Any]) -> bool:
    raw = (ts_code or "").strip().upper()
    code = raw.split(".", 1)[0]
    market = str(stock_basic.get("market") or "")
    exchange = str(stock_basic.get("exchange") or "").upper()
    return raw.endswith(".BJ") or exchange == "BSE" or "北交" in market or code.startswith(("43", "81", "82", "83", "87", "88", "92"))


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_num(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_metrics_from_precomputed(raw_metrics: dict[str, Any]) -> dict[str, float]:
    for key in ("current", "legacy"):
        payload = raw_metrics.get(key)
        if isinstance(payload, dict):
            return {
                "pct_chg": _num(payload.get("pct_chg")),
                "volume_ratio_5d": _num(payload.get("volume_ratio")),
                "turnover_rate": _num(payload.get("turnover_rate")),
                "circ_mv": _num(payload.get("circ_mv")),
                "ma5": _num(payload.get("ma5")),
                "ma10": _num(payload.get("ma10")),
                "ma20": _num(payload.get("ma20")),
                "ma60": _num(payload.get("ma60")),
            }
    return {}


def _score(metrics: dict[str, Any], matched: list[str], failed: list[str]) -> float:
    score = min(70.0, len(matched) * 10.0)
    pct_chg = _num(metrics.get("pct_chg"))
    volume_ratio = _num(metrics.get("volume_ratio_5d"))
    turnover_rate = _num(metrics.get("turnover_rate"))

    if 3.0 <= pct_chg <= 5.0:
        score += min(10.0, pct_chg * 2.0)
    if volume_ratio > 1.0:
        score += min(10.0, volume_ratio * 4.0)
    if 6.0 <= turnover_rate <= 9.0:
        score += 5.0
    score -= min(len(failed) * 6.0, 30.0)
    return round(max(0.0, min(score, 100.0)), 2)
