from __future__ import annotations

import math
import re
from typing import Any

from sats.config import Settings
from sats.storage.duckdb import DuckDBStorage
from sats.trading.broker import BrokerClient, BrokerError
from sats.trading.miniqmt_client import broker_from_settings
from sats.trading.models import BrokerPosition

QMT_POSITION_SYNC_SERVICE = "qmt-position-sync"
_TS_CODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


class QmtPositionSyncError(BrokerError):
    pass


class QmtPositionSyncService:
    def __init__(self, *, storage: DuckDBStorage, client: BrokerClient) -> None:
        self.storage = storage
        self.client = client

    @classmethod
    def from_settings(cls, *, storage: DuckDBStorage, settings: Settings) -> "QmtPositionSyncService":
        try:
            client = broker_from_settings(settings)
        except Exception as exc:
            message = _sync_error_message(exc)
            _record_sync_error(
                storage,
                provider="qmt",
                account_id=str(getattr(settings, "qmt_account_id", "") or ""),
                message=message,
            )
            raise QmtPositionSyncError(message) from exc
        return cls(storage=storage, client=client)

    def sync(self) -> list[BrokerPosition]:
        try:
            positions = self.client.positions()
            rows = _validated_position_rows(positions, provider=self.client.provider)
            self.storage.replace_qmt_position_snapshot(
                rows,
                provider=self.client.provider,
                account_id=self.client.account_id,
            )
            return positions
        except Exception as exc:
            message = _sync_error_message(exc)
            _record_sync_error(
                self.storage,
                provider=str(getattr(self.client, "provider", "qmt") or "qmt"),
                account_id=str(getattr(self.client, "account_id", "") or ""),
                message=message,
            )
            if isinstance(exc, QmtPositionSyncError):
                raise
            raise QmtPositionSyncError(message) from exc


def _validated_position_rows(positions: Any, *, provider: str) -> list[dict[str, Any]]:
    if not isinstance(positions, list):
        raise QmtPositionSyncError("QMT 持仓响应必须是列表")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, position in enumerate(positions):
        if not isinstance(position, BrokerPosition):
            raise QmtPositionSyncError(f"QMT 持仓第 {index + 1} 项类型无效")
        ts_code = str(position.ts_code or "").strip().upper()
        if not _TS_CODE_PATTERN.fullmatch(ts_code):
            raise QmtPositionSyncError(f"QMT 持仓第 {index + 1} 项股票代码无效: {ts_code or '-'}")
        if ts_code in seen:
            raise QmtPositionSyncError(f"QMT 持仓包含重复股票代码: {ts_code}")
        seen.add(ts_code)
        quantity = _nonnegative_number(position.quantity, f"{ts_code} quantity")
        available_quantity = _nonnegative_number(position.available_quantity, f"{ts_code} available_quantity")
        cost_price = _nonnegative_number(position.cost_price, f"{ts_code} cost_price")
        price = _nonnegative_number(position.price, f"{ts_code} price")
        market_value = _nonnegative_number(position.market_value, f"{ts_code} market_value")
        pnl = _finite_number(position.pnl, f"{ts_code} pnl")
        pnl_pct = _finite_number(position.pnl_pct, f"{ts_code} pnl_pct")
        if available_quantity > quantity:
            raise QmtPositionSyncError(f"QMT 持仓 {ts_code} 可用数量大于总数量")
        rows.append(
            {
                "ts_code": ts_code,
                "name": str(position.name or ""),
                "quantity": quantity,
                "available_quantity": available_quantity,
                "cost_price": cost_price,
                "price": price,
                "market_value": market_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "buy_date": _position_buy_date(position.raw),
                "source": str(provider or "qmt"),
                "raw": position.raw if isinstance(position.raw, dict) else {},
            }
        )
    return rows


def _position_buy_date(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in ("buy_date", "open_date", "position_date"):
        value = str(raw.get(key) or "").strip()
        digits = "".join(char for char in value if char.isdigit())
        if len(digits) >= 8:
            return digits[:8]
    return ""


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QmtPositionSyncError(f"QMT 持仓字段 {label} 不是有效数字") from exc
    if not math.isfinite(number):
        raise QmtPositionSyncError(f"QMT 持仓字段 {label} 不是有限数字")
    return number


def _nonnegative_number(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0:
        raise QmtPositionSyncError(f"QMT 持仓字段 {label} 不能为负数")
    return number


def _sync_error_message(exc: Exception) -> str:
    detail = str(exc or "").strip() or exc.__class__.__name__
    if detail.startswith("QMT 持仓同步失败:"):
        return detail
    return f"QMT 持仓同步失败: {detail}"


def _record_sync_error(storage: DuckDBStorage, *, provider: str, account_id: str, message: str) -> None:
    try:
        state = storage.get_monitor_runtime(QMT_POSITION_SYNC_SERVICE)
        params = dict(state.get("params") or {})
        params.update({"provider": provider, "account_id": account_id})
        storage.upsert_monitor_runtime(
            service_name=QMT_POSITION_SYNC_SERVICE,
            status="stale",
            params=params,
            last_error=message,
        )
    except Exception:
        pass
