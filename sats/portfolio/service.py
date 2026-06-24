from __future__ import annotations

import hashlib
import math
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from sats.analysis.market_llm_context import get_a_share_market_context
from sats.analysis.opportunity_discovery import run_opportunity_discovery
from sats.analysis.trading_committee import run_trading_committee
from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.portfolio.execution import PaperBroker
from sats.portfolio.models import MarketRegime, PortfolioCandidate, PortfolioConfig, PortfolioRunResult
from sats.portfolio.reporting import write_portfolio_daily_report
from sats.portfolio.storage import PortfolioStore
from sats.storage.duckdb import DuckDBStorage
from sats.trading import broker_from_settings
from sats.trading.models import OrderRequest


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
RATING_SCORES = {"Buy": 95.0, "Overweight": 78.0, "Hold": 55.0, "Underweight": 30.0, "Sell": 10.0}
PORTFOLIO_PHASES = {
    "morning",
    "morning-final",
    "review",
    "afternoon-scan",
    "afternoon-buy",
    "plan-finalize",
    "recheck",
    "report",
}
PHASE_ALIASES = {"scan": "afternoon-buy", "close": "report"}
DISCOVERY_PHASES = {"afternoon-scan", "afternoon-buy"}
EXIT_PHASES = {"morning", "morning-final", "review", "afternoon-buy", "plan-finalize", "recheck"}
BUY_PHASES = {"afternoon-buy"}
ACTIVE_PLAN_PHASES = {"afternoon-buy", "plan-finalize"}


