from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from sats.data.astock_provider import AStockDataProvider
from sats.screening.base import ScreeningInput
from sats.storage.duckdb import DuckDBStorage


@dataclass(slots=True)
class FactorPanelBuildResult:
    panel: dict[str, pd.DataFrame]
    trade_dates: list[str]
    symbols: list[str]
    names: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def build_factor_panel(
    *,
    provider: AStockDataProvider,
    storage: DuckDBStorage,
    trade_date: str,
    lookback_days: int = 260,
    symbols: list[str] | None = None,
    progress: Any | None = None,
) -> FactorPanelBuildResult:
    trade_days = max(1, int(lookback_days))
    if symbols:
        inputs = provider.load_screening_inputs(
            symbols,
            trade_date,
            storage=storage,
            trade_days=trade_days,
            rule_name="factor_panel",
        )
    else:
        inputs = provider.load_all_screening_inputs(
            trade_date,
            storage=storage,
            trade_days=trade_days,
            rule_name="factor_panel",
        )
    if progress is not None and hasattr(progress, "update"):
        progress.update(len(inputs))
    if not inputs:
        raise ValueError("No stock data available for factor panel")
    return panel_from_screening_inputs(
        inputs,
        storage=storage,
        trade_date=trade_date,
        lookback_days=lookback_days,
    )


def panel_from_screening_inputs(
    inputs: list[ScreeningInput],
    *,
    storage: DuckDBStorage | None = None,
    trade_date: str | None = None,
    lookback_days: int | None = None,
) -> FactorPanelBuildResult:
    symbols = [item.ts_code for item in inputs if str(item.ts_code).strip()]
    if not symbols:
        raise ValueError("No symbols available for factor panel")
    panel: dict[str, pd.DataFrame] = {}
    warnings: list[str] = []

    daily_map = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "vol",
        "amount": "amount",
    }
    for output, source in daily_map.items():
        frame = _wide_from_inputs(inputs, attr="daily", source_column=source)
        if not frame.empty:
            panel[output] = frame

    basic_columns = [
        "pe",
        "pb",
        "ps",
        "turnover_rate",
        "turnover_rate_f",
        "float_mv",
        "total_mv",
        "circ_mv",
    ]
    for column in basic_columns:
        frame = _wide_from_inputs(inputs, attr="daily_basic", source_column=column)
        if not frame.empty:
            panel[column] = frame

    if "amount" in panel and "volume" in panel:
        volume = panel["volume"].where(panel["volume"] > 0)
        panel["vwap"] = (panel["amount"] * 1000.0).div(volume * 100.0).replace([np.inf, -np.inf], np.nan)

    dates = _panel_dates(panel)
    if lookback_days is not None and len(dates) > int(lookback_days):
        dates = dates[-int(lookback_days):]
        panel = {key: value.reindex(index=dates) for key, value in panel.items()}
    if not dates:
        raise ValueError("No trade dates available for factor panel")

    names = _names_from_inputs(inputs)
    industry = _labels_from_inputs(inputs, dates, field_name="industry")
    if not industry.empty:
        panel["industry"] = industry
        panel["sector"] = industry.copy()

    if storage is not None:
        _augment_with_fundamentals(panel, storage, symbols, dates, trade_date, warnings)
        _augment_with_moneyflow(panel, storage, symbols, dates, warnings)

    return FactorPanelBuildResult(
        panel=panel,
        trade_dates=[str(date) for date in dates],
        symbols=symbols,
        names=names,
        warnings=warnings,
    )


