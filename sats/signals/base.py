from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class SignalDefinition:
    signal_id: str
    label: str
    category: str
    side: str
    description: str = ""


@dataclass(slots=True)
class SignalInput:
    ts_code: str
    trade_date: str
    daily: pd.DataFrame
    stock_basic: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SignalEvent:
    signal_id: str
    label: str
    category: str
    side: str
    confidence: float
    score: float
    reason: str
    action_time: str = "next_trade_day"
    evidence: dict[str, Any] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)
    related_chan: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "label": self.label,
            "category": self.category,
            "side": self.side,
            "confidence": self.confidence,
            "score": self.score,
            "reason": self.reason,
            "action_time": self.action_time,
            "evidence": self.evidence,
            "risk_flags": self.risk_flags,
            "related_chan": self.related_chan,
            "components": self.components,
        }


@dataclass(slots=True)
class SignalAnalysisResult:
    ts_code: str
    trade_date: str
    name: str
    close: float
    score: float
    decision: str
    trend: str
    events: list[SignalEvent]
    selected_signals: list[str]
    key_levels: dict[str, Any] = field(default_factory=dict)
    llm_unavailable: bool = False
    llm_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "trade_date": self.trade_date,
            "name": self.name,
            "close": self.close,
            "score": self.score,
            "decision": self.decision,
            "trend": self.trend,
            "events": [event.to_dict() for event in self.events],
            "selected_signals": self.selected_signals,
            "key_levels": self.key_levels,
            "llm_unavailable": self.llm_unavailable,
            "llm_summary": self.llm_summary,
        }


@dataclass(slots=True)
class SignalAnalysisRun:
    trade_date: str
    results: list[SignalAnalysisResult]
    report_path: str | None = None
    message: str = ""
    llm_unavailable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "results": [result.to_dict() for result in self.results],
            "report_path": self.report_path,
            "message": self.message,
            "llm_unavailable": self.llm_unavailable,
        }
