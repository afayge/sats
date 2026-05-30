from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from sats.analysis.market_llm_context import get_a_share_market_context
from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.indicators import IndicatorCalculator, IndicatorInput, IndicatorResult
from sats.llm import ChatLLM
from sats.screening.base import ScreeningInput
from sats.signals import SignalAnalysisResult, SignalEvent, SignalInput, analyze_signal_input
from sats.storage.duckdb import DuckDBStorage


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DISCOVERY_SIGNALS = "short_up"
DEFAULT_DISCOVERY_LIMIT = 5
DEFAULT_CANDIDATE_LIMIT = 30
DEFAULT_LOCAL_SCORE_THRESHOLD = 58.0
DEFAULT_HOT_SECTOR_DAYS = 5
HOT_SECTOR_SCORE_CAP = 12.0
DIVERSITY_SCORE_TOLERANCE = 8.0
DIVERSITY_PENALTY = 2.0
_OPPORTUNITY_KEYWORDS = (
    "给出几个股票",
    "推荐股票",
    "推荐几只",
    "几个股票",
    "短线机会",
    "上涨趋势",
    "未来几天",
    "未来几日",
    "可能上涨",
    "有上涨",
    "强势股",
)


@dataclass(slots=True)
class OpportunityCandidate:
    ts_code: str
    name: str
    trade_date: str
    local_score: float
    decision: str
    trend: str
    close: float
    events: list[dict[str, Any]]
    key_levels: dict[str, Any] = field(default_factory=dict)
    indicator: dict[str, Any] = field(default_factory=dict)
    hot_sectors: list[dict[str, Any]] = field(default_factory=list)
    hot_sector_score: float = 0.0
    ranking_score: float = 0.0
    llm_reason: str = ""
    entry_trigger: str = ""
    invalidation: str = ""
    risk: str = ""
    missing_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "trade_date": self.trade_date,
            "local_score": self.local_score,
            "decision": self.decision,
            "trend": self.trend,
            "close": self.close,
            "events": self.events,
            "key_levels": self.key_levels,
            "indicator": self.indicator,
            "hot_sectors": self.hot_sectors,
            "hot_sector_score": self.hot_sector_score,
            "ranking_score": self.ranking_score,
            "llm_reason": self.llm_reason,
            "entry_trigger": self.entry_trigger,
            "invalidation": self.invalidation,
            "risk": self.risk,
            "missing_fields": self.missing_fields,
        }


@dataclass(slots=True)
class OpportunityDiscoveryResult:
    trade_date: str
    signals: str
    candidates: list[OpportunityCandidate]
    candidate_count: int
    scanned_count: int
    market_context: dict[str, Any] = field(default_factory=dict)
    hot_sector_context: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    report_path: str | None = None
    message: str = ""
    llm_unavailable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "signals": self.signals,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "candidate_count": self.candidate_count,
            "scanned_count": self.scanned_count,
            "market_context": self.market_context,
            "hot_sector_context": self.hot_sector_context,
            "missing_fields": self.missing_fields,
            "report_path": self.report_path,
            "message": self.message,
            "llm_unavailable": self.llm_unavailable,
            "data_policy": "SATS used Analyze short-term bullish signals for temporary full-market screening; screening_results was not written.",
        }

    @property
    def system_message(self) -> str:
        payload = self.to_dict()
        return (
            "以下是 SATS 已基于真实本地数据完成的 A 股短线机会发现上下文。"
            "候选股来自 Analyze 中短期上涨信号的临时全市场筛选；"
            "不要编造未提供的数据，不要承诺未来必然上涨，必须给出触发条件、失效条件和风险提示。\n"
            + json.dumps(payload, ensure_ascii=False, default=str)
        )


