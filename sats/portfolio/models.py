from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    trading_mode: str = "paper"
    candidate_limit: int = 10
    selected_limit: int = 5
    max_replacements: int = 2
    replacement_score_gap: float = 5.0
    max_exposure: float = 0.70
    reduced_exposure: float = 0.35
    max_position_pct: float = 0.14
    max_stop_loss_pct: float = 0.05
    take_profit_1_r: float = 1.5
    take_profit_2_r: float = 2.5
    trailing_stop_pct: float = 0.06
    max_holding_trade_days: int = 4
    live_intent_ttl_seconds: int = 300
    live_price_drift_pct: float = 0.005
    paper_initial_cash: float = 1_000_000.0
    account_id: str = "default"
    llm_enabled: bool = True

    def normalized(self) -> "PortfolioConfig":
        mode = str(self.trading_mode or "paper").strip().lower()
        if mode not in {"paper", "live"}:
            raise ValueError("trading_mode must be paper or live")
        return PortfolioConfig(
            trading_mode=mode,
            candidate_limit=max(10, int(self.candidate_limit)),
            selected_limit=max(1, min(5, int(self.selected_limit))),
            max_replacements=max(0, int(self.max_replacements)),
            replacement_score_gap=max(0.0, float(self.replacement_score_gap)),
            max_exposure=max(0.0, min(1.0, float(self.max_exposure))),
            reduced_exposure=max(0.0, min(1.0, float(self.reduced_exposure))),
            max_position_pct=max(0.0, min(1.0, float(self.max_position_pct))),
            max_stop_loss_pct=max(0.01, min(0.20, float(self.max_stop_loss_pct))),
            take_profit_1_r=max(0.1, float(self.take_profit_1_r)),
            take_profit_2_r=max(float(self.take_profit_1_r), float(self.take_profit_2_r)),
            trailing_stop_pct=max(0.01, min(0.30, float(self.trailing_stop_pct))),
            max_holding_trade_days=max(1, int(self.max_holding_trade_days)),
            live_intent_ttl_seconds=max(30, int(self.live_intent_ttl_seconds)),
            live_price_drift_pct=max(0.001, min(0.10, float(self.live_price_drift_pct))),
            paper_initial_cash=max(10_000.0, float(self.paper_initial_cash)),
            account_id=str(self.account_id or "default"),
            llm_enabled=bool(self.llm_enabled),
        )


@dataclass(frozen=True, slots=True)
class MarketRegime:
    trade_date: str
    score: float
    exposure_limit: float
    buy_allowed: bool
    data_source: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PortfolioCandidate:
    candidate_id: str
    plan_id: str
    run_id: str
    trade_date: str
    effective_trade_date: str
    ts_code: str
    name: str
    industry: str
    rank_no: int
    selected: bool
    status: str
    total_score: float
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    trailing_stop_pct: float
    valid_until: str
    score_components: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["score_json"] = payload.pop("score_components")
        payload["evidence_json"] = payload.pop("evidence")
        return payload


@dataclass(frozen=True, slots=True)
class PortfolioRunResult:
    run_id: str
    trade_date: str
    phase: str
    trading_mode: str
    status: str
    market_regime: MarketRegime
    candidates: tuple[PortfolioCandidate, ...] = ()
    actions: tuple[dict[str, Any], ...] = ()
    message: str = ""
    report_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "phase": self.phase,
            "trading_mode": self.trading_mode,
            "status": self.status,
            "market_regime": self.market_regime.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "actions": [dict(item) for item in self.actions],
            "message": self.message,
            "report_path": self.report_path,
        }
