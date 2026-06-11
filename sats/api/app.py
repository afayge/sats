from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
import pandas as pd
from pydantic import BaseModel, ConfigDict

from sats.config import Settings, load_settings
from sats.data.astock_provider import AStockDataProvider
from sats.screening.base import ScreeningResult
from sats.screening.registry import get_rule, list_rules
from sats.screening.service import evaluate_and_store
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import parse_symbol_csv

DEFAULT_RULE = "ma_volume_relative_strength"


def _rule_required_trade_days(rule) -> int | None:
    value = getattr(rule, "required_trade_days", None)
    if value is None:
        return None
    return max(1, int(value))


class ScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trade_date: str
    rule: str = DEFAULT_RULE


def create_app(settings: Settings | None = None, storage: DuckDBStorage | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()
    resolved_storage = storage or DuckDBStorage(resolved_settings.db_path)

    app = FastAPI(title="SATS", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        rules = "".join(f"<li>{rule}</li>" for rule in list_rules())
        return f"""
        <!doctype html>
        <html lang="zh-CN">
          <head>
            <meta charset="utf-8" />
            <title>SATS</title>
            <style>
              body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 40px; }}
              code, pre {{ background: #f5f5f5; padding: 2px 4px; border-radius: 4px; }}
              section {{ max-width: 860px; }}
            </style>
          </head>
          <body>
            <section>
              <h1>SATS A股筛选管理页</h1>
              <p>当前实现的筛选规则：</p>
              <ul>{rules}</ul>
              <p>API: <code>POST /api/screen</code>，查询: <code>GET /api/screen/results</code></p>
              <p>分钟K: <code>GET /api/market/minute-k</code></p>
            </section>
          </body>
        </html>
        """

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "sats"}

    @app.post("/api/screen")
    def run_screen(request: ScreenRequest) -> dict[str, Any]:
        try:
            rule = get_rule(request.rule)
            rule_name = rule.name
            provider = AStockDataProvider(resolved_settings)
            load_kwargs = {"storage": resolved_storage, "rule_name": rule_name}
            required_trade_days = _rule_required_trade_days(rule)
            if required_trade_days is not None:
                load_kwargs["trade_days"] = required_trade_days
            inputs = provider.load_all_screening_inputs(request.trade_date, **load_kwargs)
            if not inputs:
                raise ValueError("No active A-share symbols returned by AStock provider")
            results = evaluate_and_store(inputs, rule_name=rule_name, storage=resolved_storage)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _screen_summary(request.trade_date, rule_name, len(inputs), results)

    @app.get("/api/screen/results")
    def list_screen_results(
        trade_date: str | None = Query(None),
        rule: str | None = Query(None),
        passed: bool | None = Query(None),
    ) -> dict[str, Any]:
        try:
            rule_name = get_rule(rule).name if rule else None
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rows = resolved_storage.list_screening_results(
            trade_date=trade_date,
            rule_name=rule_name,
            passed=passed,
        )
        return {"count": len(rows), "results": rows}

    @app.get("/api/market/minute-k")
    def minute_k(
        symbols: str = Query(..., description="Comma-separated symbols"),
        period: str = Query("1m"),
        mode: str = Query("realtime", pattern="^(realtime|history)$"),
        count: int | None = Query(None),
        start_date: str | None = Query(None),
        end_date: str | None = Query(None),
    ) -> dict[str, Any]:
        try:
            provider = AStockDataProvider(resolved_settings)
            symbol_list = parse_symbol_csv(symbols)
            if mode == "realtime":
                frame = provider.load_realtime_minute_klines(
                    symbol_list,
                    period=period,
                    count=count,
                )
            else:
                frame = provider.load_historical_minute_klines(
                    symbol_list,
                    period=period,
                    start_time=start_date,
                    end_time=end_date,
                    count=count,
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"count": len(frame), "results": _frame_records(frame)}

    return app


def _screen_summary(
    trade_date: str,
    rule: str,
    total_count: int,
    results: list[ScreeningResult],
) -> dict[str, Any]:
    passed_results = [item.to_dict() for item in results if item.passed]
    return {
        "trade_date": trade_date,
        "rule": rule,
        "universe": "all_a_shares",
        "total_count": total_count,
        "evaluated_count": len(results),
        "passed_count": len(passed_results),
        "passed_results": passed_results,
    }


def _frame_records(frame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    data = frame.where(pd.notna(frame), None)
    return data.to_dict("records")
