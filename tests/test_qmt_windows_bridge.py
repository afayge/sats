from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sats.trading.miniqmt_client import MiniQmtBrokerClient
from sats.trading.models import OrderRequest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "qmt_windows_bridge.py"
_SPEC = importlib.util.spec_from_file_location("qmt_windows_bridge", _SCRIPT_PATH)
qmt_windows_bridge = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = qmt_windows_bridge
_SPEC.loader.exec_module(qmt_windows_bridge)


class QmtWindowsBridgeTest(unittest.TestCase):
    def test_load_launch_config_reads_scripts_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "qmt_windows_bridge.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "host": "0.0.0.0",
                        "port": 9001,
                        "qmt_path": "C:/qmt/userdata_mini",
                        "account_id": "acct-from-file",
                        "account_type": "stock",
                        "session_id": 7,
                        "token": "file-token",
                    }
                ),
                encoding="utf-8",
            )

            launch = qmt_windows_bridge.load_launch_config(qmt_windows_bridge.parse_args([]), config_path=config_path)

        self.assertEqual(launch.host, "0.0.0.0")
        self.assertEqual(launch.port, 9001)
        self.assertEqual(launch.token, "file-token")
        self.assertEqual(launch.bridge.qmt_path, "C:/qmt/userdata_mini")
        self.assertEqual(launch.bridge.account_id, "acct-from-file")
        self.assertEqual(launch.bridge.account_type, "STOCK")
        self.assertEqual(launch.bridge.session_id, 7)

    def test_cli_values_override_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "qmt_windows_bridge.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "host": "0.0.0.0",
                        "port": 9001,
                        "qmt_path": "C:/qmt/from-file",
                        "account_id": "acct-from-file",
                        "account_type": "STOCK",
                        "session_id": 7,
                        "token": "file-token",
                    }
                ),
                encoding="utf-8",
            )

            launch = qmt_windows_bridge.load_launch_config(
                qmt_windows_bridge.parse_args(
                    [
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                        "--qmt-path",
                        "C:/qmt/from-cli",
                        "--account-id",
                        "acct-from-cli",
                        "--account-type",
                        "CREDIT",
                        "--session-id",
                        "8",
                        "--token",
                        "cli-token",
                    ]
                ),
                config_path=config_path,
            )

        self.assertEqual(launch.host, "127.0.0.1")
        self.assertEqual(launch.port, 8765)
        self.assertEqual(launch.token, "cli-token")
        self.assertEqual(launch.bridge.qmt_path, "C:/qmt/from-cli")
        self.assertEqual(launch.bridge.account_id, "acct-from-cli")
        self.assertEqual(launch.bridge.account_type, "CREDIT")
        self.assertEqual(launch.bridge.session_id, 8)

    def test_health_does_not_require_token(self) -> None:
        with _RunningBridge(token="secret") as bridge:
            payload = _request("GET", f"{bridge.base_url}/health")

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["account_id"], "acct")

    def test_query_endpoints_require_token_and_return_json(self) -> None:
        with _RunningBridge(token="secret") as bridge:
            with self.assertRaises(urllib.error.HTTPError) as error:
                _request("GET", f"{bridge.base_url}/asset")
            self.assertEqual(error.exception.code, 401)
            error.exception.close()

            asset = _request("GET", f"{bridge.base_url}/asset", token="secret")
            positions = _request("GET", f"{bridge.base_url}/positions", token="secret")
            orders = _request("GET", f"{bridge.base_url}/orders?open_only=1", token="secret")
            trades = _request("GET", f"{bridge.base_url}/trades?limit=1", token="secret")

        self.assertEqual(asset["asset"]["available_cash"], 800.0)
        self.assertEqual(positions["positions"][0]["stock_code"], "000001.SZ")
        self.assertEqual(orders["orders"][0]["order_id"], "qmt-1")
        self.assertEqual(trades["trades"][0]["trade_id"], "trade-1")
        self.assertTrue(bridge.gateway.open_only_values[-1])
        self.assertEqual(bridge.gateway.limit_values[-1], 1)

    def test_buy_sell_and_cancel_payloads(self) -> None:
        with _RunningBridge(token="secret") as bridge:
            buy = _request(
                "POST",
                f"{bridge.base_url}/orders",
                {"symbol": "000001.SZ", "side": "buy", "quantity": 100, "price_type": "latest", "sats_order_id": "sats-buy"},
                token="secret",
            )
            sell = _request(
                "POST",
                f"{bridge.base_url}/orders",
                {"symbol": "000001.SZ", "side": "sell", "quantity": 100, "price_type": "limit", "price": 12.3, "sats_order_id": "sats-sell"},
                token="secret",
            )
            cancel = _request("POST", f"{bridge.base_url}/cancel", {"order_id": "qmt-1", "sats_order_id": "sats-cancel"}, token="secret")

        self.assertEqual(buy["status"], "submitted")
        self.assertEqual(sell["order_id"], "qmt-2")
        self.assertEqual(cancel["status"], "cancel_requested")
        self.assertEqual([item["side"] for item in bridge.gateway.order_payloads], ["buy", "sell"])
        self.assertEqual(bridge.gateway.cancel_payloads[0]["order_id"], "qmt-1")

    def test_invalid_order_side_returns_400(self) -> None:
        with _RunningBridge(token="secret") as bridge:
            with self.assertRaises(urllib.error.HTTPError) as error:
                _request("POST", f"{bridge.base_url}/orders", {"symbol": "000001.SZ", "side": "hold", "quantity": 100}, token="secret")

        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(bridge.gateway.order_payloads, [])

    def test_xtquant_gateway_rejects_invalid_order_side_before_connect(self) -> None:
        gateway = qmt_windows_bridge.XtQuantGateway(qmt_windows_bridge.BridgeConfig(qmt_path="C:/qmt/userdata_mini", account_id="acct"))

        with self.assertRaisesRegex(ValueError, "buy or sell"):
            gateway.place_order({"symbol": "000001.SZ", "side": "hold", "quantity": 100})

        self.assertFalse(gateway.connected)

    def test_non_loopback_binding_requires_token(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "token"):
            qmt_windows_bridge.create_server("0.0.0.0", 0, _FakeGateway(), token="")

    def test_existing_miniqmt_client_can_use_windows_bridge(self) -> None:
        with _RunningBridge(token="secret") as bridge:
            client = MiniQmtBrokerClient(base_url=bridge.base_url, token="secret", account_id="acct", account_type="STOCK", timeout=5)

            status = client.status()
            asset = client.asset()
            positions = client.positions()
            orders = client.orders(open_only=True)
            trades = client.trades(limit=1)
            order_result = client.place_order(OrderRequest(symbol="000001.SZ", side="buy", quantity=100))
            cancel_result = client.cancel_order("qmt-1")

        self.assertTrue(status["connected"])
        self.assertEqual(asset.available_cash, 800.0)
        self.assertEqual(positions[0].ts_code, "000001.SZ")
        self.assertEqual(positions[0].available_quantity, 100.0)
        self.assertEqual(orders[0].order_id, "qmt-1")
        self.assertEqual(trades[0].trade_id, "trade-1")
        self.assertEqual(order_result.broker_order_id, "qmt-1")
        self.assertEqual(cancel_result.status, "cancel_requested")
        self.assertEqual(bridge.gateway.order_payloads[0]["symbol"], "000001.SZ")
        self.assertTrue(bridge.gateway.open_only_values[-1])


