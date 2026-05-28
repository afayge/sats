from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Any

from sats.config import Settings
from sats.trading.broker import BrokerError
from sats.trading.models import BrokerAsset, BrokerOrder, BrokerPosition, BrokerTrade, OrderRequest, OrderResult


class MiniQmtBrokerClient:
    provider = "qmt"

    def __init__(self, *, base_url: str, token: str = "", account_id: str = "", account_type: str = "STOCK", timeout: int = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.account_id = account_id
        self.account_type = account_type or "STOCK"
        self.timeout = timeout
        if not self.base_url:
            raise BrokerError("QMT bridge URL is not configured")

    def status(self) -> dict:
        return self._request("GET", "/health")

    def asset(self) -> BrokerAsset:
        payload = self._request("GET", "/asset")
        data = payload.get("asset") if isinstance(payload.get("asset"), dict) else payload
        return BrokerAsset(
            cash=_num(_pick(data, "cash", "cash_asset", "total_cash")),
            available_cash=_num(_pick(data, "available_cash", "enable_balance", "cash_available")),
            market_value=_num(_pick(data, "market_value", "stock_value", "marketValue")),
            total_asset=_num(_pick(data, "total_asset", "totalAsset", "total_asset_value", "asset")),
            account_id=str(_pick(data, "account_id", default=self.account_id) or self.account_id),
            account_type=str(_pick(data, "account_type", default=self.account_type) or self.account_type),
            raw=data,
        )

    def positions(self) -> list[BrokerPosition]:
        payload = self._request("GET", "/positions")
        rows = payload.get("positions", payload if isinstance(payload, list) else [])
        return [_position_from_payload(row) for row in rows]

    def orders(self, *, open_only: bool = False) -> list[BrokerOrder]:
        query = urllib.parse.urlencode({"open_only": "1" if open_only else "0"})
        payload = self._request("GET", f"/orders?{query}")
        rows = payload.get("orders", payload if isinstance(payload, list) else [])
        return [_order_from_payload(row) for row in rows]

    def trades(self, *, limit: int = 100) -> list[BrokerTrade]:
        query = urllib.parse.urlencode({"limit": int(limit)})
        payload = self._request("GET", f"/trades?{query}")
        rows = payload.get("trades", payload if isinstance(payload, list) else [])
        return [_trade_from_payload(row) for row in rows]

    def place_order(self, request: OrderRequest) -> OrderResult:
        sats_order_id = f"sats-{int(time.time() * 1000)}"
        payload = self._request("POST", "/orders", {**asdict(request), "sats_order_id": sats_order_id})
        return OrderResult(
            sats_order_id=str(payload.get("sats_order_id") or sats_order_id),
            broker_order_id=str(payload.get("order_id") or payload.get("broker_order_id") or ""),
            status=str(payload.get("status") or "submitted"),
            message=str(payload.get("message") or ""),
            request=asdict(request),
            raw=payload,
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        sats_order_id = f"sats-cancel-{int(time.time() * 1000)}"
        payload = self._request("POST", "/cancel", {"order_id": order_id, "sats_order_id": sats_order_id})
        return OrderResult(
            sats_order_id=str(payload.get("sats_order_id") or sats_order_id),
            broker_order_id=str(payload.get("order_id") or payload.get("broker_order_id") or order_id),
            status=str(payload.get("status") or "cancel_requested"),
            message=str(payload.get("message") or ""),
            request={"order_id": order_id},
            raw=payload,
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.token}"} if self.token else {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BrokerError(f"QMT bridge HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise BrokerError(f"QMT bridge unavailable: {exc.reason}") from exc
        if not text:
            return {}
        data = json.loads(text)
        if isinstance(data, dict) and data.get("ok") is False:
            raise BrokerError(str(data.get("error") or "QMT bridge error"))
        return data


def broker_from_settings(settings: Settings) -> MiniQmtBrokerClient:
    provider = (getattr(settings, "broker_provider", "") or "").lower()
    if provider and provider != "qmt":
        raise BrokerError(f"unsupported broker provider: {provider}")
    return MiniQmtBrokerClient(
        base_url=getattr(settings, "qmt_bridge_url", "") or getattr(settings, "miniqmt_gateway_url", ""),
        token=getattr(settings, "qmt_token", "") or getattr(settings, "miniqmt_gateway_token", ""),
        account_id=getattr(settings, "qmt_account_id", ""),
        account_type=getattr(settings, "qmt_account_type", "STOCK"),
    )


def _position_from_payload(row: dict[str, Any]) -> BrokerPosition:
    ts_code = _normalize_ts_code(str(_pick(row, "ts_code", "stock_code", "stockCode", "symbol") or ""))
    quantity = _num(_pick(row, "quantity", "volume", "total_volume", "totalVolume"))
    available = _num(_pick(row, "available_quantity", "can_use_volume", "availableVolume", default=quantity))
    cost = _num(_pick(row, "cost_price", "open_price", "avg_price", "costPrice"))
    price = _num(_pick(row, "price", "last_price", "market_price", "lastPrice"))
    market_value = _num(_pick(row, "market_value", "marketValue", default=price * quantity))
    pnl = _num(_pick(row, "pnl", "profit", "float_profit", default=(price - cost) * quantity if price and cost else 0.0))
    pnl_pct = _num(_pick(row, "pnl_pct", "profit_ratio", default=(pnl / (cost * quantity) * 100.0 if cost and quantity else 0.0)))
    return BrokerPosition(
        ts_code=ts_code,
        name=str(_pick(row, "name", "stock_name", "stockName", default="") or ""),
        quantity=quantity,
        available_quantity=available,
        cost_price=cost,
        price=price,
        market_value=market_value,
        pnl=pnl,
        pnl_pct=pnl_pct,
        raw=row,
    )


def _order_from_payload(row: dict[str, Any]) -> BrokerOrder:
    return BrokerOrder(
        order_id=str(_pick(row, "order_id", "orderId", "broker_order_id", "entrust_no", default="") or ""),
        sats_order_id=str(_pick(row, "sats_order_id", default="") or ""),
        ts_code=_normalize_ts_code(str(_pick(row, "ts_code", "stock_code", "stockCode", "symbol", default="") or "")),
        side=str(_pick(row, "side", "order_type", "orderType", default="") or ""),
        quantity=_num(_pick(row, "quantity", "volume", "order_volume")),
        price=_num(_pick(row, "price", "order_price")),
        price_type=str(_pick(row, "price_type", "priceType", default="") or ""),
        status=str(_pick(row, "status", "order_status", "orderStatus", default="") or ""),
        message=str(_pick(row, "message", "status_msg", "statusMsg", default="") or ""),
        raw=row,
    )


def _trade_from_payload(row: dict[str, Any]) -> BrokerTrade:
    return BrokerTrade(
        trade_id=str(_pick(row, "trade_id", "tradeId", "traded_id", "deal_no", default="") or ""),
        order_id=str(_pick(row, "order_id", "orderId", "broker_order_id", "entrust_no", default="") or ""),
        ts_code=_normalize_ts_code(str(_pick(row, "ts_code", "stock_code", "stockCode", "symbol", default="") or "")),
        side=str(_pick(row, "side", "order_type", "orderType", default="") or ""),
        quantity=_num(_pick(row, "quantity", "volume", "traded_volume")),
        price=_num(_pick(row, "price", "traded_price")),
        trade_time=str(_pick(row, "trade_time", "traded_time", "tradeTime", default="") or ""),
        raw=row,
    )


def _pick(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_ts_code(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not symbol:
        return ""
    if "." in symbol:
        left, right = symbol.split(".", 1)
        if right in {"SH", "SZ", "BJ"}:
            return f"{left}.{right}"
    code = "".join(ch for ch in symbol if ch.isdigit())
    if len(code) == 6:
        if code.startswith(("60", "68", "90")):
            return f"{code}.SH"
        if code.startswith(("8", "4")):
            return f"{code}.BJ"
        return f"{code}.SZ"
    return symbol
