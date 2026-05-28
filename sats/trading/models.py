from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class BrokerAsset:
    cash: float = 0.0
    available_cash: float = 0.0
    market_value: float = 0.0
    total_asset: float = 0.0
    account_id: str = ""
    account_type: str = "STOCK"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BrokerPosition:
    ts_code: str
    name: str = ""
    quantity: float = 0.0
    available_quantity: float = 0.0
    cost_price: float = 0.0
    price: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BrokerOrder:
    order_id: str = ""
    sats_order_id: str = ""
    ts_code: str = ""
    side: str = ""
    quantity: float = 0.0
    price: float = 0.0
    price_type: str = ""
    status: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BrokerTrade:
    trade_id: str = ""
    order_id: str = ""
    ts_code: str = ""
    side: str = ""
    quantity: float = 0.0
    price: float = 0.0
    trade_time: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: str
    quantity: int
    price_type: str = "latest"
    price: float | None = None
    dry_run: bool = False
    strategy: str = "sats"
    source_event_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OrderResult:
    sats_order_id: str
    broker_order_id: str = ""
    status: str = ""
    message: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
