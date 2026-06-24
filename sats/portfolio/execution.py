from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sats.portfolio.storage import PortfolioStore


class PaperBroker:
    def __init__(self, store: PortfolioStore, *, account_id: str, initial_cash: float) -> None:
        self.store = store
        self.storage = store.storage
        self.account_id = account_id
        self.store.ensure_paper_account(account_id, initial_cash)

    def refresh(self, quotes: dict[str, dict[str, Any]], *, trade_date: str) -> dict[str, Any]:
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                """
                SELECT ts_code, quantity, available_quantity, cost_price, price,
                       peak_price, trough_price, last_buy_trade_date
                FROM paper_positions
                WHERE account_id = ? AND quantity > 0
                """,
                [self.account_id],
            ).fetchall()
            for row in rows:
                ts_code = str(row[0])
                quantity = float(row[1])
                available = quantity if str(row[7] or "") < trade_date else float(row[2])
                price = _quote_price(quotes.get(ts_code) or {}) or float(row[4])
                cost = float(row[3])
                peak = max(float(row[5]), price)
                trough = min(_number(row[6]) or price, price)
                market_value = quantity * price
                pnl = (price - cost) * quantity
                pnl_pct = ((price / cost - 1.0) * 100.0) if cost > 0 else 0.0
                con.execute(
                    """
                    UPDATE paper_positions
                    SET available_quantity = ?, price = ?, market_value = ?, pnl = ?,
                        pnl_pct = ?, peak_price = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE account_id = ? AND ts_code = ?
                    """,
                    [available, price, market_value, pnl, pnl_pct, peak, self.account_id, ts_code],
                )
                con.execute(
                    """
                    UPDATE paper_positions
                    SET trough_price = ?
                    WHERE account_id = ? AND ts_code = ?
                    """,
                    [trough, self.account_id, ts_code],
                )
            market_value = float(
                con.execute(
                    """
                    SELECT COALESCE(SUM(market_value), 0)
                    FROM paper_positions
                    WHERE account_id = ? AND quantity > 0
                    """,
                    [self.account_id],
                ).fetchone()[0]
            )
            account = con.execute(
                "SELECT cash, equity_peak, max_drawdown_pct FROM paper_accounts WHERE account_id = ?",
                [self.account_id],
            ).fetchone()
            cash = float(account[0]) if account else 0.0
            total_asset = cash + market_value
            equity_peak = max(float(account[1] or 0.0), total_asset) if account else total_asset
            drawdown = ((total_asset / equity_peak - 1.0) * 100.0) if equity_peak > 0 else 0.0
            max_drawdown = min(float(account[2] or 0.0), drawdown) if account else drawdown
            con.execute(
                """
                UPDATE paper_accounts
                SET available_cash = ?, market_value = ?, total_asset = ?,
                    equity_peak = ?, max_drawdown_pct = ?, updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ?
                """,
                [cash, market_value, total_asset, equity_peak, max_drawdown, self.account_id],
            )
        return self.store.paper_account(self.account_id)

    def buy_quantity(
        self,
        *,
        ts_code: str,
        price: float,
        exposure_limit: float,
        max_position_pct: float,
    ) -> int:
        if price <= 0 or exposure_limit <= 0:
            return 0
        account = self.store.paper_account(self.account_id)
        positions = self.store.paper_positions(self.account_id)
        current = next((row for row in positions if row["ts_code"] == ts_code), None)
        current_value = float(current.get("market_value") or 0.0) if current else 0.0
        total_asset = float(account.get("total_asset") or 0.0)
        portfolio_room = max(0.0, total_asset * exposure_limit - float(account.get("market_value") or 0.0))
        position_room = max(0.0, total_asset * max_position_pct - current_value)
        budget = min(float(account.get("available_cash") or 0.0), portfolio_room, position_room)
        return max(0, int(budget // (price * 100)) * 100)

    def place_order(
        self,
        *,
        plan_id: str,
        source_run_id: str,
        ts_code: str,
        name: str,
        side: str,
        quantity: int,
        price: float,
        trade_date: str,
        trade_time: str,
        reason: str,
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        order_id = f"paper_order_{uuid.uuid4().hex[:16]}"
        trade_id = f"paper_trade_{uuid.uuid4().hex[:16]}"
        side = str(side or "").lower()
        rejection = self._validate_order(side=side, quantity=quantity, price=price, quote=quote)
        if rejection:
            return self._insert_rejected(
                order_id=order_id,
                plan_id=plan_id,
                source_run_id=source_run_id,
                ts_code=ts_code,
                name=name,
                side=side,
                quantity=quantity,
                price=price,
                trade_date=trade_date,
                trade_time=trade_time,
                reason=rejection,
                quote=quote,
            )

        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute("BEGIN TRANSACTION")
            try:
                account = con.execute(
                    """
                    SELECT cash, available_cash, realized_pnl, equity_peak, max_drawdown_pct
                    FROM paper_accounts
                    WHERE account_id = ?
                    """,
                    [self.account_id],
                ).fetchone()
                if account is None:
                    raise ValueError("paper account is missing")
                cash = float(account[0])
                realized_total = float(account[2])
                position = con.execute(
                    """
                    SELECT quantity, available_quantity, cost_price, peak_price,
                           trough_price, opened_trade_date
                    FROM paper_positions
                    WHERE account_id = ? AND ts_code = ?
                    """,
                    [self.account_id, ts_code],
                ).fetchone()
                realized_pnl = 0.0
                if side == "buy":
                    amount = float(quantity) * price
                    if amount > float(account[1]) + 1e-9:
                        raise ValueError("模拟账户可用资金不足")
                    old_quantity = float(position[0]) if position else 0.0
                    old_cost = float(position[2]) if position else 0.0
                    new_quantity = old_quantity + quantity
                    new_cost = ((old_quantity * old_cost) + amount) / new_quantity
                    old_available = float(position[1]) if position else 0.0
                    opened = str(position[5]) if position else trade_date
                    peak = max(float(position[3]), price) if position else price
                    trough = min(_number(position[4]) or price, price) if position else price
                    con.execute(
                        """
                        INSERT OR REPLACE INTO paper_positions
                            (account_id, ts_code, name, quantity, available_quantity,
                             cost_price, price, market_value, pnl, pnl_pct, peak_price,
                             trough_price, opened_trade_date, last_buy_trade_date, plan_id, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        [
                            self.account_id,
                            ts_code,
                            name,
                            new_quantity,
                            old_available,
                            new_cost,
                            price,
                            new_quantity * price,
                            (price - new_cost) * new_quantity,
                            ((price / new_cost - 1.0) * 100.0) if new_cost else 0.0,
                            peak,
                            trough,
                            opened,
                            trade_date,
                            plan_id,
                        ],
                    )
                    cash -= amount
                else:
                    if position is None:
                        raise ValueError("模拟账户无该持仓")
                    available = float(position[1])
                    if quantity > available + 1e-9:
                        raise ValueError("模拟账户可卖数量不足，可能受 T+1 限制")
                    old_quantity = float(position[0])
                    cost = float(position[2])
                    new_quantity = old_quantity - quantity
                    realized_pnl = (price - cost) * quantity
                    cash += price * quantity
                    if new_quantity <= 0:
                        con.execute(
                            "DELETE FROM paper_positions WHERE account_id = ? AND ts_code = ?",
                            [self.account_id, ts_code],
                        )
                    else:
                        con.execute(
                            """
                            UPDATE paper_positions
                            SET quantity = ?, available_quantity = ?, price = ?,
                                market_value = ?, pnl = ?, pnl_pct = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE account_id = ? AND ts_code = ?
                            """,
                            [
                                new_quantity,
                                max(0.0, available - quantity),
                                price,
                                new_quantity * price,
                                (price - cost) * new_quantity,
                                ((price / cost - 1.0) * 100.0) if cost else 0.0,
                                self.account_id,
                                ts_code,
                            ],
                        )
                    realized_total += realized_pnl

                con.execute(
                    """
                    INSERT INTO paper_orders
                        (order_id, account_id, plan_id, source_run_id, ts_code, name,
                         side, quantity, price, status, reason, trade_date, trade_time,
                         request_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?)
                    """,
                    [
                        order_id,
                        self.account_id,
                        plan_id,
                        source_run_id,
                        ts_code,
                        name,
                        side,
                        quantity,
                        price,
                        reason,
                        trade_date,
                        trade_time,
                        json.dumps({"quote": quote}, ensure_ascii=False, default=str),
                    ],
                )
                con.execute(
                    """
                    INSERT INTO paper_trades
                        (trade_id, order_id, account_id, plan_id, ts_code, name, side,
                         quantity, price, realized_pnl, trade_date, trade_time, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        trade_id,
                        order_id,
                        self.account_id,
                        plan_id,
                        ts_code,
                        name,
                        side,
                        quantity,
                        price,
                        realized_pnl,
                        trade_date,
                        trade_time,
                        reason,
                    ],
                )
                market_value = float(
                    con.execute(
                        """
                        SELECT COALESCE(SUM(market_value), 0)
                        FROM paper_positions
                        WHERE account_id = ?
                        """,
                        [self.account_id],
                    ).fetchone()[0]
                )
                total_asset = cash + market_value
                equity_peak = max(float(account[3] or 0.0), total_asset)
                drawdown = ((total_asset / equity_peak - 1.0) * 100.0) if equity_peak > 0 else 0.0
                max_drawdown = min(float(account[4] or 0.0), drawdown)
                con.execute(
                    """
                    UPDATE paper_accounts
                    SET cash = ?, available_cash = ?, market_value = ?,
                        total_asset = ?, realized_pnl = ?, equity_peak = ?,
                        max_drawdown_pct = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE account_id = ?
                    """,
                    [
                        cash,
                        cash,
                        market_value,
                        total_asset,
                        realized_total,
                        equity_peak,
                        max_drawdown,
                        self.account_id,
                    ],
                )
                con.execute("COMMIT")
            except Exception as exc:
                con.execute("ROLLBACK")
                return self._insert_rejected(
                    order_id=order_id,
                    plan_id=plan_id,
                    source_run_id=source_run_id,
                    ts_code=ts_code,
                    name=name,
                    side=side,
                    quantity=quantity,
                    price=price,
                    trade_date=trade_date,
                    trade_time=trade_time,
                    reason=str(exc),
                    quote=quote,
                )
        return {
            "order_id": order_id,
            "trade_id": trade_id,
            "status": "filled",
            "ts_code": ts_code,
            "name": name,
            "side": side,
            "quantity": quantity,
            "price": price,
            "realized_pnl": realized_pnl,
            "reason": reason,
        }

    def _validate_order(self, *, side: str, quantity: int, price: float, quote: dict[str, Any]) -> str:
        if side not in {"buy", "sell"}:
            return f"不支持的模拟交易方向: {side}"
        if quantity <= 0:
            return "模拟委托数量必须大于 0"
        if side == "buy" and quantity % 100 != 0:
            return "A股模拟买入数量必须是 100 股整数倍"
        if price <= 0:
            return "模拟委托缺少有效实时价格"
        up_limit = _first_number(quote, ("up_limit", "upper_limit", "limit_up"))
        down_limit = _first_number(quote, ("down_limit", "lower_limit", "limit_down"))
        if side == "buy" and up_limit is not None and price >= up_limit:
            return "股票处于涨停价，模拟买入拒绝"
        if side == "sell" and down_limit is not None and price <= down_limit:
            return "股票处于跌停价，模拟卖出拒绝"
        return ""

    def _insert_rejected(
        self,
        *,
        order_id: str,
        plan_id: str,
        source_run_id: str,
        ts_code: str,
        name: str,
        side: str,
        quantity: int,
        price: float,
        trade_date: str,
        trade_time: str,
        reason: str,
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO paper_orders
                    (order_id, account_id, plan_id, source_run_id, ts_code, name,
                     side, quantity, price, status, reason, trade_date, trade_time,
                     request_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?, ?, ?)
                """,
                [
                    order_id,
                    self.account_id,
                    plan_id,
                    source_run_id,
                    ts_code,
                    name,
                    side,
                    max(0, int(quantity)),
                    max(0.0, float(price)),
                    reason,
                    trade_date,
                    trade_time,
                    json.dumps({"quote": quote}, ensure_ascii=False, default=str),
                ],
            )
        return {
            "order_id": order_id,
            "status": "rejected",
            "ts_code": ts_code,
            "name": name,
            "side": side,
            "quantity": quantity,
            "price": price,
            "reason": reason,
        }


def _quote_price(row: dict[str, Any]) -> float:
    for key in ("price", "last_price", "latest_price", "close"):
        value = _number(row.get(key))
        if value is not None and value > 0:
            return value
    return 0.0


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_number(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _number(payload.get(key))
        if value is not None:
            return value
    return None
