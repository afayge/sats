from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from sats.data.astock_provider import AStockDataProvider
from sats.factors.composite import (
    FactorPickResult,
    compose_scores,
    compute_factor_panels,
    make_pick_result,
    pick_top,
)
from sats.factors.panel import FactorPanelBuildResult, build_factor_panel, panel_from_screening_inputs
from sats.factors.profiles import DEFAULT_FACTOR_PROFILE, get_factor_profile, resolve_factor_ids
from sats.factors.registry import Registry
from sats.screening.base import ScreeningInput
from sats.storage.duckdb import DuckDBStorage


@dataclass(slots=True)
class FactorSnapshot:
    trade_date: str
    profile: str
    factor_ids: list[str]
    score: pd.DataFrame
    panels: dict[str, pd.DataFrame]
    metas: dict[str, dict[str, Any]]
    names: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def score_for(self, ts_code: str) -> float | None:
        date = _resolve_score_date(self.score, self.trade_date)
        if date is None or ts_code not in self.score.columns:
            return None
        value = self.score.at[date, ts_code]
        if pd.isna(value):
            return None
        return round(float(value), 6)

    def factor_values_for(self, ts_code: str) -> dict[str, float]:
        date = _resolve_score_date(self.score, self.trade_date)
        if date is None:
            return {}
        values: dict[str, float] = {}
        for factor_id, frame in self.panels.items():
            if date in frame.index and ts_code in frame.columns:
                raw = frame.at[date, ts_code]
                if pd.notna(raw) and np.isfinite(float(raw)):
                    values[factor_id] = round(float(raw), 6)
        return values

    def coverage_for(self, ts_code: str) -> float:
        values = self.factor_values_for(ts_code)
        return round(len(values) / max(1, len(self.factor_ids)), 6)

    def exposure_for(self, ts_code: str) -> dict[str, Any]:
        score = self.score_for(ts_code)
        values = self.factor_values_for(ts_code)
        missing = [factor_id for factor_id in self.factor_ids if factor_id not in values]
        return {
            "ts_code": ts_code,
            "name": self.names.get(ts_code, ""),
            "profile": self.profile,
            "score": score,
            "coverage": self.coverage_for(ts_code),
            "factor_values": values,
            "missing_factors": missing,
        }

    def to_dict(self, *, symbols: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        symbols = list(symbols or self.score.columns.astype(str).tolist())
        return {
            "trade_date": self.trade_date,
            "profile": self.profile,
            "profile_display_name": get_factor_profile(self.profile).display_name,
            "factor_ids": list(self.factor_ids),
            "coverage": _snapshot_coverage(self),
            "warnings": list(self.warnings),
            "exposures": [self.exposure_for(symbol) for symbol in symbols],
        }


def compute_factor_snapshot(
    panel: dict[str, pd.DataFrame],
    *,
    trade_date: str,
    profile: str = DEFAULT_FACTOR_PROFILE,
    factor_ids: list[str] | tuple[str, ...] | None = None,
    names: dict[str, str] | None = None,
    neutralize: str = "none",
    registry: Registry | None = None,
    warnings: list[str] | None = None,
) -> FactorSnapshot:
    resolved_profile = get_factor_profile(profile).name
    ids = resolve_factor_ids(profile=resolved_profile, factor_ids=factor_ids)
    panels, metas, factor_warnings = compute_factor_panels(ids, panel, registry=registry or Registry())
    group_panel = panel.get("industry") if neutralize == "industry" else None
    score = compose_scores(panels, metas, neutralize=neutralize, group_panel=group_panel)
    if score.empty and panels:
        factor_warnings.append("factor_score: empty composite score")
    return FactorSnapshot(
        trade_date=str(trade_date),
        profile=resolved_profile,
        factor_ids=list(ids),
        score=score,
        panels=panels,
        metas=metas,
        names=dict(names or {}),
        warnings=[*(warnings or []), *factor_warnings],
    )


def pick_with_factor_profile(
    *,
    provider: AStockDataProvider,
    storage: DuckDBStorage,
    trade_date: str,
    profile: str = DEFAULT_FACTOR_PROFILE,
    factor_ids: list[str] | tuple[str, ...] | None = None,
    symbols: list[str] | None = None,
    lookback_days: int = 260,
    top: int = 20,
    neutralize: str = "none",
    weighting: str = "equal",
    registry: Registry | None = None,
) -> tuple[FactorPickResult, FactorPanelBuildResult, FactorSnapshot]:
    panel_result = build_factor_panel(
        provider=provider,
        storage=storage,
        trade_date=trade_date,
        lookback_days=lookback_days,
        symbols=symbols,
    )
    snapshot = compute_factor_snapshot(
        panel_result.panel,
        trade_date=trade_date,
        profile=profile,
        factor_ids=factor_ids,
        names=panel_result.names,
        neutralize=neutralize,
        registry=registry,
        warnings=panel_result.warnings,
    )
    candidates = pick_top(snapshot.score, snapshot.panels, trade_date=trade_date, top=top, names=panel_result.names)
    result = make_pick_result(
        trade_date=trade_date,
        factor_ids=list(snapshot.panels),
        weighting=weighting,
        neutralization=neutralize,
        candidates=candidates,
        warnings=snapshot.warnings,
    )
    return result, panel_result, snapshot


def snapshot_from_screening_inputs(
    inputs: list[ScreeningInput],
    *,
    storage: DuckDBStorage | None,
    trade_date: str,
    profile: str = DEFAULT_FACTOR_PROFILE,
    factor_ids: list[str] | tuple[str, ...] | None = None,
    lookback_days: int = 260,
    neutralize: str = "none",
    registry: Registry | None = None,
) -> tuple[FactorSnapshot, FactorPanelBuildResult]:
    panel_result = panel_from_screening_inputs(
        inputs,
        storage=storage,
        trade_date=trade_date,
        lookback_days=lookback_days,
    )
    snapshot = compute_factor_snapshot(
        panel_result.panel,
        trade_date=trade_date,
        profile=profile,
        factor_ids=factor_ids,
        names=panel_result.names,
        neutralize=neutralize,
        registry=registry,
        warnings=panel_result.warnings,
    )
    return snapshot, panel_result


def summarize_factor_exposure(snapshot: FactorSnapshot, symbols: list[str] | tuple[str, ...]) -> dict[str, Any]:
    return snapshot.to_dict(symbols=list(symbols))


def _snapshot_coverage(snapshot: FactorSnapshot) -> float:
    if snapshot.score.empty:
        return 0.0
    date = _resolve_score_date(snapshot.score, snapshot.trade_date)
    if date is None:
        return 0.0
    row = snapshot.score.loc[date]
    return round(float(row.notna().mean()), 6)


def _resolve_score_date(score: pd.DataFrame, trade_date: str) -> Any | None:
    if score.empty:
        return None
    if trade_date in score.index:
        return trade_date
    values = [idx for idx in score.index if str(idx) <= str(trade_date)]
    return values[-1] if values else None