def is_opportunity_discovery_question(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if any(keyword in text for keyword in _OPPORTUNITY_KEYWORDS):
        return True
    return ("股票" in text and any(term in text for term in ("上涨", "短线", "未来", "机会", "推荐")))


def run_opportunity_discovery(
    *,
    settings: Settings,
    storage: DuckDBStorage,
    provider: Any | None = None,
    trade_date: str | None = None,
    signals: str = DEFAULT_DISCOVERY_SIGNALS,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    lookback_days: int = 180,
    score_threshold: float = DEFAULT_LOCAL_SCORE_THRESHOLD,
    reports_dir: Path | None = None,
    report: bool = True,
    llm_enabled: bool = True,
    llm_factory: Callable[..., Any] | None = None,
    hot_sector_enabled: bool = True,
    hot_sector_days: int = DEFAULT_HOT_SECTOR_DAYS,
    market_indices: list[str] | tuple[str, ...] | None = None,
    market_dimensions: list[str] | tuple[str, ...] | None = None,
    market_horizons: list[str] | tuple[str, ...] | None = None,
    market_plan_source: str | None = None,
    progress: Any | None = None,
) -> OpportunityDiscoveryResult:
    provider = provider or AStockDataProvider(settings)
    trade_date = trade_date or _resolve_trade_date(storage=storage, provider=provider)
    limit = max(1, int(limit))
    candidate_limit = max(limit, int(candidate_limit))

    missing: list[str] = []
    if progress is None:
        inputs, input_missing = _load_all_screening_inputs_with_lock_fallback(
            provider,
            trade_date,
            storage=storage,
            trade_days=max(80, int(lookback_days)),
        )
    else:
        with progress.step("全市场数据") as step:
            inputs, input_missing = _load_all_screening_inputs_with_lock_fallback(
                provider,
                trade_date,
                storage=storage,
                trade_days=max(80, int(lookback_days)),
            )
            step.complete(message=f"{len(inputs)} 只")
    missing.extend(input_missing)
    signal_results = []
    if progress is None:
        signal_results = [
            analyze_signal_input(_signal_input_from_screening_input(item), selected_signals=signals)
            for item in inputs
        ]
    else:
        with progress.step("Analyze 信号筛选", total=len(inputs)) as step:
            for index, item in enumerate(inputs, start=1):
                signal_results.append(analyze_signal_input(_signal_input_from_screening_input(item), selected_signals=signals))
                step.update(index)
    if progress is None:
        hot_sector_context, hot_missing = _safe_hot_sector_context(
            provider=provider,
            storage=storage,
            trade_date=trade_date,
            enabled=hot_sector_enabled,
            lookback_days=hot_sector_days,
        )
    else:
        with progress.step("热点板块") as step:
            hot_sector_context, hot_missing = _safe_hot_sector_context(
                provider=provider,
                storage=storage,
                trade_date=trade_date,
                enabled=hot_sector_enabled,
                lookback_days=hot_sector_days,
            )
            step.complete()
    stock_basic_map = _stock_basic_map(inputs)
    local_candidates = _select_local_candidates(
        signal_results,
        score_threshold=score_threshold,
        hot_sector_context=hot_sector_context,
        stock_basic_map=stock_basic_map,
    )
    local_candidates = _pick_diversified_signal_results(
        local_candidates,
        limit=candidate_limit,
        hot_sector_context=hot_sector_context,
        stock_basic_map=stock_basic_map,
    )
    if not local_candidates:
        message = _no_candidate_message(scanned_count=len(inputs), missing_fields=[*missing, *hot_missing])
        return OpportunityDiscoveryResult(
            trade_date=trade_date,
            signals=signals,
            candidates=[],
            candidate_count=0,
            scanned_count=len(inputs),
            hot_sector_context=hot_sector_context,
            missing_fields=[*missing, *hot_missing],
            message=message,
        )

    if progress is None:
        market_context, market_missing = _safe_market_context(
            settings,
            trade_date,
            provider=provider,
            indices=market_indices,
            dimensions=market_dimensions,
            horizons=market_horizons,
            market_plan_source=market_plan_source,
        )
    else:
        with progress.step("A股大盘数据") as step:
            market_context, market_missing = _safe_market_context(
                settings,
                trade_date,
                provider=provider,
                indices=market_indices,
                dimensions=market_dimensions,
                horizons=market_horizons,
                market_plan_source=market_plan_source,
            )
            step.complete()
    missing.extend(market_missing)
    missing.extend(hot_missing)
    if progress is None:
        enriched = _enrich_candidates(
            local_candidates,
            settings=settings,
            storage=storage,
            provider=provider,
            trade_date=trade_date,
            lookback_days=lookback_days,
            hot_sector_context=hot_sector_context,
            stock_basic_map=stock_basic_map,
        )
    else:
        with progress.step("候选增强", total=len(local_candidates)) as step:
            enriched = _enrich_candidates(
                local_candidates,
                settings=settings,
                storage=storage,
                provider=provider,
                trade_date=trade_date,
                lookback_days=lookback_days,
                hot_sector_context=hot_sector_context,
                stock_basic_map=stock_basic_map,
            )
            step.update(len(local_candidates))
    missing.extend(_candidate_missing_fields(enriched))
    llm_unavailable = False
    if llm_enabled:
        try:
            if progress is None:
                enriched = _rank_with_llm(
                    enriched,
                    settings=settings,
                    market_context=market_context,
                    trade_date=trade_date,
                    limit=limit,
                    llm_factory=llm_factory or ChatLLM,
                )
            else:
                model_name = str(getattr(settings, "openai_model", "") or "LLM")
                with progress.step(f"{model_name} 排名") as step:
                    enriched = _rank_with_llm(
                        enriched,
                        settings=settings,
                        market_context=market_context,
                        trade_date=trade_date,
                        limit=limit,
                        llm_factory=llm_factory or ChatLLM,
                    )
                    step.complete()
        except Exception:
            llm_unavailable = True
            enriched = enriched[:limit]
    else:
        enriched = enriched[:limit]

    report_path = None
    if report and reports_dir is not None:
        draft = OpportunityDiscoveryResult(
            trade_date=trade_date,
            signals=signals,
            candidates=enriched,
            candidate_count=len(local_candidates),
            scanned_count=len(inputs),
            market_context=market_context,
            hot_sector_context=hot_sector_context,
            missing_fields=missing,
            llm_unavailable=llm_unavailable,
        )
        if progress is None:
            report_path = str(write_opportunity_report(draft, reports_dir=reports_dir))
        else:
            with progress.step("报告生成", total=1) as step:
                report_path = str(write_opportunity_report(draft, reports_dir=reports_dir))
                step.update(1)
    return OpportunityDiscoveryResult(
        trade_date=trade_date,
        signals=signals,
        candidates=enriched,
        candidate_count=len(local_candidates),
        scanned_count=len(inputs),
        market_context=market_context,
        hot_sector_context=hot_sector_context,
        missing_fields=missing,
        report_path=report_path,
        llm_unavailable=llm_unavailable,
    )


def format_opportunity_discovery(result: OpportunityDiscoveryResult) -> str:
    if result.message:
        return result.message
    if not result.candidates:
        return "无结果"
    lines = []
    for index, candidate in enumerate(result.candidates, start=1):
        labels = ",".join(event.get("label", "") for event in candidate.events[:4] if event.get("label")) or "无命中信号"
        reason = f" 理由 {candidate.llm_reason}" if candidate.llm_reason else ""
        trigger = f" 触发 {candidate.entry_trigger}" if candidate.entry_trigger else ""
        invalidation = f" 失效 {candidate.invalidation}" if candidate.invalidation else ""
        risk = f" 风险 {candidate.risk}" if candidate.risk else ""
        hot_text = _hot_sector_label_text(candidate.hot_sectors)
        hot = f" 热点 {hot_text}" if hot_text else ""
        ranking = f" 排名分 {_fmt(candidate.ranking_score)}" if candidate.ranking_score else ""
        name = f" {candidate.name}" if candidate.name else ""
        lines.append(
            f"{index}. {candidate.ts_code}{name} 评分 {_fmt(candidate.local_score)}{ranking} "
            f"{candidate.decision} {candidate.trend} 信号 {labels}{hot}{reason}{trigger}{invalidation}{risk}".rstrip()
        )
    return "\n".join(lines)


def write_opportunity_report(result: OpportunityDiscoveryResult, *, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"opportunity_discovery_{result.trade_date}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        "# SATS 短线机会发现报告",
        "",
        f"- 交易日: {result.trade_date}",
        f"- 信号组: {result.signals}",
        f"- 扫描股票数: {result.scanned_count}",
        f"- 本地候选数: {result.candidate_count}",
        f"- LLM: {'unavailable，本地信号排序' if result.llm_unavailable else 'available'}",
        f"- 缺失字段: {', '.join(result.missing_fields) if result.missing_fields else '无'}",
        "",
        "## 候选排名",
        "",
        "| 排名 | 代码 | 名称 | 评分 | 排名分 | 热点 | 结论 | 趋势 | 触发 | 失效 | 风险 |",
        "| --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for index, candidate in enumerate(result.candidates, start=1):
        lines.append(
            f"| {index} | {candidate.ts_code} | {candidate.name} | {_fmt(candidate.local_score)} | "
            f"{_fmt(candidate.ranking_score)} | {_hot_sector_label_text(candidate.hot_sectors)} | "
            f"{candidate.decision} | {candidate.trend} | {candidate.entry_trigger or ''} | "
            f"{candidate.invalidation or ''} | {candidate.risk or ''} |"
        )
    for candidate in result.candidates:
        lines.extend(["", f"## {candidate.ts_code} {candidate.name}".strip(), ""])
        lines.append(f"- 收盘价: {_fmt(candidate.close)}")
        if candidate.hot_sectors:
            lines.append(f"- 热点板块: {_hot_sector_label_text(candidate.hot_sectors)}")
        if candidate.llm_reason:
            lines.append(f"- 排名理由: {candidate.llm_reason}")
        lines.append(f"- 关键位: 支撑 {candidate.key_levels.get('support', '')}；压力 {candidate.key_levels.get('resistance', '')}")
        lines.extend(["", "| 信号 | 方向 | 置信度 | 理由 | 风险 |", "| --- | --- | ---: | --- | --- |"])
        for event in candidate.events:
            lines.append(
                f"| {event.get('label', '')} | {event.get('side', '')} | {_fmt(event.get('confidence'))} | "
                f"{event.get('reason', '')} | {'；'.join(event.get('risk_flags') or [])} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _signal_input_from_screening_input(item: ScreeningInput) -> SignalInput:
    return SignalInput(
        ts_code=item.ts_code,
        trade_date=item.trade_date,
        daily=item.daily,
        stock_basic=item.stock_basic,
        metadata=item.metadata,
    )


def _load_all_screening_inputs_with_lock_fallback(
    provider: Any,
    trade_date: str,
    *,
    storage: DuckDBStorage,
    trade_days: int,
) -> tuple[list[ScreeningInput], list[str]]:
    try:
        return (
            provider.load_all_screening_inputs(
                trade_date,
                storage=storage,
                trade_days=trade_days,
                rule_name="signal_discovery",
            ),
            [],
        )
    except Exception as exc:
        if not _is_duckdb_lock_error(exc):
            raise
        readonly_storage = storage.readonly()
        try:
            inputs = _load_all_screening_inputs_from_cache(
                readonly_storage,
                trade_date,
                trade_days=trade_days,
            )
        except Exception as cache_exc:
            if not _is_duckdb_lock_error(cache_exc):
                raise
            return [], [f"all_market_data: duckdb_cache_unavailable_after_lock: {cache_exc}"]
        return inputs, [f"all_market_data: duckdb_readonly_cache_after_lock: {exc}"]


def _load_all_screening_inputs_from_cache(
    storage: DuckDBStorage,
    trade_date: str,
    *,
    trade_days: int,
) -> list[ScreeningInput]:
    trade_dates = _cached_trade_dates_for_screening(storage, trade_date, count=trade_days)
    if not trade_dates:
        return []
    daily = storage.get_stock_daily(trade_dates)
    if daily.empty:
        return []
    daily_basic = storage.get_stock_daily_basic(trade_dates)
    stock_basic = _stock_basic_for_daily(storage.get_stock_basic(), daily)
    stock_lookup = {str(row["ts_code"]): row.dropna().to_dict() for _, row in stock_basic.iterrows()}
    daily_groups = _group_frames_by_ts_code(daily)
    daily_basic_groups = _group_frames_by_ts_code(daily_basic)
    inputs: list[ScreeningInput] = []
    for ts_code, group in daily_groups.items():
        stock_info = stock_lookup.get(ts_code) or {"ts_code": ts_code, "symbol": ts_code.split(".", 1)[0], "name": ""}
        inputs.append(
            ScreeningInput(
                ts_code=ts_code,
                trade_date=str(trade_date),
                daily=group,
                daily_basic=daily_basic_groups.get(ts_code, pd.DataFrame()),
                stock_basic=stock_info,
                metadata={
                    "data_source": "duckdb_readonly_cache",
                    "daily_basic_source": "duckdb_readonly_cache",
                },
            )
        )
    return inputs


def _cached_trade_dates_for_screening(storage: DuckDBStorage, trade_date: str, *, count: int) -> list[str]:
    with storage.connect() as con:
        rows = con.execute(
            """
            SELECT DISTINCT trade_date
            FROM stock_daily
            WHERE trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [str(trade_date), int(count)],
        ).fetchall()
    return sorted(str(row[0]) for row in rows if row and row[0])


def _stock_basic_for_daily(stock_basic: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    if stock_basic is not None and not stock_basic.empty:
        return stock_basic
    symbols = sorted({str(value) for value in daily.get("ts_code", pd.Series(dtype=str)).dropna().tolist() if str(value)})
    return pd.DataFrame([{"ts_code": symbol, "symbol": symbol.split(".", 1)[0], "name": ""} for symbol in symbols])


def _group_frames_by_ts_code(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    groups: dict[str, pd.DataFrame] = {}
    for ts_code, group in frame.groupby(frame["ts_code"].astype(str), sort=False):
        groups[str(ts_code)] = group.sort_values("trade_date").reset_index(drop=True)
    return groups


def _is_duckdb_lock_error(exc: Exception) -> bool:
    text = str(exc)
    return "Could not set lock on file" in text or "Conflicting lock is held" in text


def _no_candidate_message(*, scanned_count: int, missing_fields: list[str]) -> str:
    if scanned_count == 0 and any("duckdb_cache_unavailable_after_lock" in item for item in missing_fields):
        return (
            "全市场数据暂时不可用：DuckDB 数据库文件正被另一个进程占用。"
            "请结束或等待占用 data/sats.duckdb 的 SATS/Python 进程后重试。"
        )
    return "无符合中短期上涨信号的候选股票"


def _stock_basic_map(inputs: Iterable[ScreeningInput]) -> dict[str, dict[str, Any]]:
    return {item.ts_code: dict(item.stock_basic or {}) for item in inputs}


def _select_local_candidates(
    results: Iterable[SignalAnalysisResult],
    *,
    score_threshold: float,
    hot_sector_context: dict[str, Any] | None = None,
    stock_basic_map: dict[str, dict[str, Any]] | None = None,
) -> list[SignalAnalysisResult]:
    stock_hot = (hot_sector_context or {}).get("stock_hot_sectors") if isinstance(hot_sector_context, dict) else {}
    stock_basic_map = stock_basic_map or {}
    selected: list[SignalAnalysisResult] = []
    for result in results:
        buy_events = [event for event in result.events if event.side == "buy"]
        sell_events = [event for event in result.events if event.side == "sell"]
        if not buy_events or result.score < score_threshold or result.trend == "看空":
            continue
        buy_score = sum(event.score for event in buy_events)
        sell_score = sum(event.score for event in sell_events)
        if sell_score >= buy_score * 0.6 or any(event.confidence >= 0.75 for event in sell_events):
            continue
        selected.append(result)
    return sorted(
        selected,
        key=lambda item: -_signal_ranking_score(item, stock_hot=stock_hot, stock_basic=stock_basic_map.get(item.ts_code, {})),
    )


def _pick_diversified_signal_results(
    results: list[SignalAnalysisResult],
    *,
    limit: int,
    hot_sector_context: dict[str, Any] | None = None,
    stock_basic_map: dict[str, dict[str, Any]] | None = None,
) -> list[SignalAnalysisResult]:
    if limit <= 0 or len(results) <= limit:
        return results[:limit]
    stock_hot = (hot_sector_context or {}).get("stock_hot_sectors") if isinstance(hot_sector_context, dict) else {}
    stock_basic_map = stock_basic_map or {}
    remaining = list(results)
    selected: list[SignalAnalysisResult] = []
    while remaining and len(selected) < limit:
        best = max(
            remaining,
            key=lambda item: _diversified_signal_score(
                item,
                selected,
                stock_hot=stock_hot,
                stock_basic=stock_basic_map.get(item.ts_code, {}),
                selected_basic_map=stock_basic_map,
            ),
        )
        selected.append(best)
        remaining.remove(best)
    return selected


def _enrich_candidates(
    results: list[SignalAnalysisResult],
    *,
    settings: Settings,
    storage: DuckDBStorage,
    provider: Any,
    trade_date: str,
    lookback_days: int,
    hot_sector_context: dict[str, Any] | None = None,
    stock_basic_map: dict[str, dict[str, Any]] | None = None,
) -> list[OpportunityCandidate]:
    symbols = [result.ts_code for result in results]
    indicators = _load_indicators(
        symbols,
        settings=settings,
        storage=storage,
        provider=provider,
        trade_date=trade_date,
        lookback_days=lookback_days,
    )
    candidates = []
    stock_hot = (hot_sector_context or {}).get("stock_hot_sectors") if isinstance(hot_sector_context, dict) else {}
    stock_basic_map = stock_basic_map or {}
    for result in results:
        indicator = indicators.get(result.ts_code)
        indicator_payload = indicator.to_dict() if indicator is not None else {}
        industry = _industry_label(stock_basic_map.get(result.ts_code, {}))
        if industry:
            indicator_payload.setdefault("fundamentals", {})["industry"] = industry
        hot_sectors = list(stock_hot.get(result.ts_code, [])) if isinstance(stock_hot, dict) else []
        hot_score = _hot_sector_score(hot_sectors)
        candidates.append(
            OpportunityCandidate(
                ts_code=result.ts_code,
                name=result.name,
                trade_date=result.trade_date,
                local_score=result.score,
                decision=result.decision,
                trend=result.trend,
                close=result.close,
                events=[event.to_dict() for event in result.events if event.side == "buy"],
                key_levels=result.key_levels,
                indicator=_compact_indicator(indicator_payload),
                hot_sectors=hot_sectors,
                hot_sector_score=hot_score,
                ranking_score=round(float(result.score) + hot_score, 4),
                missing_fields=_indicator_missing_fields(result.ts_code, indicator_payload),
            )
        )
    return sorted(candidates, key=_candidate_sort_key)


def _candidate_missing_fields(candidates: Iterable[OpportunityCandidate]) -> list[str]:
    missing: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        for item in candidate.missing_fields:
            if item not in seen:
                seen.add(item)
                missing.append(item)
    return missing


def _signal_ranking_score(result: SignalAnalysisResult, *, stock_hot: Any, stock_basic: dict[str, Any]) -> float:
    buy_events = [event for event in result.events if event.side == "buy"]
    confidence_bonus = sum(max(0.0, _float(event.confidence)) for event in buy_events[:5]) * 0.35
    event_bonus = min(2.0, len(buy_events) * 0.35)
    industry_bonus = 0.1 if _industry_label(stock_basic) else 0.0
    return round(
        _float(result.score)
        + _hot_sector_score_for_symbol(result.ts_code, stock_hot)
        + confidence_bonus
        + event_bonus
        + industry_bonus,
        6,
    )


def _diversified_signal_score(
    result: SignalAnalysisResult,
    selected: list[SignalAnalysisResult],
    *,
    stock_hot: Any,
    stock_basic: dict[str, Any],
    selected_basic_map: dict[str, dict[str, Any]],
) -> float:
    score = _signal_ranking_score(result, stock_hot=stock_hot, stock_basic=stock_basic)
    if not selected:
        return score
    best_selected = max(
        _signal_ranking_score(item, stock_hot=stock_hot, stock_basic=selected_basic_map.get(item.ts_code, {}))
        for item in selected
    )
    if best_selected - score > DIVERSITY_SCORE_TOLERANCE:
        return score
    return score - _signal_diversity_penalty(
        result.ts_code,
        selected,
        stock_hot=stock_hot,
        stock_basic=stock_basic,
        selected_basic_map=selected_basic_map,
    )


def _signal_diversity_penalty(
    ts_code: str,
    selected: list[SignalAnalysisResult],
    *,
    stock_hot: Any,
    stock_basic: dict[str, Any],
    selected_basic_map: dict[str, dict[str, Any]],
) -> float:
    board = _board_group(ts_code)
    industry = _industry_label(stock_basic)
    hot_labels = set(_hot_sector_labels(stock_hot.get(ts_code) if isinstance(stock_hot, dict) else []))
    penalty = 0.0
    for item in selected:
        if _board_group(item.ts_code) == board:
            penalty += DIVERSITY_PENALTY
        selected_industry = _industry_label(selected_basic_map.get(item.ts_code, {}))
        if industry and industry == selected_industry:
            penalty += DIVERSITY_PENALTY * 0.6
        selected_hot = set(_hot_sector_labels(stock_hot.get(item.ts_code) if isinstance(stock_hot, dict) else []))
        if hot_labels and selected_hot and hot_labels.intersection(selected_hot):
            penalty += DIVERSITY_PENALTY * 0.5
    return penalty


def _candidate_sort_key(candidate: OpportunityCandidate) -> tuple[float, int]:
    return (-_candidate_quality_score(candidate), _stable_rank_value(candidate.ts_code))


def _candidate_quality_score(candidate: OpportunityCandidate) -> float:
    confidence_bonus = sum(max(0.0, _float(event.get("confidence"))) for event in candidate.events[:5]) * 0.35
    event_bonus = min(2.0, len(candidate.events) * 0.35)
    return round(_float(candidate.ranking_score) + confidence_bonus + event_bonus, 6)


def _rebalance_ranked_candidates(
    ranked: list[OpportunityCandidate],
    all_candidates: list[OpportunityCandidate],
    *,
    limit: int,
) -> list[OpportunityCandidate]:
    if limit <= 1 or len(ranked) < limit:
        return ranked[:limit]
    selected = list(ranked[:limit])
    remaining = [candidate for candidate in all_candidates if candidate.ts_code not in {item.ts_code for item in selected}]
    while _is_over_concentrated(selected):
        replacement = _best_diversifying_candidate(selected, remaining)
        if replacement is None:
            break
        replace_index = _weakest_repeated_candidate_index(selected)
        if replace_index is None:
            break
        removed = selected[replace_index]
        selected[replace_index] = replacement
        remaining = [candidate for candidate in remaining if candidate.ts_code != replacement.ts_code]
        remaining.append(removed)
        if selected[replace_index].llm_reason:
            selected[replace_index].llm_reason = f"{selected[replace_index].llm_reason}；分散调整"
        else:
            selected[replace_index].llm_reason = "分散调整：分数接近时避免结果过度集中在同一板块"
    return selected[:limit]


def _is_over_concentrated(candidates: list[OpportunityCandidate]) -> bool:
    if len(candidates) < 3:
        return False
    board_counts: dict[str, int] = {}
    industry_counts: dict[str, int] = {}
    hot_counts: dict[str, int] = {}
    for candidate in candidates:
        board = _board_group(candidate.ts_code)
        board_counts[board] = board_counts.get(board, 0) + 1
        industry = _candidate_industry(candidate)
        if industry:
            industry_counts[industry] = industry_counts.get(industry, 0) + 1
        for label in _candidate_hot_labels(candidate):
            hot_counts[label] = hot_counts.get(label, 0) + 1
    threshold = max(3, len(candidates) - 1)
    return (
        max(board_counts.values(), default=0) >= threshold
        or max(industry_counts.values(), default=0) >= threshold
        or max(hot_counts.values(), default=0) >= threshold
    )


def _best_diversifying_candidate(
    selected: list[OpportunityCandidate],
    remaining: list[OpportunityCandidate],
) -> OpportunityCandidate | None:
    repeated_boards = _repeated_candidate_groups(selected, _board_group)
    repeated_industries = _repeated_candidate_groups(selected, _candidate_industry)
    repeated_hot = _repeated_candidate_hot_labels(selected)
    weakest_index = _weakest_repeated_candidate_index(selected)
    weakest = selected[weakest_index] if weakest_index is not None else selected[-1]
    best: OpportunityCandidate | None = None
    best_score = float("-inf")
    for candidate in remaining:
        if _candidate_quality_score(weakest) - _candidate_quality_score(candidate) > DIVERSITY_SCORE_TOLERANCE:
            continue
        diversity_gain = 0.0
        if _board_group(candidate.ts_code) not in repeated_boards:
            diversity_gain += 2.0
        industry = _candidate_industry(candidate)
        if industry and industry not in repeated_industries:
            diversity_gain += 1.2
        hot_labels = set(_candidate_hot_labels(candidate))
        if hot_labels and not hot_labels.intersection(repeated_hot):
            diversity_gain += 1.0
        candidate_score = diversity_gain + _candidate_quality_score(candidate) * 0.01
        if candidate_score > best_score:
            best = candidate
            best_score = candidate_score
    return best


def _weakest_repeated_candidate_index(candidates: list[OpportunityCandidate]) -> int | None:
    repeated_boards = _repeated_candidate_groups(candidates, _board_group)
    repeated_industries = _repeated_candidate_groups(candidates, _candidate_industry)
    repeated_hot = _repeated_candidate_hot_labels(candidates)
    repeated_indexes = []
    for index, candidate in enumerate(candidates):
        if _board_group(candidate.ts_code) in repeated_boards:
            repeated_indexes.append(index)
            continue
        industry = _candidate_industry(candidate)
        if industry and industry in repeated_industries:
            repeated_indexes.append(index)
            continue
        if set(_candidate_hot_labels(candidate)).intersection(repeated_hot):
            repeated_indexes.append(index)
    if not repeated_indexes:
        return None
    return min(repeated_indexes, key=lambda index: (_candidate_quality_score(candidates[index]), -index))


def _repeated_candidate_groups(candidates: list[OpportunityCandidate], grouper: Callable[[Any], str]) -> set[str]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        label = grouper(candidate)
        if label:
            counts[label] = counts.get(label, 0) + 1
    return {label for label, count in counts.items() if count >= 2}


def _repeated_candidate_hot_labels(candidates: list[OpportunityCandidate]) -> set[str]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        for label in _candidate_hot_labels(candidate):
            counts[label] = counts.get(label, 0) + 1
    return {label for label, count in counts.items() if count >= 2}


def _board_group(value: Any) -> str:
    ts_code = value.ts_code if isinstance(value, OpportunityCandidate) else str(value or "")
    code = ts_code.split(".", 1)[0]
    suffix = ts_code.split(".", 1)[1].upper() if "." in ts_code else ""
    if suffix == "BJ" or code.startswith(("8", "4", "9")):
        return "北交所"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith("688"):
        return "科创板"
    if suffix == "SH" or code.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if code.startswith(("000", "001", "002", "003")):
        return "深市主板"
    return "其他"


def _candidate_industry(candidate: OpportunityCandidate) -> str:
    fundamentals = candidate.indicator.get("fundamentals") if isinstance(candidate.indicator, dict) else {}
    return str((fundamentals or {}).get("industry") or "").strip()


def _industry_label(stock_basic: dict[str, Any]) -> str:
    return str((stock_basic or {}).get("industry") or "").strip()


def _candidate_hot_labels(candidate: OpportunityCandidate) -> list[str]:
    return _hot_sector_labels(candidate.hot_sectors)


def _hot_sector_labels(hot_sectors: Any) -> list[str]:
    labels = []
    if not isinstance(hot_sectors, list):
        return labels
    for item in hot_sectors:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            labels.append(name)
    return labels


def _stable_rank_value(value: str) -> int:
    digest = hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _indicator_missing_fields(ts_code: str, payload: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not payload:
        return [f"{ts_code}:indicator"]
    for field in ("moneyflow", "fundamentals"):
        value = payload.get(field)
        if not value or (isinstance(value, dict) and value.get("status") == "unavailable"):
            missing.append(f"{ts_code}:{field}")
    return missing


def _load_indicators(
    symbols: list[str],
    *,
    settings: Settings,
    storage: DuckDBStorage,
    provider: Any,
    trade_date: str,
    lookback_days: int,
) -> dict[str, IndicatorResult]:
    if not hasattr(provider, "load_indicator_inputs"):
        return {}
    try:
        inputs = provider.load_indicator_inputs(symbols, trade_date, lookback_days=lookback_days, storage=storage)
    except Exception:
        return {}
    calculator = IndicatorCalculator()
    return {item.ts_code: calculator.calculate(item) for item in inputs}


def _safe_market_context(
    settings: Settings,
    trade_date: str,
    *,
    provider: Any | None = None,
    indices: list[str] | tuple[str, ...] | None = None,
    dimensions: list[str] | tuple[str, ...] | None = None,
    horizons: list[str] | tuple[str, ...] | None = None,
    market_plan_source: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    try:
        payload = get_a_share_market_context(
            settings=settings,
            trade_date=trade_date,
            horizons=horizons or ["tomorrow"],
            indices=indices,
            dimensions=dimensions,
            market_plan_source=market_plan_source,
            astock_provider=provider,
        )
        missing = [str(item) for item in payload.get("missing_fields", []) if str(item).strip()]
        return payload, missing
    except Exception as exc:
        return {}, [f"market_context: {exc}"]


def _safe_hot_sector_context(
    *,
    provider: Any,
    storage: DuckDBStorage,
    trade_date: str,
    enabled: bool,
    lookback_days: int,
) -> tuple[dict[str, Any], list[str]]:
    if not enabled:
        return {}, []
    if not hasattr(provider, "load_hot_sector_context"):
        return {}, ["hot_sector_context: provider_unavailable"]
    try:
        payload = provider.load_hot_sector_context(
            trade_date,
            storage=storage,
            lookback_days=max(3, min(5, int(lookback_days))),
        )
    except Exception as exc:
        return {}, [f"hot_sector_context: {exc}"]
    missing = [str(item) for item in payload.get("missing_fields", []) if str(item).strip()] if isinstance(payload, dict) else []
    return payload if isinstance(payload, dict) else {}, missing


def _rank_with_llm(
    candidates: list[OpportunityCandidate],
    *,
    settings: Settings,
    market_context: dict[str, Any],
    trade_date: str,
    limit: int,
    llm_factory: Callable[..., Any],
) -> list[OpportunityCandidate]:
    payload = {
        "trade_date": trade_date,
        "limit": limit,
        "market_context": _compact_market_context(market_context),
        "hot_sector_policy": "热点板块只作为优先加权，不能把热点当作上涨保证；解释应基于信号和持续热点共振。",
        "diversity_policy": "若候选分数接近，最终 Top N 应避免全部来自同一交易板块、行业或热点概念；只有信号明显更强时才允许集中。",
        "candidates": [candidate.to_dict() for candidate in candidates],
    }
    prompt = (
        "你是 SATS A 股短线机会排序助手。请只基于 JSON 中的真实结构化数据排序，"
        "优先解释技术信号与 3-5 日持续热点板块共振；分数接近时避免 Top N 全部来自同一交易板块、行业或热点概念。"
        "不要编造新闻、题材、价格或财务字段。返回严格 JSON："
        '{"rankings":[{"ts_code":"000001.SZ","reason":"...","entry_trigger":"...","invalidation":"...","risk":"..."}]}。'
        "最多返回 limit 只。\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )
    llm = _build_llm(llm_factory, settings=settings)
    response = llm.chat([{"role": "user", "content": prompt}])
    data = _parse_json_object(str(getattr(response, "content", "") or ""))
    rankings = data.get("rankings")
    if not isinstance(rankings, list):
        raise ValueError("LLM did not return rankings")
    by_code = {candidate.ts_code: candidate for candidate in candidates}
    ranked: list[OpportunityCandidate] = []
    seen: set[str] = set()
    for row in rankings:
        if not isinstance(row, dict):
            continue
        ts_code = str(row.get("ts_code") or "").strip().upper()
        candidate = by_code.get(ts_code)
        if candidate is None or ts_code in seen:
            continue
        seen.add(ts_code)
        candidate.llm_reason = str(row.get("reason") or "").strip()
        candidate.entry_trigger = str(row.get("entry_trigger") or "").strip()
        candidate.invalidation = str(row.get("invalidation") or "").strip()
        candidate.risk = str(row.get("risk") or "").strip()
        ranked.append(candidate)
        if len(ranked) >= limit:
            break
    if not ranked:
        raise ValueError("LLM rankings had no valid candidates")
    if len(ranked) < limit:
        ranked.extend(candidate for candidate in candidates if candidate.ts_code not in seen)
    return _rebalance_ranked_candidates(ranked[:limit], candidates, limit=limit)


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("JSON root is not object")
    return data


def _compact_indicator(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        "technical": payload.get("technical", {}),
        "volume": payload.get("volume", {}),
        "support_resistance": payload.get("support_resistance", {}),
        "moneyflow": payload.get("moneyflow", {}),
        "fundamentals": payload.get("fundamentals", {}),
        "data_sources": payload.get("data_sources", {}),
    }


def _compact_market_context(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        "trade_date": payload.get("trade_date"),
        "requested_indices": payload.get("requested_indices"),
        "requested_dimensions": payload.get("requested_dimensions"),
        "requested_horizons": payload.get("requested_horizons"),
        "indices": payload.get("indices"),
        "market_breadth": payload.get("market_breadth"),
        "limit_sentiment": payload.get("limit_sentiment"),
        "hot_sector_context": payload.get("hot_sector_context"),
        "market_plan_source": payload.get("market_plan_source"),
        "missing_fields": payload.get("missing_fields"),
        "data_sources": payload.get("data_sources"),
    }


def _build_llm(llm_factory: Callable[..., Any], *, settings: Settings) -> Any:
    model_name = str(getattr(settings, "openai_model", "") or "")
    try:
        return llm_factory(model_name=model_name, profile="default")
    except TypeError:
        try:
            return llm_factory(model_name=model_name)
        except TypeError:
            return llm_factory()


def _hot_sector_score(hot_sectors: list[dict[str, Any]]) -> float:
    if not hot_sectors:
        return 0.0
    scores = sorted([_float(item.get("heat_score")) for item in hot_sectors], reverse=True)
    if not scores:
        return 0.0
    score = min(HOT_SECTOR_SCORE_CAP, max(0.0, scores[0]) * 0.8 + sum(max(0.0, item) for item in scores[1:3]) * 0.25)
    return round(score, 4)


def _hot_sector_score_for_symbol(ts_code: str, stock_hot: Any) -> float:
    if not isinstance(stock_hot, dict):
        return 0.0
    sectors = stock_hot.get(ts_code) or []
    return _hot_sector_score(sectors if isinstance(sectors, list) else [])


def _hot_sector_label_text(hot_sectors: list[dict[str, Any]]) -> str:
    labels = []
    for item in hot_sectors[:4]:
        name = str(item.get("name") or "").strip()
        if name:
            labels.append(name)
    return ",".join(labels)


def _float(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _resolve_trade_date(*, storage: DuckDBStorage, provider: Any) -> str:
    today = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")
    if hasattr(provider, "_recent_trade_dates"):
        try:
            dates = provider._recent_trade_dates(today, count=1)
            if dates:
                return str(dates[-1])
        except Exception:
            pass
    try:
        storage.initialize()
        with storage.connect() as con:
            row = con.execute("SELECT MAX(trade_date) FROM stock_daily WHERE trade_date <= ?", [today]).fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    current = datetime.strptime(today, "%Y%m%d")
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.strftime("%Y%m%d")


def _fmt(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")
