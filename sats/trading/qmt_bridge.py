from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class QmtBridgeConfig:
    qmt_path: str
    account_id: str
    account_type: str = "STOCK"
    session_id: int = 0
    token: str = ""


class XtQuantQmtGateway:
    def __init__(self, config: QmtBridgeConfig) -> None:
        self.config = config
        self.connected = False
        self._trader = None
        self._account = None

    def connect(self) -> None:
        try:
            from xtquant import xtconstant
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
        except ImportError as exc:  # pragma: no cover - Windows bridge runtime only
            raise RuntimeError("xtquant 未安装；请在安装国金证券 QMT/MiniQMT 的 Windows Python 环境中运行 bridge") from exc

        account_type = self.config.account_type or "STOCK"
        self._xtconstant = xtconstant
        self._trader = XtQuantTrader(self.config.qmt_path, int(self.config.session_id or 0))
        self._trader.start()
        result = self._trader.connect()
        if result not in (0, None):
            raise RuntimeError(f"QMT connect failed: {result}")
        self._account = StockAccount(self.config.account_id, account_type)
        subscribe_result = self._trader.subscribe(self._account)
        if subscribe_result not in (0, None):
            raise RuntimeError(f"QMT subscribe failed: {subscribe_result}")
        self.connected = True

    def health(self) -> dict[str, Any]:
        return {"ok": True, "connected": self.connected, "account_id": self.config.account_id, "account_type": self.config.account_type}

    def asset(self) -> dict[str, Any]:
        self._ensure_connected()
        return {"asset": _obj_to_dict(self._trader.query_stock_asset(self._account))}

    def positions(self) -> dict[str, Any]:
        self._ensure_connected()
        rows = self._trader.query_stock_positions(self._account)
        if rows is None:
            raise RuntimeError("QMT position query failed: query_stock_positions returned None")
        return {"positions": [_obj_to_dict(item) for item in rows]}

    def orders(self, *, open_only: bool = False) -> dict[str, Any]:
        self._ensure_connected()
        rows = self._trader.query_stock_orders(self._account, bool(open_only)) or []
        return {"orders": [_obj_to_dict(item) for item in rows]}

    def trades(self, *, limit: int = 100) -> dict[str, Any]:
        self._ensure_connected()
        rows = self._trader.query_stock_trades(self._account) or []
        return {"trades": [_obj_to_dict(item) for item in rows[: int(limit)]]}

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_connected()
        side = str(payload.get("side") or "").lower()
        price_type = str(payload.get("price_type") or "latest").lower()
        order_type = self._xtconstant.STOCK_BUY if side == "buy" else self._xtconstant.STOCK_SELL
        xt_price_type = self._xtconstant.FIX_PRICE if price_type == "limit" else self._xtconstant.LATEST_PRICE
        order_id = self._trader.order_stock(
            self._account,
            _to_qmt_symbol(str(payload.get("symbol") or "")),
            order_type,
            int(payload.get("quantity") or 0),
            xt_price_type,
            float(payload.get("price") or 0.0),
            str(payload.get("strategy") or "sats"),
            str(payload.get("source_event_id") or payload.get("sats_order_id") or ""),
        )
        return {"ok": True, "status": "submitted", "order_id": str(order_id), "sats_order_id": payload.get("sats_order_id", "")}

    def cancel_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_connected()
        order_id = str(payload.get("order_id") or "")
        result = self._trader.cancel_order_stock(self._account, order_id)
        return {"ok": True, "status": "cancel_requested", "order_id": order_id, "result": result, "sats_order_id": payload.get("sats_order_id", "")}

    def _ensure_connected(self) -> None:
        if not self.connected:
            self.connect()


def create_app(config: QmtBridgeConfig):
    from fastapi import Depends, FastAPI, Header, HTTPException

    gateway = XtQuantQmtGateway(config)
    app = FastAPI(title="SATS MiniQMT Bridge")

    def verify_token(authorization: str = Header(default="")) -> None:
        if not config.token:
            return
        if authorization != f"Bearer {config.token}":
            raise HTTPException(status_code=401, detail="invalid QMT bridge token")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return gateway.health()

    @app.get("/asset")
    def asset(_: None = Depends(verify_token)) -> dict[str, Any]:
        return gateway.asset()

    @app.get("/positions")
    def positions(_: None = Depends(verify_token)) -> dict[str, Any]:
        return gateway.positions()

    @app.get("/orders")
    def orders(open_only: bool = False, _: None = Depends(verify_token)) -> dict[str, Any]:
        return gateway.orders(open_only=open_only)

    @app.get("/trades")
    def trades(limit: int = 100, _: None = Depends(verify_token)) -> dict[str, Any]:
        return gateway.trades(limit=limit)

    @app.post("/orders")
    def place_order(payload: dict[str, Any], _: None = Depends(verify_token)) -> dict[str, Any]:
        return gateway.place_order(payload)

    @app.post("/cancel")
    def cancel_order(payload: dict[str, Any], _: None = Depends(verify_token)) -> dict[str, Any]:
        return gateway.cancel_order(payload)

    return app


def run_bridge(*, host: str, port: int, config: QmtBridgeConfig) -> None:
    if host not in {"127.0.0.1", "localhost"} and not config.token:
        raise RuntimeError("QMT bridge 绑定非本机地址时必须配置 SATS_QMT_TOKEN 或 --token")
    import uvicorn

    uvicorn.run(create_app(config), host=host, port=int(port))


def _obj_to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    data = {k: v for k, v in getattr(obj, "__dict__", {}).items() if not k.startswith("_")}
    for name in (
        "account_id",
        "account_type",
        "cash",
        "available_cash",
        "market_value",
        "total_asset",
        "stock_code",
        "stock_name",
        "volume",
        "can_use_volume",
        "open_price",
        "buy_date",
        "open_date",
        "position_date",
        "market_value",
        "order_id",
        "order_status",
        "traded_volume",
        "traded_price",
        "traded_time",
    ):
        if hasattr(obj, name):
            data.setdefault(name, getattr(obj, name))
    return data


def _to_qmt_symbol(symbol: str) -> str:
    return symbol.strip().upper()
