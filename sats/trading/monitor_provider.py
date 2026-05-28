from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sats.storage.duckdb import DuckDBStorage
from sats.trading.broker import BrokerClient, BrokerError
from sats.trading.models import OrderRequest


@dataclass(slots=True)
class AutoTradeConfig:
    enabled_actions: set[str]
    max_order_value: float = 20000.0
    max_position_pct: float = 0.2
    sell_ratio: float = 1.0


class QmtTradingProvider:
    def __init__(self, *, client: BrokerClient, storage: DuckDBStorage, config: AutoTradeConfig) -> None:
        self.client = client
        self.storage = storage
        self.config = config

    def build_trade_event(self, event: dict, *, action: str, quantity: Any = None) -> dict:
        trade_event_id = _stable_id(f"trade:{event.get('event_id')}:{action}")
        base = {
            "trade_event_id": trade_event_id,
            "event_id": event.get("event_id"),
            "ts_code": event.get("ts_code"),
            "name": event.get("name"),
            "action": action,
            "side": event.get("side"),
            "price": event.get("price"),
            "quantity": quantity,
            "status": "rejected",
            "message": "",
            "metrics": {"source_event": event, "broker": "qmt"},
        }
        if action not in self.config.enabled_actions:
            return {**base, "status": "not_configured", "message": f"自动{action}未启用，仅记录监控建议"}
        try:
            request = self._request_for_event(event, action=action, quantity=quantity)
            result = self.client.place_order(request)
            self.storage.insert_broker_order(
                {
                    "sats_order_id": result.sats_order_id,
                    "provider": self.client.provider,
                    "account_id": self.client.account_id,
                    "broker_order_id": result.broker_order_id,
                    "ts_code": request.symbol,
                    "side": request.side,
                    "quantity": request.quantity,
                    "price": request.price,
                    "price_type": request.price_type,
                    "status": result.status,
                    "message": result.message,
                    "request": request.to_dict(),
                    "response": result.raw,
                }
            )
            self.storage.insert_broker_order_event(
                {
                    "sats_order_id": result.sats_order_id,
                    "broker_order_id": result.broker_order_id,
                    "provider": self.client.provider,
                    "account_id": self.client.account_id,
                    "event_type": "monitor_order",
                    "status": result.status,
                    "message": result.message,
                    "payload": result.to_dict(),
                }
            )
            return {
                **base,
                "quantity": request.quantity,
                "status": result.status or "submitted",
                "message": f"QMT 已提交 {action} 委托 {result.broker_order_id}".strip(),
                "metrics": {**base["metrics"], "order": result.to_dict()},
            }
        except Exception as exc:
            return {**base, "message": f"QMT {action} 拒绝: {exc}"}

    def _request_for_event(self, event: dict, *, action: str, quantity: Any = None) -> OrderRequest:
        ts_code = str(event.get("ts_code") or "")
        price = float(event.get("price") or 0.0)
        if not ts_code:
            raise BrokerError("missing symbol")
        if action == "buy":
            shares = self._buy_quantity(price)
        else:
            shares = self._sell_quantity(ts_code, quantity)
        if shares <= 0:
            raise BrokerError("calculated order quantity is zero")
        return OrderRequest(symbol=ts_code, side=action, quantity=shares, price_type="latest", price=None, strategy="sats-monitor", source_event_id=str(event.get("event_id") or ""))

    def _buy_quantity(self, price: float) -> int:
        if price <= 0:
            raise BrokerError("missing realtime price for buy")
        asset = self.client.asset()
        budget = min(float(asset.available_cash or 0.0), float(self.config.max_order_value or 0.0))
        if self.config.max_position_pct and asset.total_asset:
            budget = min(budget, float(asset.total_asset) * float(self.config.max_position_pct))
        shares = int(budget // (price * 100)) * 100
        if shares <= 0:
            raise BrokerError("cash is insufficient for one board lot")
        return shares

    def _sell_quantity(self, ts_code: str, quantity: Any) -> int:
        available = 0.0
        for position in self.client.positions():
            if position.ts_code == ts_code:
                available = position.available_quantity or position.quantity
                break
        target = float(quantity or available or 0.0) * float(self.config.sell_ratio or 1.0)
        target = min(target, available)
        shares = int(target)
        if shares <= 0:
            raise BrokerError("available position is insufficient")
        return shares


def _stable_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:24]
