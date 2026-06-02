from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from sats.factors.registry import Registry, RegistryError, SkipAlpha


@dataclass(slots=True)
class FactorPickCandidate:
    ts_code: str
    rank: int
    score: float
    factors: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "rank": self.rank,
            "score": self.score,
            "factors": self.factors,
            "metrics": self.metrics,
        }


@dataclass(slots=True)
class FactorPickResult:
    run_id: str
    trade_date: str
    factors: list[str]
    weighting: str
    neutralization: str
    candidates: list[FactorPickCandidate]
    warnings: list[str] = field(default_factory=list)
    report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "factors": list(self.factors),
            "weighting": self.weighting,
            "neutralization": self.neutralization,
            "candidates": [item.to_dict() for item in self.candidates],
            "warnings": list(self.warnings),
            "report_path": self.report_path,
        }


def cross_section_zscore(df: pd.DataFrame) -> pd.DataFrame:
    mean = df.mean(axis=1, skipna=True)
    std = df.std(axis=1, ddof=1, skipna=True).where(lambda value: value > 1e-12)
    return df.sub(mean, axis=0).div(std, axis=0).replace([np.inf, -np.inf], np.nan)


def neutralize_by_group(df: pd.DataFrame, groups: pd.DataFrame) -> pd.DataFrame:
    groups = groups.reindex(index=df.index, columns=df.columns)
    output = pd.DataFrame(np.nan, index=df.index, columns=df.columns)
    for date in df.index:
        values = df.loc[date]
        labels = groups.loc[date]
        for label in sorted({str(item) for item in labels.dropna().unique() if str(item).strip()}):
            cols = labels[labels.astype(str) == label].index
            part = values[cols]
            valid = part.dropna()
            if len(valid) < 2:
                continue
            std = valid.std(ddof=1)
            if std <= 1e-12:
                continue
            output.loc[date, valid.index] = (valid - valid.mean()) / std
    return output


def compute_factor_panels(
    factor_ids: list[str],
    panel: dict[str, pd.DataFrame],
    *,
    registry: Registry | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, Any]], list[str]]:
    reg = registry or Registry()
    panels: dict[str, pd.DataFrame] = {}
    metas: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for factor_id in factor_ids:
        try:
            factor = reg.get(factor_id)
            raw = reg.compute(factor_id, panel)
        except (KeyError, SkipAlpha, RegistryError, ValueError) as exc:
            warnings.append(f"{factor_id}: {exc}")
            continue
        panels[factor_id] = raw
        metas[factor_id] = dict(factor.meta or {})
    return panels, metas, warnings


def compose_scores(
    panels: dict[str, pd.DataFrame],
    metas: dict[str, dict[str, Any]],
    *,
    weights: dict[str, float] | None = None,
    neutralize: str = "none",
    group_panel: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if not panels:
        return pd.DataFrame()
    transformed: dict[str, pd.DataFrame] = {}
    for factor_id, frame in panels.items():
        direction = str(metas.get(factor_id, {}).get("direction") or "positive")
        data = -frame if direction == "negative" else frame
        if neutralize == "industry" and group_panel is not None:
            data = neutralize_by_group(data, group_panel)
        else:
            data = cross_section_zscore(data)
        transformed[factor_id] = data
    raw_weights = weights or {factor_id: 1.0 for factor_id in transformed}
    total_weight = sum(abs(float(raw_weights.get(factor_id, 0.0))) for factor_id in transformed)
    if total_weight <= 1e-12:
        raw_weights = {factor_id: 1.0 for factor_id in transformed}
        total_weight = float(len(transformed))
    score: pd.DataFrame | None = None
    weight_sum: pd.DataFrame | None = None
    for factor_id, frame in transformed.items():
        weight = float(raw_weights.get(factor_id, 0.0)) / total_weight
        weighted = frame * weight
        valid_weight = frame.notna().astype(float) * abs(weight)
        score = weighted if score is None else score.add(weighted, fill_value=0.0)
        weight_sum = valid_weight if weight_sum is None else weight_sum.add(valid_weight, fill_value=0.0)
    if score is None or weight_sum is None:
        return pd.DataFrame()
    return score.where(weight_sum > 0).div(weight_sum.where(weight_sum > 0))


def pick_top(
    score: pd.DataFrame,
    panels: dict[str, pd.DataFrame],
    *,
    trade_date: str,
    top: int,
    names: dict[str, str] | None = None,
) -> list[FactorPickCandidate]:
    if score.empty:
        return []
    date = _resolve_score_date(score, trade_date)
    if date is None:
        return []
    row = score.loc[date].dropna().sort_values(ascending=False).head(max(1, int(top)))
    names = names or {}
    candidates: list[FactorPickCandidate] = []
    for rank, (ts_code, value) in enumerate(row.items(), start=1):
        factor_values: dict[str, float] = {}
        for factor_id, frame in panels.items():
            if date in frame.index and ts_code in frame.columns:
                raw = frame.at[date, ts_code]
                if pd.notna(raw):
                    factor_values[factor_id] = round(float(raw), 6)
        candidates.append(
            FactorPickCandidate(
                ts_code=str(ts_code),
                name=names.get(str(ts_code), ""),
                rank=rank,
                score=round(float(value), 6),
                factors=factor_values,
                metrics={"score_date": str(date)},
            )
        )
    return candidates


def make_pick_result(
    *,
    trade_date: str,
    factor_ids: list[str],
    weighting: str,
    neutralization: str,
    candidates: list[FactorPickCandidate],
    warnings: list[str] | None = None,
) -> FactorPickResult:
    return FactorPickResult(
        run_id=f"factor_{uuid.uuid4().hex[:12]}",
        trade_date=str(trade_date),
        factors=list(factor_ids),
        weighting=weighting,
        neutralization=neutralization,
        candidates=candidates,
        warnings=list(warnings or []),
    )


def _resolve_score_date(score: pd.DataFrame, trade_date: str) -> Any | None:
    if trade_date in score.index:
        return trade_date
    values = [idx for idx in score.index if str(idx) <= str(trade_date)]
    return values[-1] if values else None
