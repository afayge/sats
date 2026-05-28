from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_MA_PERIODS = (5, 10, 20, 60, 120)
DEFAULT_RSI_PERIODS = (6, 12, 24)


@dataclass(slots=True)
class IndicatorInput:
    ts_code: str
    trade_date: str
    daily: pd.DataFrame
    daily_basic: pd.DataFrame = field(default_factory=pd.DataFrame)
    moneyflow: pd.DataFrame = field(default_factory=pd.DataFrame)
    fundamentals: pd.DataFrame = field(default_factory=pd.DataFrame)
    stock_basic: dict[str, Any] = field(default_factory=dict)
    data_sources: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class IndicatorResult:
    ts_code: str
    trade_date: str
    name: str
    close: float
    technical: dict[str, Any]
    patterns: dict[str, Any]
    volume: dict[str, Any]
    support_resistance: dict[str, Any]
    elliott_wave: dict[str, Any]
    moneyflow: dict[str, Any]
    fundamentals: dict[str, Any]
    data_sources: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "trade_date": self.trade_date,
            "name": self.name,
            "close": self.close,
            "technical": self.technical,
            "patterns": self.patterns,
            "volume": self.volume,
            "support_resistance": self.support_resistance,
            "elliott_wave": self.elliott_wave,
            "moneyflow": self.moneyflow,
            "fundamentals": self.fundamentals,
            "data_sources": self.data_sources,
        }


