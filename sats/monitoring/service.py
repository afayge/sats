from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.llm import ChatLLM, build_light_fallback_llm
from sats.rag.chan_knowledge import search_chan_knowledge
from sats.screening.base import ScreeningInput
from sats.screening.registry import get_rule
from sats.screening.rules.chan_signals import ChanSignalsRule
from sats.storage.duckdb import DuckDBStorage
from sats.trading.sync import QmtPositionSyncService

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_MONITOR_RULE = "chan_signals"
DEFAULT_MONITOR_LISTS = ("positions", "watchlist")
DEFAULT_MINUTE_PERIOD = "30m"
DEFAULT_MINUTE_COUNT = 80


@dataclass(slots=True)
class MonitorConfig:
    rules: tuple[str, ...] = (DEFAULT_MONITOR_RULE,)
    lists: tuple[str, ...] = DEFAULT_MONITOR_LISTS
    interval_seconds: int = 60
    llm_review: bool = False
    max_cycles: int | None = None
    broker: str = "noop"
    auto_trade: tuple[str, ...] = ()
    max_order_value: float = 20000.0
    max_position_pct: float = 0.2
    sell_ratio: float = 1.0


class MonitorService:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: DuckDBStorage,
        provider: AStockDataProvider | None = None,
        trading_provider: "NoopTradingProvider | None" = None,
        position_sync: QmtPositionSyncService | None = None,
        sleep=time.sleep,
        progress: Any | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.provider = provider or AStockDataProvider(settings)
        self.trading_provider = trading_provider or NoopTradingProvider()
        self.position_sync = position_sync
        self.sleep = sleep
        self.progress = progress

    def run_forever(self, config: MonitorConfig) -> None:
        self._validate_rules(config.rules)
        params = {
            "rules": list(config.rules),
            "lists": list(config.lists),
            "interval": config.interval_seconds,
            "llm_review": config.llm_review,
            "broker": config.broker,
            "auto_trade": list(config.auto_trade),
        }
        self.storage.upsert_monitor_runtime(
            service_name="monitor",
            status="running",
            pid=os.getpid(),
            params=params,
            heartbeat=True,
        )
        cycle = 0
        while config.max_cycles is None or cycle < config.max_cycles:
            try:
                self.run_once(config)
            except Exception as exc:  # pragma: no cover - defensive long-running boundary
                self.storage.upsert_monitor_runtime(
                    service_name="monitor",
                    status="running",
                    params=params,
                    last_error=str(exc),
                    heartbeat=True,
                )
            cycle += 1
            if config.max_cycles is not None and cycle >= config.max_cycles:
                break
            self.sleep(max(1, int(config.interval_seconds)))

    def run_once(self, config: MonitorConfig) -> list[dict]:
        self._validate_rules(config.rules)
        now = _now()
        trade_date = now.strftime("%Y%m%d")
        self.storage.expire_monitor_plans(trade_date)
        plan_groups = self.storage.list_active_monitor_plan_groups(trade_date=trade_date)
        if "positions" in {str(item).strip() for item in config.lists}:
            self._position_sync_service().sync()
        targets = self._load_targets(config.lists)
        if not targets and not plan_groups:
            self.storage.upsert_monitor_runtime(
                service_name="monitor",
                status="running",
                params={"rules": list(config.rules), "lists": list(config.lists)},
                heartbeat=True,
            )
            return []

        target_symbols = sorted({target["ts_code"] for target in targets})
        quote_symbols = sorted(
            {
                *target_symbols,
                *(str(group.get("ts_code") or "") for group in plan_groups),
                *(
                    str(condition.get("subject", {}).get("symbol") or "")
                    for group in plan_groups
                    for condition in group.get("conditions") or []
                ),
            }
            - {""}
        )
        start_date = _days_before(trade_date, 240)
        progress = self.progress
        if progress is None:
            daily = (
                self.provider.load_historical_daily_klines(
                    target_symbols,
                    start_date=start_date,
                    end_date=trade_date,
                    storage=self.storage,
                )
                if target_symbols
                else pd.DataFrame()
            )
            quotes = self.provider.load_realtime_quotes(symbols=quote_symbols) if quote_symbols else pd.DataFrame()
            minute = (
                self.provider.load_realtime_minute_klines(
                    target_symbols,
                    period=DEFAULT_MINUTE_PERIOD,
                    count=DEFAULT_MINUTE_COUNT,
                )
                if target_symbols
                else pd.DataFrame()
            )
        else:
            if target_symbols:
                with progress.step("AStock 日线数据") as step:
                    daily = self.provider.load_historical_daily_klines(
                        target_symbols,
                        start_date=start_date,
                        end_date=trade_date,
                        storage=self.storage,
                    )
                    step.complete(message=f"{len(daily)} 条")
            else:
                daily = pd.DataFrame()
            with progress.step("AStock 实时行情") as step:
                quotes = self.provider.load_realtime_quotes(symbols=quote_symbols) if quote_symbols else pd.DataFrame()
                step.complete(message=f"{len(quotes)} 条")
            if target_symbols:
                with progress.step("AStock 分钟K") as step:
                    minute = self.provider.load_realtime_minute_klines(
                        target_symbols,
                        period=DEFAULT_MINUTE_PERIOD,
                        count=DEFAULT_MINUTE_COUNT,
                    )
                    step.complete(message=f"{len(minute)} 条")
            else:
                minute = pd.DataFrame()
        daily = _merge_realtime_daily(daily, quotes, trade_date)
        stock_basic = _stock_basic_lookup(self.storage.get_stock_basic())
        daily_groups = _group_by_ts_code(daily)
        minute_groups = _group_by_ts_code(minute)
        quote_lookup = _quote_lookup(quotes)

        written: list[dict] = []
        rule_step = progress.step("监控规则计算", total=len(targets)) if progress is not None else None
        for target_index, target in enumerate(targets, start=1):
            ts_code = target["ts_code"]
            for rule_name in config.rules:
                rule = get_rule(rule_name)
                if not isinstance(rule, ChanSignalsRule):
                    raise ValueError("monitor v1 only supports chan_signals")
                item = ScreeningInput(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    daily=daily_groups.get(ts_code, pd.DataFrame()),
                    daily_basic=pd.DataFrame(),
                    stock_basic={**stock_basic.get(ts_code, {}), "name": target.get("name") or stock_basic.get(ts_code, {}).get("name", "")},
                    metadata={
                        "data_source": str(
                            quote_lookup.get(ts_code, {}).get("data_source") or "tickflow_current_1m_quote"
                        ),
                        "minute_30m": minute_groups.get(ts_code, pd.DataFrame()),
                        "minute_30m_source": "tickflow_realtime",
                    },
                )
                result = rule.evaluate(item)
                if not result.passed:
                    continue
                for signal in _passed_signals(result.metrics):
                    event = self._event_from_signal(
                        target=target,
                        signal=signal,
                        rule_name=rule.name,
                        quote=quote_lookup.get(ts_code, {}),
                        minute=minute_groups.get(ts_code, pd.DataFrame()),
                        result_metrics=result.metrics,
                        llm_review=config.llm_review,
                    )
                    if self.storage.insert_monitor_event(event):
                        written.append(event)
                        self._handle_signal_side(event, target)
            if rule_step is not None:
                rule_step.update(target_index)
        if rule_step is not None and not rule_step.done:
            rule_step.complete()
        written.extend(
            self._evaluate_monitor_plans(
                config=config,
                plan_groups=plan_groups,
                quote_lookup=quote_lookup,
                now=now,
            )
        )

        self.storage.upsert_monitor_runtime(
            service_name="monitor",
            status="running",
            params={"rules": list(config.rules), "lists": list(config.lists), "interval": config.interval_seconds},
            heartbeat=True,
        )
        return written

    def _evaluate_monitor_plans(
        self,
        *,
        config: MonitorConfig,
        plan_groups: list[dict],
        quote_lookup: dict[str, dict],
        now: datetime,
    ) -> list[dict]:
        trade_date = now.strftime("%Y%m%d")
        written: list[dict] = []
        for group in plan_groups:
            if not _inside_active_windows(now, group.get("active_windows") or []):
                continue
            evaluations = [
                _evaluate_plan_condition(condition, quote_lookup)
                for condition in group.get("conditions") or []
            ]
            result = _condition_group_result(evaluations)
            state = self.storage.get_monitor_plan_trigger_state(str(group["group_id"]), trade_date)
            previous = str(state.get("last_result") or "unknown")
            crossing_count = int(state.get("crossing_count") or 0)
            notification_count = int(state.get("notification_count") or 0)
            trade_count = int(state.get("trade_count") or 0)
            if result == "unknown":
                self.storage.upsert_monitor_plan_trigger_state(
                    group_id=str(group["group_id"]),
                    trade_date=trade_date,
                    last_result=previous,
                    crossing_count=crossing_count,
                    notification_count=notification_count,
                    trade_count=trade_count,
                    last_values=evaluations,
                )
                continue
            if result == "false":
                self.storage.upsert_monitor_plan_trigger_state(
                    group_id=str(group["group_id"]),
                    trade_date=trade_date,
                    last_result="false",
                    crossing_count=crossing_count,
                    notification_count=notification_count,
                    trade_count=trade_count,
                    last_values=evaluations,
                )
                continue
            if previous == "true":
                self.storage.upsert_monitor_plan_trigger_state(
                    group_id=str(group["group_id"]),
                    trade_date=trade_date,
                    last_result="true",
                    crossing_count=crossing_count,
                    notification_count=notification_count,
                    trade_count=trade_count,
                    last_values=evaluations,
                )
                continue

            next_crossing = crossing_count + 1
            action = str(group.get("action") or "notify")
            trade_attempted = action in {"buy", "sell"} and trade_count == 0
            event = _monitor_plan_event(
                group,
                evaluations=evaluations,
                quote=quote_lookup.get(str(group.get("ts_code") or ""), {}),
                now=now,
                crossing_count=next_crossing,
            )
            inserted = self.storage.insert_monitor_event(event)
            self.storage.upsert_monitor_plan_trigger_state(
                group_id=str(group["group_id"]),
                trade_date=trade_date,
                last_result="true",
                crossing_count=next_crossing,
                notification_count=notification_count + 1,
                trade_count=trade_count + (1 if trade_attempted else 0),
                last_values=evaluations,
                triggered=True,
            )
            if not inserted:
                continue
            written.append(event)
            if trade_attempted:
                self._handle_plan_action(event, group, config=config)
        return written

    def _handle_plan_action(self, event: dict, group: dict, *, config: MonitorConfig) -> None:
        action = str(group.get("action") or "")
        if action == "sell" and config.broker == "qmt" and "sell" in set(config.auto_trade):
            try:
                self._position_sync_service().sync()
            except Exception as exc:
                self.storage.insert_monitor_trade_event(
                    {
                        "trade_event_id": _stable_id(f"plan-sync:{event.get('event_id')}:sell"),
                        "event_id": event.get("event_id"),
                        "ts_code": event.get("ts_code"),
                        "name": event.get("name"),
                        "action": "sell",
                        "side": "sell",
                        "price": event.get("price"),
                        "quantity": None,
                        "status": "rejected",
                        "message": f"QMT 持仓同步失败，未执行计划卖出: {exc}",
                        "metrics": {"source_event": event, "sizing": group.get("sizing") or {}},
                    }
                )
                return
        trade_event = self.trading_provider.build_trade_event(
            event,
            action=action,
            quantity=None,
            sizing=group.get("sizing") or {"mode": "default"},
        )
        self.storage.insert_monitor_trade_event(trade_event)

    def _load_targets(self, lists: tuple[str, ...]) -> list[dict]:
        result: list[dict] = []
        requested = {str(item).strip() for item in lists if str(item).strip()}
        if "positions" in requested:
            for row in self.storage.list_monitor_positions(enabled=True):
                result.append({**row, "source_list": "positions"})
        if "watchlist" in requested:
            for row in self.storage.list_monitor_watchlist(enabled=True):
                result.append({**row, "source_list": "watchlist"})
        return result

    def _position_sync_service(self) -> QmtPositionSyncService:
        if self.position_sync is None:
            self.position_sync = QmtPositionSyncService.from_settings(storage=self.storage, settings=self.settings)
        return self.position_sync

    def _event_from_signal(
        self,
        *,
        target: dict,
        signal: dict,
        rule_name: str,
        quote: dict,
        minute: pd.DataFrame,
        result_metrics: dict,
        llm_review: bool,
    ) -> dict:
        ts_code = str(target["ts_code"])
        name = str(target.get("name") or "")
        trade_time = _latest_trade_time(minute) or str(quote.get("trade_time") or _today())
        signal_name = str(signal.get("signal_name") or "")
        signal_label = str(signal.get("label") or signal_name)
        side = str(signal.get("side") or "")
        price = _num(quote.get("close"))
        score = _num(signal.get("score"))
        key = f"{ts_code}:{target.get('source_list')}:{rule_name}:{signal_name}:{trade_time}"
        message = f"{ts_code} {name} {signal_label} {side} 评分 {score:.1f} 价格 {price:.2f}".strip()
        metrics = {
            "signal": signal,
            "quote": quote,
            "source_metrics": result_metrics,
        }
        if llm_review:
            metrics["llm_review"] = self._llm_review(ts_code, name, signal, result_metrics)
        return {
            "event_id": _stable_id(key),
            "event_key": key,
            "ts_code": ts_code,
            "name": name,
            "source_list": str(target.get("source_list") or ""),
            "rule_name": rule_name,
            "signal_name": signal_name,
            "signal_label": signal_label,
            "side": side,
            "score": score,
            "price": price,
            "trade_time": trade_time,
            "message": message,
            "watch_levels": signal.get("watch_levels") or {},
            "risk_flags": signal.get("risk_flags") or [],
            "metrics": metrics,
        }

    def _handle_signal_side(self, event: dict, target: dict) -> None:
        side = str(event.get("side") or "")
        source_list = str(event.get("source_list") or "")
        if source_list == "watchlist" and side == "buy":
            self.storage.upsert_monitor_buy_candidate(
                ts_code=str(event["ts_code"]),
                name=str(event.get("name") or ""),
                source_event_id=str(event["event_id"]),
                rule_name=str(event["rule_name"]),
                signal_name=str(event["signal_name"]),
                signal_label=str(event["signal_label"]),
                score=float(event.get("score") or 0.0),
                price=float(event.get("price") or 0.0),
                reason=str(event.get("message") or ""),
            )
            trade_event = self.trading_provider.build_trade_event(event, action="buy", quantity=None)
            self.storage.insert_monitor_trade_event(trade_event)
        if source_list == "positions" and side in {"sell", "cash"}:
            quantity = target.get("quantity")
            trade_event = self.trading_provider.build_trade_event(event, action="sell", quantity=quantity)
            self.storage.insert_monitor_trade_event(trade_event)

    def _llm_review(self, ts_code: str, name: str, signal: dict, metrics: dict) -> str:
        cards = search_chan_knowledge(str(signal.get("label") or ""), limit=3)
        prompt = (
            "请用两句话解释这个缠论监控信号，不构成投资建议。\n"
            f"股票: {ts_code} {name}\n"
            f"信号: {json.dumps(signal, ensure_ascii=False, default=str)}\n"
            f"规则依据: {json.dumps(cards, ensure_ascii=False, default=str)}\n"
            f"指标: {json.dumps(metrics, ensure_ascii=False, default=str)[:3000]}"
        )
        llm = build_light_fallback_llm(
            ChatLLM,
            light_model_name=str(getattr(self.settings, "light_model_name", "") or getattr(self.settings, "openai_model", "") or ""),
            default_model_name=str(getattr(self.settings, "openai_model", "") or ""),
            timeout_seconds=_llm_timeout_seconds(self.settings),
        )
        response = llm.chat([{"role": "user", "content": prompt}])
        return response.content or ""

    def _validate_rules(self, rules: tuple[str, ...]) -> None:
        if not rules:
            raise ValueError("monitor requires at least one rule")
        for rule_name in rules:
            rule = get_rule(rule_name)
            if not isinstance(rule, ChanSignalsRule):
                raise ValueError("monitor v1 only supports chan_signals")