class _RunningBridge:
    def __init__(self, *, token: str = "") -> None:
        self.token = token
        self.gateway = _FakeGateway()
        self.server = None
        self.thread = None
        self.base_url = ""

    def __enter__(self) -> "_RunningBridge":
        self.server = qmt_windows_bridge.create_server("127.0.0.1", 0, self.gateway, self.token)
        self.server.log_requests = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        assert self.server is not None
        assert self.thread is not None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _FakeGateway:
    def __init__(self) -> None:
        self.open_only_values: list[bool] = []
        self.limit_values: list[int] = []
        self.order_payloads: list[dict[str, Any]] = []
        self.cancel_payloads: list[dict[str, Any]] = []

    def health(self) -> dict[str, Any]:
        return {"ok": True, "connected": True, "account_id": "acct", "account_type": "STOCK"}

    def asset(self) -> dict[str, Any]:
        return {
            "asset": {
                "account_id": "acct",
                "account_type": "STOCK",
                "cash": 1000.0,
                "available_cash": 800.0,
                "market_value": 200.0,
                "total_asset": 1200.0,
            }
        }

    def positions(self) -> dict[str, Any]:
        return {
            "positions": [
                {
                    "stock_code": "000001.SZ",
                    "stock_name": "Ping An Bank",
                    "volume": 200,
                    "can_use_volume": 100,
                    "open_price": 10.0,
                    "last_price": 11.0,
                    "market_value": 2200.0,
                }
            ]
        }

    def orders(self, *, open_only: bool = False) -> dict[str, Any]:
        self.open_only_values.append(open_only)
        return {
            "orders": [
                {
                    "order_id": "qmt-1",
                    "stock_code": "000001.SZ",
                    "side": "buy",
                    "volume": 100,
                    "order_price": 11.0,
                    "order_status": "submitted",
                }
            ]
        }

    def trades(self, *, limit: int = 100) -> dict[str, Any]:
        self.limit_values.append(limit)
        rows = [
            {
                "trade_id": "trade-1",
                "order_id": "qmt-1",
                "stock_code": "000001.SZ",
                "side": "buy",
                "traded_volume": 100,
                "traded_price": 11.0,
                "traded_time": "2026-06-10 10:00:00",
            },
            {
                "trade_id": "trade-2",
                "order_id": "qmt-2",
                "stock_code": "000002.SZ",
                "side": "sell",
                "traded_volume": 100,
                "traded_price": 12.0,
                "traded_time": "2026-06-10 10:01:00",
            },
        ]
        return {"trades": rows[:limit]}

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("side") not in {"buy", "sell"}:
            raise ValueError("order side must be buy or sell")
        self.order_payloads.append(payload)
        return {
            "ok": True,
            "status": "submitted",
            "order_id": f"qmt-{len(self.order_payloads)}",
            "sats_order_id": payload.get("sats_order_id", ""),
        }

    def cancel_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.cancel_payloads.append(payload)
        return {
            "ok": True,
            "status": "cancel_requested",
            "order_id": payload.get("order_id", ""),
            "sats_order_id": payload.get("sats_order_id", ""),
        }


def _request(method: str, url: str, payload: dict[str, Any] | None = None, *, token: str = "") -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
