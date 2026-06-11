#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class BridgeConfig:
    qmt_path: str
    account_id: str
    account_type: str = "STOCK"
    session_id: int = 0


class XtQuantGateway:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.connected = False
        self._connect_lock = threading.Lock()
        self._trader = None
        self._account = None
        self._xtconstant = None

    def connect(self) -> None:
        try:
            from xtquant import xtconstant
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
        except ImportError as exc:
            raise RuntimeError("xtquant is not installed; run this script in the Windows QMT Python environment") from exc

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
        return {
            "ok": True,
            "connected": self.connected,
            "account_id": self.config.account_id,
            "account_type": self.config.account_type,
        }

    def asset(self) -> dict[str, Any]:
        self._ensure_connected()
        return {"asset": _obj_to_dict(self._trader.query_stock_asset(self._account))}

    def positions(self) -> dict[str, Any]:
        self._ensure_connected()
        rows = self._trader.query_stock_positions(self._account) or []
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
        side = str(payload.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("order side must be buy or sell")
        price_type = str(payload.get("price_type") or "latest").lower()
        self._ensure_connected()
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
        return {
            "ok": True,
            "status": "submitted",
            "order_id": str(order_id),
            "sats_order_id": payload.get("sats_order_id", ""),
        }

    def cancel_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_connected()
        order_id = str(payload.get("order_id") or "")
        result = self._trader.cancel_order_stock(self._account, order_id)
        return {
            "ok": True,
            "status": "cancel_requested",
            "order_id": order_id,
            "result": result,
            "sats_order_id": payload.get("sats_order_id", ""),
        }

    def _ensure_connected(self) -> None:
        if self.connected:
            return
        with self._connect_lock:
            if not self.connected:
                self.connect()


class QmtBridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], gateway: Any, token: str = "") -> None:
        super().__init__(server_address, QmtBridgeRequestHandler)
        self.gateway = gateway
        self.token = token
        self.log_requests = True


class QmtBridgeRequestHandler(BaseHTTPRequestHandler):
    server: QmtBridgeHTTPServer

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if not self._verify_token(path):
            return

        try:
            if path == "/health":
                self._write_json(200, self.server.gateway.health())
            elif path == "/asset":
                self._write_json(200, self.server.gateway.asset())
            elif path == "/positions":
                self._write_json(200, self.server.gateway.positions())
            elif path == "/orders":
                self._write_json(200, self.server.gateway.orders(open_only=_bool_query(query, "open_only")))
            elif path == "/trades":
                self._write_json(200, self.server.gateway.trades(limit=_int_query(query, "limit", 100)))
            else:
                self._write_json(404, {"ok": False, "error": f"unknown path: {path}"})
        except Exception as exc:
            self._write_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._verify_token(path):
            return

        try:
            payload = self._read_json()
            if path == "/orders":
                self._write_json(200, self.server.gateway.place_order(payload))
            elif path == "/cancel":
                self._write_json(200, self.server.gateway.cancel_order(payload))
            else:
                self._write_json(404, {"ok": False, "error": f"unknown path: {path}"})
        except ValueError as exc:
            self._write_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._write_json(500, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        if not self.server.log_requests:
            return
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def _verify_token(self, path: str) -> bool:
        if path == "/health" or not self.server.token:
            return True
        if self.headers.get("Authorization", "") == f"Bearer {self.server.token}":
            return True
        self._write_json(401, {"ok": False, "error": "invalid QMT bridge token"})
        return False

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _write_json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str, port: int, gateway: Any, token: str = "") -> QmtBridgeHTTPServer:
    if not _is_loopback_host(host) and not token:
        raise RuntimeError("QMT bridge requires --token or SATS_QMT_TOKEN when binding a non-localhost address")
    return QmtBridgeHTTPServer((host, int(port)), gateway, token)


def run_bridge(*, host: str, port: int, config: BridgeConfig, token: str = "") -> None:
    gateway = XtQuantGateway(config)
    server = create_server(host, port, gateway, token)
    print(f"SATS QMT bridge listening on http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping SATS QMT bridge.")
    finally:
        server.server_close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone Windows bridge for SATS QMT/MiniQMT access")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--qmt-path", default=os.getenv("SATS_QMT_USERDATA_PATH", ""))
    parser.add_argument("--account-id", default=os.getenv("SATS_QMT_ACCOUNT_ID", ""))
    parser.add_argument("--account-type", default=os.getenv("SATS_QMT_ACCOUNT_TYPE", "STOCK"))
    parser.add_argument("--session-id", type=int, default=int(os.getenv("SATS_QMT_SESSION_ID", "0") or 0))
    parser.add_argument("--token", default=os.getenv("SATS_QMT_TOKEN", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.qmt_path:
        raise SystemExit("--qmt-path or SATS_QMT_USERDATA_PATH is required")
    if not args.account_id:
        raise SystemExit("--account-id or SATS_QMT_ACCOUNT_ID is required")
    config = BridgeConfig(
        qmt_path=args.qmt_path,
        account_id=args.account_id,
        account_type=args.account_type,
        session_id=args.session_id,
    )
    run_bridge(host=args.host, port=args.port, config=config, token=args.token)
    return 0


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


def _bool_query(query: dict[str, list[str]], name: str) -> bool:
    value = (query.get(name) or ["0"])[0]
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}


if __name__ == "__main__":
    raise SystemExit(main())
