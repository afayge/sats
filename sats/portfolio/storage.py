from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sats.storage.duckdb import DuckDBStorage


class PortfolioStore:
    def __init__(self, storage: DuckDBStorage) -> None:
        self.storage = storage

    def insert_run(self, run: dict[str, Any]) -> None:
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO portfolio_runs
                    (run_id, trade_date, phase, trading_mode, status, market_score,
                     exposure_limit, candidate_count, selected_count, replacement_count,
                     summary, details_json, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(run["run_id"]),
                    str(run["trade_date"]),
                    str(run["phase"]),
                    str(run["trading_mode"]),
                    str(run.get("status") or "running"),
                    _float(run.get("market_score")),
                    _float(run.get("exposure_limit")),
                    int(run.get("candidate_count") or 0),
                    int(run.get("selected_count") or 0),
                    int(run.get("replacement_count") or 0),
                    str(run.get("summary") or ""),
                    _json(run.get("details") or {}),
                    run.get("started_at") or datetime.now(),
                    run.get("finished_at"),
                ],
            )

    def update_run(self, run_id: str, **changes: Any) -> None:
        allowed = {
            "status",
            "market_score",
            "exposure_limit",
            "candidate_count",
            "selected_count",
            "replacement_count",
            "summary",
            "details_json",
            "finished_at",
        }
        values: list[Any] = []
        assignments: list[str] = []
        for key, value in changes.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            values.append(_json(value) if key == "details_json" else value)
        if not assignments:
            return
        values.append(run_id)
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                f"UPDATE portfolio_runs SET {', '.join(assignments)} WHERE run_id = ?",
                values,
            )

    def insert_market_regime(self, snapshot: dict[str, Any]) -> None:
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO market_regime_snapshots
                    (snapshot_id, run_id, trade_date, score, exposure_limit,
                     buy_allowed, data_source, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(snapshot["snapshot_id"]),
                    str(snapshot.get("run_id") or ""),
                    str(snapshot["trade_date"]),
                    float(snapshot["score"]),
                    float(snapshot["exposure_limit"]),
                    bool(snapshot["buy_allowed"]),
                    str(snapshot.get("data_source") or ""),
                    _json(snapshot.get("details") or {}),
                ],
            )

    def insert_candidates(self, candidates: list[dict[str, Any]]) -> None:
        if not candidates:
            return
        self.storage.initialize()
        with self.storage.connect() as con:
            for item in candidates:
                con.execute(
                    """
                    INSERT INTO portfolio_candidates
                        (candidate_id, plan_id, run_id, trade_date, effective_trade_date, ts_code, name, industry,
                         rank_no, selected, status, total_score, entry_price, stop_loss,
                         take_profit_1, take_profit_2, trailing_stop_pct, valid_until,
                         score_json, evidence_json, outcome_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(item["candidate_id"]),
                        str(item["plan_id"]),
                        str(item["run_id"]),
                        str(item["trade_date"]),
                        str(item.get("effective_trade_date") or item.get("trade_date") or ""),
                        str(item["ts_code"]),
                        str(item.get("name") or ""),
                        str(item.get("industry") or ""),
                        int(item.get("rank_no") or 0),
                        bool(item.get("selected")),
                        str(item.get("status") or "candidate"),
                        float(item.get("total_score") or 0.0),
                        _float(item.get("entry_price")),
                        _float(item.get("stop_loss")),
                        _float(item.get("take_profit_1")),
                        _float(item.get("take_profit_2")),
                        _float(item.get("trailing_stop_pct")),
                        str(item.get("valid_until") or ""),
                        _json(item.get("score_json") or {}),
                        _json(item.get("evidence_json") or {}),
                        _json(item.get("outcome_json") or {}),
                    ],
                )

    def list_candidates(
        self,
        *,
        trade_date: str | None = None,
        run_id: str | None = None,
        selected: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if selected is not None:
            clauses.append("selected = ?")
            params.append(bool(selected))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT candidate_id, plan_id, run_id, trade_date, effective_trade_date, ts_code, name, industry,
                       rank_no, selected, status, total_score, entry_price, stop_loss,
                       take_profit_1, take_profit_2, trailing_stop_pct, valid_until,
                       score_json, evidence_json, outcome_json, created_at, updated_at
                FROM portfolio_candidates
                {where}
                ORDER BY created_at DESC, rank_no ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_candidate_row(row) for row in rows]

    def latest_selected_candidates(self, trade_date: str) -> list[dict[str, Any]]:
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                """
                SELECT r.run_id
                FROM portfolio_runs r
                WHERE r.trade_date = ? AND r.status IN ('done', 'partial')
                  AND EXISTS (
                      SELECT 1
                      FROM portfolio_candidates c
                      WHERE c.run_id = r.run_id AND c.selected = TRUE
                  )
                ORDER BY r.started_at DESC
                LIMIT 1
                """,
                [trade_date],
            ).fetchone()
        return self.list_candidates(run_id=str(row[0]), selected=True) if row else []

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        rows = self.list_candidates(limit=1000)
        return next((row for row in rows if row["plan_id"] == plan_id), {})

    def latest_plan_for_symbol(self, ts_code: str) -> dict[str, Any]:
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                """
                SELECT candidate_id, plan_id, run_id, trade_date, effective_trade_date, ts_code, name, industry,
                       rank_no, selected, status, total_score, entry_price, stop_loss,
                       take_profit_1, take_profit_2, trailing_stop_pct, valid_until,
                       score_json, evidence_json, outcome_json, created_at, updated_at
                FROM portfolio_candidates
                WHERE ts_code = ? AND selected = TRUE AND status != 'disabled'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [ts_code],
            ).fetchone()
        return _candidate_row(row) if row else {}

    def set_plan_status(self, plan_id: str, status: str) -> bool:
        if status not in {"active", "disabled", "candidate", "selected", "completed"}:
            raise ValueError(f"unsupported portfolio plan status: {status}")
        self.storage.initialize()
        with self.storage.connect() as con:
            count = int(
                con.execute(
                    "SELECT COUNT(*) FROM portfolio_candidates WHERE plan_id = ?",
                    [plan_id],
                ).fetchone()[0]
            )
            if count:
                con.execute(
                    """
                    UPDATE portfolio_candidates
                    SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE plan_id = ?
                    """,
                    [status, plan_id],
                )
        return bool(count)

    def update_candidate_outcome(self, candidate_id: str, outcome: dict[str, Any]) -> None:
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                UPDATE portfolio_candidates
                SET outcome_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = ?
                """,
                [_json(outcome), candidate_id],
            )

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                """
                SELECT run_id, trade_date, phase, trading_mode, status, market_score,
                       exposure_limit, candidate_count, selected_count, replacement_count,
                       summary, details_json, started_at, finished_at
                FROM portfolio_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()
        return [
            {
                "run_id": row[0],
                "trade_date": row[1],
                "phase": row[2],
                "trading_mode": row[3],
                "status": row[4],
                "market_score": _float(row[5]),
                "exposure_limit": _float(row[6]),
                "candidate_count": int(row[7]),
                "selected_count": int(row[8]),
                "replacement_count": int(row[9]),
                "summary": row[10] or "",
                "details": json.loads(row[11] or "{}"),
                "started_at": str(row[12]),
                "finished_at": str(row[13]) if row[13] is not None else "",
            }
            for row in rows
        ]

    def latest_market_regime(self, trade_date: str | None = None) -> dict[str, Any]:
        where = "WHERE trade_date = ?" if trade_date else ""
        params = [trade_date] if trade_date else []
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                f"""
                SELECT snapshot_id, run_id, trade_date, score, exposure_limit,
                       buy_allowed, data_source, details_json, created_at
                FROM market_regime_snapshots
                {where}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        if not row:
            return {}
        return {
            "snapshot_id": row[0],
            "run_id": row[1],
            "trade_date": row[2],
            "score": float(row[3]),
            "exposure_limit": float(row[4]),
            "buy_allowed": bool(row[5]),
            "data_source": row[6] or "",
            "details": json.loads(row[7] or "{}"),
            "created_at": str(row[8]),
        }

    def ensure_paper_account(self, account_id: str, initial_cash: float) -> dict[str, Any]:
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO paper_accounts
                    (account_id, initial_cash, cash, available_cash, market_value, total_asset,
                     equity_peak, max_drawdown_pct)
                SELECT ?, ?, ?, ?, 0, ?, ?, 0
                WHERE NOT EXISTS (SELECT 1 FROM paper_accounts WHERE account_id = ?)
                """,
                [account_id, initial_cash, initial_cash, initial_cash, initial_cash, initial_cash, account_id],
            )
            con.execute(
                """
                UPDATE paper_accounts
                SET equity_peak = COALESCE(equity_peak, total_asset),
                    max_drawdown_pct = COALESCE(max_drawdown_pct, 0)
                WHERE account_id = ?
                """,
                [account_id],
            )
        return self.paper_account(account_id)

    def paper_account(self, account_id: str) -> dict[str, Any]:
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                """
                SELECT account_id, initial_cash, cash, available_cash, market_value,
                       total_asset, realized_pnl, equity_peak, max_drawdown_pct, updated_at
                FROM paper_accounts
                WHERE account_id = ?
                """,
                [account_id],
            ).fetchone()
        if not row:
            return {}
        return {
            "account_id": row[0],
            "initial_cash": float(row[1]),
            "cash": float(row[2]),
            "available_cash": float(row[3]),
            "market_value": float(row[4]),
            "total_asset": float(row[5]),
            "realized_pnl": float(row[6]),
            "equity_peak": float(row[7] or 0.0),
            "max_drawdown_pct": float(row[8] or 0.0),
            "updated_at": str(row[9]),
        }

    def paper_positions(self, account_id: str) -> list[dict[str, Any]]:
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                """
                SELECT account_id, ts_code, name, quantity, available_quantity, cost_price,
                       price, market_value, pnl, pnl_pct, peak_price, trough_price,
                       opened_trade_date, last_buy_trade_date, plan_id, updated_at
                FROM paper_positions
                WHERE account_id = ? AND quantity > 0
                ORDER BY ts_code
                """,
                [account_id],
            ).fetchall()
        return [
            {
                "account_id": row[0],
                "ts_code": row[1],
                "name": row[2],
                "quantity": float(row[3]),
                "available_quantity": float(row[4]),
                "cost_price": float(row[5]),
                "price": float(row[6]),
                "market_value": float(row[7]),
                "pnl": float(row[8]),
                "pnl_pct": float(row[9]),
                "peak_price": float(row[10]),
                "trough_price": float(row[11] or row[6] or row[5] or 0.0),
                "opened_trade_date": row[12],
                "last_buy_trade_date": row[13],
                "plan_id": row[14] or "",
                "updated_at": str(row[15]),
            }
            for row in rows
        ]

    def performance_summary(self, account_id: str = "default") -> dict[str, Any]:
        account = self.paper_account(account_id)
        trades = self.paper_trades(account_id, limit=10000)
        sells = [row for row in trades if row.get("side") == "sell"]
        wins = [row for row in sells if float(row.get("realized_pnl") or 0.0) > 0]
        candidates = self.list_candidates(selected=True, limit=5000)
        return_metrics: dict[str, float] = {}
        for horizon in (1, 2, 4):
            values = [
                float((row.get("outcome") or {}).get(f"return_{horizon}d_pct"))
                for row in candidates
                if (row.get("outcome") or {}).get(f"return_{horizon}d_pct") is not None
            ]
            if values:
                return_metrics[f"average_return_{horizon}d_pct"] = round(sum(values) / len(values), 4)
                return_metrics[f"hit_rate_{horizon}d"] = round(
                    len([value for value in values if value > 0]) / len(values),
                    4,
                )
        positions = self.paper_positions(account_id)
        adverse = [
            ((float(row["trough_price"]) / float(row["cost_price"]) - 1.0) * 100.0)
            for row in positions
            if float(row.get("cost_price") or 0.0) > 0 and float(row.get("trough_price") or 0.0) > 0
        ]
        return {
            "account_id": account_id,
            "total_asset": account.get("total_asset"),
            "realized_pnl": account.get("realized_pnl"),
            "max_drawdown_pct": account.get("max_drawdown_pct"),
            "closed_trade_count": len(sells),
            "closed_trade_win_rate": round(len(wins) / len(sells), 4) if sells else None,
            "current_max_adverse_excursion_pct": round(min(adverse), 4) if adverse else None,
            **return_metrics,
        }

    def paper_orders(
        self,
        account_id: str,
        *,
        trade_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["account_id = ?"]
        params: list[Any] = [account_id]
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        params.append(int(limit))
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT order_id, account_id, plan_id, source_run_id, ts_code, name, side,
                       quantity, price, status, reason, trade_date, trade_time,
                       request_json, created_at, updated_at
                FROM paper_orders
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "order_id": row[0],
                "account_id": row[1],
                "plan_id": row[2] or "",
                "source_run_id": row[3] or "",
                "ts_code": row[4],
                "name": row[5],
                "side": row[6],
                "quantity": float(row[7]),
                "price": float(row[8]),
                "status": row[9],
                "reason": row[10] or "",
                "trade_date": row[11] or "",
                "trade_time": row[12] or "",
                "request": json.loads(row[13] or "{}"),
                "created_at": str(row[14]),
                "updated_at": str(row[15]),
            }
            for row in rows
        ]

    def paper_trades(
        self,
        account_id: str,
        *,
        trade_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["account_id = ?"]
        params: list[Any] = [account_id]
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        params.append(int(limit))
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT trade_id, order_id, account_id, plan_id, ts_code, name, side,
                       quantity, price, realized_pnl, trade_date, trade_time, reason, created_at
                FROM paper_trades
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "trade_id": row[0],
                "order_id": row[1],
                "account_id": row[2],
                "plan_id": row[3] or "",
                "ts_code": row[4],
                "name": row[5],
                "side": row[6],
                "quantity": float(row[7]),
                "price": float(row[8]),
                "realized_pnl": float(row[9]),
                "trade_date": row[10],
                "trade_time": row[11],
                "reason": row[12] or "",
                "created_at": str(row[13]),
            }
            for row in rows
        ]

    def insert_pending_intent(self, intent: dict[str, Any]) -> bool:
        self.storage.initialize()
        with self.storage.connect() as con:
            exists = con.execute(
                """
                SELECT 1 FROM pending_trade_intents
                WHERE plan_id = ? AND side = ? AND status = 'pending'
                """,
                [str(intent.get("plan_id") or ""), str(intent["side"])],
            ).fetchone()
            if exists:
                return False
            con.execute(
                """
                INSERT INTO pending_trade_intents
                    (intent_id, plan_id, source_run_id, trading_mode, ts_code, name,
                     side, quantity, reference_price, status, reason, market_score,
                     expires_at, request_json, result_json)
                VALUES (?, ?, ?, 'live', ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, '{}')
                """,
                [
                    str(intent["intent_id"]),
                    str(intent.get("plan_id") or ""),
                    str(intent.get("source_run_id") or ""),
                    str(intent["ts_code"]),
                    str(intent.get("name") or ""),
                    str(intent["side"]),
                    float(intent["quantity"]),
                    float(intent["reference_price"]),
                    str(intent.get("reason") or ""),
                    _float(intent.get("market_score")),
                    intent["expires_at"],
                    _json(intent.get("request") or {}),
                ],
            )
        return True

    def list_pending_intents(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE status = ?" if status else ""
        params: list[Any] = [status] if status else []
        params.append(int(limit))
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT intent_id, plan_id, source_run_id, trading_mode, ts_code, name,
                       side, quantity, reference_price, status, reason, market_score,
                       expires_at, request_json, result_json, created_at, updated_at
                FROM pending_trade_intents
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_intent_row(row) for row in rows]

    def get_pending_intent(self, intent_id: str) -> dict[str, Any]:
        rows = self.list_pending_intents(limit=1000)
        return next((row for row in rows if row["intent_id"] == intent_id), {})

    def update_pending_intent(self, intent_id: str, *, status: str, result: dict[str, Any]) -> bool:
        self.storage.initialize()
        with self.storage.connect() as con:
            count = int(
                con.execute(
                    "SELECT COUNT(*) FROM pending_trade_intents WHERE intent_id = ?",
                    [intent_id],
                ).fetchone()[0]
            )
            if count:
                con.execute(
                    """
                    UPDATE pending_trade_intents
                    SET status = ?, result_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE intent_id = ?
                    """,
                    [status, _json(result), intent_id],
                )
        return bool(count)

    def reject_unselected_pending_buys(self, active_plan_ids: set[str]) -> int:
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                """
                SELECT intent_id, plan_id
                FROM pending_trade_intents
                WHERE status = 'pending' AND side = 'buy'
                """
            ).fetchall()
            rejected = 0
            for intent_id, plan_id in rows:
                if str(plan_id or "") in active_plan_ids:
                    continue
                con.execute(
                    """
                    UPDATE pending_trade_intents
                    SET status = 'rejected',
                        result_json = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE intent_id = ?
                    """,
                    [_json({"status": "rejected", "message": "候选已被有限换榜移出"}), intent_id],
                )
                rejected += 1
        return rejected

    def enqueue_review_request(
        self,
        request: dict[str, Any],
        *,
        cooldown_seconds: int = 300,
    ) -> dict[str, Any]:
        requested_at = request.get("requested_at") or datetime.now()
        cutoff = requested_at - timedelta(seconds=max(0, int(cooldown_seconds)))
        plan_id = str(request.get("plan_id") or "")
        trigger_type = str(request.get("trigger_type") or "review")
        reason = str(request.get("reason") or "")
        ts_code = str(request["ts_code"])
        self.storage.initialize()
        with self.storage.connect() as con:
            existing = con.execute(
                """
                SELECT request_id, status, requested_at
                FROM portfolio_review_requests
                WHERE ts_code = ?
                  AND COALESCE(plan_id, '') = ?
                  AND trigger_type = ?
                  AND reason = ?
                  AND requested_at >= ?
                ORDER BY requested_at DESC
                LIMIT 1
                """,
                [ts_code, plan_id, trigger_type, reason, cutoff],
            ).fetchone()
            if existing:
                return {
                    "created": False,
                    "request_id": existing[0],
                    "status": existing[1],
                    "requested_at": str(existing[2]),
                }
            con.execute(
                """
                INSERT INTO portfolio_review_requests
                    (request_id, trade_date, ts_code, name, plan_id, source_event_id,
                     reason, trigger_type, status, priority, price, snapshot_json,
                     result_json, requested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, '{}', ?)
                """,
                [
                    str(request["request_id"]),
                    str(request["trade_date"]),
                    ts_code,
                    str(request.get("name") or ""),
                    plan_id,
                    str(request.get("source_event_id") or ""),
                    reason,
                    trigger_type,
                    int(request.get("priority") or 0),
                    _float(request.get("price")),
                    _json(request.get("snapshot") or {}),
                    requested_at,
                ],
            )
        return {"created": True, "request_id": str(request["request_id"]), "status": "pending"}

    def list_review_requests(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE status = ?" if status else ""
        params: list[Any] = [status] if status else []
        params.append(int(limit))
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT request_id, trade_date, ts_code, name, plan_id, source_event_id,
                       reason, trigger_type, status, priority, price, snapshot_json,
                       result_json, requested_at, processed_at
                FROM portfolio_review_requests
                {where}
                ORDER BY priority DESC, requested_at ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_review_request_row(row) for row in rows]

    def update_review_request(self, request_id: str, *, status: str, result: dict[str, Any]) -> bool:
        self.storage.initialize()
        with self.storage.connect() as con:
            count = int(
                con.execute(
                    "SELECT COUNT(*) FROM portfolio_review_requests WHERE request_id = ?",
                    [request_id],
                ).fetchone()[0]
            )
            if count:
                con.execute(
                    """
                    UPDATE portfolio_review_requests
                    SET status = ?, result_json = ?, processed_at = CURRENT_TIMESTAMP
                    WHERE request_id = ?
                    """,
                    [status, _json(result), request_id],
                )
        return bool(count)

    def upsert_daily_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO portfolio_daily_snapshots
                    (snapshot_id, trade_date, trading_mode, account_id,
                     opening_total_asset, closing_total_asset, cash, market_value,
                     realized_pnl, unrealized_pnl, max_drawdown_pct, report_path,
                     summary_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (trade_date, trading_mode, account_id) DO UPDATE SET
                    closing_total_asset = excluded.closing_total_asset,
                    cash = excluded.cash,
                    market_value = excluded.market_value,
                    realized_pnl = excluded.realized_pnl,
                    unrealized_pnl = excluded.unrealized_pnl,
                    max_drawdown_pct = excluded.max_drawdown_pct,
                    report_path = excluded.report_path,
                    summary_json = excluded.summary_json,
                    updated_at = excluded.updated_at
                """,
                [
                    str(snapshot["snapshot_id"]),
                    str(snapshot["trade_date"]),
                    str(snapshot["trading_mode"]),
                    str(snapshot["account_id"]),
                    _float(snapshot.get("opening_total_asset")),
                    _float(snapshot.get("closing_total_asset")),
                    _float(snapshot.get("cash")),
                    _float(snapshot.get("market_value")),
                    _float(snapshot.get("realized_pnl")),
                    _float(snapshot.get("unrealized_pnl")),
                    _float(snapshot.get("max_drawdown_pct")),
                    str(snapshot.get("report_path") or ""),
                    _json(snapshot.get("summary") or {}),
                    datetime.now(),
                    datetime.now(),
                ],
            )


def _candidate_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "candidate_id": row[0],
        "plan_id": row[1],
        "run_id": row[2],
        "trade_date": row[3],
        "effective_trade_date": row[4] or row[3],
        "ts_code": row[5],
        "name": row[6],
        "industry": row[7] or "",
        "rank_no": int(row[8]),
        "selected": bool(row[9]),
        "status": row[10],
        "total_score": float(row[11]),
        "entry_price": _float(row[12]),
        "stop_loss": _float(row[13]),
        "take_profit_1": _float(row[14]),
        "take_profit_2": _float(row[15]),
        "trailing_stop_pct": _float(row[16]),
        "valid_until": row[17] or "",
        "score_components": json.loads(row[18] or "{}"),
        "evidence": json.loads(row[19] or "{}"),
        "outcome": json.loads(row[20] or "{}"),
        "created_at": str(row[21]),
        "updated_at": str(row[22]),
    }


def _intent_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "intent_id": row[0],
        "plan_id": row[1] or "",
        "source_run_id": row[2] or "",
        "trading_mode": row[3],
        "ts_code": row[4],
        "name": row[5],
        "side": row[6],
        "quantity": float(row[7]),
        "reference_price": float(row[8]),
        "status": row[9],
        "reason": row[10] or "",
        "market_score": _float(row[11]),
        "expires_at": str(row[12]),
        "request": json.loads(row[13] or "{}"),
        "result": json.loads(row[14] or "{}"),
        "created_at": str(row[15]),
        "updated_at": str(row[16]),
    }


def _review_request_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "request_id": row[0],
        "trade_date": row[1],
        "ts_code": row[2],
        "name": row[3],
        "plan_id": row[4] or "",
        "source_event_id": row[5] or "",
        "reason": row[6] or "",
        "trigger_type": row[7] or "",
        "status": row[8],
        "priority": int(row[9] or 0),
        "price": _float(row[10]),
        "snapshot": json.loads(row[11] or "{}"),
        "result": json.loads(row[12] or "{}"),
        "requested_at": str(row[13]),
        "processed_at": str(row[14]) if row[14] is not None else "",
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
