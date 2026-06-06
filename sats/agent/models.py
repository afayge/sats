from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentExecutionPolicy:
    auto_trade: tuple[str, ...] = ()
    broker: str = "noop"
    live_trading: bool = False
    max_order_value: float = 20000.0
    max_position_pct: float = 0.2
    sell_ratio: float = 1.0
    max_iterations: int = 6
    command_timeout: int = 120
    python_timeout: int = 30

    def allows_trade(self, side: str) -> bool:
        return str(side or "").lower() in {item.lower() for item in self.auto_trade}

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["auto_trade"] = list(self.auto_trade)
        return payload


@dataclass(frozen=True, slots=True)
class AgentStep:
    step_id: str
    kind: str
    title: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    code: str = ""
    trade: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    side_effect: str = "readonly"
    success_criteria: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["command"] = list(self.command)
        return payload


@dataclass(frozen=True, slots=True)
class AgentPlan:
    objective: str
    success_criteria: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    steps: tuple[AgentStep, ...] = ()
    risk_level: str = "medium"
    requires_live_trading: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["success_criteria"] = list(self.success_criteria)
        payload["assumptions"] = list(self.assumptions)
        payload["steps"] = [step.to_dict() for step in self.steps]
        return payload


@dataclass(frozen=True, slots=True)
class AgentObservation:
    step_id: str
    kind: str
    status: str
    content: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TradeIntent:
    ts_code: str
    side: str
    quantity: int | None = None
    price_type: str = "latest"
    price: float | None = None
    reason: str = ""
    source_step_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TradeDecisionAudit:
    intent: TradeIntent
    status: str
    message: str
    request: dict[str, Any] = field(default_factory=dict)
    quote: dict[str, Any] = field(default_factory=dict)
    order: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["intent"] = self.intent.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class AgentResult:
    content: str
    plan: AgentPlan
    observations: tuple[AgentObservation, ...] = ()
    data_names: tuple[str, ...] = ("Agent",)
    skill_names: tuple[str, ...] = ()
    tool_call_count: int = 0
    artifacts: tuple[dict[str, Any], ...] = ()
    turn_id: str | None = None
    session_id: str = "agent"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["plan"] = self.plan.to_dict()
        payload["observations"] = [item.to_dict() for item in self.observations]
        return payload
