from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import numpy as np


MIN_VALID_PER_DATE = 5


@dataclass(slots=True)
class FactorAnalysisResult:
    factor_id: str
    trade_date: str
    horizon: int
    ic_mean: float
    rank_ic_mean: float
    icir: float
    rank_icir: float
    positive_ratio: float
    coverage: float
    nan_ratio: float
    group_equity: dict[str, float] = field(default_factory=dict)
    long_short_spread: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "trade_date": self.trade_date,
            "horizon": self.horizon,
            "ic_mean": self.ic_mean,
            "rank_ic_mean": self.rank_ic_mean,
            "icir": self.icir,
            "rank_icir": self.rank_icir,
            "positive_ratio": self.positive_ratio,
            "coverage": self.coverage,
            "nan_ratio": self.nan_ratio,
            "group_equity": self.group_equity,
            "long_short_spread": self.long_short_spread,
            "warnings": list(self.warnings),
        }


def compute_forward_returns(
    close: pd.DataFrame,
    *,
    horizon: int = 1,
    label_mode: str = "t1_close_to_horizon",
) -> pd.DataFrame:
    horizon = max(1, int(horizon))
    if label_mode == "close_to_close":
        returns = close.shift(-horizon).div(close) - 1.0
    else:
        returns = close.shift(-(horizon + 1)).div(close.shift(-1)) - 1.0
    return returns.replace([np.inf, -np.inf], np.nan).astype(float)


def compute_ic_series(factor_df: pd.DataFrame, return_df: pd.DataFrame, *, rank: bool = False) -> pd.Series:
    common_dates = factor_df.index.intersection(return_df.index)
    common_codes = factor_df.columns.intersection(return_df.columns)
    if len(common_dates) == 0 or len(common_codes) == 0:
        return pd.Series(dtype=float)
    factor = factor_df.loc[common_dates, common_codes]
    returns = return_df.loc[common_dates, common_codes]
    pair_mask = factor.notna() & returns.notna()
    valid_count = pair_mask.sum(axis=1)
    factor = factor.where(pair_mask)
    returns = returns.where(pair_mask)
    if rank:
        factor = factor.rank(axis=1, method="average")
        returns = returns.rank(axis=1, method="average")
    ic = factor.corrwith(returns, axis=1, method="pearson")
    return ic[valid_count >= MIN_VALID_PER_DATE].dropna().astype(float)


def compute_group_equity(factor_df: pd.DataFrame, return_df: pd.DataFrame, *, groups: int = 5) -> pd.DataFrame:
    groups = max(2, int(groups))
    common_dates = sorted(factor_df.index.intersection(return_df.index))
    common_codes = factor_df.columns.intersection(return_df.columns)
    if len(common_dates) == 0:
        return pd.DataFrame()
    if len(common_codes) == 0:
        return pd.DataFrame()
    factor = factor_df.loc[common_dates, common_codes]
    returns = return_df.loc[common_dates, common_codes]
    group_returns: dict[str, list[float]] = {f"Group_{idx + 1}": [] for idx in range(groups)}
    dates: list[Any] = []
    for date in common_dates:
        f = factor.loc[date].dropna()
        r = returns.loc[date].dropna()
        shared = f.index.intersection(r.index)
        if len(shared) < groups:
            continue
        ranked = f[shared].rank(method="first")
        bins = pd.qcut(ranked, groups, labels=False, duplicates="drop")
        if bins.nunique() < groups:
            continue
        dates.append(date)
        for idx in range(groups):
            members = bins[bins == idx].index
            group_returns[f"Group_{idx + 1}"].append(float(r[members].mean()) if len(members) else 0.0)
    if not dates:
        return pd.DataFrame()
    return (1.0 + pd.DataFrame(group_returns, index=dates)).cumprod()


def analyze_factor_panel(
    factor_id: str,
    factor_df: pd.DataFrame,
    close: pd.DataFrame,
    *,
    trade_date: str,
    horizon: int = 1,
    groups: int = 5,
    label_mode: str = "t1_close_to_horizon",
) -> FactorAnalysisResult:
    factor_df = _as_of_frame(factor_df, trade_date)
    close = _as_of_frame(close, trade_date)
    returns = compute_forward_returns(close, horizon=horizon, label_mode=label_mode)
    ic = compute_ic_series(factor_df, returns, rank=False)
    rank_ic = compute_ic_series(factor_df, returns, rank=True)
    equity = compute_group_equity(factor_df, returns, groups=groups)
    total_cells = max(1, int(factor_df.shape[0] * factor_df.shape[1]))
    valid_cells = int(factor_df.notna().sum().sum())
    warnings: list[str] = []
    if ic.empty:
        warnings.append("IC 样本不足，可能是股票池太小、因子缺失或收益标签不足")
    if rank_ic.empty:
        warnings.append("RankIC 样本不足")
    ic_std = float(ic.std()) if not ic.empty else 0.0
    rank_std = float(rank_ic.std()) if not rank_ic.empty else 0.0
    group_equity = {col: round(float(equity[col].iloc[-1]), 6) for col in equity.columns} if not equity.empty else {}
    long_short_spread = 0.0
    if len(group_equity) >= 2:
        keys = list(group_equity)
        long_short_spread = round(group_equity[keys[-1]] - group_equity[keys[0]], 6)
    return FactorAnalysisResult(
        factor_id=factor_id,
        trade_date=str(trade_date),
        horizon=max(1, int(horizon)),
        ic_mean=round(float(ic.mean()), 6) if not ic.empty else 0.0,
        rank_ic_mean=round(float(rank_ic.mean()), 6) if not rank_ic.empty else 0.0,
        icir=round(float(ic.mean() / ic_std), 6) if ic_std > 0 else 0.0,
        rank_icir=round(float(rank_ic.mean() / rank_std), 6) if rank_std > 0 else 0.0,
        positive_ratio=round(float((rank_ic > 0).mean()), 6) if not rank_ic.empty else 0.0,
        coverage=round(valid_cells / total_cells, 6),
        nan_ratio=round(1.0 - valid_cells / total_cells, 6),
        group_equity=group_equity,
        long_short_spread=long_short_spread,
        warnings=warnings,
    )


def _as_of_frame(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    target = pd.to_datetime(str(trade_date), errors="coerce")
    if pd.isna(target) or frame.empty:
        return frame
    parsed = pd.to_datetime(pd.Index(frame.index).astype(str), errors="coerce")
    mask = parsed <= target
    if mask.all():
        return frame
    return frame.loc[mask]
