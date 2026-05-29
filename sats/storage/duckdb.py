from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

import pandas as pd

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - dependency guard
    duckdb = None  # type: ignore[assignment]
    _DUCKDB_IMPORT_ERROR = exc
else:
    _DUCKDB_IMPORT_ERROR = None

from sats.screening.base import ScreeningResult

_DATE_CACHE_TABLES = {"stock_daily", "stock_daily_basic"}


class DuckDBStorage:
    def __init__(self, db_path: Path | str) -> None:
        if duckdb is None:
            raise RuntimeError("duckdb is not installed; install requirements.txt") from _DUCKDB_IMPORT_ERROR
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        return duckdb.connect(str(self.db_path))

    def initialize(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        with self.connect() as con:
            con.execute(schema_path.read_text(encoding="utf-8"))

    def upsert_stock_daily(self, frame: pd.DataFrame) -> int:
        columns = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]
        data = _prepare_frame(frame, columns, required=["ts_code", "trade_date"])
        return self._upsert_frame("stock_daily", columns, data)

    def upsert_stock_daily_basic(self, frame: pd.DataFrame) -> int:
        columns = [
            "ts_code",
            "trade_date",
            "turnover_rate",
            "turnover_rate_f",
            "circ_mv",
            "float_share",
            "free_share",
            "float_mv",
            "total_mv",
            "pe",
            "pb",
            "ps",
        ]
        data = _prepare_frame(frame, columns, required=["ts_code", "trade_date"])
        return self._upsert_frame("stock_daily_basic", columns, data)

    def upsert_stock_moneyflow(self, frame: pd.DataFrame) -> int:
        columns = ["ts_code", "trade_date", "main_net_amount", "data_source"]
        data = _prepare_frame(frame, columns, required=["ts_code", "trade_date"])
        return self._upsert_frame("stock_moneyflow", columns, data)

    def upsert_stock_fundamentals(self, frame: pd.DataFrame) -> int:
        columns = [
            "ts_code",
            "end_date",
            "ann_date",
            "total_revenue",
            "revenue",
            "net_profit",
            "profit",
            "roe",
            "debt_to_assets",
            "total_assets",
            "total_liab",
            "data_source",
        ]
        data = _prepare_frame(frame, columns, required=["ts_code", "end_date"])
        return self._upsert_frame("stock_fundamentals", columns, data)

    def upsert_stock_basic(self, frame: pd.DataFrame) -> int:
        columns = ["ts_code", "symbol", "name", "industry", "market", "exchange", "list_date"]
        data = _prepare_frame(frame, columns, required=["ts_code"])
        if data.empty:
            return 0
        self.initialize()
        with self.connect() as con:
            con.register("stock_basic_rows", data)
            con.execute(
                """
                INSERT OR REPLACE INTO stock_basic
                    (ts_code, symbol, name, industry, market, exchange, list_date, updated_at)
                SELECT ts_code, symbol, name, industry, market, exchange, list_date, CURRENT_TIMESTAMP
                FROM stock_basic_rows
                """
            )
            con.unregister("stock_basic_rows")
        return len(data)

    def get_stock_basic(self) -> pd.DataFrame:
        columns = ["ts_code", "symbol", "name", "industry", "market", "exchange", "list_date"]
        self.initialize()
        with self.connect() as con:
            return con.execute(
                f"""
                SELECT {", ".join(columns)}
                FROM stock_basic
                ORDER BY ts_code ASC
                """
            ).fetchdf()

    def upsert_industry_daily(self, index_code: str, frame: pd.DataFrame) -> int:
        data = frame.copy()
        if data.empty:
            return 0
        if "index_code" not in data.columns:
            data["index_code"] = data["ts_code"] if "ts_code" in data.columns else index_code
        data["index_code"] = data["index_code"].fillna(index_code).astype(str)
        columns = ["index_code", "trade_date", "close"]
        data = _prepare_frame(data, columns, required=["index_code", "trade_date"])
        return self._upsert_frame("industry_daily", columns, data)

    def upsert_sector_basic(self, frame: pd.DataFrame) -> int:
        columns = ["sector_code", "name", "sector_type", "exchange", "list_date", "data_source"]
        data = _prepare_frame(frame, columns, required=["sector_code"])
        if data.empty:
            return 0
        self.initialize()
        with self.connect() as con:
            con.register("sector_basic_rows", data)
            con.execute(
                """
                INSERT OR REPLACE INTO sector_basic
                    (sector_code, name, sector_type, exchange, list_date, data_source, updated_at)
                SELECT sector_code, name, sector_type, exchange, list_date, data_source, CURRENT_TIMESTAMP
                FROM sector_basic_rows
                """
            )
            con.unregister("sector_basic_rows")
        return len(data)

    def upsert_sector_daily(self, frame: pd.DataFrame) -> int:
        columns = ["sector_code", "trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount", "data_source"]
        data = _prepare_frame(frame, columns, required=["sector_code", "trade_date"])
        return self._upsert_frame("sector_daily", columns, data)

    def upsert_sector_members(self, frame: pd.DataFrame) -> int:
        columns = ["sector_code", "ts_code", "name", "weight", "in_date", "out_date", "is_new", "data_source"]
        data = _prepare_frame(frame, columns, required=["sector_code", "ts_code"])
        if data.empty:
            return 0
        self.initialize()
        with self.connect() as con:
            con.register("sector_member_rows", data)
            con.execute(
                """
                INSERT OR REPLACE INTO sector_members
                    (sector_code, ts_code, name, weight, in_date, out_date, is_new, data_source, updated_at)
                SELECT sector_code, ts_code, name, weight, in_date, out_date, is_new, data_source, CURRENT_TIMESTAMP
                FROM sector_member_rows
                """
            )
            con.unregister("sector_member_rows")
        return len(data)

    def cached_trade_dates(self, table: str, trade_dates: list[str]) -> set[str]:
        if table not in _DATE_CACHE_TABLES:
            raise ValueError(f"Unsupported date cache table: {table}")
        dates = [str(date) for date in trade_dates]
        if not dates:
            return set()
        self.initialize()
        placeholders = ", ".join("?" for _ in dates)
        with self.connect() as con:
            rows = con.execute(
                f"SELECT DISTINCT trade_date FROM {table} WHERE trade_date IN ({placeholders})",
                dates,
            ).fetchall()
        return {str(row[0]) for row in rows}

    def cached_index_dates(self, index_code: str, trade_dates: list[str]) -> set[str]:
        dates = [str(date) for date in trade_dates]
        if not dates:
            return set()
        self.initialize()
        placeholders = ", ".join("?" for _ in dates)
        with self.connect() as con:
            rows = con.execute(
                f"""
                SELECT DISTINCT trade_date
                FROM industry_daily
                WHERE index_code = ? AND trade_date IN ({placeholders})
                """,
                [index_code, *dates],
            ).fetchall()
        return {str(row[0]) for row in rows}

    def get_stock_daily(self, trade_dates: list[str]) -> pd.DataFrame:
        return self._get_by_trade_dates(
            "stock_daily",
            ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"],
            trade_dates,
        )

    def get_stock_daily_basic(self, trade_dates: list[str]) -> pd.DataFrame:
        return self._get_by_trade_dates(
            "stock_daily_basic",
            [
                "ts_code",
                "trade_date",
                "turnover_rate",
                "turnover_rate_f",
                "circ_mv",
                "float_share",
                "free_share",
                "float_mv",
                "total_mv",
                "pe",
                "pb",
                "ps",
            ],
            trade_dates,
        )

    def get_stock_moneyflow(self, symbols: list[str], *, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
        columns = ["ts_code", "trade_date", "main_net_amount", "data_source"]
        clean_symbols = [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
        if not clean_symbols:
            return pd.DataFrame(columns=columns)
        self.initialize()
        clauses = [f"ts_code IN ({', '.join('?' for _ in clean_symbols)})"]
        params: list[object] = [*clean_symbols]
        if start_date:
            clauses.append("trade_date >= ?")
            params.append(str(start_date))
        if end_date:
            clauses.append("trade_date <= ?")
            params.append(str(end_date))
        with self.connect() as con:
            return con.execute(
                f"""
                SELECT {", ".join(columns)}
                FROM stock_moneyflow
                WHERE {" AND ".join(clauses)}
                ORDER BY ts_code ASC, trade_date ASC
                """,
                params,
            ).fetchdf()

    def get_stock_fundamentals(self, symbols: list[str], *, as_of: str | None = None) -> pd.DataFrame:
        columns = [
            "ts_code",
            "end_date",
            "ann_date",
            "total_revenue",
            "revenue",
            "net_profit",
            "profit",
            "roe",
            "debt_to_assets",
            "total_assets",
            "total_liab",
            "data_source",
        ]
        clean_symbols = [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
        if not clean_symbols:
            return pd.DataFrame(columns=columns)
        self.initialize()
        clauses = [f"ts_code IN ({', '.join('?' for _ in clean_symbols)})"]
        params: list[object] = [*clean_symbols]
        if as_of:
            clauses.append("(ann_date IS NULL OR ann_date <= ?)")
            params.append(str(as_of))
        with self.connect() as con:
            return con.execute(
                f"""
                SELECT {", ".join(columns)}
                FROM stock_fundamentals
                WHERE {" AND ".join(clauses)}
                ORDER BY ts_code ASC, end_date ASC
                """,
                params,
            ).fetchdf()

    def get_industry_daily(self, index_code: str, trade_dates: list[str]) -> pd.DataFrame:
        dates = [str(date) for date in trade_dates]
        if not dates:
            return pd.DataFrame(columns=["index_code", "trade_date", "close"])
        self.initialize()
        placeholders = ", ".join("?" for _ in dates)
        with self.connect() as con:
            return con.execute(
                f"""
                SELECT index_code, trade_date, close
                FROM industry_daily
                WHERE index_code = ? AND trade_date IN ({placeholders})
                ORDER BY trade_date ASC
                """,
                [index_code, *dates],
            ).fetchdf()

    def get_sector_basic(self, *, sector_types: list[str] | None = None) -> pd.DataFrame:
        columns = ["sector_code", "name", "sector_type", "exchange", "list_date", "data_source"]
        self.initialize()
        clauses: list[str] = []
        params: list[object] = []
        if sector_types:
            clean = [str(item).strip() for item in sector_types if str(item).strip()]
            if clean:
                clauses.append(f"sector_type IN ({', '.join('?' for _ in clean)})")
                params.extend(clean)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as con:
            return con.execute(
                f"""
                SELECT {", ".join(columns)}
                FROM sector_basic
                {where}
                ORDER BY sector_type ASC, sector_code ASC
                """,
                params,
            ).fetchdf()

    def get_sector_daily(self, sector_codes: list[str], *, trade_dates: list[str]) -> pd.DataFrame:
        columns = ["sector_code", "trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount", "data_source"]
        codes = [str(code).strip() for code in sector_codes if str(code).strip()]
        dates = [str(date).strip() for date in trade_dates if str(date).strip()]
        if not codes or not dates:
            return pd.DataFrame(columns=columns)
        self.initialize()
        code_placeholders = ", ".join("?" for _ in codes)
        date_placeholders = ", ".join("?" for _ in dates)
        with self.connect() as con:
            return con.execute(
                f"""
                SELECT {", ".join(columns)}
                FROM sector_daily
                WHERE sector_code IN ({code_placeholders})
                  AND trade_date IN ({date_placeholders})
                ORDER BY sector_code ASC, trade_date ASC
                """,
                [*codes, *dates],
            ).fetchdf()

    def get_sector_members(self, sector_codes: list[str]) -> pd.DataFrame:
        columns = ["sector_code", "ts_code", "name", "weight", "in_date", "out_date", "is_new", "data_source"]
        codes = [str(code).strip() for code in sector_codes if str(code).strip()]
        if not codes:
            return pd.DataFrame(columns=columns)
        self.initialize()
        placeholders = ", ".join("?" for _ in codes)
        with self.connect() as con:
            return con.execute(
                f"""
                SELECT {", ".join(columns)}
                FROM sector_members
                WHERE sector_code IN ({placeholders})
                ORDER BY sector_code ASC, ts_code ASC
                """,
                codes,
            ).fetchdf()

    def upsert_monitor_position(
        self,
        *,
        ts_code: str,
        name: str = "",
        quantity: float = 0.0,
        buy_price: float = 0.0,
        buy_date: str = "",
        enabled: bool = True,
        note: str = "",
    ) -> None:
        self.initialize()
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO monitor_positions
                    (ts_code, name, quantity, buy_price, buy_date, enabled, note, created_at, updated_at)
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM monitor_positions WHERE ts_code = ?), CURRENT_TIMESTAMP),
                    CURRENT_TIMESTAMP
                )
                """,
                [ts_code, name, quantity, buy_price, buy_date, enabled, note, ts_code],
            )

    def list_monitor_positions(self, *, enabled: bool | None = None) -> list[dict]:
        return self._list_monitor_table(
            "monitor_positions",
            ["ts_code", "name", "quantity", "buy_price", "buy_date", "enabled", "note", "created_at", "updated_at"],
            enabled=enabled,
        )

    def delete_monitor_position(self, ts_code: str) -> bool:
        return self._delete_monitor_symbol("monitor_positions", ts_code)

    def upsert_monitor_watchlist(
        self,
        *,
        ts_code: str,
        name: str = "",
        enabled: bool = True,
        note: str = "",
    ) -> None:
        self.initialize()
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO monitor_watchlist
                    (ts_code, name, enabled, note, created_at, updated_at)
                VALUES (
                    ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM monitor_watchlist WHERE ts_code = ?), CURRENT_TIMESTAMP),
                    CURRENT_TIMESTAMP
                )
                """,
                [ts_code, name, enabled, note, ts_code],
            )

    def list_monitor_watchlist(self, *, enabled: bool | None = None) -> list[dict]:
        return self._list_monitor_table(
            "monitor_watchlist",
            ["ts_code", "name", "enabled", "note", "created_at", "updated_at"],
            enabled=enabled,
        )

    def delete_monitor_watchlist(self, ts_code: str) -> bool:
        return self._delete_monitor_symbol("monitor_watchlist", ts_code)

    def upsert_monitor_buy_candidate(
        self,
        *,
        ts_code: str,
        name: str = "",
        source_event_id: str = "",
        rule_name: str = "",
        signal_name: str = "",
        signal_label: str = "",
        score: float = 0.0,
        price: float = 0.0,
        reason: str = "",
        enabled: bool = True,
        note: str = "",
    ) -> None:
        self.initialize()
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO monitor_buy_candidates
                    (ts_code, name, source_event_id, rule_name, signal_name, signal_label,
                     score, price, reason, enabled, note, created_at, updated_at)
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM monitor_buy_candidates WHERE ts_code = ?), CURRENT_TIMESTAMP),
                    CURRENT_TIMESTAMP
                )
                """,
                [
                    ts_code,
                    name,
                    source_event_id,
                    rule_name,
                    signal_name,
                    signal_label,
                    score,
                    price,
                    reason,
                    enabled,
                    note,
                    ts_code,
                ],
            )

    def list_monitor_buy_candidates(self, *, enabled: bool | None = None) -> list[dict]:
        return self._list_monitor_table(
            "monitor_buy_candidates",
            [
                "ts_code",
                "name",
                "source_event_id",
                "rule_name",
                "signal_name",
                "signal_label",
                "score",
                "price",
                "reason",
                "enabled",
                "note",
                "created_at",
                "updated_at",
            ],
            enabled=enabled,
        )

    def delete_monitor_buy_candidate(self, ts_code: str) -> bool:
        return self._delete_monitor_symbol("monitor_buy_candidates", ts_code)

    def insert_monitor_event(self, event: dict) -> bool:
        event_id = str(event.get("event_id") or event.get("event_key") or "").strip()
        event_key = str(event.get("event_key") or event_id).strip()
        if not event_id:
            raise ValueError("monitor event requires event_id")
        self.initialize()
        with self.connect() as con:
            exists = con.execute("SELECT 1 FROM monitor_events WHERE event_id = ?", [event_id]).fetchone()
            if exists:
                return False
            con.execute(
                """
                INSERT INTO monitor_events
                    (event_id, event_key, ts_code, name, source_list, rule_name, signal_name,
                     signal_label, side, score, price, trade_time, message, watch_levels_json,
                     risk_flags_json, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    event_id,
                    event_key,
                    str(event.get("ts_code") or ""),
                    str(event.get("name") or ""),
                    str(event.get("source_list") or ""),
                    str(event.get("rule_name") or ""),
                    str(event.get("signal_name") or ""),
                    str(event.get("signal_label") or ""),
                    str(event.get("side") or ""),
                    _optional_float(event.get("score")),
                    _optional_float(event.get("price")),
                    str(event.get("trade_time") or ""),
                    str(event.get("message") or ""),
                    json.dumps(event.get("watch_levels") or {}, ensure_ascii=False, default=str),
                    json.dumps(event.get("risk_flags") or [], ensure_ascii=False, default=str),
                    json.dumps(event.get("metrics") or {}, ensure_ascii=False, default=str),
                ],
            )
        return True

    def list_monitor_events(self, *, limit: int = 100) -> list[dict]:
        self.initialize()
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT event_id, event_key, ts_code, name, source_list, rule_name, signal_name,
                       signal_label, side, score, price, trade_time, message, watch_levels_json,
                       risk_flags_json, metrics_json, created_at
                FROM monitor_events
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "event_key": row[1],
                "ts_code": row[2],
                "name": row[3],
                "source_list": row[4],
                "rule_name": row[5],
                "signal_name": row[6],
                "signal_label": row[7],
                "side": row[8],
                "score": _optional_float(row[9]),
                "price": _optional_float(row[10]),
                "trade_time": row[11],
                "message": row[12],
                "watch_levels": json.loads(row[13] or "{}"),
                "risk_flags": json.loads(row[14] or "[]"),
                "metrics": json.loads(row[15] or "{}"),
                "created_at": str(row[16]),
            }
            for row in rows
        ]

    def insert_monitor_trade_event(self, event: dict) -> bool:
        trade_event_id = str(event.get("trade_event_id") or "").strip()
        if not trade_event_id:
            raise ValueError("monitor trade event requires trade_event_id")
        self.initialize()
        with self.connect() as con:
            exists = con.execute("SELECT 1 FROM monitor_trade_events WHERE trade_event_id = ?", [trade_event_id]).fetchone()
            if exists:
                return False
            con.execute(
                """
                INSERT INTO monitor_trade_events
                    (trade_event_id, event_id, ts_code, name, action, side, price,
                     quantity, status, message, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    trade_event_id,
                    str(event.get("event_id") or ""),
                    str(event.get("ts_code") or ""),
                    str(event.get("name") or ""),
                    str(event.get("action") or ""),
                    str(event.get("side") or ""),
                    _optional_float(event.get("price")),
                    _optional_float(event.get("quantity")),
                    str(event.get("status") or "not_configured"),
                    str(event.get("message") or ""),
                    json.dumps(event.get("metrics") or {}, ensure_ascii=False, default=str),
                ],
            )
        return True

    def list_monitor_trade_events(self, *, limit: int = 100) -> list[dict]:
        self.initialize()
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT trade_event_id, event_id, ts_code, name, action, side, price,
                       quantity, status, message, metrics_json, created_at
                FROM monitor_trade_events
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()
        return [
            {
                "trade_event_id": row[0],
                "event_id": row[1],
                "ts_code": row[2],
                "name": row[3],
                "action": row[4],
                "side": row[5],
                "price": _optional_float(row[6]),
                "quantity": _optional_float(row[7]),
                "status": row[8],
                "message": row[9],
                "metrics": json.loads(row[10] or "{}"),
                "created_at": str(row[11]),
            }
            for row in rows
        ]

    def upsert_broker_account(self, account: dict) -> None:
        self.initialize()
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO broker_accounts
                    (provider, account_id, account_type, cash, available_cash, market_value, total_asset, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    str(account.get("provider") or "qmt"),
                    str(account.get("account_id") or ""),
                    str(account.get("account_type") or "STOCK"),
                    _optional_float(account.get("cash")),
                    _optional_float(account.get("available_cash")),
                    _optional_float(account.get("market_value")),
                    _optional_float(account.get("total_asset")),
                    json.dumps(account.get("raw") or account, ensure_ascii=False, default=str),
                ],
            )

    def upsert_broker_positions(self, positions: list[dict], *, provider: str = "qmt", account_id: str = "") -> None:
        self.initialize()
        with self.connect() as con:
            for position in positions:
                con.execute(
                    """
                    INSERT OR REPLACE INTO broker_positions
                        (provider, account_id, ts_code, name, quantity, available_quantity, cost_price,
                         price, market_value, pnl, pnl_pct, source, raw_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    [
                        provider,
                        account_id,
                        str(position.get("ts_code") or ""),
                        str(position.get("name") or ""),
                        _optional_float(position.get("quantity")),
                        _optional_float(position.get("available_quantity")),
                        _optional_float(position.get("cost_price")),
                        _optional_float(position.get("price")),
                        _optional_float(position.get("market_value")),
                        _optional_float(position.get("pnl")),
                        _optional_float(position.get("pnl_pct")),
                        str(position.get("source") or provider),
                        json.dumps(position.get("raw") or position, ensure_ascii=False, default=str),
                    ],
                )

    def list_broker_positions(self, *, provider: str | None = None, account_id: str | None = None) -> list[dict]:
        self.initialize()
        clauses: list[str] = []
        params: list[object] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as con:
            rows = con.execute(
                f"""
                SELECT provider, account_id, ts_code, name, quantity, available_quantity, cost_price,
                       price, market_value, pnl, pnl_pct, source, raw_json, updated_at
                FROM broker_positions
                {where}
                ORDER BY updated_at DESC, ts_code
                """,
                params,
            ).fetchall()
        return [
            {
                "provider": row[0],
                "account_id": row[1],
                "ts_code": row[2],
                "name": row[3],
                "quantity": _optional_float(row[4]),
                "available_quantity": _optional_float(row[5]),
                "cost_price": _optional_float(row[6]),
                "price": _optional_float(row[7]),
                "market_value": _optional_float(row[8]),
                "pnl": _optional_float(row[9]),
                "pnl_pct": _optional_float(row[10]),
                "source": row[11],
                "raw": json.loads(row[12] or "{}"),
                "updated_at": str(row[13]),
            }
            for row in rows
        ]

    def insert_broker_order(self, order: dict) -> None:
        self.initialize()
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO broker_orders
                    (sats_order_id, provider, account_id, broker_order_id, ts_code, side, quantity,
                     price, price_type, status, message, request_json, response_json, created_at, updated_at)
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM broker_orders WHERE sats_order_id = ?), CURRENT_TIMESTAMP),
                    CURRENT_TIMESTAMP
                )
                """,
                [
                    str(order.get("sats_order_id") or ""),
                    str(order.get("provider") or "qmt"),
                    str(order.get("account_id") or ""),
                    str(order.get("broker_order_id") or ""),
                    str(order.get("ts_code") or ""),
                    str(order.get("side") or ""),
                    _optional_float(order.get("quantity")),
                    _optional_float(order.get("price")),
                    str(order.get("price_type") or ""),
                    str(order.get("status") or ""),
                    str(order.get("message") or ""),
                    json.dumps(order.get("request") or {}, ensure_ascii=False, default=str),
                    json.dumps(order.get("response") or {}, ensure_ascii=False, default=str),
                    str(order.get("sats_order_id") or ""),
                ],
            )

    def list_broker_orders(self, *, open_only: bool = False, limit: int = 100) -> list[dict]:
        self.initialize()
        where = ""
        if open_only:
            where = "WHERE lower(status) NOT IN ('filled', 'cancelled', 'rejected', 'failed', 'error')"
        with self.connect() as con:
            rows = con.execute(
                f"""
                SELECT sats_order_id, provider, account_id, broker_order_id, ts_code, side, quantity,
                       price, price_type, status, message, request_json, response_json, created_at, updated_at
                FROM broker_orders
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()
        return [
            {
                "sats_order_id": row[0],
                "provider": row[1],
                "account_id": row[2],
                "broker_order_id": row[3],
                "ts_code": row[4],
                "side": row[5],
                "quantity": _optional_float(row[6]),
                "price": _optional_float(row[7]),
                "price_type": row[8],
                "status": row[9],
                "message": row[10],
                "request": json.loads(row[11] or "{}"),
                "response": json.loads(row[12] or "{}"),
                "created_at": str(row[13]),
                "updated_at": str(row[14]),
            }
            for row in rows
        ]

    def insert_broker_trade(self, trade: dict) -> None:
        self.initialize()
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO broker_trades
                    (provider, account_id, trade_id, broker_order_id, ts_code, side, quantity, price, trade_time, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(trade.get("provider") or "qmt"),
                    str(trade.get("account_id") or ""),
                    str(trade.get("trade_id") or ""),
                    str(trade.get("broker_order_id") or trade.get("order_id") or ""),
                    str(trade.get("ts_code") or ""),
                    str(trade.get("side") or ""),
                    _optional_float(trade.get("quantity")),
                    _optional_float(trade.get("price")),
                    str(trade.get("trade_time") or ""),
                    json.dumps(trade.get("raw") or trade, ensure_ascii=False, default=str),
                ],
            )

    def list_broker_trades(self, *, limit: int = 100) -> list[dict]:
        self.initialize()
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT provider, account_id, trade_id, broker_order_id, ts_code, side, quantity, price, trade_time, raw_json, created_at
                FROM broker_trades
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()
        return [
            {
                "provider": row[0],
                "account_id": row[1],
                "trade_id": row[2],
                "broker_order_id": row[3],
                "ts_code": row[4],
                "side": row[5],
                "quantity": _optional_float(row[6]),
                "price": _optional_float(row[7]),
                "trade_time": row[8],
                "raw": json.loads(row[9] or "{}"),
                "created_at": str(row[10]),
            }
            for row in rows
        ]

    def insert_broker_order_event(self, event: dict) -> None:
        event_id = str(event.get("event_id") or _stable_id(json.dumps(event, ensure_ascii=False, default=str))).strip()
        self.initialize()
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO broker_order_events
                    (event_id, sats_order_id, broker_order_id, provider, account_id, event_type, status, message, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    event_id,
                    str(event.get("sats_order_id") or ""),
                    str(event.get("broker_order_id") or ""),
                    str(event.get("provider") or "qmt"),
                    str(event.get("account_id") or ""),
                    str(event.get("event_type") or ""),
                    str(event.get("status") or ""),
                    str(event.get("message") or ""),
                    json.dumps(event.get("payload") or event, ensure_ascii=False, default=str),
                ],
            )

    def upsert_monitor_runtime(
        self,
        *,
        service_name: str,
        status: str,
        pid: int | None = None,
        params: dict | None = None,
        last_error: str | None = None,
        heartbeat: bool = False,
    ) -> None:
        self.initialize()
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO monitor_runtime
                    (service_name, status, pid, heartbeat_at, started_at, stopped_at,
                     params_json, last_error, updated_at)
                VALUES (
                    ?, ?, ?,
                    CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE (SELECT heartbeat_at FROM monitor_runtime WHERE service_name = ?) END,
                    COALESCE((SELECT started_at FROM monitor_runtime WHERE service_name = ?), CURRENT_TIMESTAMP),
                    CASE WHEN ? = 'stopped' THEN CURRENT_TIMESTAMP ELSE (SELECT stopped_at FROM monitor_runtime WHERE service_name = ?) END,
                    ?, ?, CURRENT_TIMESTAMP
                )
                """,
                [
                    service_name,
                    status,
                    pid,
                    heartbeat,
                    service_name,
                    service_name,
                    status,
                    service_name,
                    json.dumps(params or {}, ensure_ascii=False, default=str),
                    last_error,
                ],
            )

    def get_monitor_runtime(self, service_name: str = "monitor") -> dict:
        self.initialize()
        with self.connect() as con:
            row = con.execute(
                """
                SELECT service_name, status, pid, heartbeat_at, started_at, stopped_at,
                       params_json, last_error, updated_at
                FROM monitor_runtime
                WHERE service_name = ?
                """,
                [service_name],
            ).fetchone()
        if not row:
            return {
                "service_name": service_name,
                "status": "stopped",
                "pid": None,
                "params": {},
                "last_error": "",
            }
        return {
            "service_name": row[0],
            "status": row[1],
            "pid": row[2],
            "heartbeat_at": str(row[3]) if row[3] is not None else "",
            "started_at": str(row[4]) if row[4] is not None else "",
            "stopped_at": str(row[5]) if row[5] is not None else "",
            "params": json.loads(row[6] or "{}"),
            "last_error": row[7] or "",
            "updated_at": str(row[8]) if row[8] is not None else "",
        }

    def insert_scheduled_task(self, task: dict) -> None:
        name = str(task.get("name") or "").strip()
        if not name:
            raise ValueError("scheduled task requires name")
        self.initialize()
        with self.connect() as con:
            exists = con.execute("SELECT 1 FROM scheduled_tasks WHERE name = ?", [name]).fetchone()
            if exists:
                raise ValueError(f"定时任务已存在: {name}")
            con.execute(
                """
                INSERT INTO scheduled_tasks
                    (name, task_type, text, schedule_kind, days_json, time_of_day,
                     timezone, enabled, next_run_at, last_status, running)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE)
                """,
                [
                    name,
                    str(task.get("task_type") or ""),
                    str(task.get("text") or ""),
                    str(task.get("schedule_kind") or ""),
                    json.dumps(list(task.get("days") or []), ensure_ascii=False),
                    str(task.get("time_of_day") or ""),
                    str(task.get("timezone") or "Asia/Shanghai"),
                    bool(task.get("enabled", True)),
                    task.get("next_run_at") or None,
                    str(task.get("last_status") or ""),
                ],
            )

    def list_scheduled_tasks(self, *, enabled: bool | None = None) -> list[dict]:
        self.initialize()
        where = "WHERE enabled = ?" if enabled is not None else ""
        params: list[object] = [bool(enabled)] if enabled is not None else []
        with self.connect() as con:
            rows = con.execute(
                f"""
                SELECT name, task_type, text, schedule_kind, days_json, time_of_day,
                       timezone, enabled, next_run_at, last_run_at, last_status,
                       running, created_at, updated_at
                FROM scheduled_tasks
                {where}
                ORDER BY name ASC
                """,
                params,
            ).fetchall()
        return [_scheduled_task_row(row) for row in rows]

    def list_due_scheduled_tasks(self, now: str) -> list[dict]:
        self.initialize()
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT name, task_type, text, schedule_kind, days_json, time_of_day,
                       timezone, enabled, next_run_at, last_run_at, last_status,
                       running, created_at, updated_at
                FROM scheduled_tasks
                WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC, name ASC
                """,
                [now],
            ).fetchall()
        return [_scheduled_task_row(row) for row in rows]

    def get_scheduled_task(self, name: str) -> dict:
        self.initialize()
        with self.connect() as con:
            row = con.execute(
                """
                SELECT name, task_type, text, schedule_kind, days_json, time_of_day,
                       timezone, enabled, next_run_at, last_run_at, last_status,
                       running, created_at, updated_at
                FROM scheduled_tasks
                WHERE name = ?
                """,
                [name],
            ).fetchone()
        return _scheduled_task_row(row) if row else {}

    def delete_scheduled_task(self, name: str) -> bool:
        self.initialize()
        with self.connect() as con:
            count = int(con.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE name = ?", [name]).fetchone()[0])
            if count:
                con.execute("DELETE FROM scheduled_tasks WHERE name = ?", [name])
        return bool(count)

    def set_scheduled_task_enabled(self, name: str, enabled: bool) -> bool:
        self.initialize()
        with self.connect() as con:
            count = int(con.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE name = ?", [name]).fetchone()[0])
            if count:
                con.execute(
                    "UPDATE scheduled_tasks SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
                    [bool(enabled), name],
                )
        return bool(count)

    def mark_scheduled_task_running(self, name: str, *, running: bool = True) -> bool:
        self.initialize()
        with self.connect() as con:
            row = con.execute("SELECT running FROM scheduled_tasks WHERE name = ?", [name]).fetchone()
            if not row:
                return False
            current = bool(row[0])
            if running and current:
                return False
            con.execute(
                "UPDATE scheduled_tasks SET running = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
                [bool(running), name],
            )
        return True

    def update_scheduled_task_after_run(
        self,
        name: str,
        *,
        last_status: str,
        last_run_at: str,
        next_run_at: str,
    ) -> bool:
        self.initialize()
        with self.connect() as con:
            count = int(con.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE name = ?", [name]).fetchone()[0])
            if count:
                con.execute(
                    """
                    UPDATE scheduled_tasks
                    SET last_status = ?, last_run_at = ?, next_run_at = ?,
                        running = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE name = ?
                    """,
                    [last_status, last_run_at or None, next_run_at or None, name],
                )
        return bool(count)

    def update_scheduled_task_last_status(self, name: str, *, last_status: str, last_run_at: str) -> bool:
        self.initialize()
        with self.connect() as con:
            count = int(con.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE name = ?", [name]).fetchone()[0])
            if count:
                con.execute(
                    """
                    UPDATE scheduled_tasks
                    SET last_status = ?, last_run_at = ?, running = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE name = ?
                    """,
                    [last_status, last_run_at or None, name],
                )
        return bool(count)

    def insert_scheduled_task_run(self, run: dict) -> None:
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("scheduled task run requires run_id")
        self.initialize()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO scheduled_task_runs
                    (run_id, task_name, task_type, text, scheduled_for, started_at,
                     finished_at, status, duration_seconds, output_text, error, report_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    str(run.get("task_name") or ""),
                    str(run.get("task_type") or ""),
                    str(run.get("text") or ""),
                    run.get("scheduled_for") or None,
                    run.get("started_at") or None,
                    run.get("finished_at") or None,
                    str(run.get("status") or ""),
                    _optional_float(run.get("duration_seconds")),
                    str(run.get("output_text") or ""),
                    str(run.get("error") or ""),
                    str(run.get("report_path") or ""),
                ],
            )

    def list_scheduled_task_runs(self, *, limit: int = 20, task_name: str | None = None) -> list[dict]:
        self.initialize()
        where = "WHERE task_name = ?" if task_name else ""
        params: list[object] = [task_name] if task_name else []
        params.append(int(limit))
        with self.connect() as con:
            rows = con.execute(
                f"""
                SELECT run_id, task_name, task_type, text, scheduled_for, started_at,
                       finished_at, status, duration_seconds, output_text, error,
                       report_path, created_at
                FROM scheduled_task_runs
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "run_id": row[0],
                "task_name": row[1],
                "task_type": row[2],
                "text": row[3],
                "scheduled_for": str(row[4]) if row[4] is not None else "",
                "started_at": str(row[5]) if row[5] is not None else "",
                "finished_at": str(row[6]) if row[6] is not None else "",
                "status": row[7],
                "duration_seconds": _optional_float(row[8]),
                "output_text": row[9] or "",
                "error": row[10] or "",
                "report_path": row[11] or "",
                "created_at": str(row[12]) if row[12] is not None else "",
            }
            for row in rows
        ]

    def _list_monitor_table(self, table: str, columns: list[str], *, enabled: bool | None = None) -> list[dict]:
        self.initialize()
        where = "WHERE enabled = ?" if enabled is not None else ""
        params: list[object] = [enabled] if enabled is not None else []
        with self.connect() as con:
            rows = con.execute(
                f"""
                SELECT {", ".join(columns)}
                FROM {table}
                {where}
                ORDER BY ts_code ASC
                """,
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(zip(columns, row, strict=True))
            for key, value in list(item.items()):
                if key in {"created_at", "updated_at"} and value is not None:
                    item[key] = str(value)
            result.append(item)
        return result

    def _delete_monitor_symbol(self, table: str, ts_code: str) -> bool:
        self.initialize()
        with self.connect() as con:
            count = int(con.execute(f"SELECT COUNT(*) FROM {table} WHERE ts_code = ?", [ts_code]).fetchone()[0])
            if count:
                con.execute(f"DELETE FROM {table} WHERE ts_code = ?", [ts_code])
        return bool(count)

    def _upsert_frame(self, table: str, columns: list[str], data: pd.DataFrame) -> int:
        if data.empty:
            return 0
        self.initialize()
        select_columns = ", ".join(columns)
        placeholders = ", ".join(columns)
        with self.connect() as con:
            con.register("upsert_rows", data)
            con.execute(
                f"""
                INSERT OR REPLACE INTO {table} ({placeholders})
                SELECT {select_columns}
                FROM upsert_rows
                """
            )
            con.unregister("upsert_rows")
        return len(data)

    def _get_by_trade_dates(self, table: str, columns: list[str], trade_dates: list[str]) -> pd.DataFrame:
        dates = [str(date) for date in trade_dates]
        if not dates:
            return pd.DataFrame(columns=columns)
        self.initialize()
        placeholders = ", ".join("?" for _ in dates)
        column_sql = ", ".join(columns)
        with self.connect() as con:
            return con.execute(
                f"""
                SELECT {column_sql}
                FROM {table}
                WHERE trade_date IN ({placeholders})
                ORDER BY ts_code ASC, trade_date ASC
                """,
                dates,
            ).fetchdf()

    def upsert_screening_results(self, results: Iterable[ScreeningResult]) -> int:
        rows = list(results)
        if not rows:
            return 0
        self.initialize()
        with self.connect() as con:
            con.executemany(
                """
                INSERT OR REPLACE INTO screening_results
                    (trade_date, ts_code, rule_name, passed, score,
                     matched_conditions, failed_conditions, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row.trade_date,
                        row.ts_code,
                        row.rule_name,
                        row.passed,
                        row.score,
                        json.dumps(row.matched_conditions, ensure_ascii=False),
                        json.dumps(row.failed_conditions, ensure_ascii=False),
                        json.dumps(row.metrics, ensure_ascii=False, default=str),
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def list_screening_results(
        self,
        *,
        trade_date: str | None = None,
        rule_name: str | None = None,
        passed: bool | None = None,
    ) -> list[dict]:
        self.initialize()
        clauses: list[str] = []
        params: list[object] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if rule_name:
            clauses.append("rule_name = ?")
            params.append(rule_name)
        if passed is not None:
            clauses.append("passed = ?")
            params.append(passed)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT trade_date, ts_code, rule_name, passed, score,
                   matched_conditions, failed_conditions, metrics_json, created_at
            FROM screening_results
            {where}
            ORDER BY trade_date DESC, passed DESC, score DESC, ts_code ASC
        """
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "trade_date": row[0],
                    "ts_code": row[1],
                    "rule_name": row[2],
                    "passed": bool(row[3]),
                    "score": float(row[4]),
                    "matched_conditions": json.loads(row[5]),
                    "failed_conditions": json.loads(row[6]),
                    "metrics": json.loads(row[7]),
                    "created_at": str(row[8]),
                }
            )
        return result

    def list_screening_stocks(
        self,
        *,
        trade_date: str | None = None,
        rule_name: str | None = None,
        passed: bool | None = None,
    ) -> list[dict]:
        self.initialize()
        clauses: list[str] = []
        params: list[object] = []
        if trade_date:
            clauses.append("r.trade_date = ?")
            params.append(trade_date)
        if rule_name:
            clauses.append("r.rule_name = ?")
            params.append(rule_name)
        if passed is not None:
            clauses.append("r.passed = ?")
            params.append(passed)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT r.ts_code, COALESCE(b.name, '') AS name, r.rule_name, r.score, r.metrics_json
            FROM screening_results r
            LEFT JOIN stock_basic b ON b.ts_code = r.ts_code
            {where}
            ORDER BY r.trade_date DESC, r.passed DESC, r.score DESC, r.ts_code ASC
        """
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
        result = []
        for row in rows:
            metrics = json.loads(row[4] or "{}")
            matched_labels = metrics.get("matched_signal_labels") or metrics.get("matched_chan_rules", [])
            if not isinstance(matched_labels, list):
                matched_labels = []
            result.append(
                {
                    "ts_code": str(row[0]),
                    "name": str(row[1] or ""),
                    "rule_name": str(row[2] or ""),
                    "score": float(row[3]),
                    "metrics": metrics,
                    "matched_labels": [str(label) for label in matched_labels if str(label).strip()],
                }
            )
        return result

    def list_screening_rule_names(self) -> list[str]:
        self.initialize()
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT DISTINCT rule_name
                FROM screening_results
                ORDER BY rule_name ASC
                """
            ).fetchall()
        return [str(row[0]) for row in rows]


def _prepare_frame(frame: pd.DataFrame, columns: list[str], *, required: list[str]) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.copy()
    for column in columns:
        if column not in data.columns:
            data[column] = None
    data = data[columns]
    for column in required:
        data[column] = data[column].where(data[column].notna(), "").astype(str).str.strip()
        data = data[data[column] != ""]
    if "trade_date" in data.columns:
        data["trade_date"] = data["trade_date"].astype(str).str.strip()
    return data.drop_duplicates(subset=required, keep="last").reset_index(drop=True)


def _scheduled_task_row(row) -> dict:
    if not row:
        return {}
    return {
        "name": row[0],
        "task_type": row[1],
        "text": row[2],
        "schedule_kind": row[3],
        "days": json.loads(row[4] or "[]"),
        "time_of_day": row[5],
        "timezone": row[6],
        "enabled": bool(row[7]),
        "next_run_at": str(row[8]) if row[8] is not None else "",
        "last_run_at": str(row[9]) if row[9] is not None else "",
        "last_status": row[10] or "",
        "running": bool(row[11]),
        "created_at": str(row[12]) if row[12] is not None else "",
        "updated_at": str(row[13]) if row[13] is not None else "",
    }


def _optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _stable_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:24]