class DailyPortfolioAgent:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: DuckDBStorage,
        provider: AStockDataProvider | None = None,
        discovery_runner: Callable[..., Any] = run_opportunity_discovery,
        committee_runner: Callable[..., Any] = run_trading_committee,
        market_loader: Callable[..., dict[str, Any]] = get_a_share_market_context,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.store = PortfolioStore(storage)
        self.provider = provider or AStockDataProvider(settings)
        self.discovery_runner = discovery_runner
        self.committee_runner = committee_runner
        self.market_loader = market_loader
        self.now = now or (lambda: datetime.now(SHANGHAI_TZ))

    def run(
        self,
        *,
        phase: str = "scan",
        trade_date: str | None = None,
        config: PortfolioConfig | None = None,
        progress: Any | None = None,
    ) -> PortfolioRunResult:
        cfg = (config or PortfolioConfig()).normalized()
        requested_phase = str(phase or "afternoon-buy").strip().lower()
        phase = PHASE_ALIASES.get(requested_phase, requested_phase)
        if phase not in PORTFOLIO_PHASES:
            raise ValueError(
                "portfolio phase must be one of "
                + ", ".join(sorted(PORTFOLIO_PHASES | set(PHASE_ALIASES)))
            )
        alias_note = (
            f"phase {requested_phase} 是兼容别名，已按 {phase} 执行。"
            if requested_phase != phase
            else ""
        )
        current = self.now()
        requested_date = trade_date or current.strftime("%Y%m%d")
        run_id = f"portfolio_run_{uuid.uuid4().hex[:16]}"
        self.store.insert_run(
            {
                "run_id": run_id,
                "trade_date": requested_date,
                "phase": phase,
                "trading_mode": cfg.trading_mode,
                "status": "running",
                "started_at": current,
            }
        )
        try:
            if not self.is_trading_day(requested_date):
                regime = MarketRegime(
                    trade_date=requested_date,
                    score=0.0,
                    exposure_limit=0.0,
                    buy_allowed=False,
                    data_source="trade_calendar",
                    details={"reason": "非交易日"},
                )
                return self._finish(
                    run_id,
                    phase=phase,
                    config=cfg,
                    regime=regime,
                    candidates=[],
                    actions=[],
                    status="skipped",
                    message=f"{requested_date} 非交易日，已跳过",
                )

            market_payload = self._market_payload(requested_date)
            regime = self._market_regime(market_payload, config=cfg, run_id=run_id)
            if phase == "report":
                report_path = self._write_daily_report(
                    run_id=run_id,
                    trade_date=requested_date,
                    config=cfg,
                    regime=regime,
                )
                self._update_outcomes(requested_date)
                return self._finish(
                    run_id,
                    phase=phase,
                    config=cfg,
                    regime=regime,
                    candidates=[],
                    actions=[],
                    status="done",
                    message=(alias_note + " " if alias_note else "") + f"交易日总结报告已生成: {report_path}",
                    report_path=report_path,
                )

            if phase not in DISCOVERY_PHASES:
                candidates = self.store.latest_selected_candidates(requested_date)
                actions = self._execute(
                    run_id=run_id,
                    trade_date=requested_date,
                    phase=phase,
                    config=cfg,
                    regime=regime,
                    candidates=candidates,
                    allow_buys=False,
                    allow_exits=phase in EXIT_PHASES,
                )
                if phase == "recheck":
                    review_actions = self._close_pending_review_requests(
                        trade_date=requested_date,
                        actions=actions,
                    )
                    actions.extend(review_actions)
                return self._finish(
                    run_id,
                    phase=phase,
                    config=cfg,
                    regime=regime,
                    candidates=[],
                    actions=actions,
                    status="done",
                    message=(
                        (alias_note + " " if alias_note else "")
                        + f"{phase} 复核完成，产生 {len(actions)} 个动作；未执行新增买入"
                    ),
                )

            discovery = self.discovery_runner(
                settings=self.settings,
                storage=self.storage,
                provider=self.provider,
                trade_date=requested_date,
                signals="short_up",
                limit=cfg.candidate_limit,
                candidate_limit=max(50, cfg.candidate_limit),
                report=False,
                llm_enabled=False,
                hot_sector_enabled=True,
                market_dimensions=("core_indices", "market_breadth", "limit_sentiment", "hot_sectors"),
                market_horizons=("today", "tomorrow"),
                market_plan_source="daily_portfolio_agent",
                progress=progress,
            )
            source_candidates = list(getattr(discovery, "candidates", []) or [])[: cfg.candidate_limit]
            if not source_candidates:
                return self._finish(
                    run_id,
                    phase=phase,
                    config=cfg,
                    regime=regime,
                    candidates=[],
                    actions=[],
                    status="partial",
                    message="机会发现未产生可交易候选",
                )
            symbols = [str(_value(item, "ts_code") or "") for item in source_candidates]
            committee = self.committee_runner(
                symbols,
                trade_date=requested_date,
                settings=self.settings,
                storage=self.storage,
                astock_provider=self.provider,
                debate_rounds=1,
                risk_rounds=1,
                llm_enabled=cfg.llm_enabled,
                report=False,
                progress=progress,
            )
            ratings = {
                str(report.ts_code): {
                    "rating": str(report.final_rating or "Hold"),
                    "final_decision": str(report.final_decision or ""),
                    "risk_debate": str(report.risk_debate or ""),
                }
                for report in getattr(committee, "reports", ()) or ()
            }
            quotes = self._quotes(symbols)
            stock_basic = self._stock_basic()
            ranked = self._rank_candidates(
                run_id=run_id,
                trade_date=requested_date,
                source_candidates=source_candidates,
                ratings=ratings,
                quotes=quotes,
                stock_basic=stock_basic,
                regime=regime,
                config=cfg,
            )
            selected_codes, replacements = self._select_with_limited_replacements(
                ranked,
                trade_date=requested_date,
                config=cfg,
            )
            stored: list[PortfolioCandidate] = []
            for index, item in enumerate(ranked, start=1):
                selected = item["ts_code"] in selected_codes
                plan = self._candidate_plan(
                    run_id=run_id,
                    trade_date=requested_date,
                    rank_no=index,
                    selected=selected,
                    activate_selected=phase in ACTIVE_PLAN_PHASES,
                    item=item,
                    config=cfg,
                )
                stored.append(plan)
            self.store.insert_candidates([item.to_dict() for item in stored])
            if cfg.trading_mode == "live":
                self.store.reject_unselected_pending_buys(
                    {item.plan_id for item in stored if item.selected}
                )
            actions = self._execute(
                run_id=run_id,
                trade_date=requested_date,
                phase=phase,
                config=cfg,
                regime=regime,
                candidates=[item.to_dict() for item in stored if item.selected],
                allow_buys=phase in BUY_PHASES,
                allow_exits=phase in EXIT_PHASES,
            )
            return self._finish(
                run_id,
                phase=phase,
                config=cfg,
                regime=regime,
                candidates=stored,
                actions=actions,
                replacements=replacements,
                status="done",
                message=(
                    (alias_note + " " if alias_note else "")
                    + f"完成 {len(stored)} 只候选评审，选中 {len(selected_codes)} 只"
                    + ("，已允许模拟/实盘买入流程" if phase in BUY_PHASES else "，本阶段不执行新增买入")
                ),
            )
        except Exception as exc:
            self.store.update_run(
                run_id,
                status="failed",
                summary=str(exc),
                details_json={"error": str(exc)},
                finished_at=self.now(),
            )
            raise

    def is_trading_day(self, trade_date: str) -> bool:
        try:
            payload = self.provider.fetch_data_operation(
                "tushare.dataset.fetch",
                {
                    "dataset": "trade_cal",
                    "params": {
                        "exchange": "SSE",
                        "start_date": trade_date,
                        "end_date": trade_date,
                    },
                },
                limit=10,
                storage=self.storage,
            )
            rows = list(payload.get("data") or payload.get("rows") or [])
            if rows:
                return any(str(row.get("cal_date") or "") == trade_date and int(row.get("is_open") or 0) == 1 for row in rows)
        except Exception:
            pass
        try:
            return datetime.strptime(trade_date, "%Y%m%d").weekday() < 5
        except ValueError:
            raise ValueError("trade_date must be YYYYMMDD")

    def status(self, *, mode: str = "paper") -> dict[str, Any]:
        runs = self.store.list_runs(limit=1)
        payload = {
            "trading_mode": mode,
            "latest_run": runs[0] if runs else {},
            "market_regime": self.store.latest_market_regime(),
            "pending_intents": len(self.store.list_pending_intents(status="pending")),
        }
        if mode == "paper":
            payload["account"] = self.store.paper_account("default")
            payload["positions"] = self.store.paper_positions("default")
            payload["performance"] = self.store.performance_summary("default")
        return payload

    def approve_live_intent(self, intent_id: str, *, client: Any | None = None) -> dict[str, Any]:
        intent = self.store.get_pending_intent(intent_id)
        if not intent:
            raise ValueError(f"未找到实盘待确认委托: {intent_id}")
        if intent["status"] != "pending":
            raise ValueError(f"委托状态不是 pending: {intent['status']}")
        expires_at = _parse_timestamp(intent["expires_at"])
        if expires_at is None or self.now().replace(tzinfo=None) > expires_at.replace(tzinfo=None):
            result = {"status": "expired", "message": "待确认委托已过期"}
            self.store.update_pending_intent(intent_id, status="expired", result=result)
            return result
        plan = self.store.get_plan(intent["plan_id"])
        if not plan or str(plan.get("status") or "") != "active":
            result = {"status": "rejected", "message": "交易计划已失效、未入选或停用"}
            self.store.update_pending_intent(intent_id, status="rejected", result=result)
            return result
        if str(plan.get("valid_until") or "") and self.now().strftime("%Y%m%d") > str(plan.get("valid_until")):
            result = {"status": "rejected", "message": "交易计划已超过有效期"}
            self.store.update_pending_intent(intent_id, status="rejected", result=result)
            return result
        quote = self._quotes([intent["ts_code"]]).get(intent["ts_code"]) or {}
        price = _quote_price(quote)
        if price <= 0 or not _tradable_quote(quote, trade_date=self.now().strftime("%Y%m%d")):
            result = {"status": "rejected", "message": "缺少有效实时行情"}
            self.store.update_pending_intent(intent_id, status="rejected", result=result)
            return result
        drift = abs(price / float(intent["reference_price"]) - 1.0)
        max_drift = float((intent.get("request") or {}).get("max_price_drift_pct") or 0.005)
        if drift > max_drift:
            result = {"status": "rejected", "message": f"价格漂移 {drift:.2%} 超过 {max_drift:.2%}"}
            self.store.update_pending_intent(intent_id, status="rejected", result=result)
            return result
        if intent["side"] == "buy":
            regime = self.store.latest_market_regime()
            if float(regime.get("score") or 0.0) < 45.0:
                result = {"status": "rejected", "message": "大盘风控闸门已关闭新增买入"}
                self.store.update_pending_intent(intent_id, status="rejected", result=result)
                return result
        broker = client or broker_from_settings(self.settings)
        quantity = int(intent["quantity"])
        request_meta = intent.get("request") or {}
        if quantity <= 0:
            quantity = self._live_quantity(
                broker,
                ts_code=intent["ts_code"],
                side=intent["side"],
                price=price,
                request=request_meta,
            )
        request = OrderRequest(
            symbol=intent["ts_code"],
            side=intent["side"],
            quantity=quantity,
            price_type="latest",
            strategy="sats-daily-portfolio",
            source_event_id=intent_id,
        )
        try:
            order = broker.place_order(request)
            self.storage.insert_broker_order(
                {
                    "sats_order_id": order.sats_order_id,
                    "provider": broker.provider,
                    "account_id": broker.account_id,
                    "broker_order_id": order.broker_order_id,
                    "ts_code": request.symbol,
                    "side": request.side,
                    "quantity": request.quantity,
                    "price": request.price,
                    "price_type": request.price_type,
                    "status": order.status,
                    "message": order.message,
                    "request": request.to_dict(),
                    "response": order.raw,
                }
            )
            result = {"status": order.status or "submitted", "order": order.to_dict()}
            self.store.update_pending_intent(intent_id, status="submitted", result=result)
            return result
        except Exception as exc:
            result = {"status": "rejected", "message": str(exc)}
            self.store.update_pending_intent(intent_id, status="rejected", result=result)
            return result

    def reject_live_intent(self, intent_id: str) -> dict[str, Any]:
        intent = self.store.get_pending_intent(intent_id)
        if not intent:
            raise ValueError(f"未找到实盘待确认委托: {intent_id}")
        result = {"status": "rejected", "message": "用户已拒绝实盘委托"}
        self.store.update_pending_intent(intent_id, status="rejected", result=result)
        return result

    def _market_payload(self, trade_date: str) -> dict[str, Any]:
        return self.market_loader(
            settings=self.settings,
            trade_date=trade_date,
            horizons=("today", "tomorrow"),
            dimensions=("core_indices", "market_breadth", "limit_sentiment", "hot_sectors"),
            market_plan_source="daily_portfolio_agent",
            require_complete_market=False,
            astock_provider=self.provider,
        )

    def _market_regime(self, payload: dict[str, Any], *, config: PortfolioConfig, run_id: str) -> MarketRegime:
        score = 50.0
        index_changes = [
            _number((item.get("latest") or {}).get("pct_chg"))
            for item in payload.get("indices") or []
        ]
        valid_changes = [value for value in index_changes if value is not None]
        if valid_changes:
            score += max(-15.0, min(15.0, (sum(valid_changes) / len(valid_changes)) * 6.0))
        breadth = payload.get("market_breadth") or {}
        advancing = _number(breadth.get("advancing_count")) or 0.0
        declining = _number(breadth.get("declining_count")) or 0.0
        if advancing + declining > 0:
            score += ((advancing / (advancing + declining)) - 0.5) * 40.0
        median_change = _number(breadth.get("median_pct_chg"))
        if median_change is not None:
            score += max(-8.0, min(8.0, median_change * 4.0))
        sentiment = payload.get("limit_sentiment") or {}
        limit_up = _first_number(sentiment, ("limit_up_count", "up_count", "涨停数")) or 0.0
        limit_down = _first_number(sentiment, ("limit_down_count", "down_count", "跌停数")) or 0.0
        score += max(-10.0, min(10.0, (limit_up - limit_down) / 5.0))
        score -= min(10.0, len(payload.get("missing_fields") or []) * 1.5)
        score = round(max(0.0, min(100.0, score)), 2)
        if score >= 60.0:
            exposure = config.max_exposure
        elif score >= 45.0:
            exposure = min(config.reduced_exposure, config.max_exposure)
        else:
            exposure = 0.0
        regime = MarketRegime(
            trade_date=str(payload.get("trade_date") or ""),
            score=score,
            exposure_limit=round(exposure, 4),
            buy_allowed=score >= 45.0,
            data_source="+".join(str(value) for value in (payload.get("data_sources") or {}).values() if value)[:500],
            details=payload,
        )
        self.store.insert_market_regime(
            {
                "snapshot_id": f"market_{uuid.uuid4().hex[:16]}",
                "run_id": run_id,
                **regime.to_dict(),
            }
        )
        return regime

    def _rank_candidates(
        self,
        *,
        run_id: str,
        trade_date: str,
        source_candidates: list[Any],
        ratings: dict[str, dict[str, str]],
        quotes: dict[str, dict[str, Any]],
        stock_basic: dict[str, dict[str, Any]],
        regime: MarketRegime,
        config: PortfolioConfig,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for source in source_candidates:
            ts_code = str(_value(source, "ts_code") or "")
            basic = stock_basic.get(ts_code) or {}
            name = str(_value(source, "name") or basic.get("name") or "")
            if not ts_code or not name or "ST" in name.upper():
                continue
            quote = quotes.get(ts_code) or {}
            entry_price = _quote_price(quote) or float(_value(source, "close") or 0.0)
            if entry_price <= 0 or not _tradable_quote(quote, trade_date=trade_date):
                continue
            local_score = _clamp(_number(_value(source, "ranking_score")) or _number(_value(source, "local_score")) or 50.0)
            events = list(_value(source, "events") or [])
            confidence_values = [_number(item.get("confidence")) for item in events if isinstance(item, dict)]
            confidence = [value for value in confidence_values if value is not None]
            volume_money = _clamp(50.0 + ((sum(confidence) / len(confidence) - 0.5) * 50.0 if confidence else 0.0))
            rating_payload = ratings.get(ts_code) or {}
            debate_score = RATING_SCORES.get(str(rating_payload.get("rating") or "Hold"), 55.0)
            indicator = _value(source, "indicator") or {}
            factor_score = _number(((indicator.get("factor") or {}).get("score") if isinstance(indicator, dict) else None))
            fundamental = _clamp(50.0 + (factor_score or 0.0) * 10.0)
            missing = list(_value(source, "missing_fields") or [])
            completeness = _clamp(100.0 - len(missing) * 8.0)
            total = (
                local_score * 0.35
                + regime.score * 0.20
                + volume_money * 0.15
                + debate_score * 0.15
                + fundamental * 0.10
                + completeness * 0.05
            )
            rows.append(
                {
                    "run_id": run_id,
                    "trade_date": trade_date,
                    "ts_code": ts_code,
                    "name": name,
                    "industry": str(basic.get("industry") or "未分类"),
                    "entry_price": round(entry_price, 4),
                    "total_score": round(total, 4),
                    "score_components": {
                        "technical_factor": round(local_score, 2),
                        "market_sector": regime.score,
                        "volume_money": round(volume_money, 2),
                        "debate_risk": debate_score,
                        "fundamental_event": round(fundamental, 2),
                        "data_completeness": round(completeness, 2),
                    },
                    "evidence": {
                        "discovery": _candidate_evidence(source),
                        "committee": rating_payload,
                        "quote": quote,
                    },
                    "key_levels": _value(source, "key_levels") or {},
                    "indicator": indicator,
                }
            )
        return sorted(rows, key=lambda item: (-item["total_score"], item["ts_code"]))[: config.candidate_limit]

    def _select_with_limited_replacements(
        self,
        ranked: list[dict[str, Any]],
        *,
        trade_date: str,
        config: PortfolioConfig,
    ) -> tuple[set[str], int]:
        ideal = _diversified_selection(ranked, config.selected_limit)
        previous = self.store.latest_selected_candidates(trade_date)
        if not previous:
            return {item["ts_code"] for item in ideal}, 0
        by_code = {item["ts_code"]: item for item in ranked}
        held = self._held_symbols(config.trading_mode, config.account_id)
        previous_codes = {str(row["ts_code"]) for row in previous}
        selected = [by_code[row["ts_code"]] for row in previous if row["ts_code"] in by_code]
        replacements = 0
        for item in ideal:
            selected_codes = {row["ts_code"] for row in selected}
            if item["ts_code"] in selected_codes or len(selected) >= config.selected_limit:
                continue
            if replacements >= config.max_replacements:
                break
            if _industry_count(selected, item["industry"]) >= 2:
                continue
            selected.append(item)
            replacements += 1
        outsiders = [item for item in ideal if item["ts_code"] not in {row["ts_code"] for row in selected}]
        for outsider in outsiders:
            replaceable = [item for item in selected if item["ts_code"] not in held]
            if not replaceable or replacements >= config.max_replacements:
                break
            weakest = min(
                [
                    item
                    for item in replaceable
                    if item["ts_code"] in previous_codes
                    and (
                        item["industry"] == outsider["industry"]
                        or _industry_count(selected, outsider["industry"]) < 2
                    )
                ],
                key=lambda item: item["total_score"],
                default=None,
            )
            if weakest is None:
                continue
            if outsider["total_score"] < weakest["total_score"] + config.replacement_score_gap:
                continue
            selected.remove(weakest)
            selected.append(outsider)
            replacements += 1
        selected = _diversified_selection(
            sorted(selected, key=lambda item: -item["total_score"]),
            config.selected_limit,
        )
        return {item["ts_code"] for item in selected}, replacements

    def _candidate_plan(
        self,
        *,
        run_id: str,
        trade_date: str,
        rank_no: int,
        selected: bool,
        activate_selected: bool,
        item: dict[str, Any],
        config: PortfolioConfig,
    ) -> PortfolioCandidate:
        entry = float(item["entry_price"])
        atr = _atr(item.get("indicator") or {})
        support = _nearest_support(item.get("key_levels") or {}, entry)
        candidates = [entry * (1.0 - config.max_stop_loss_pct)]
        if atr and atr > 0:
            candidates.append(entry - atr * 2.0)
        if support and support < entry:
            candidates.append(support)
        stop = max(value for value in candidates if value > 0)
        stop = min(stop, entry * 0.995)
        risk = max(entry * 0.01, entry - stop)
        plan_key = f"{trade_date}:{item['ts_code']}"
        plan_id = f"portfolio_plan_{_stable_id(plan_key)}"
        effective_trade_date = self._future_trade_date(trade_date, 1)
        return PortfolioCandidate(
            candidate_id=f"portfolio_candidate_{uuid.uuid4().hex[:16]}",
            plan_id=plan_id,
            run_id=run_id,
            trade_date=trade_date,
            effective_trade_date=effective_trade_date,
            ts_code=item["ts_code"],
            name=item["name"],
            industry=item["industry"],
            rank_no=rank_no,
            selected=selected,
            status="active" if selected and activate_selected else "candidate",
            total_score=float(item["total_score"]),
            entry_price=round(entry, 4),
            stop_loss=round(stop, 4),
            take_profit_1=round(entry + risk * config.take_profit_1_r, 4),
            take_profit_2=round(entry + risk * config.take_profit_2_r, 4),
            trailing_stop_pct=config.trailing_stop_pct,
            valid_until=self._future_trade_date(trade_date, config.max_holding_trade_days),
            score_components=item["score_components"],
            evidence=item["evidence"],
        )

    def _execute(
        self,
        *,
        run_id: str,
        trade_date: str,
        phase: str,
        config: PortfolioConfig,
        regime: MarketRegime,
        candidates: list[dict[str, Any]],
        allow_buys: bool,
        allow_exits: bool,
    ) -> list[dict[str, Any]]:
        position_rows = (
            self.store.paper_positions(config.account_id)
            if config.trading_mode == "paper"
            else self.storage.list_broker_positions(provider="qmt")
        )
        symbols = {str(row.get("ts_code") or "") for row in position_rows}
        symbols.update(str(item.get("ts_code") or "") for item in candidates)
        quotes = self._quotes(sorted(symbols))
        actions: list[dict[str, Any]] = []
        if config.trading_mode == "paper":
            broker = PaperBroker(
                self.store,
                account_id=config.account_id,
                initial_cash=config.paper_initial_cash,
            )
            broker.refresh(quotes, trade_date=trade_date)
            positions = self.store.paper_positions(config.account_id)
            if allow_exits:
                actions.extend(
                    self._paper_exits(
                        broker,
                        positions=positions,
                        quotes=quotes,
                        trade_date=trade_date,
                        run_id=run_id,
                        config=config,
                        regime=regime,
                        phase=phase,
                    )
                )
            if allow_buys and regime.buy_allowed:
                held = {row["ts_code"] for row in self.store.paper_positions(config.account_id)}
                for plan in candidates:
                    if plan.get("ts_code") in held or str(plan.get("status") or "") == "disabled":
                        continue
                    quote = quotes.get(str(plan["ts_code"])) or {}
                    price = _quote_price(quote)
                    quantity = broker.buy_quantity(
                        ts_code=str(plan["ts_code"]),
                        price=price,
                        exposure_limit=regime.exposure_limit,
                        max_position_pct=config.max_position_pct,
                    )
                    if quantity <= 0:
                        continue
                    action = broker.place_order(
                        plan_id=str(plan["plan_id"]),
                        source_run_id=run_id,
                        ts_code=str(plan["ts_code"]),
                        name=str(plan.get("name") or ""),
                        side="buy",
                        quantity=quantity,
                        price=price,
                        trade_date=trade_date,
                        trade_time=self.now().strftime("%Y-%m-%d %H:%M:%S"),
                        reason=f"组合入选，市场评分 {regime.score:.1f}",
                        quote=quote,
                    )
                    actions.append(action)
                    if action.get("status") == "filled":
                        held.add(str(plan["ts_code"]))
            broker.refresh(quotes, trade_date=trade_date)
            return actions

        actions.extend(
            self._live_exit_intents(
                positions=position_rows,
                quotes=quotes,
                trade_date=trade_date,
                run_id=run_id,
                config=config,
                regime=regime,
            )
            if allow_exits
            else []
        )
        if allow_buys and regime.buy_allowed:
            held = {row.get("ts_code") for row in position_rows}
            for plan in candidates:
                if plan.get("ts_code") in held or str(plan.get("status") or "") == "disabled":
                    continue
                action = self._create_live_intent(
                    run_id=run_id,
                    plan=plan,
                    side="buy",
                    quantity=0,
                    reference_price=_quote_price(quotes.get(str(plan["ts_code"])) or {}),
                    reason=f"组合入选，市场评分 {regime.score:.1f}",
                    market_score=regime.score,
                    config=config,
                    request={"target_position_pct": config.max_position_pct},
                )
                if action:
                    actions.append(action)
        return actions

    def _paper_exits(
        self,
        broker: PaperBroker,
        *,
        positions: list[dict[str, Any]],
        quotes: dict[str, dict[str, Any]],
        trade_date: str,
        run_id: str,
        config: PortfolioConfig,
        regime: MarketRegime,
        phase: str,
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        trades = self.store.paper_trades(config.account_id, limit=1000)
        for position in positions:
            plan = self.store.get_plan(str(position.get("plan_id") or ""))
            if not plan:
                continue
            quote = quotes.get(position["ts_code"]) or {}
            price = _quote_price(quote)
            if price <= 0:
                continue
            reason, ratio = self._exit_signal(
                position,
                plan,
                price=price,
                trade_date=trade_date,
                trades=trades,
                regime=regime,
                phase=phase,
            )
            if not reason:
                continue
            available = int(position.get("available_quantity") or 0)
            if available <= 0:
                continue
            quantity = available if ratio >= 1.0 else max(100, int((available * ratio) // 100) * 100)
            quantity = min(available, quantity)
            actions.append(
                broker.place_order(
                    plan_id=str(plan["plan_id"]),
                    source_run_id=run_id,
                    ts_code=position["ts_code"],
                    name=position["name"],
                    side="sell",
                    quantity=quantity,
                    price=price,
                    trade_date=trade_date,
                    trade_time=self.now().strftime("%Y-%m-%d %H:%M:%S"),
                    reason=reason,
                    quote=quote,
                )
            )
        return actions

    def _live_exit_intents(
        self,
        *,
        positions: list[dict[str, Any]],
        quotes: dict[str, dict[str, Any]],
        trade_date: str,
        run_id: str,
        config: PortfolioConfig,
        regime: MarketRegime,
    ) -> list[dict[str, Any]]:
        actions = []
        for position in positions:
            plan = self.store.get_plan(str(position.get("plan_id") or "")) or self.store.latest_plan_for_symbol(
                str(position.get("ts_code") or "")
            )
            if not plan:
                continue
            price = _quote_price(quotes.get(str(position.get("ts_code"))) or {})
            reason, ratio = self._exit_signal(
                position,
                plan,
                price=price,
                trade_date=trade_date,
                trades=[],
                regime=regime,
                phase="live-review",
            )
            if not reason:
                continue
            action = self._create_live_intent(
                run_id=run_id,
                plan=plan,
                side="sell",
                quantity=0,
                reference_price=price,
                reason=reason,
                market_score=regime.score,
                config=config,
                request={"sell_ratio": ratio},
            )
            if action:
                actions.append(action)
        return actions

    def _exit_signal(
        self,
        position: dict[str, Any],
        plan: dict[str, Any],
        *,
        price: float,
        trade_date: str,
        trades: list[dict[str, Any]],
        regime: MarketRegime,
        phase: str,
    ) -> tuple[str, float]:
        if price <= 0:
            return "", 0.0
        if price <= float(plan.get("stop_loss") or 0.0):
            return "触发止损", 1.0
        if price >= float(plan.get("take_profit_2") or math.inf):
            return "触发 2.5R 止盈", 1.0
        tp1_done = any(
            row.get("plan_id") == plan.get("plan_id")
            and row.get("side") == "sell"
            and "1.5R" in str(row.get("reason") or "")
            for row in trades
        )
        if not tp1_done and price >= float(plan.get("take_profit_1") or math.inf):
            return "触发 1.5R 分批止盈", 0.5
        peak = float(position.get("peak_price") or price)
        trailing = float(plan.get("trailing_stop_pct") or 0.06)
        if peak > 0 and price <= peak * (1.0 - trailing) and price > float(plan.get("entry_price") or 0.0):
            return "触发移动止盈", 1.0
        entry = float(plan.get("entry_price") or 0.0)
        if phase in {"morning", "morning-final", "review", "recheck"} and regime.score < 35.0 and entry > 0 and price < entry:
            return f"大盘风险评分 {regime.score:.1f} 极弱且跌破入场价，风险退出", 1.0
        opened = str(position.get("opened_trade_date") or plan.get("trade_date") or "")
        if opened and self._trading_day_distance(opened, trade_date) >= 4:
            return "持有达到第 4 个交易日，执行时间止损", 1.0
        return "", 0.0

    def _create_live_intent(
        self,
        *,
        run_id: str,
        plan: dict[str, Any],
        side: str,
        quantity: int,
        reference_price: float,
        reason: str,
        market_score: float,
        config: PortfolioConfig,
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        if reference_price <= 0:
            return None
        intent = {
            "intent_id": f"trade_intent_{uuid.uuid4().hex[:16]}",
            "plan_id": str(plan.get("plan_id") or ""),
            "source_run_id": run_id,
            "ts_code": str(plan.get("ts_code") or ""),
            "name": str(plan.get("name") or ""),
            "side": side,
            "quantity": quantity,
            "reference_price": reference_price,
            "reason": reason,
            "market_score": market_score,
            "expires_at": self.now() + timedelta(seconds=config.live_intent_ttl_seconds),
            "request": {
                **request,
                "max_position_pct": config.max_position_pct,
                "max_exposure": config.max_exposure,
                "max_price_drift_pct": config.live_price_drift_pct,
            },
        }
        if not self.store.insert_pending_intent(intent):
            return None
        return {**intent, "status": "pending"}

    def _live_quantity(self, broker: Any, *, ts_code: str, side: str, price: float, request: dict[str, Any]) -> int:
        if side == "buy":
            asset = broker.asset()
            target_pct = float(request.get("target_position_pct") or request.get("max_position_pct") or 0.14)
            exposure = float(request.get("max_exposure") or 0.70)
            portfolio_room = max(0.0, float(asset.total_asset or 0.0) * exposure - float(asset.market_value or 0.0))
            budget = min(
                float(asset.available_cash or 0.0),
                float(asset.total_asset or 0.0) * target_pct,
                portfolio_room,
            )
            quantity = int(budget // (price * 100)) * 100
        else:
            available = 0.0
            for position in broker.positions():
                if position.ts_code == ts_code:
                    available = float(position.available_quantity or position.quantity or 0.0)
                    break
            quantity = int(available * float(request.get("sell_ratio") or 1.0))
        if quantity <= 0:
            raise ValueError("审批时计算的委托数量为 0")
        return quantity

    def _quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        clean = sorted({symbol for symbol in symbols if symbol})
        if not clean:
            return {}
        frame = self.provider.load_realtime_quotes(symbols=clean)
        if frame is None or frame.empty or "ts_code" not in frame.columns:
            return {}
        return {
            str(row["ts_code"]): row.dropna().to_dict()
            for _, row in frame.iterrows()
        }

    def _stock_basic(self) -> dict[str, dict[str, Any]]:
        frame = self.storage.get_stock_basic()
        if frame is None or frame.empty:
            return {}
        return {str(row["ts_code"]): row.dropna().to_dict() for _, row in frame.iterrows()}

    def _held_symbols(self, mode: str, account_id: str) -> set[str]:
        if mode == "paper":
            return {row["ts_code"] for row in self.store.paper_positions(account_id)}
        return {str(row.get("ts_code") or "") for row in self.storage.list_broker_positions(provider="qmt")}

    def _future_trade_date(self, start_date: str, count: int) -> str:
        end_date = (datetime.strptime(start_date, "%Y%m%d") + timedelta(days=max(10, count * 4))).strftime("%Y%m%d")
        try:
            payload = self.provider.fetch_data_operation(
                "tushare.dataset.fetch",
                {
                    "dataset": "trade_cal",
                    "params": {
                        "exchange": "SSE",
                        "start_date": start_date,
                        "end_date": end_date,
                        "is_open": "1",
                    },
                },
                limit=30,
                storage=self.storage,
            )
            dates = sorted(
                str(row.get("cal_date") or "")
                for row in payload.get("data") or payload.get("rows") or []
                if int(row.get("is_open") or 0) == 1 and str(row.get("cal_date") or "") >= start_date
            )
            if len(dates) > count:
                return dates[count]
        except Exception:
            pass
        current = datetime.strptime(start_date, "%Y%m%d")
        remaining = count
        while remaining > 0:
            current += timedelta(days=1)
            if current.weekday() < 5:
                remaining -= 1
        return current.strftime("%Y%m%d")

    def _trading_day_distance(self, start_date: str, end_date: str) -> int:
        if not start_date or not end_date or end_date <= start_date:
            return 0
        try:
            payload = self.provider.fetch_data_operation(
                "tushare.dataset.fetch",
                {
                    "dataset": "trade_cal",
                    "params": {
                        "exchange": "SSE",
                        "start_date": start_date,
                        "end_date": end_date,
                        "is_open": "1",
                    },
                },
                limit=30,
                storage=self.storage,
            )
            dates = {
                str(row.get("cal_date") or "")
                for row in payload.get("data") or payload.get("rows") or []
                if int(row.get("is_open") or 0) == 1
            }
            return len([date for date in dates if start_date < date <= end_date])
        except Exception:
            start = datetime.strptime(start_date, "%Y%m%d")
            end = datetime.strptime(end_date, "%Y%m%d")
            count = 0
            while start < end:
                start += timedelta(days=1)
                if start.weekday() < 5:
                    count += 1
            return count

    def _update_outcomes(self, trade_date: str) -> None:
        rows = self.store.list_candidates(selected=True, limit=500)
        symbols = sorted({row["ts_code"] for row in rows})
        quotes = self._quotes(symbols)
        for row in rows:
            days = self._trading_day_distance(row["trade_date"], trade_date)
            if days not in {1, 2, 4}:
                continue
            price = _quote_price(quotes.get(row["ts_code"]) or {})
            entry = float(row.get("entry_price") or 0.0)
            if price <= 0 or entry <= 0:
                continue
            outcome = dict(row.get("outcome") or {})
            outcome[f"return_{days}d_pct"] = round((price / entry - 1.0) * 100.0, 4)
            outcome[f"price_{days}d"] = price
            self.store.update_candidate_outcome(row["candidate_id"], outcome)

    def _write_daily_report(
        self,
        *,
        run_id: str,
        trade_date: str,
        config: PortfolioConfig,
        regime: MarketRegime,
    ) -> str:
        account: dict[str, Any] = {"account_id": config.account_id}
        positions: list[dict[str, Any]]
        performance: dict[str, Any] = {}
        if config.trading_mode == "paper":
            broker = PaperBroker(
                self.store,
                account_id=config.account_id,
                initial_cash=config.paper_initial_cash,
            )
            positions = self.store.paper_positions(config.account_id)
            quotes = self._quotes([str(row["ts_code"]) for row in positions])
            broker.refresh(quotes, trade_date=trade_date)
            account = self.store.paper_account(config.account_id)
            positions = self.store.paper_positions(config.account_id)
            performance = self.store.performance_summary(config.account_id)
        else:
            positions = self.storage.list_broker_positions(provider="qmt")

        orders = (
            self.store.paper_orders(config.account_id, trade_date=trade_date, limit=10000)
            if config.trading_mode == "paper"
            else []
        )
        trades = (
            self.store.paper_trades(config.account_id, trade_date=trade_date, limit=10000)
            if config.trading_mode == "paper"
            else []
        )
        candidates = self.store.list_candidates(trade_date=trade_date, limit=200)
        plans_by_id = {
            str(plan.get("plan_id") or ""): plan
            for plan in self.store.list_candidates(limit=5000)
            if str(plan.get("plan_id") or "")
        }
        pending_intents = self.store.list_pending_intents(limit=1000)
        review_requests = self.store.list_review_requests(limit=1000)
        root = Path(getattr(self.settings, "project_root", "") or self.storage.db_path.parent)
        report_path = (
            root
            / "reports"
            / "portfolio"
            / trade_date
            / f"portfolio_daily_{config.trading_mode}_{config.account_id}.md"
        )
        unrealized = sum(float(row.get("pnl") or 0.0) for row in positions)
        summary = {
            "run_id": run_id,
            "orders": len(orders),
            "trades": len(trades),
            "positions": len(positions),
            "review_requests": len(review_requests),
        }
        payload = {
            "trade_date": trade_date,
            "trading_mode": config.trading_mode,
            "account_id": config.account_id,
            "market_regime": regime.to_dict(),
            "account": account,
            "positions": positions,
            "orders": orders,
            "trades": trades,
            "candidates": candidates,
            "plans_by_id": plans_by_id,
            "pending_intents": pending_intents,
            "review_requests": review_requests,
            "performance": performance,
            "warnings": [],
        }
        write_portfolio_daily_report(report_path, payload)
        self.store.upsert_daily_snapshot(
            {
                "snapshot_id": f"portfolio_daily_{_stable_id(f'{trade_date}:{config.trading_mode}:{config.account_id}')}",
                "trade_date": trade_date,
                "trading_mode": config.trading_mode,
                "account_id": config.account_id,
                "opening_total_asset": account.get("initial_cash") or account.get("total_asset"),
                "closing_total_asset": account.get("total_asset"),
                "cash": account.get("cash"),
                "market_value": account.get("market_value"),
                "realized_pnl": account.get("realized_pnl"),
                "unrealized_pnl": unrealized,
                "max_drawdown_pct": account.get("max_drawdown_pct"),
                "report_path": str(report_path),
                "summary": summary,
            }
        )
        return str(report_path)

    def _close_pending_review_requests(self, *, trade_date: str, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        requests = self.store.list_review_requests(status="pending", limit=100)
        if not requests:
            return []
        result = {
            "trade_date": trade_date,
            "actions": actions,
            "message": "Portfolio recheck completed",
        }
        closed: list[dict[str, Any]] = []
        for request in requests:
            self.store.update_review_request(str(request["request_id"]), status="processed", result=result)
            closed.append(
                {
                    "status": "processed",
                    "request_id": request["request_id"],
                    "ts_code": request["ts_code"],
                    "name": request["name"],
                    "side": "review",
                    "reason": request["reason"],
                }
            )
        return closed

    def _finish(
        self,
        run_id: str,
        *,
        phase: str,
        config: PortfolioConfig,
        regime: MarketRegime,
        candidates: list[PortfolioCandidate],
        actions: list[dict[str, Any]],
        status: str,
        message: str,
        replacements: int = 0,
        report_path: str = "",
    ) -> PortfolioRunResult:
        selected_count = sum(1 for item in candidates if item.selected)
        self.store.update_run(
            run_id,
            status=status,
            market_score=regime.score,
            exposure_limit=regime.exposure_limit,
            candidate_count=len(candidates),
            selected_count=selected_count,
            replacement_count=replacements,
            summary=message,
            details_json={"actions": actions, "report_path": report_path},
            finished_at=self.now(),
        )
        return PortfolioRunResult(
            run_id=run_id,
            trade_date=regime.trade_date,
            phase=phase,
            trading_mode=config.trading_mode,
            status=status,
            market_regime=regime,
            candidates=tuple(candidates),
            actions=tuple(actions),
            message=message,
            report_path=report_path,
        )


def _diversified_selection(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    industry_counts: dict[str, int] = {}
    for row in rows:
        industry = str(row.get("industry") or "未分类")
        if industry_counts.get(industry, 0) >= 2:
            continue
        selected.append(row)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def _industry_count(rows: list[dict[str, Any]], industry: str) -> int:
    return sum(1 for row in rows if str(row.get("industry") or "未分类") == str(industry or "未分类"))


def _candidate_evidence(candidate: Any) -> dict[str, Any]:
    return {
        "local_score": _value(candidate, "local_score"),
        "ranking_score": _value(candidate, "ranking_score"),
        "decision": _value(candidate, "decision"),
        "trend": _value(candidate, "trend"),
        "events": list(_value(candidate, "events") or [])[:8],
        "hot_sectors": list(_value(candidate, "hot_sectors") or [])[:6],
        "chan_signals": list(_value(candidate, "chan_signals") or [])[:6],
        "missing_fields": list(_value(candidate, "missing_fields") or []),
    }


def _value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _quote_price(row: dict[str, Any]) -> float:
    for key in ("price", "last_price", "latest_price", "close"):
        value = _number(row.get(key))
        if value is not None and value > 0:
            return value
    return 0.0


def _tradable_quote(row: dict[str, Any], *, trade_date: str) -> bool:
    if not row:
        return True
    status = str(row.get("status") or row.get("trade_status") or "").lower()
    if status in {"suspended", "停牌", "halted"}:
        return False
    trade_time = str(row.get("trade_time") or row.get("as_of_time") or "")
    if len(trade_time) >= 10:
        compact = trade_time[:10].replace("-", "")
        if compact.isdigit() and compact != trade_date:
            return False
    return True


def _atr(indicator: dict[str, Any]) -> float | None:
    if not isinstance(indicator, dict):
        return None
    technical = indicator.get("technical") or indicator
    atr_payload = technical.get("atr") if isinstance(technical, dict) else {}
    return _number((atr_payload or {}).get("atr14")) if isinstance(atr_payload, dict) else None


def _nearest_support(levels: dict[str, Any], entry: float) -> float | None:
    values: list[float] = []
    for key in ("support", "supports", "support_levels"):
        raw = levels.get(key) if isinstance(levels, dict) else None
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            value = _number(item)
            if value is not None and 0 < value < entry:
                values.append(value)
    return max(values) if values else None


def _first_number(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _number(payload.get(key))
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _stable_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    for parser in (datetime.fromisoformat, lambda item: datetime.strptime(item, "%Y-%m-%d %H:%M:%S")):
        try:
            return parser(text)
        except ValueError:
            continue
    return None