class IndicatorCalculator:
    def calculate(self, item: IndicatorInput) -> IndicatorResult:
        daily = _prepare_daily(item.daily, item.trade_date)
        if daily.empty:
            return _empty_result(item, reason="daily data unavailable")

        enriched = self._enrich_daily(daily)
        latest = enriched.iloc[-1]
        close = _num(latest.get("close"))
        technical = self._technical_snapshot(enriched)
        return IndicatorResult(
            ts_code=item.ts_code,
            trade_date=item.trade_date,
            name=str(item.stock_basic.get("name") or ""),
            close=close,
            technical=technical,
            patterns=_candlestick_patterns(enriched),
            volume=_volume_analysis(enriched),
            support_resistance=_support_resistance(enriched),
            elliott_wave=_elliott_wave(enriched),
            moneyflow=_moneyflow_snapshot(item.moneyflow),
            fundamentals=_fundamental_snapshot(item.daily_basic, item.fundamentals),
            data_sources=dict(item.data_sources or {}),
        )

    def _enrich_daily(self, daily: pd.DataFrame) -> pd.DataFrame:
        data = daily.copy().sort_values("trade_date").reset_index(drop=True)
        close = pd.to_numeric(data["close"], errors="coerce")
        high = pd.to_numeric(data["high"], errors="coerce")
        low = pd.to_numeric(data["low"], errors="coerce")
        prev_close = close.shift(1)

        for period in DEFAULT_MA_PERIODS:
            data[f"ma{period}"] = close.rolling(period, min_periods=period).mean()
            data[f"sma{period}"] = data[f"ma{period}"]
            data[f"ema{period}"] = close.ewm(span=period, adjust=False).mean()

        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        data["macd_dif"] = ema12 - ema26
        data["macd_dea"] = data["macd_dif"].ewm(span=9, adjust=False).mean()
        data["macd_hist"] = (data["macd_dif"] - data["macd_dea"]) * 2

        for period in DEFAULT_RSI_PERIODS:
            data[f"rsi{period}"] = _rsi(close, period)

        boll_mid = close.rolling(20, min_periods=20).mean()
        boll_std = close.rolling(20, min_periods=20).std(ddof=0)
        data["boll_mid"] = boll_mid
        data["boll_upper"] = boll_mid + boll_std * 2
        data["boll_lower"] = boll_mid - boll_std * 2

        tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        data["atr14"] = tr.rolling(14, min_periods=14).mean()

        low_n = low.rolling(9, min_periods=9).min()
        high_n = high.rolling(9, min_periods=9).max()
        rsv = ((close - low_n) / (high_n - low_n).replace(0, np.nan) * 100).fillna(50)
        data["kdj_k"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        data["kdj_d"] = data["kdj_k"].ewm(alpha=1 / 3, adjust=False).mean()
        data["kdj_j"] = 3 * data["kdj_k"] - 2 * data["kdj_d"]
        data["stoch_k"] = rsv
        data["stoch_d"] = rsv.rolling(3, min_periods=3).mean()
        return data

    def _technical_snapshot(self, data: pd.DataFrame) -> dict[str, Any]:
        latest = data.iloc[-1]
        close = _num(latest.get("close"))
        ma = {f"ma{period}": _optional_num(latest.get(f"ma{period}")) for period in DEFAULT_MA_PERIODS}
        sma = {f"sma{period}": _optional_num(latest.get(f"sma{period}")) for period in DEFAULT_MA_PERIODS}
        ema = {f"ema{period}": _optional_num(latest.get(f"ema{period}")) for period in DEFAULT_MA_PERIODS}
        boll = {
            "mid": _optional_num(latest.get("boll_mid")),
            "upper": _optional_num(latest.get("boll_upper")),
            "lower": _optional_num(latest.get("boll_lower")),
        }
        boll["position"] = _boll_position(close, boll)
        return {
            "ma": ma,
            "sma": sma,
            "ema": ema,
            "ma_alignment": _ma_alignment(ma),
            "bias": {
                "ma5": _bias(close, ma.get("ma5")),
                "ma10": _bias(close, ma.get("ma10")),
                "ma20": _bias(close, ma.get("ma20")),
            },
            "macd": {
                "dif": _optional_num(latest.get("macd_dif")),
                "dea": _optional_num(latest.get("macd_dea")),
                "hist": _optional_num(latest.get("macd_hist")),
                "signal": _macd_signal(data),
            },
            "rsi": {f"rsi{period}": _optional_num(latest.get(f"rsi{period}")) for period in DEFAULT_RSI_PERIODS},
            "boll": boll,
            "atr": {"atr14": _optional_num(latest.get("atr14"))},
            "kdj": {
                "k": _optional_num(latest.get("kdj_k")),
                "d": _optional_num(latest.get("kdj_d")),
                "j": _optional_num(latest.get("kdj_j")),
            },
            "stochastic": {
                "k": _optional_num(latest.get("stoch_k")),
                "d": _optional_num(latest.get("stoch_d")),
            },
        }


def format_indicator_results(results: list[IndicatorResult]) -> str:
    if not results:
        return "无结果"
    blocks = []
    for result in results:
        tech = result.technical
        money = result.moneyflow
        fundamentals = result.fundamentals
        sr = result.support_resistance
        patterns = result.patterns
        volume = result.volume
        name = f" {result.name}" if result.name else ""
        blocks.append(
            "\n".join(
                [
                    f"{result.ts_code}{name} 收盘 {result.close:.2f}",
                    f"MA: {tech.get('ma_alignment')} MACD: {tech.get('macd', {}).get('signal')} "
                    f"RSI6/12/24: {_fmt(tech.get('rsi', {}).get('rsi6'))}/"
                    f"{_fmt(tech.get('rsi', {}).get('rsi12'))}/{_fmt(tech.get('rsi', {}).get('rsi24'))}",
                    f"BOLL: {tech.get('boll', {}).get('position')} KDJ: "
                    f"{_fmt(tech.get('kdj', {}).get('k'))}/{_fmt(tech.get('kdj', {}).get('d'))}/"
                    f"{_fmt(tech.get('kdj', {}).get('j'))} ATR14: {_fmt(tech.get('atr', {}).get('atr14'))}",
                    f"量能: {volume.get('status')} 量比5日 {_fmt(volume.get('volume_ratio_5d'))} "
                    f"形态: {', '.join(patterns.get('latest', [])) or '无'}",
                    f"支撑: {_join_numbers(sr.get('support'))} 压力: {_join_numbers(sr.get('resistance'))}",
                    f"资金流: 1d {_fmt(money.get('main_net_amount'))} 5d {_fmt(money.get('main_net_amount_5d'))} "
                    f"10d {_fmt(money.get('main_net_amount_10d'))}",
                    f"基本面: PE {_fmt(fundamentals.get('pe'))} PB {_fmt(fundamentals.get('pb'))} "
                    f"市值 {_fmt(fundamentals.get('total_mv'))} 营收 {_fmt(fundamentals.get('revenue'))} "
                    f"利润 {_fmt(fundamentals.get('profit'))} ROE {_fmt(fundamentals.get('roe'))} "
                    f"负债率 {_fmt(fundamentals.get('debt_to_assets'))}",
                    f"波浪: {result.elliott_wave.get('pattern')} 置信度 {_fmt(result.elliott_wave.get('confidence'))}",
                ]
            )
        )
    return "\n\n".join(blocks)


def _prepare_daily(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if "volume" in data.columns and "vol" not in data.columns:
        data["vol"] = data["volume"]
    required = ["trade_date", "open", "high", "low", "close", "vol"]
    if any(column not in data.columns for column in required):
        return pd.DataFrame()
    data["trade_date"] = data["trade_date"].astype(str)
    data = data[data["trade_date"] <= str(trade_date)].copy()
    for column in ["open", "high", "low", "close", "vol", "amount", "pct_chg"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["open", "high", "low", "close"]).sort_values("trade_date").reset_index(drop=True)


def _empty_result(item: IndicatorInput, *, reason: str) -> IndicatorResult:
    return IndicatorResult(
        ts_code=item.ts_code,
        trade_date=item.trade_date,
        name=str(item.stock_basic.get("name") or ""),
        close=0.0,
        technical={"status": "unavailable", "reason": reason},
        patterns={"latest": []},
        volume={"status": "unavailable"},
        support_resistance={"support": [], "resistance": []},
        elliott_wave={"pattern": "unavailable", "confidence": 0.0, "points": []},
        moneyflow={"status": "unavailable"},
        fundamentals={"status": "unavailable"},
        data_sources=dict(item.data_sources or {}),
    )


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(period, min_periods=period).mean()
    avg_loss = loss.rolling(period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50)
    return rsi.fillna(50)


def _macd_signal(data: pd.DataFrame) -> str:
    if len(data) < 2:
        return "数据不足"
    latest = data.iloc[-1]
    prev = data.iloc[-2]
    prev_diff = _num(prev.get("macd_dif")) - _num(prev.get("macd_dea"))
    curr_diff = _num(latest.get("macd_dif")) - _num(latest.get("macd_dea"))
    dif = _num(latest.get("macd_dif"))
    dea = _num(latest.get("macd_dea"))
    if prev_diff <= 0 < curr_diff and dif > 0:
        return "零轴上金叉"
    if prev_diff <= 0 < curr_diff:
        return "金叉"
    if prev_diff >= 0 > curr_diff:
        return "死叉"
    if dif > dea > 0:
        return "多头"
    if dif < dea < 0:
        return "空头"
    return "中性"


def _candlestick_patterns(data: pd.DataFrame) -> dict[str, Any]:
    if data.empty:
        return {"latest": []}
    latest = data.iloc[-1]
    patterns = []
    open_ = _num(latest.get("open"))
    high = _num(latest.get("high"))
    low = _num(latest.get("low"))
    close = _num(latest.get("close"))
    body = abs(close - open_)
    full_range = max(high - low, 0.0)
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    if full_range > 0 and body / full_range < 0.1:
        patterns.append("十字星")
    if body > 0 and lower > 2 * body and upper <= body:
        patterns.append("锤头线")
    if body > 0 and upper > 2 * body and lower <= body:
        patterns.append("射击之星")
    if len(data) >= 2:
        prev = data.iloc[-2]
        prev_open = _num(prev.get("open"))
        prev_close = _num(prev.get("close"))
        if prev_close < prev_open and close > open_ and open_ <= prev_close and close >= prev_open:
            patterns.append("看涨吞没")
        if prev_close > prev_open and close < open_ and open_ >= prev_close and close <= prev_open:
            patterns.append("看跌吞没")
    if len(data) >= 3:
        first, second, third = data.iloc[-3], data.iloc[-2], data.iloc[-1]
        first_body = abs(_num(first.get("close")) - _num(first.get("open")))
        second_body = abs(_num(second.get("close")) - _num(second.get("open")))
        if _num(first.get("close")) < _num(first.get("open")) and second_body < first_body * 0.5 and close > open_:
            patterns.append("早晨星")
        if _num(first.get("close")) > _num(first.get("open")) and second_body < first_body * 0.5 and close < open_:
            patterns.append("黄昏星")
    return {"latest": patterns}


def _volume_analysis(data: pd.DataFrame) -> dict[str, Any]:
    if len(data) < 6 or "vol" not in data.columns:
        return {"status": "数据不足", "volume_ratio_5d": None, "volume_ratio_10d": None}
    latest = data.iloc[-1]
    latest_vol = _num(latest.get("vol"))
    prev5 = pd.to_numeric(data.iloc[-6:-1]["vol"], errors="coerce").mean()
    prev10 = pd.to_numeric(data.iloc[-11:-1]["vol"], errors="coerce").mean() if len(data) >= 11 else np.nan
    ratio5 = latest_vol / prev5 if prev5 and prev5 > 0 else None
    ratio10 = latest_vol / prev10 if prev10 and prev10 > 0 else None
    price_change = _num(latest.get("pct_chg"))
    if ratio5 is None:
        status = "数据不足"
    elif ratio5 >= 1.5 and price_change > 0:
        status = "放量上涨"
    elif ratio5 >= 1.5 and price_change <= 0:
        status = "放量下跌"
    elif ratio5 <= 0.7 and price_change > 0:
        status = "缩量上涨"
    elif ratio5 <= 0.7:
        status = "缩量回调"
    else:
        status = "量能正常"
    return {
        "status": status,
        "volume_ratio_5d": _round_or_none(ratio5),
        "volume_ratio_10d": _round_or_none(ratio10),
        "latest_vol": latest_vol,
    }


def _support_resistance(data: pd.DataFrame, *, window: int = 5, levels: int = 3) -> dict[str, Any]:
    if len(data) < window * 2 + 1:
        return {"support": [], "resistance": []}
    close = pd.to_numeric(data["close"], errors="coerce").reset_index(drop=True)
    peaks, valleys = _peaks_valleys(close, window)
    current = _num(close.iloc[-1])
    support_prices = [_num(close.iloc[index]) for index in valleys if _num(close.iloc[index]) < current]
    resistance_prices = [_num(close.iloc[index]) for index in peaks if _num(close.iloc[index]) > current]
    return {
        "support": _cluster_levels(support_prices, levels, reverse=True),
        "resistance": _cluster_levels(resistance_prices, levels, reverse=False),
    }


def _elliott_wave(data: pd.DataFrame) -> dict[str, Any]:
    close = pd.to_numeric(data["close"], errors="coerce").reset_index(drop=True)
    if len(close) < 20:
        return {"pattern": "insufficient_data", "confidence": 0.0, "points": []}
    peaks, valleys = _peaks_valleys(close, window=3)
    pivots = sorted([(index, "peak") for index in peaks] + [(index, "valley") for index in valleys])
    pivots = _alternate_pivots(pivots, close)
    recent = pivots[-6:]
    points = [
        {
            "index": index,
            "trade_date": str(data.iloc[index].get("trade_date")),
            "type": kind,
            "price": _num(close.iloc[index]),
        }
        for index, kind in recent
    ]
    if len(recent) >= 6:
        return {"pattern": "potential_5_wave", "confidence": 0.65, "points": points}
    if len(recent) >= 4:
        return {"pattern": "potential_abc", "confidence": 0.45, "points": points}
    return {"pattern": "no_clear_wave", "confidence": 0.2, "points": points}


def _peaks_valleys(close: pd.Series, window: int) -> tuple[list[int], list[int]]:
    values = close.to_numpy(dtype=float)
    peaks: list[int] = []
    valleys: list[int] = []
    for index in range(window, len(values) - window):
        segment = values[index - window : index + window + 1]
        if np.isnan(values[index]) or np.isnan(segment).all():
            continue
        if values[index] == np.nanmax(segment):
            peaks.append(index)
        if values[index] == np.nanmin(segment):
            valleys.append(index)
    return peaks, valleys


def _alternate_pivots(pivots: list[tuple[int, str]], close: pd.Series) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for pivot in pivots:
        if not result or result[-1][1] != pivot[1]:
            result.append(pivot)
            continue
        previous = result[-1]
        if pivot[1] == "peak" and close.iloc[pivot[0]] > close.iloc[previous[0]]:
            result[-1] = pivot
        elif pivot[1] == "valley" and close.iloc[pivot[0]] < close.iloc[previous[0]]:
            result[-1] = pivot
    return result


def _moneyflow_snapshot(frame: pd.DataFrame) -> dict[str, Any]:
    if frame is None or frame.empty:
        return {"status": "unavailable", "main_net_amount": None, "main_net_amount_5d": None, "main_net_amount_10d": None}
    data = frame.copy()
    if "trade_date" in data.columns:
        data = data.sort_values("trade_date")
    col = _first_column(data, ["main_net_amount", "net_mf_amount", "main_net_inflow"])
    if not col:
        return {"status": "unavailable", "main_net_amount": None, "main_net_amount_5d": None, "main_net_amount_10d": None}
    values = pd.to_numeric(data[col], errors="coerce")
    return {
        "status": "available",
        "main_net_amount": _round_or_none(values.iloc[-1] if len(values) else None),
        "main_net_amount_5d": _round_or_none(values.tail(5).sum()),
        "main_net_amount_10d": _round_or_none(values.tail(10).sum()),
    }


def _fundamental_snapshot(daily_basic: pd.DataFrame, fundamentals: pd.DataFrame) -> dict[str, Any]:
    result = {"status": "available"}
    if daily_basic is not None and not daily_basic.empty:
        row = daily_basic.sort_values("trade_date").iloc[-1] if "trade_date" in daily_basic.columns else daily_basic.iloc[-1]
        for key in ["pe", "pb", "ps", "total_mv", "circ_mv", "turnover_rate", "turnover_rate_f"]:
            result[key] = _optional_num(row.get(key))
    else:
        result.update({key: None for key in ["pe", "pb", "ps", "total_mv", "circ_mv", "turnover_rate", "turnover_rate_f"]})
    if fundamentals is not None and not fundamentals.empty:
        row = fundamentals.iloc[-1]
        result["revenue"] = _optional_num(row.get("revenue") if pd.notna(row.get("revenue")) else row.get("total_revenue"))
        result["profit"] = _optional_num(row.get("profit") if pd.notna(row.get("profit")) else row.get("net_profit"))
        result["roe"] = _optional_num(row.get("roe"))
        result["debt_to_assets"] = _optional_num(row.get("debt_to_assets"))
    else:
        result.update({"revenue": None, "profit": None, "roe": None, "debt_to_assets": None})
    if all(value is None for key, value in result.items() if key != "status"):
        result["status"] = "unavailable"
    return result


def _ma_alignment(ma: dict[str, float | None]) -> str:
    ma5, ma10, ma20, ma60 = ma.get("ma5"), ma.get("ma10"), ma.get("ma20"), ma.get("ma60")
    if all(value is not None for value in [ma5, ma10, ma20, ma60]):
        if ma5 > ma10 > ma20 > ma60:  # type: ignore[operator]
            return "多头排列"
        if ma5 < ma10 < ma20 < ma60:  # type: ignore[operator]
            return "空头排列"
    return "均线纠缠"


def _boll_position(close: float, boll: dict[str, float | None]) -> str:
    upper, mid, lower = boll.get("upper"), boll.get("mid"), boll.get("lower")
    if upper is None or mid is None or lower is None:
        return "数据不足"
    if close >= upper:
        return "上轨上方"
    if close <= lower:
        return "下轨下方"
    if close >= mid:
        return "中轨上方"
    return "中轨下方"


def _cluster_levels(prices: list[float], limit: int, *, reverse: bool) -> list[float]:
    clean = sorted([price for price in prices if price > 0], reverse=reverse)
    levels: list[float] = []
    for price in clean:
        if not any(abs(price - existing) / existing < 0.02 for existing in levels if existing):
            levels.append(round(price, 2))
        if len(levels) >= limit:
            break
    return levels


def _bias(close: float, base: float | None) -> float | None:
    if base is None or base == 0:
        return None
    return round((close / base - 1.0) * 100, 4)


def _first_column(frame: pd.DataFrame, names: list[str]) -> str | None:
    lower_map = {str(column).lower(): column for column in frame.columns}
    for name in names:
        if name.lower() in lower_map:
            return lower_map[name.lower()]
    return None


def _optional_num(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: Any) -> float | None:
    return _optional_num(value)


def _num(value: Any, default: float = 0.0) -> float:
    parsed = _optional_num(value)
    return default if parsed is None else float(parsed)


def _fmt(value: Any) -> str:
    parsed = _optional_num(value)
    return "N/A" if parsed is None else f"{parsed:.2f}"


def _join_numbers(values: Any) -> str:
    if not values:
        return "N/A"
    return ",".join(_fmt(value) for value in values)
