from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(slots=True)
class ScreeningInput:
    ts_code: str
    trade_date: str
    daily: pd.DataFrame
    daily_basic: pd.DataFrame
    stock_basic: dict[str, Any] = field(default_factory=dict)
    industry_daily: pd.DataFrame | None = None
    fallback_index_daily: pd.DataFrame | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScreeningResult:
    trade_date: str
    ts_code: str
    rule_name: str
    passed: bool
    score: float
    matched_conditions: list[str]
    failed_conditions: list[str]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "ts_code": self.ts_code,
            "rule_name": self.rule_name,
            "passed": self.passed,
            "score": self.score,
            "matched_conditions": self.matched_conditions,
            "failed_conditions": self.failed_conditions,
            "metrics_json": self.metrics,
        }


@dataclass(frozen=True, slots=True)
class IntradayKlineRequirement:
    period: str
    metadata_key: str
    source_metadata_key: str
    history_calendar_days: int = 0
    count: int | None = None
    candidate_metadata_key: str | None = None


class ScreeningRule(ABC):
    name: str
    required_trade_days: int | None = None
    intraday_kline_requirements: tuple[IntradayKlineRequirement, ...] = ()

    def prepare_inputs(self, inputs: list[ScreeningInput]) -> list[ScreeningInput]:
        return inputs

    def intraday_candidate_labels(
        self,
        data: ScreeningInput,
        requirement: IntradayKlineRequirement,
    ) -> list[str]:
        return [self.name]

    @abstractmethod
    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        raise NotImplementedError
