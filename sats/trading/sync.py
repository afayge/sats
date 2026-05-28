from __future__ import annotations

from datetime import datetime

from sats.storage.duckdb import DuckDBStorage
from sats.trading.models import BrokerPosition


def sync_positions_to_monitor(
    storage: DuckDBStorage,
    positions: list[BrokerPosition],
    *,
    provider: str = "qmt",
    account_id: str = "",
    prune_missing: bool = False,
) -> int:
    existing = {row["ts_code"]: row for row in storage.list_monitor_positions(enabled=None)}
    synced_symbols = {position.ts_code for position in positions if position.ts_code}
    today = datetime.now().strftime("%Y%m%d")
    for position in positions:
        if not position.ts_code:
            continue
        storage.upsert_monitor_position(
            ts_code=position.ts_code,
            name=position.name,
            quantity=position.quantity,
            buy_price=position.cost_price,
            buy_date=today,
            enabled=True,
            note=f"{provider}_sync:{account_id}",
        )
    if prune_missing:
        for ts_code, row in existing.items():
            note = str(row.get("note") or "")
            if note.startswith(f"{provider}_sync:") and ts_code not in synced_symbols:
                storage.upsert_monitor_position(
                    ts_code=ts_code,
                    name=str(row.get("name") or ""),
                    quantity=float(row.get("quantity") or 0.0),
                    buy_price=float(row.get("buy_price") or 0.0),
                    buy_date=str(row.get("buy_date") or ""),
                    enabled=False,
                    note=note,
                )
    return len(synced_symbols)
