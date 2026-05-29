from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from sats.config import Settings
from sats.symbols import normalize_symbols, normalize_ts_code


@dataclass(frozen=True, slots=True)
class StockNameResolution:
    symbols: tuple[str, ...]
    questions: tuple[str, ...] = ()


def load_stock_basic_frame(
    settings: Settings,
    *,
    storage_factory=None,
    provider_factory=None,
) -> pd.DataFrame:
    if storage_factory is None:
        from sats.storage.duckdb import DuckDBStorage

        storage_factory = DuckDBStorage
    if provider_factory is None:
        from sats.data.astock_provider import AStockDataProvider

        provider_factory = AStockDataProvider
    db_path = getattr(settings, "db_path", None)
    storage = None
    if db_path is not None:
        try:
            storage = storage_factory(Path(db_path))
            cached = storage.get_stock_basic()
        except Exception:
            cached = pd.DataFrame()
        if not cached.empty:
            return cached
    try:
        provider = provider_factory(settings)
        frame = provider.load_stock_basic(storage=storage)
    except Exception:
        return pd.DataFrame()
    return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def resolve_stock_names(
    names: Iterable[str],
    stock_basic: pd.DataFrame,
    *,
    allow_ambiguous_first: bool = False,
) -> StockNameResolution:
    clean_names = [str(name or "").strip() for name in names if str(name or "").strip()]
    if not clean_names:
        return StockNameResolution(symbols=())
    if stock_basic.empty or "name" not in stock_basic.columns or "ts_code" not in stock_basic.columns:
        return StockNameResolution(
            symbols=(),
            questions=tuple(f"未能识别股票名称“{name}”，请补充 6 位股票代码。" for name in clean_names),
        )
    symbols: list[str] = []
    questions: list[str] = []
    data = _clean_stock_basic(stock_basic)
    for name in clean_names:
        matched = match_stock_name(name, data)
        if len(matched) == 1 or (allow_ambiguous_first and len(matched) > 1):
            symbols.append(str(matched.iloc[0]["ts_code"]))
        elif len(matched) > 1:
            options = [
                f"{row['name']}({row['ts_code']})"
                for _, row in matched.head(6).iterrows()
                if str(row.get("ts_code") or "")
            ]
            questions.append(f"股票名称“{name}”匹配到多个结果：{', '.join(options)}。请指定 6 位代码。")
        else:
            questions.append(f"未能识别股票名称“{name}”，请补充 6 位股票代码。")
    return StockNameResolution(
        symbols=tuple(normalize_symbols(symbols, required=False)),
        questions=tuple(_dedupe(questions)),
    )


def resolve_symbol_or_name_values(
    values: Iterable[Any] | Any,
    stock_basic: pd.DataFrame,
    *,
    required: bool = True,
) -> list[str]:
    if isinstance(values, str):
        raw_values: Iterable[Any] = [values]
    elif values is None:
        raw_values = []
    elif isinstance(values, Iterable):
        raw_values = values
    else:
        raw_values = [values]
    symbols: list[str] = []
    for value in raw_values:
        text = str(value or "").strip()
        if not text:
            continue
        normalized = normalize_ts_code(text)
        if _looks_symbol(normalized):
            symbols.append(normalized)
        else:
            resolution = resolve_stock_names([text], stock_basic)
            if resolution.questions:
                raise ValueError("; ".join(resolution.questions))
            symbols.extend(resolution.symbols)
    result = normalize_symbols(symbols, required=False)
    if required and not result:
        raise ValueError("At least one symbol is required")
    return result


def stock_basic_rows_to_documents(stock_basic: pd.DataFrame) -> list[dict[str, str]]:
    if stock_basic.empty:
        return []
    data = _clean_stock_basic(stock_basic)
    rows = []
    for _, row in data.iterrows():
        ts_code = str(row.get("ts_code") or "").strip()
        name = str(row.get("name") or "").strip()
        if not ts_code or not name:
            continue
        symbol = str(row.get("symbol") or ts_code[:6]).strip()
        industry = str(row.get("industry") or "").strip()
        market = str(row.get("market") or "").strip()
        exchange = str(row.get("exchange") or "").strip()
        content = (
            f"股票名称: {name}\n"
            f"股票代码: {ts_code}\n"
            f"裸代码: {symbol}\n"
            f"行业: {industry or '未知'}\n"
            f"市场: {market or '未知'}\n"
            f"交易所: {exchange or '未知'}"
        )
        rows.append(
            {
                "ts_code": ts_code,
                "name": name,
                "symbol": symbol,
                "title": f"{name} {ts_code}",
                "content": content,
                "industry": industry,
                "market": market,
                "exchange": exchange,
            }
        )
    return rows


def match_stock_name(name: str, stock_basic: pd.DataFrame) -> pd.DataFrame:
    value = str(name or "").strip()
    if not value:
        return stock_basic.iloc[0:0]
    data = _clean_stock_basic(stock_basic)
    exact = data[data["name"] == value]
    if not exact.empty:
        return exact.drop_duplicates(subset=["ts_code"])
    symbol = normalize_ts_code(value)
    if _looks_symbol(symbol):
        by_symbol = data[(data["ts_code"] == symbol) | (data["symbol"] == symbol[:6])]
        if not by_symbol.empty:
            return by_symbol.drop_duplicates(subset=["ts_code"])
    contains = data[data["name"].str.contains(value, regex=False, na=False)]
    if not contains.empty:
        return contains.drop_duplicates(subset=["ts_code"])
    reverse = data[data["name"].map(lambda item: bool(item and item in value))]
    return reverse.drop_duplicates(subset=["ts_code"])


def names_from_stock_basic(message: str, stock_basic: pd.DataFrame) -> list[str]:
    if stock_basic.empty or "name" not in stock_basic.columns:
        return []
    text = str(message or "")
    names: list[str] = []
    for raw in stock_basic["name"].dropna().astype(str):
        name = raw.strip()
        if len(name) >= 2 and name in text:
            names.append(name)
    return _dedupe(names)


def _clean_stock_basic(stock_basic: pd.DataFrame) -> pd.DataFrame:
    data = stock_basic.copy()
    for column in ("ts_code", "symbol", "name", "industry", "market", "exchange"):
        if column not in data.columns:
            data[column] = ""
        data[column] = data[column].fillna("").astype(str)
    data["ts_code"] = data["ts_code"].map(normalize_ts_code)
    data["symbol"] = data["symbol"].where(data["symbol"].astype(bool), data["ts_code"].str[:6])
    return data


def _looks_symbol(value: str) -> bool:
    text = str(value or "")
    return len(text) == 9 and text[:6].isdigit() and text[6] == "." or (len(text) == 6 and text.isdigit())


def _dedupe(values: Iterable[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
