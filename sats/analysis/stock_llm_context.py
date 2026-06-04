from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.factors.profiles import DEFAULT_FACTOR_PROFILE
from sats.factors.service import snapshot_from_screening_inputs
from sats.indicators import IndicatorCalculator, IndicatorInput, IndicatorResult
from sats.stock_question import StockQuestion, parse_stock_question
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_PERIOD_LIMITS = {"15m": 160, "30m": 120}


@dataclass(frozen=True, slots=True)
class StockLLMContext:
    question: StockQuestion
    trade_date: str
    payload: dict[str, Any]
    system_message: str


def build_stock_llm_context(
    message: str,
    *,
    settings: Settings,
    storage: DuckDBStorage | None = None,
    question: StockQuestion | None = None,
    lookback_days: int = 180,
    minute_lookback_days: int = 30,
) -> StockLLMContext | None:
    question = question or parse_stock_question(message)
    if not question.has_stock_question:
        return None
    storage = storage or DuckDBStorage(settings.db_path)
    trade_date = question.trade_date or _resolve_trade_date()
    symbols = question.symbols
    if not symbols:
        return None

    stock_contexts = ensure_stock_analysis_data(
        symbols,
        trade_date,
        settings=settings,
        storage=storage,
        as_of_time=question.as_of_time,
        lookback_days=lookback_days,
        minute_lookback_days=minute_lookback_days,
    )
    as_of_trade_time = _as_of_trade_time(trade_date, question.as_of_time)

    payload = {
        "user_question": message,
        "trade_date": trade_date,
        "as_of_time": as_of_trade_time,
        "symbols": symbols,
        "data_policy": "SATS has fetched real structured market data before calling the LLM.",
        "stocks": [stock_contexts[symbol] for symbol in symbols if symbol in stock_contexts],
    }
    return StockLLMContext(
        question=question,
        trade_date=trade_date,
        payload=payload,
        system_message=_system_message(payload),
    )


def ensure_stock_analysis_data(
    symbols: list[str],
    trade_date: str,
    *,
    settings: Settings,
    storage: DuckDBStorage,
    periods: tuple[str, ...] = ("15m", "30m"),
    as_of_time: str | None = None,
    lookback_days: int = 180,
    minute_lookback_days: int = 30,
    astock_provider: Any | None = None,
) -> dict[str, dict[str, Any]]:
    clean_symbols = normalize_symbols(symbols, required=False)
    if not clean_symbols:
        return {}
    question = StockQuestion(
        symbols=clean_symbols,
        trade_date=trade_date,
        as_of_time=as_of_time,
        has_stock_question=True,
    )
    provider = astock_provider or AStockDataProvider(settings)
    inputs = provider.load_indicator_inputs(
        clean_symbols,
        trade_date,
        lookback_days=lookback_days,
        storage=storage,
    )
    if not inputs:
        raise ValueError(f"{trade_date} 无法获取 {','.join(clean_symbols)} 的真实日线数据，已停止调用 LLM。")

    calculator = IndicatorCalculator()
    indicator_results = {item.ts_code: calculator.calculate(item) for item in inputs}
    input_lookup = {item.ts_code: item for item in inputs}
    missing_daily = [
        symbol
        for symbol in clean_symbols
        if indicator_results.get(symbol) is None or indicator_results[symbol].close <= 0
    ]
    if missing_daily:
        raise ValueError(f"{trade_date} 缺少 {','.join(missing_daily)} 的真实日线行情，已停止调用 LLM。")

    minute_frames = _load_required_minute_frames(
        clean_symbols,
        trade_date,
        periods=periods,
        question=question,
        storage=storage,
        astock_provider=provider,
        minute_lookback_days=minute_lookback_days,
    )
    quotes = _load_quotes(clean_symbols, astock_provider=provider) if _use_realtime(trade_date, question) else {}
    factor_summary = _factor_summary_for_inputs(
        inputs,
        storage=storage,
        trade_date=trade_date,
        lookback_days=lookback_days,
    )
    return _build_stock_contexts(
        clean_symbols,
        trade_date,
        inputs=input_lookup,
        indicator_results=indicator_results,
        minute_frames=minute_frames,
        quotes=quotes,
        as_of_time=as_of_time,
        lookback_days=lookback_days,
        factor_summary=factor_summary,
    )


