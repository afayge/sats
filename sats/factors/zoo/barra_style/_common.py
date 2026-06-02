from __future__ import annotations

import numpy as np
import pandas as pd

from sats.factors.registry import SkipAlpha


SOURCE = "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data."
LICENSE_NOTE = "SATS original implementation. MSCI Barra names are used only as style-factor references."


def require_panel(panel: dict[str, pd.DataFrame], *columns: str) -> list[pd.DataFrame]:
    missing = [column for column in columns if column not in panel]
    if missing:
        raise SkipAlpha(f"panel missing required columns {missing}")
    return [panel[column] for column in columns]


def finite(df: pd.DataFrame) -> pd.DataFrame:
    return df.replace([np.inf, -np.inf], np.nan)


def positive(df: pd.DataFrame) -> pd.DataFrame:
    return df.where(df > 0)


def inverse(df: pd.DataFrame) -> pd.DataFrame:
    return finite(1.0 / positive(df))


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    mean = df.mean(axis=1, skipna=True)
    std = df.std(axis=1, ddof=1, skipna=True).where(lambda value: value > 1e-12)
    return finite(df.sub(mean, axis=0).div(std, axis=0))


def close_returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)


def benchmark_returns(panel: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    benchmark = panel.get("benchmark_close")
    if benchmark is None:
        return None
    if isinstance(benchmark, pd.Series):
        benchmark = benchmark.to_frame("benchmark")
    if benchmark.empty:
        return None
    if benchmark.shape[1] > 1:
        benchmark = benchmark.iloc[:, :1]
    return close_returns(benchmark).iloc[:, 0].to_frame("benchmark")


def row_mean_returns(close: pd.DataFrame) -> pd.DataFrame:
    return close_returns(close).mean(axis=1, skipna=True).to_frame("benchmark")


def all_nan_like(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    close = panel.get("close")
    if close is None:
        raise SkipAlpha("panel missing close reference")
    return pd.DataFrame(np.nan, index=close.index, columns=close.columns)
