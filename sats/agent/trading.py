from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sats.agent.models import AgentExecutionPolicy, TradeDecisionAudit, TradeIntent
from sats.data.resolver import MarketDataResolver, require_market_data_provenance
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols
from sats.trading import broker_from_settings
from sats.trading.models import BrokerAsset, BrokerPosition, OrderRequest, OrderResult


class AgentTradingExecutor:
    def __init__(
        self,
        *,
        settings: Any,
        storage: DuckDBStorage,
        resolver: MarketDataResolver,
        policy: AgentExecutionPolicy,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.resolver = resolver
        self.policy = policy
        self.client = client

    def execute(self, intent: TradeIntent) -> TradeDecisionAudit:
        side = str(intent.side or "").lower()
        if side not in {"buy", "sell"}:
            return TradeDecisionAudit(intent=intent, status="rejected", message=f"unsupported trade side: {intent.side}")
        if not self.policy.allows_trade(side):
            return TradeDecisionAudit(intent=intent, status="rejected", message=f"auto-trade does not allow {side}")
        symbols = normalize_symbols([intent.ts_code], required=False)
        if not symbols:
            return TradeDecisionAudit(intent=intent, status="rejected", message="trade intent requires A-share symbol")
        ts_code = symbols[0]
        quote = self.resolver.load_realtime_quotes([ts_code], for_trading=True)
        try:
            require_market_data_provenance(quote, dataset="realtime_quote")
        except ValueError as exc:
            return TradeDecisionAudit(intent=intent, status="rejected", message=str(exc))
        if quote.empty:
            return TradeDecisionAudit(intent=intent, status="rejected", message="missing realtime quote for trade")
        price = _quote_price(quote.iloc[0].to_dict())
        if price <= 0:
            return TradeDecisionAudit(intent=intent, status="rejected", message="realtime quote has no usable price")
        client = self._client()
        try:
            quantity = self._quantity(intent, side=side, ts_code=ts_code, price=price, client=client)
            request = OrderRequest(
                symbol=ts_code,
                side=side,
                quantity=quantity,
                price_type=intent.price_type or "latest",
                price=intent.price,
                dry_run=not self.policy.live_trading or self.policy.broker != "qmt",
                strategy="sats-agent",
                source_event_id=intent.source_step_id,
            )
            if request.dry_run:
                result = OrderResult(
                    sats_order_id=f"agent-dry-{intent.source_step_id or ts_code}",
                    status="dry_run",
                    message="agent dry-run; no broker endpoint called",
                    request=request.to_dict(),
                    raw={},
                )
            else:
                result = client.place_order(request)
            self._persist_order(result, request, quote=quote.iloc[0].to_dict())
            return TradeDecisionAudit(
                intent=intent,
                status=result.status or "submitted",
                message=result.message or f"agent {side} {result.status}",
                request=request.to_dict(),
                quote=quote.iloc[0].to_dict(),
                order=result.to_dict(),
            )
        except Exception as exc:
            return TradeDecisionAudit(intent=intent, status="rejected", message=str(exc), quote=quote.iloc[0].to_dict())

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        if self.policy.broker == "qmt" and self.policy.live_trading:
            self.client = broker_from_settings(self.settings)
        else:
            self.client = _DryRunBroker(account_id=getattr(self.settings, "qmt_account_id", ""))
        return self.client

    def _quantity(self, intent: TradeIntent, *, side: str, ts_code: str, price: float, client: Any) -> int:
        if intent.quantity is not None:
            quantity = int(intent.quantity)
        elif side == "buy":
            asset = client.asset()
            budget = min(float(asset.available_cash or 0.0), float(self.policy.max_order_value or 0.0))
            if self.policy.max_position_pct and asset.total_asset:
                budget = min(budget, float(asset.total_asset) * float(self.policy.max_position_pct))
            quantity = int(budget // (price * 100)) * 100
        else:
            quantity = int(_available_position(client.positions(), ts_code) * float(self.policy.sell_ratio or 1.0))
        if quantity <= 0:
            raise ValueError("calculated order quantity is zero")
        if side == "buy" and quantity % 100 != 0:
            raise ValueError("A股买入数量必须是 100 股整数倍")
        if side == "sell":
            available = _available_position(client.positions(), ts_code)
            if available < quantity:
                raise ValueError(f"可用持仓不足: {ts_code} 可用 {available:g}")
        return quantity

    def _persist_order(self, result: OrderResult, request: OrderRequest, *, quote: dict[str, Any]) -> None:
        self.storage.insert_broker_order(
            {
                "sats_order_id": result.sats_order_id,
                "provider": getattr(self._client(), "provider", self.policy.broker),
                "account_id": getattr(self._client(), "account_id", ""),
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
        self.storage.insert_monitor_trade_event(
            {
                "trade_event_id": result.sats_order_id,
                "event_id": request.source_event_id,
                "ts_code": request.symbol,
                "name": "",
                "action": request.side,
                "side": request.side,
                "price": quote.get("price"),
                "quantity": request.quantity,
                "status": result.status,
                "message": result.message,
                "metrics": {"broker_order": result.to_dict(), "quote": quote},
            }
        )


@dataclass(slots=True)
class _DryRunBroker:
    provider: str = "noop"
    account_id: str = ""

    def asset(self) -> BrokerAsset:
        return BrokerAsset(cash=1_000_000.0, available_cash=1_000_000.0, total_asset=1_000_000.0, account_id=self.account_id)

    def positions(self) -> list[BrokerPosition]:
        return []


def _quote_price(row: dict[str, Any]) -> float:
    for key in ("price", "last_price", "latest_price", "close"):
        try:
            value = float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _available_position(positions: list[BrokerPosition], ts_code: str) -> float:
    for position in positions:
        if position.ts_code == ts_code:
            return float(position.available_quantity or position.quantity or 0.0)
    return 0.0