def _llm_timeout_seconds(settings: Any) -> int | None:
    value = getattr(settings, "llm_timeout_seconds", None)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


class NoopTradingProvider:
    def build_trade_event(
        self,
        event: dict,
        *,
        action: str,
        quantity: Any = None,
        sizing: dict[str, Any] | None = None,
    ) -> dict:
        trade_event_id = _stable_id(f"trade:{event.get('event_id')}:{action}")
        return {
            "trade_event_id": trade_event_id,
            "event_id": event.get("event_id"),
            "ts_code": event.get("ts_code"),
            "name": event.get("name"),
            "action": action,
            "side": event.get("side"),
            "price": event.get("price"),
            "quantity": quantity,
            "status": "not_configured",
            "message": "交易系统未配置，仅记录监控建议",
            "metrics": {"source_event": event, "sizing": sizing or {"mode": "default"}},
        }


def _passed_signals(metrics: dict) -> list[dict]:
    signals = metrics.get("chan_signals") or []
    return [signal for signal in signals if signal.get("passed")]


def _merge_realtime_daily(daily: pd.DataFrame, quotes: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    quote_daily = _quotes_to_daily(quotes, trade_date)
    if daily is None or daily.empty:
        return quote_daily
    if quote_daily.empty:
        return daily
    data = daily.copy()
    data = data[~((data["ts_code"].astype(str).isin(quote_daily["ts_code"].astype(str))) & (data["trade_date"].astype(str) == trade_date))]
    return pd.concat([data, quote_daily], ignore_index=True, sort=False).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def _quotes_to_daily(quotes: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    columns = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]
    if quotes is None or quotes.empty:
        return pd.DataFrame(columns=columns)
    data = quotes.copy()
    for column in columns:
        if column not in data.columns:
            data[column] = None
    data["trade_date"] = str(trade_date)
    return data[columns].dropna(subset=["ts_code", "open", "high", "low", "close"]).reset_index(drop=True)


def _group_by_ts_code(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    sort_column = "trade_time" if "trade_time" in frame.columns else "trade_date" if "trade_date" in frame.columns else None
    result = {}
    for ts_code, group in frame.groupby("ts_code", sort=False):
        result[str(ts_code)] = group.sort_values(sort_column).reset_index(drop=True) if sort_column else group.reset_index(drop=True)
    return result


def _quote_lookup(frame: pd.DataFrame) -> dict[str, dict]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    return {str(row["ts_code"]): row.dropna().to_dict() for _, row in frame.iterrows()}


def _stock_basic_lookup(frame: pd.DataFrame) -> dict[str, dict]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    return {str(row["ts_code"]): row.dropna().to_dict() for _, row in frame.iterrows()}


def _latest_trade_time(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty or "trade_time" not in frame.columns:
        return ""
    values = [str(value) for value in frame["trade_time"].dropna().tolist() if str(value)]
    return max(values) if values else ""


def _evaluate_plan_condition(condition: dict, quote_lookup: dict[str, dict]) -> dict:
    subject = condition.get("subject") or {}
    symbol = str(subject.get("symbol") or "")
    quote = quote_lookup.get(symbol) or {}
    metric = str(condition.get("metric") or "")
    actual = _plan_metric_value(metric, quote)
    operator = str(condition.get("operator") or "")
    target = _optional_num(condition.get("value"))
    if actual is None or target is None:
        status = "unknown"
        matched = None
    else:
        matched = _compare(actual, operator, target)
        status = "true" if matched else "false"
    return {
        "subject": {"type": str(subject.get("type") or ""), "symbol": symbol},
        "metric": metric,
        "operator": operator,
        "target": target,
        "actual": actual,
        "status": status,
        "matched": matched,
        "data_source": str(quote.get("data_source") or ""),
        "trade_time": str(quote.get("trade_time") or quote.get("as_of_time") or ""),
    }


def _condition_group_result(evaluations: list[dict]) -> str:
    if not evaluations or any(item.get("status") == "unknown" for item in evaluations):
        return "unknown"
    return "true" if all(item.get("status") == "true" for item in evaluations) else "false"


def _monitor_plan_event(
    group: dict,
    *,
    evaluations: list[dict],
    quote: dict,
    now: datetime,
    crossing_count: int,
) -> dict:
    ts_code = str(group.get("ts_code") or "")
    action = str(group.get("action") or "notify")
    group_id = str(group.get("group_id") or "")
    trade_date = now.strftime("%Y%m%d")
    key = f"plan:{group.get('plan_id')}:{group_id}:{trade_date}:{crossing_count}"
    price = _plan_metric_value("latest_price", quote)
    message = str(group.get("message") or "").strip() or f"{ts_code} 监控计划触发 {action}"
    return {
        "event_id": _stable_id(key),
        "event_key": key,
        "ts_code": ts_code,
        "name": str(group.get("name") or ""),
        "source_list": "plan",
        "rule_name": "monitor_plan",
        "signal_name": group_id,
        "signal_label": message,
        "side": action,
        "score": None,
        "price": price,
        "trade_time": str(quote.get("trade_time") or now.strftime("%Y-%m-%d %H:%M:%S")),
        "message": message,
        "watch_levels": {},
        "risk_flags": [str(group.get("risk_note") or "")] if str(group.get("risk_note") or "").strip() else [],
        "metrics": {
            "plan_id": group.get("plan_id"),
            "plan_name": group.get("plan_name"),
            "item_id": group.get("item_id"),
            "group_id": group_id,
            "action": action,
            "sizing": group.get("sizing") or {"mode": "default"},
            "conditions": evaluations,
            "summary": group.get("summary") or "",
            "crossing_count": crossing_count,
        },
    }


def _inside_active_windows(now: datetime, windows: list[dict]) -> bool:
    current = now.strftime("%H:%M")
    return any(str(window.get("start") or "") <= current <= str(window.get("end") or "") for window in windows)


def _plan_metric_value(metric: str, quote: dict) -> float | None:
    price = _first_num(quote, ("price", "last_price", "latest_price", "close"))
    if metric == "latest_price":
        return price
    if metric == "pct_change":
        value = _first_num(quote, ("pct_chg", "pct_change", "change_pct"))
        if value is not None:
            return value
        pre_close = _first_num(quote, ("pre_close", "prev_close"))
        return ((price / pre_close - 1.0) * 100.0) if price is not None and pre_close not in {None, 0.0} else None
    if metric == "change_points":
        value = _first_num(quote, ("change_points", "change", "price_change"))
        if value is not None:
            return value
        pre_close = _first_num(quote, ("pre_close", "prev_close"))
        return price - pre_close if price is not None and pre_close is not None else None
    return None


def _first_num(payload: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _optional_num(payload.get(key))
        if value is not None:
            return value
    return None


def _optional_num(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _compare(actual: float, operator: str, target: float) -> bool:
    if operator == ">=":
        return actual >= target
    if operator == ">":
        return actual > target
    if operator == "<=":
        return actual <= target
    if operator == "<":
        return actual < target
    return False


def _now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def _today() -> str:
    return _now().strftime("%Y%m%d")


def _days_before(trade_date: str, days: int) -> str:
    dt = datetime.strptime(str(trade_date), "%Y%m%d")
    return (dt - timedelta(days=days)).strftime("%Y%m%d")


def _stable_id(value: str) -> str:
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()


def _num(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
