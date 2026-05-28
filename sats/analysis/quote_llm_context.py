from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd

from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.symbols import normalize_symbols


@dataclass(frozen=True, slots=True)
class StockQuoteLLMContext:
    payload: dict[str, Any]
    system_message: str


def build_stock_quote_llm_context(
    message: str,
    *,
    settings: Settings,
    symbols: list[str],
    astock_provider: Any | None = None,
) -> StockQuoteLLMContext | None:
    clean_symbols = normalize_symbols(symbols, required=False)
    if not clean_symbols:
        return None
    provider = astock_provider or AStockDataProvider(settings)
    error = ""
    try:
        frame = provider.load_realtime_quotes(symbols=clean_symbols)
    except Exception as exc:
        frame = pd.DataFrame()
        error = str(exc)
    payload = _quote_payload(message, clean_symbols, frame, error=error)
    return StockQuoteLLMContext(payload=payload, system_message=_system_message(payload))


def _quote_payload(message: str, symbols: list[str], frame: pd.DataFrame, *, error: str = "") -> dict[str, Any]:
    records = _records_by_symbol(frame)
    missing = [f"realtime_quote:{symbol}" for symbol in symbols if symbol not in records]
    if error:
        missing.append("realtime_quote:error")
    return {
        "user_question": message,
        "symbols": symbols,
        "data_policy": "SATS has fetched real realtime quote data before calling the LLM.",
        "quotes": [records[symbol] for symbol in symbols if symbol in records],
        "data_source": str(frame.attrs.get("data_source") or "unavailable") if frame is not None and not frame.empty else "unavailable",
        "missing_fields": missing,
        "error": error,
    }


def _records_by_symbol(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    return {
        str(row.get("ts_code")): _jsonable(row.dropna().to_dict())
        for _, row in frame.iterrows()
        if str(row.get("ts_code") or "").strip()
    }


def _system_message(payload: dict[str, Any]) -> str:
    return (
        "以下是 SATS 在调用 LLM 前获取的真实 A 股实时报价上下文。你只能基于这些 quote 数据回答；"
        "不得编造价格、涨跌幅、成交量或成交额。如果字段在 missing_fields 中，必须说明该数据缺失。"
        "涉及股票、交易或投资判断时，必须说明仅供研究，不构成投资建议。\n"
        f"{json.dumps(_jsonable(payload), ensure_ascii=False, default=str)}"
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return value