def _load_required_minute_frames(
    symbols: list[str],
    trade_date: str,
    *,
    periods: tuple[str, ...] = tuple(MINUTE_PERIOD_LIMITS),
    question: StockQuestion,
    storage: DuckDBStorage,
    astock_provider: Any,
    minute_lookback_days: int,
) -> dict[str, pd.DataFrame]:
    frames = {}
    for period in periods:
        frame = _load_minute_frame(
            symbols,
            trade_date,
            period=period,
            question=question,
            astock_provider=astock_provider,
            minute_lookback_days=minute_lookback_days,
        )
        missing = _symbols_missing_minute(symbols, frame)
        if missing:
            raise ValueError(f"{trade_date} 缺少 {','.join(missing)} 的真实 {period} 分钟K数据，已停止调用 LLM。")
        frames[period] = frame
    return frames


def _build_stock_contexts(
    symbols: list[str],
    trade_date: str,
    *,
    inputs: dict[str, IndicatorInput],
    indicator_results: dict[str, IndicatorResult],
    minute_frames: dict[str, pd.DataFrame],
    quotes: dict[str, dict[str, Any]],
    as_of_time: str | None,
    lookback_days: int,
    factor_summary: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    contexts = {}
    as_of_trade_time = _as_of_trade_time(trade_date, as_of_time)
    factor_summary = factor_summary or {}
    for symbol in symbols:
        item = inputs[symbol]
        result = indicator_results[symbol]
        contexts[symbol] = {
            "ts_code": symbol,
            "name": result.name,
            "trade_date": trade_date,
            "as_of_time": as_of_trade_time,
            "price_context": _price_context(result, quotes.get(symbol, {})),
            "indicator_result": result.to_dict(),
            "daily_tail": _records_tail(item.daily, limit=lookback_days, columns=[
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "vol",
                "amount",
                "pct_chg",
            ]),
            "minute_curves": {
                period: {
                    "source": str(frame.attrs.get("data_source") or frame.attrs.get("tickflow_source") or ""),
                    "rows": _records_tail(
                        _filter_symbol_minute(frame, symbol),
                        limit=MINUTE_PERIOD_LIMITS.get(period, 120),
                        columns=["trade_time", "open", "high", "low", "close", "vol", "amount"],
                    ),
                }
                for period, frame in minute_frames.items()
            },
            "data_sources": _data_sources(item, minute_frames, quotes.get(symbol, {})),
            "missing_fields": _missing_fields(item, quotes.get(symbol, {})),
            "factor_summary": factor_summary.get(symbol, {}),
        }
    return contexts


def _factor_summary_for_inputs(
    inputs: list[IndicatorInput],
    *,
    storage: DuckDBStorage,
    trade_date: str,
    lookback_days: int,
) -> dict[str, Any]:
    try:
        snapshot, _panel_result = snapshot_from_screening_inputs(
            inputs,  # IndicatorInput has the same daily/daily_basic/stock_basic surface as ScreeningInput.
            storage=storage,
            trade_date=trade_date,
            profile=DEFAULT_FACTOR_PROFILE,
            lookback_days=max(lookback_days, 260),
        )
    except Exception as exc:
        return {item.ts_code: {"profile": DEFAULT_FACTOR_PROFILE, "warnings": [f"factor_summary: {exc}"]} for item in inputs}
    result = {}
    for item in inputs:
        exposure = snapshot.exposure_for(item.ts_code)
        exposure["warnings"] = list(snapshot.warnings)
        result[item.ts_code] = exposure
    return result


def _load_minute_frame(
    symbols: list[str],
    trade_date: str,
    *,
    period: str,
    question: StockQuestion,
    astock_provider: Any,
    minute_lookback_days: int,
) -> pd.DataFrame:
    start_time = _days_before(trade_date, minute_lookback_days)
    end_time = _as_of_trade_time(trade_date, question.as_of_time) or trade_date
    use_realtime = _use_realtime(trade_date, question)
    try:
        if use_realtime:
            history = astock_provider.load_historical_minute_klines(
                symbols,
                period=period,
                start_time=start_time,
                end_time=trade_date,
            )
            realtime = astock_provider.load_realtime_minute_klines(
                symbols,
                period=period,
                count=MINUTE_PERIOD_LIMITS[period],
            )
            frame = _combine_minute_frames([history, realtime])
            frame.attrs["data_source"] = "tickflow_history+realtime"
        else:
            frame = astock_provider.load_historical_minute_klines(
                symbols,
                period=period,
                start_time=start_time,
                end_time=end_time,
            )
            frame.attrs["data_source"] = "tickflow_history"
    except Exception:
        return pd.DataFrame()
    source = str(frame.attrs.get("data_source") or frame.attrs.get("tickflow_source") or "")
    frame = _filter_minute_as_of(frame, trade_date, question.as_of_time)
    combined = _combine_minute_frames([frame])
    combined.attrs["data_source"] = source
    return combined


def _load_quotes(symbols: list[str], *, astock_provider: Any) -> dict[str, dict[str, Any]]:
    try:
        frame = astock_provider.load_realtime_quotes(symbols=symbols)
    except Exception:
        return {}
    if frame is None or frame.empty:
        return {}
    return {
        str(row.get("ts_code")): _jsonable(row.dropna().to_dict())
        for _, row in frame.iterrows()
        if str(row.get("ts_code") or "").strip()
    }


def _system_message(payload: dict[str, Any]) -> str:
    return (
        "以下是 SATS 在调用 LLM 前获取的真实股票结构化数据。你只能基于这些数据回答；"
        "不得编造价格、涨跌幅、成交量、新闻、公告、题材或基本面。"
        "如果字段在 missing_fields 中，必须说明该数据缺失。"
        "输出任何投资相关判断时必须说明仅供研究，不构成投资建议。\n"
        f"{json.dumps(_jsonable(payload), ensure_ascii=False, default=str)}"
    )


def _resolve_trade_date() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")


def _use_realtime(trade_date: str, question: StockQuestion) -> bool:
    today = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")
    return question.trade_date is None or str(trade_date) == today


def _as_of_trade_time(trade_date: str, as_of_time: str | None) -> str | None:
    if not as_of_time:
        return None
    day = datetime.strptime(trade_date, "%Y%m%d").strftime("%Y-%m-%d")
    return f"{day} {as_of_time}"


def _days_before(trade_date: str, days: int) -> str:
    return (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _records_tail(frame: pd.DataFrame, *, limit: int, columns: list[str]) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    data = frame.copy()
    if "trade_date" in data.columns:
        data = data.sort_values("trade_date")
    if "trade_time" in data.columns:
        data = data.sort_values("trade_time")
    existing = [column for column in columns if column in data.columns]
    return [_jsonable(row.dropna().to_dict()) for _, row in data[existing].tail(limit).iterrows()]


def _filter_symbol_minute(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return pd.DataFrame()
    return frame[frame["ts_code"].astype(str) == symbol].copy()


def _filter_minute_as_of(frame: pd.DataFrame, trade_date: str, as_of_time: str | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if as_of_time and "trade_time" in data.columns:
        end = _as_of_trade_time(trade_date, as_of_time)
        data = data[data["trade_time"].astype(str) <= str(end)]
    return data


def _combine_minute_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame()
    data = pd.concat(valid, ignore_index=True)
    if {"ts_code", "period", "trade_time"}.issubset(data.columns):
        data = data.drop_duplicates(subset=["ts_code", "period", "trade_time"], keep="last")
    sort_columns = [column for column in ["ts_code", "trade_time"] if column in data.columns]
    if sort_columns:
        data = data.sort_values(sort_columns)
    return data.reset_index(drop=True)


def _symbols_missing_minute(symbols: list[str], frame: pd.DataFrame) -> list[str]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return list(symbols)
    present = set(frame["ts_code"].astype(str))
    return [symbol for symbol in symbols if symbol not in present]


def _price_context(result: IndicatorResult, quote: dict[str, Any]) -> dict[str, Any]:
    if quote:
        return quote
    return {
        "ts_code": result.ts_code,
        "trade_date": result.trade_date,
        "close": result.close,
        "data_source": "indicator_latest_close",
    }


def _data_sources(item: IndicatorInput, minute_frames: dict[str, pd.DataFrame], quote: dict[str, Any]) -> dict[str, Any]:
    sources = dict(item.data_sources or {})
    for period, frame in minute_frames.items():
        sources[f"minute_{period}"] = str(frame.attrs.get("data_source") or frame.attrs.get("tickflow_source") or "")
    sources["quote"] = str(quote.get("data_source") or "indicator_latest_close")
    return sources


def _missing_fields(item: IndicatorInput, quote: dict[str, Any]) -> list[str]:
    missing = []
    for key, value in (item.data_sources or {}).items():
        if not value or value == "unavailable":
            missing.append(key)
    if quote == {}:
        missing.append("realtime_quote")
    return missing


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if pd.isna(value):
        return None
    return value