def _wide_from_inputs(
    inputs: list[ScreeningInput],
    *,
    attr: str,
    source_column: str,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for item in inputs:
        frame = getattr(item, attr, None)
        if frame is None or frame.empty or source_column not in frame.columns or "trade_date" not in frame.columns:
            continue
        data = frame[["trade_date", source_column]].copy()
        data["ts_code"] = item.ts_code
        frames.append(data)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data["trade_date"] = data["trade_date"].astype(str)
    data[source_column] = pd.to_numeric(data[source_column], errors="coerce")
    wide = data.pivot_table(
        index="trade_date",
        columns="ts_code",
        values=source_column,
        aggfunc="last",
    )
    return wide.sort_index().astype(float)


def _panel_dates(panel: dict[str, pd.DataFrame]) -> list[str]:
    dates: set[str] = set()
    for frame in panel.values():
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            dates.update(str(item) for item in frame.index)
    return sorted(dates)


def _names_from_inputs(inputs: list[ScreeningInput]) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in inputs:
        name = str((item.stock_basic or {}).get("name") or "").strip()
        if name:
            names[item.ts_code] = name
    return names


def _labels_from_inputs(inputs: list[ScreeningInput], dates: list[str], *, field_name: str) -> pd.DataFrame:
    values = {}
    for item in inputs:
        label = str((item.stock_basic or {}).get(field_name) or "").strip()
        if label:
            values[item.ts_code] = label
    if not values:
        return pd.DataFrame()
    frame = pd.DataFrame(index=dates, columns=sorted(values), dtype=object)
    for ts_code, label in values.items():
        frame[ts_code] = label
    return frame


def _augment_with_fundamentals(
    panel: dict[str, pd.DataFrame],
    storage: DuckDBStorage,
    symbols: list[str],
    dates: list[str],
    trade_date: str | None,
    warnings: list[str],
) -> None:
    try:
        fundamentals = storage.get_stock_fundamentals(symbols, as_of=trade_date)
    except Exception as exc:  # pragma: no cover - storage backend guard
        warnings.append(f"fundamentals unavailable: {exc}")
        return
    if fundamentals.empty:
        return
    for column in ("roe", "debt_to_assets", "revenue", "net_profit"):
        if column not in fundamentals.columns:
            continue
        wide = _effective_date_wide(
            fundamentals,
            dates=dates,
            value_column=column,
            date_columns=("ann_date", "end_date"),
        )
        if not wide.empty:
            panel[column] = wide


def _augment_with_moneyflow(
    panel: dict[str, pd.DataFrame],
    storage: DuckDBStorage,
    symbols: list[str],
    dates: list[str],
    warnings: list[str],
) -> None:
    try:
        moneyflow = storage.get_stock_moneyflow(symbols, start_date=dates[0], end_date=dates[-1])
    except Exception as exc:  # pragma: no cover - storage backend guard
        warnings.append(f"moneyflow unavailable: {exc}")
        return
    if moneyflow.empty or "main_net_amount" not in moneyflow.columns:
        return
    wide = _wide_from_frame(moneyflow, value_column="main_net_amount", dates=dates)
    if not wide.empty:
        panel["main_net_amount"] = wide


def _effective_date_wide(
    frame: pd.DataFrame,
    *,
    dates: list[str],
    value_column: str,
    date_columns: tuple[str, ...],
) -> pd.DataFrame:
    rows = frame.copy()
    effective = pd.Series("", index=rows.index, dtype=object)
    for column in date_columns:
        if column in rows.columns:
            values = rows[column].fillna("").astype(str).str.strip()
            effective = effective.where(effective.astype(str).str.strip() != "", values)
    rows["trade_date"] = effective
    rows = rows[rows["trade_date"].astype(str).str.strip() != ""]
    if rows.empty:
        return pd.DataFrame()
    return _wide_from_frame(rows, value_column=value_column, dates=dates, ffill=True)


def _wide_from_frame(
    frame: pd.DataFrame,
    *,
    value_column: str,
    dates: list[str],
    ffill: bool = False,
) -> pd.DataFrame:
    if frame.empty or "ts_code" not in frame.columns or "trade_date" not in frame.columns or value_column not in frame.columns:
        return pd.DataFrame()
    data = frame[["ts_code", "trade_date", value_column]].copy()
    data["ts_code"] = data["ts_code"].astype(str)
    data["trade_date"] = data["trade_date"].astype(str)
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    wide = data.pivot_table(
        index="trade_date",
        columns="ts_code",
        values=value_column,
        aggfunc="last",
    ).sort_index()
    wide = wide.reindex(index=sorted(set([*dates, *map(str, wide.index)]))).sort_index()
    if ffill:
        wide = wide.ffill()
    return wide.reindex(index=dates).astype(float)
