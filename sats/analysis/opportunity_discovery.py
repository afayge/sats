from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
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
from sats.llm import ChatLLM, build_light_fallback_llm
from sats.screening.base import ScreeningInput
from sats.signals import SignalAnalysisResult, SignalEvent, SignalInput, analyze_signal_input
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DISCOVERY_SIGNALS = "short_up"
DEFAULT_DISCOVERY_LIMIT = 5
DEFAULT_CANDIDATE_LIMIT = 50
DEFAULT_LOCAL_SCORE_THRESHOLD = 58.0
DEFAULT_HOT_SECTOR_DAYS = 5
DEFAULT_LLM_CONTEXT_LIMIT_TOKENS = 1_048_565
HOT_SECTOR_SCORE_CAP = 12.0
CHAN_SCORE_CAP = 8.0
DIVERSITY_SCORE_TOLERANCE = 8.0
DIVERSITY_PENALTY = 2.0
_OPPORTUNITY_KEYWORDS = (
    "给出几个股票",
    "推荐股票",
    "推荐几只",
    "几个股票",
    "短线机会",
    "大概率上涨",
    "上涨趋势",
    "未来几天",
    "未来几日",
    "可能上涨",
    "有上涨",
    "强势股",
)
_GENERIC_HOT_SECTOR_TERMS = (
    "热点板块",
    "热点题材",
    "热点概念",
    "市场热点",
    "当前热点",
    "今日热点",
    "今天的热点",
    "近期热点",
    "市场主线",
    "主线板块",
    "强势板块",
    "强势行业",
    "强势概念",
    "涨幅居前",
    "涨幅靠前",
    "上涨较好",
    "上涨比较好",
)
_STOCK_PICKING_GOAL_TERMS = (
    "股票",
    "个股",
    "A股",
    "a股",
    "标的",
    "选股",
    "筛选",
    "推荐",
    "列出",
    "给出",
    "找出",
    "选出",
    "挑选",
    "短线机会",
    "上涨",
    "大概率",
    "可能涨",
    "未来几天",
    "未来几日",
    "明天",
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
    chan_score: float = 0.0
    chan_signals: list[dict[str, Any]] = field(default_factory=list)
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
            "chan_score": self.chan_score,
            "chan_signals": self.chan_signals,
            "ranking_score": self.ranking_score,
            "llm_reason": self.llm_reason,
            "entry_trigger": self.entry_trigger,
            "invalidation": self.invalidation,
            "risk": self.risk,
            "missing_fields": self.missing_fields,
        }

    def to_llm_context(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "trade_date": self.trade_date,
            "local_score": self.local_score,
            "ranking_score": self.ranking_score,
            "hot_sector_score": self.hot_sector_score,
            "chan_score": self.chan_score,
            "decision": self.decision,
            "trend": self.trend,
            "close": self.close,
            "events": [_compact_event_for_llm(event) for event in self.events[:6]],
            "key_levels": self.key_levels,
            "indicator": _compact_indicator_for_llm(self.indicator),
            "hot_sectors": [_compact_hot_sector_for_llm(item) for item in self.hot_sectors[:6]],
            "chan_signals": [_compact_chan_signal_for_llm(item) for item in self.chan_signals[:6]],
            "llm_reason": _truncate_text(self.llm_reason, 160),
            "entry_trigger": _truncate_text(self.entry_trigger, 120),
            "invalidation": _truncate_text(self.invalidation, 120),
            "risk": _truncate_text(self.risk, 160),
            "missing_fields": [_truncate_text(item, 120) for item in self.missing_fields[:8]],
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
    llm_pool_count: int = 0

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
            "llm_pool_count": self.llm_pool_count,
            "data_policy": "SATS used Analyze short-term bullish signals for temporary screening; screening_results was not written.",
        }

    @property
    def system_message(self) -> str:
        payload = self.to_dict()
        return (
            "以下是 SATS 已基于真实本地数据完成的 A 股短线机会发现上下文。"
            "候选股来自 Analyze 中短期上涨信号的临时筛选；"
            "不要编造未提供的数据，不要承诺未来必然上涨，必须给出触发条件、失效条件和风险提示。\n"
            + json.dumps(payload, ensure_ascii=False, default=str)
        )

    def to_llm_context(self, *, candidate_limit: int | None = None) -> dict[str, Any]:
        candidates = self.candidates
        if candidate_limit is not None and candidate_limit >= 0:
            candidates = candidates[:candidate_limit]
        return {
            "trade_date": self.trade_date,
            "signals": self.signals,
            "candidates": [candidate.to_llm_context() for candidate in candidates],
            "candidate_count": self.candidate_count,
            "scanned_count": self.scanned_count,
            "market_context": _compact_market_context(self.market_context),
            "hot_sector_context": _compact_hot_sector_context(self.hot_sector_context),
            "missing_fields": [_truncate_text(item, 160) for item in self.missing_fields[:20]],
            "report_path": self.report_path,
            "message": self.message,
            "llm_unavailable": self.llm_unavailable,
            "llm_pool_count": self.llm_pool_count,
            "data_policy": "SATS used Analyze short-term bullish signals for temporary screening; screening_results was not written.",
        }

    def system_message_for_llm(self) -> str:
        payload = self.to_llm_context()
        return (
            "以下是 SATS 已基于真实本地数据完成的 A 股短线机会发现精简上下文。"
            "候选股来自 Analyze 中短期上涨信号的临时筛选；"
            "该上下文已移除原始长 K 线、全量热点成员映射和冗长因子明细。"
            "不要编造未提供的数据，不要承诺未来必然上涨，必须给出触发条件、失效条件和风险提示。\n"
            + json.dumps(payload, ensure_ascii=False, default=str)
        )


@dataclass(frozen=True, slots=True)
class LLMContextBudgetResult:
    candidates: list[OpportunityCandidate]
    prompt: str
    estimated_tokens: int
    input_budget_tokens: int
    warnings: tuple[str, ...] = ()
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class LLMRankResult:
    candidates: list[OpportunityCandidate]
    warnings: tuple[str, ...] = ()
    llm_pool_count: int = 0
    llm_unavailable: bool = False


class LLMContextBudgetExceeded(RuntimeError):
    def __init__(self, warnings: Iterable[str]) -> None:
        self.warnings = tuple(str(item) for item in warnings if str(item).strip())
        super().__init__("LLM prompt exceeds configured context budget")


def is_opportunity_discovery_question(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if is_generic_hot_sector_discovery_question(text):
        return True
    if any(keyword in text for keyword in _OPPORTUNITY_KEYWORDS):
        return True
    if _has_stock_picking_action(text):
        return True
    return ("股票" in text and any(term in text for term in ("上涨", "短线", "未来", "机会", "推荐")))


def is_generic_hot_sector_discovery_question(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if not any(term in text for term in _STOCK_PICKING_GOAL_TERMS):
        return False
    if any(term in text for term in _GENERIC_HOT_SECTOR_TERMS):
        return True
    return bool(
        re.search(r"(?:行业|概念|板块|题材).{0,8}(?:上涨|涨幅|表现).{0,8}(?:较好|比较好|最好|靠前|居前)", text)
        or re.search(r"(?:上涨|涨幅|表现).{0,8}(?:较好|比较好|最好|靠前|居前).{0,8}(?:行业|概念|板块|题材)", text)
    )


def extract_opportunity_discovery_limit(message: str) -> int | None:
    text = str(message or "").strip()
    if not text:
        return None
    number = r"([0-9]{1,4}|[零〇一二两三四五六七八九十百]{1,6})"
    patterns = (
        rf"(?:前|top\s*)\s*{number}\s*(?:名|只|支|个股|股票|标的|候选)?",
        rf"(?:列出|给出|推荐|筛选|找出|选出|挑选|返回)\s*{number}\s*(?:支|只|名|个股|股票|标的|候选)",
        rf"{number}\s*(?:支|只|名|个股|股票|标的|候选)",
        rf"(?:列出|给出|推荐|筛选|找出|选出|挑选|返回)\s*{number}\s*个(?!交易日|工作日|日|天)",
        rf"{number}\s*个(?:股票|个股|标的|候选|机会)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        value = _parse_count_number(match.group(1))
        if value is not None and value > 0:
            return value
    return None


def _has_stock_picking_action(text: str) -> bool:
    return (
        any(term in text for term in ("股票", "个股", "A股", "a股", "标的", "强势股"))
        and any(term in text for term in ("列出", "给出", "推荐", "筛选", "找出", "选出", "挑选", "返回"))
        and any(term in text for term in ("上涨", "大概率", "可能涨", "短线", "机会", "强势"))
    )


def _parse_count_number(value: str) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return _parse_chinese_count(text)


def _parse_chinese_count(value: str) -> int | None:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    text = str(value or "").strip()
    if not text:
        return None
    if "百" in text:
        left, _, right = text.partition("百")
        hundreds = digits.get(left, 1 if not left else None)
        if hundreds is None:
            return None
        rest = _parse_chinese_count(right) if right else 0
        return hundreds * 100 + int(rest or 0)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1 if not left else None)
        ones = digits.get(right, 0 if not right else None)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if all(char in digits for char in text):
        rendered = "".join(str(digits[char]) for char in text).lstrip("0")
        return int(rendered or "0")
    return None


def run_opportunity_discovery(
    *,
    settings: Settings,
    storage: DuckDBStorage,
    provider: Any | None = None,
    symbols: list[str] | tuple[str, ...] | None = None,
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

    requested_symbols = normalize_symbols(symbols, required=False) if symbols is not None else None
    missing: list[str] = []
    if progress is None:
        if requested_symbols is None:
            inputs, input_missing = _load_all_screening_inputs_with_lock_fallback(
                provider,
                trade_date,
                storage=storage,
                trade_days=max(80, int(lookback_days)),
            )
        else:
            inputs, input_missing = _load_screening_inputs_with_lock_fallback(
                provider,
                requested_symbols,
                trade_date,
                storage=storage,
                trade_days=max(80, int(lookback_days)),
            )
    else:
        data_label = "全市场数据" if requested_symbols is None else "限定股票池数据"
        with progress.step(data_label) as step:
            if requested_symbols is None:
                inputs, input_missing = _load_all_screening_inputs_with_lock_fallback(
                    provider,
                    trade_date,
                    storage=storage,
                    trade_days=max(80, int(lookback_days)),
                )
            else:
                inputs, input_missing = _load_screening_inputs_with_lock_fallback(
                    provider,
                    requested_symbols,
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
    all_local_candidates = _select_local_candidates(
        signal_results,
        score_threshold=score_threshold,
        hot_sector_context=hot_sector_context,
        stock_basic_map=stock_basic_map,
    )
    candidate_count = len(all_local_candidates)
    local_candidates = _pick_diversified_signal_results(
        all_local_candidates,
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
            llm_pool_count=0,
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
    llm_pool_count = len(enriched) if llm_enabled else 0
    llm_unavailable = False
    if llm_enabled:
        try:
            if progress is None:
                rank_result = _rank_with_llm(
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
                    rank_result = _rank_with_llm(
                        enriched,
                        settings=settings,
                        market_context=market_context,
                        trade_date=trade_date,
                        limit=limit,
                        llm_factory=llm_factory or ChatLLM,
                    )
                    step.complete()
            enriched = rank_result.candidates
            llm_pool_count = rank_result.llm_pool_count
            missing.extend(rank_result.warnings)
            llm_unavailable = rank_result.llm_unavailable
        except LLMContextBudgetExceeded as exc:
            missing.extend(exc.warnings)
            llm_pool_count = 0
            llm_unavailable = True
            enriched = enriched[:limit]
        except Exception:
            llm_unavailable = True
            llm_pool_count = 0
            enriched = enriched[:limit]
    else:
        enriched = enriched[:limit]

    report_path = None
    if report and reports_dir is not None:
        draft = OpportunityDiscoveryResult(
            trade_date=trade_date,
            signals=signals,
            candidates=enriched,
            candidate_count=candidate_count,
            scanned_count=len(inputs),
            market_context=market_context,
            hot_sector_context=hot_sector_context,
            missing_fields=missing,
            llm_unavailable=llm_unavailable,
            llm_pool_count=llm_pool_count,
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
        candidate_count=candidate_count,
        scanned_count=len(inputs),
        market_context=market_context,
        hot_sector_context=hot_sector_context,
        missing_fields=missing,
        report_path=report_path,
        llm_unavailable=llm_unavailable,
        llm_pool_count=llm_pool_count,
    )


def format_opportunity_discovery(result: OpportunityDiscoveryResult) -> str:
    message = str(getattr(result, "message", "") or "")
    if message:
        return message
    candidates = list(getattr(result, "candidates", []) or [])
    if not candidates:
        return "无结果"
    candidate_count = int(getattr(result, "candidate_count", len(candidates)) or len(candidates))
    scanned_count = int(getattr(result, "scanned_count", candidate_count) or candidate_count)
    trade_date = str(getattr(result, "trade_date", "") or "--")
    signals = str(getattr(result, "signals", "") or "--")
    llm_unavailable = bool(getattr(result, "llm_unavailable", False))
    raw_llm_pool_count = getattr(result, "llm_pool_count", None)
    llm_pool_count = int(raw_llm_pool_count) if raw_llm_pool_count else len(candidates)
    lines = [
        (
            f"交易日: {trade_date} | 信号组: {signals} | 扫描: {scanned_count} 只 | "
            f"短期信号候选: {candidate_count} 只 | 展示: {len(candidates)} 只 | "
            f"LLM: {'unavailable，本地信号排序' if llm_unavailable else 'available'}"
        ),
        f"LLM分析池: {llm_pool_count} 只",
        "",
        *_format_discovery_table(candidates),
        "",
        "详情:",
    ]
    for index, candidate in enumerate(candidates, start=1):
        name = f" {candidate.name}" if candidate.name else ""
        lines.extend(
            [
                f"{index}. {candidate.ts_code}{name}".rstrip(),
                f"   缠论: {_text_or_dash(_chan_signal_label_text(getattr(candidate, 'chan_signals', []) or []))}",
                f"   理由: {_text_or_dash(candidate.llm_reason)}",
                f"   触发: {_text_or_dash(candidate.entry_trigger)}",
                f"   失效: {_text_or_dash(candidate.invalidation)}",
                f"   风险: {_text_or_dash(candidate.risk)}",
            ]
        )
    return "\n".join(lines)


def _format_discovery_table(candidates: list[OpportunityCandidate]) -> list[str]:
    rows = [
        {
            "rank": str(index),
            "code": candidate.ts_code,
            "name": candidate.name or "--",
            "score": _text_or_dash(_fmt(candidate.local_score)),
            "ranking": _text_or_dash(_fmt(candidate.ranking_score)),
            "trend": candidate.trend or "--",
            "signals": _event_label_text(candidate.events),
            "chan": _chan_signal_label_text(getattr(candidate, "chan_signals", []) or []) or "--",
            "hot": _hot_sector_label_text(candidate.hot_sectors) or "--",
        }
        for index, candidate in enumerate(candidates, start=1)
    ]
    columns = [
        ("rank", "排名", 4),
        ("code", "代码", 9),
        ("name", "名称", 12),
        ("score", "评分", 6),
        ("ranking", "排名分", 6),
        ("trend", "趋势", 6),
        ("signals", "主要信号", 24),
        ("chan", "缠论", 16),
        ("hot", "热点", 18),
    ]
    widths = {
        key: min(max_width, max(_display_width(title), *(_display_width(row[key]) for row in rows)))
        for key, title, max_width in columns
    }
    lines = [
        "  ".join(_display_ljust(title, widths[key]) for key, title, _ in columns).rstrip()
    ]
    for row in rows:
        lines.append("  ".join(_display_ljust(row[key], widths[key]) for key, _, _ in columns).rstrip())
    return lines


def _event_label_text(events: list[dict[str, Any]]) -> str:
    labels = [str(event.get("label") or "").strip() for event in events[:4] if event.get("label")]
    return ",".join(labels) or "--"


def _chan_signal_label_text(chan_signals: list[dict[str, Any]]) -> str:
    labels = []
    for signal in chan_signals[:4]:
        label = str(signal.get("chan_type") or signal.get("label") or signal.get("signal_id") or "").strip()
        bonus = _fmt(signal.get("bonus"))
        if label:
            labels.append(f"{label}(+{bonus})" if bonus else label)
    return ",".join(labels)


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


def _load_screening_inputs_with_lock_fallback(
    provider: Any,
    symbols: list[str],
    trade_date: str,
    *,
    storage: DuckDBStorage,
    trade_days: int,
) -> tuple[list[ScreeningInput], list[str]]:
    if not symbols:
        return [], []
    if not hasattr(provider, "load_screening_inputs"):
        return [], ["symbol_data: provider_unavailable"]
    try:
        return (
            provider.load_screening_inputs(
                symbols,
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
            inputs = _load_screening_inputs_from_cache(
                readonly_storage,
                symbols,
                trade_date,
                trade_days=trade_days,
            )
        except Exception as cache_exc:
            if not _is_duckdb_lock_error(cache_exc):
                raise
            return [], [f"symbol_data: duckdb_cache_unavailable_after_lock: {cache_exc}"]
        return inputs, [f"symbol_data: duckdb_readonly_cache_after_lock: {exc}"]


def _load_screening_inputs_from_cache(
    storage: DuckDBStorage,
    symbols: list[str],
    trade_date: str,
    *,
    trade_days: int,
) -> list[ScreeningInput]:
    cached = _load_all_screening_inputs_from_cache(storage, trade_date, trade_days=trade_days)
    by_symbol = {item.ts_code: item for item in cached}
    return [by_symbol[symbol] for symbol in symbols if symbol in by_symbol]


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
        chan_signals = _chan_buy_signals(result.events)
        chan_score = _chan_score(chan_signals)
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
                chan_score=chan_score,
                chan_signals=chan_signals,
                ranking_score=round(float(result.score) + hot_score + chan_score, 4),
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


def _chan_buy_signals(events: Iterable[SignalEvent]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if str(getattr(event, "side", "") or "").lower() != "buy":
            continue
        signal_id = str(getattr(event, "signal_id", "") or "").strip()
        label = str(getattr(event, "label", "") or "").strip()
        category = str(getattr(event, "category", "") or "").strip()
        reason = str(getattr(event, "reason", "") or "").strip()
        components = [str(item) for item in getattr(event, "components", []) or []]
        related_chan = [str(item) for item in getattr(event, "related_chan", []) or []]
        haystack = " ".join([signal_id, label, category, reason, *components, *related_chan]).lower()
        raw_text = " ".join([signal_id, label, category, reason, *components, *related_chan])
        if category != "chan" and "chan" not in haystack and "缠论" not in raw_text and not related_chan:
            continue
        if "_sell" in haystack or ("卖" in label and "买" not in label):
            continue
        chan_type, bonus = _chan_signal_bonus(haystack, raw_text)
        key = signal_id or label
        if key in seen:
            continue
        seen.add(key)
        signals.append(
            {
                "signal_id": signal_id,
                "label": label,
                "category": category,
                "confidence": getattr(event, "confidence", 0.0),
                "score": getattr(event, "score", 0.0),
                "chan_type": chan_type,
                "bonus": bonus,
            }
        )
    return signals


def _chan_signal_bonus(haystack: str, raw_text: str) -> tuple[str, float]:
    if "chan_second_third_overlap" in haystack or "二三买" in raw_text:
        return "二三买重合", 8.0
    if "chan_third_buy" in haystack or "三买" in raw_text or "❸买" in raw_text:
        return "三买", 6.0
    if "chan_second_buy" in haystack or "二买" in raw_text or "❷买" in raw_text:
        return "二买", 4.0
    if "chan_first_buy" in haystack or "一买" in raw_text or "❶买" in raw_text:
        return "一买", 3.0
    if (
        "chan_center_oscillation_low" in haystack
        or "graph_chan_center_b_complete" in haystack
        or "中枢低吸" in raw_text
        or "中枢 b" in raw_text
    ):
        return "中枢低吸", 3.0
    return "缠论共振", 2.0


def _chan_score(signals: list[dict[str, Any]]) -> float:
    if not signals:
        return 0.0
    return round(min(CHAN_SCORE_CAP, sum(max(0.0, _float(item.get("bonus"))) for item in signals)), 4)


def _signal_ranking_score(result: SignalAnalysisResult, *, stock_hot: Any, stock_basic: dict[str, Any]) -> float:
    buy_events = [event for event in result.events if event.side == "buy"]
    confidence_bonus = sum(max(0.0, _float(event.confidence)) for event in buy_events[:5]) * 0.35
    event_bonus = min(2.0, len(buy_events) * 0.35)
    industry_bonus = 0.1 if _industry_label(stock_basic) else 0.0
    return round(
        _float(result.score)
        + _hot_sector_score_for_symbol(result.ts_code, stock_hot)
        + _chan_score(_chan_buy_signals(result.events))
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
    seen_states: set[tuple[str, ...]] = set()
    steps = 0
    while _is_over_concentrated(selected) and steps < len(all_candidates):
        state = tuple(candidate.ts_code for candidate in selected)
        if state in seen_states:
            break
        seen_states.add(state)
        steps += 1
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
        if diversity_gain <= 0:
            continue
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
) -> LLMRankResult:
    def build_prompt(pool: list[OpportunityCandidate]) -> str:
        return _build_opportunity_llm_prompt(
            pool,
            market_context=market_context,
            trade_date=trade_date,
            limit=limit,
        )

    budgeted = prepare_llm_context_prompt(
        candidates,
        settings=settings,
        build_prompt=build_prompt,
        min_candidates=limit,
    )
    if budgeted.skipped:
        raise LLMContextBudgetExceeded(budgeted.warnings)
    pool = budgeted.candidates
    prompt = budgeted.prompt
    warnings = list(budgeted.warnings)
    llm = _build_llm(llm_factory, settings=settings)
    try:
        data = _chat_json(llm, [{"role": "user", "content": prompt}])
    except Exception as exc:
        if not _is_context_length_error(exc) or len(pool) <= 1:
            raise
        retry_count = max(1, len(pool) // 2)
        if retry_count >= len(pool):
            retry_count = len(pool) - 1
        retry_pool = pool[:retry_count]
        retry_budgeted = prepare_llm_context_prompt(
            retry_pool,
            settings=settings,
            build_prompt=build_prompt,
            min_candidates=1,
        )
        warnings.extend(retry_budgeted.warnings)
        warnings.append(f"llm_context_budget: retry_reduced_pool {len(pool)}->{len(retry_budgeted.candidates)} after_context_error")
        if retry_budgeted.skipped:
            raise LLMContextBudgetExceeded(warnings)
        pool = retry_budgeted.candidates
        prompt = retry_budgeted.prompt
        data = _chat_json(llm, [{"role": "user", "content": prompt}])
    ranked = _parse_llm_rankings(data, pool, limit=limit)
    return LLMRankResult(
        candidates=ranked,
        warnings=tuple(_dedupe(warnings)),
        llm_pool_count=len(pool),
    )


def _build_opportunity_llm_prompt(
    candidates: list[OpportunityCandidate],
    *,
    market_context: dict[str, Any],
    trade_date: str,
    limit: int,
) -> str:
    payload = {
        "trade_date": trade_date,
        "limit": limit,
        "llm_pool_count": len(candidates),
        "market_context": _compact_market_context(market_context),
        "hot_sector_policy": "热点板块只作为优先加权，不能把热点当作上涨保证；解释应基于信号和持续热点共振。",
        "chan_policy": "chan_score 来自 SATS Analyze 的结构化缠论买点/共振信号，只能作为排序证据，不能保证上涨。",
        "diversity_policy": "若候选分数接近，最终 Top N 应避免全部来自同一交易板块、行业或热点概念；只有信号明显更强时才允许集中。",
        "candidate_boundary": "只能从 candidates 中选择和排序，不得新增、替换或补齐未提供的股票。",
        "candidates": [candidate.to_llm_context() for candidate in candidates],
    }
    return (
        "你是 SATS A 股短线机会排序助手。请只基于 JSON 中的真实结构化数据排序，"
        "只能从 candidates 里选择，不得新增股票或为了凑数量补齐弱信号；"
        "优先解释技术信号、3-5 日持续热点板块共振和 SATS Analyze 缠论买点加分；"
        "缠论加分只能作为结构化排序证据，不能写成上涨保证。"
        "分数接近时避免 Top N 全部来自同一交易板块、行业或热点概念。"
        "不要编造新闻、题材、价格或财务字段。返回严格 JSON："
        '{"rankings":[{"ts_code":"000001.SZ","reason":"...","entry_trigger":"...","invalidation":"...","risk":"..."}]}。'
        "最多返回 limit 只。\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


def _parse_llm_rankings(data: dict[str, Any], candidates: list[OpportunityCandidate], *, limit: int) -> list[OpportunityCandidate]:
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


def prepare_llm_context_prompt(
    candidates: list[OpportunityCandidate],
    *,
    settings: Settings,
    build_prompt: Callable[[list[OpportunityCandidate]], str],
    min_candidates: int,
) -> LLMContextBudgetResult:
    original_count = len(candidates)
    current_count = original_count
    input_budget = _llm_context_input_budget_tokens(settings)
    warnings: list[str] = []
    while True:
        pool = candidates[:current_count]
        prompt = build_prompt(pool)
        estimated = estimate_llm_message_tokens(prompt)
        if estimated <= input_budget:
            if current_count < original_count:
                warnings.append(
                    f"llm_context_budget: reduced_pool {original_count}->{current_count} "
                    f"estimated_tokens={estimated} budget={input_budget}"
                )
            return LLMContextBudgetResult(
                candidates=pool,
                prompt=prompt,
                estimated_tokens=estimated,
                input_budget_tokens=input_budget,
                warnings=tuple(warnings),
            )
        if current_count <= 1:
            warnings.append(
                f"llm_context_budget: prompt_too_large estimated_tokens={estimated} budget={input_budget}"
            )
            return LLMContextBudgetResult(
                candidates=[],
                prompt="",
                estimated_tokens=estimated,
                input_budget_tokens=input_budget,
                warnings=tuple(warnings),
                skipped=True,
            )
        if current_count > max(1, min_candidates):
            target = max(int(min_candidates), int(current_count * input_budget / max(estimated, 1) * 0.85))
            if target >= current_count:
                target = max(int(min_candidates), current_count // 2)
        else:
            target = max(1, int(current_count * input_budget / max(estimated, 1) * 0.85))
            if target >= current_count:
                target = current_count - 1
        current_count = max(1, target)


def estimate_llm_message_tokens(message: str) -> int:
    text = str(message or "")
    return max(1, len(text), math.ceil(len(text.encode("utf-8")) / 2))


def llm_context_input_budget_tokens(settings: Settings) -> int:
    return _llm_context_input_budget_tokens(settings)


def _llm_context_input_budget_tokens(settings: Settings) -> int:
    limit = _positive_int(getattr(settings, "llm_context_limit_tokens", None)) or DEFAULT_LLM_CONTEXT_LIMIT_TOKENS
    reserve = _positive_int(getattr(settings, "llm_context_output_reserve_tokens", None))
    if reserve is None:
        reserve = min(8192, max(0, int(limit * 0.05)))
    safety_ratio = _safe_float(getattr(settings, "llm_context_safety_ratio", None), default=0.92)
    safety_ratio = max(0.1, min(1.0, safety_ratio))
    return max(1, int(limit * safety_ratio) - int(reserve))


def _is_context_length_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    if "context_length_exceeded" in text:
        return True
    if "maximum context length" in text:
        return True
    return "context" in text and ("length" in text or "token" in text) and ("exceed" in text or "too long" in text)


def is_llm_context_length_error(exc: Exception) -> bool:
    return _is_context_length_error(exc)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


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


def _truncate_text(value: Any, limit: int = 120) -> str:
    text = str(value or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _compact_event_for_llm(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {"label": _truncate_text(event, 80)}
    return {
        "signal_id": event.get("signal_id"),
        "label": event.get("label"),
        "category": event.get("category"),
        "side": event.get("side"),
        "confidence": event.get("confidence"),
        "score": event.get("score"),
        "reason": _truncate_text(event.get("reason"), 120),
        "risk_flags": [_truncate_text(item, 80) for item in list(event.get("risk_flags") or [])[:5]],
        "components": [_truncate_text(item, 80) for item in list(event.get("components") or [])[:6]],
        "related_chan": [_truncate_text(item, 80) for item in list(event.get("related_chan") or [])[:6]],
    }


def _compact_hot_sector_for_llm(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"name": _truncate_text(item, 40)}
    return {
        "sector_code": item.get("sector_code"),
        "name": item.get("name"),
        "sector_type": item.get("sector_type"),
        "heat_score": item.get("heat_score"),
        "return_5d": item.get("return_5d"),
        "return_3d": item.get("return_3d"),
        "latest_pct_chg": item.get("latest_pct_chg"),
        "up_days_5": item.get("up_days_5"),
        "up_days_3": item.get("up_days_3"),
    }


def _compact_chan_signal_for_llm(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"label": _truncate_text(item, 60)}
    return {
        "signal_id": item.get("signal_id"),
        "label": item.get("label"),
        "category": item.get("category"),
        "confidence": item.get("confidence"),
        "score": item.get("score"),
        "chan_type": item.get("chan_type"),
        "bonus": item.get("bonus"),
    }


def _compact_indicator_for_llm(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {}
    result = {
        "technical": payload.get("technical", {}),
        "volume": payload.get("volume", {}),
        "support_resistance": payload.get("support_resistance", {}),
        "moneyflow": payload.get("moneyflow", {}),
        "fundamentals": payload.get("fundamentals", {}),
        "data_sources": payload.get("data_sources", {}),
    }
    factor = payload.get("factor")
    if isinstance(factor, dict):
        result["factor"] = {
            "profile": factor.get("profile"),
            "score": factor.get("score"),
            "coverage": factor.get("coverage"),
            "missing_factors": list(factor.get("missing_factors") or [])[:12],
        }
    return result


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


def _compact_market_context(payload: dict[str, Any], *, daily_tail_limit: int = 0) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        "trade_date": payload.get("trade_date"),
        "requested_indices": payload.get("requested_indices"),
        "requested_dimensions": payload.get("requested_dimensions"),
        "requested_horizons": payload.get("requested_horizons"),
        "indices": [_compact_index_context(item, daily_tail_limit=daily_tail_limit) for item in list(payload.get("indices") or [])],
        "market_breadth": payload.get("market_breadth"),
        "limit_sentiment": payload.get("limit_sentiment"),
        "hot_sector_context": _compact_hot_sector_context(payload.get("hot_sector_context")),
        "market_plan_source": payload.get("market_plan_source"),
        "missing_fields": payload.get("missing_fields"),
        "data_sources": payload.get("data_sources"),
    }


def _compact_index_context(item: dict[str, Any], *, daily_tail_limit: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    result = {
        "ts_code": item.get("ts_code"),
        "name": item.get("name"),
        "trade_date": item.get("trade_date"),
        "latest": item.get("latest"),
        "technical": item.get("technical"),
        "missing_fields": item.get("missing_fields"),
        "data_sources": item.get("data_sources"),
    }
    if daily_tail_limit > 0:
        result["daily_tail"] = list(item.get("daily_tail") or [])[-daily_tail_limit:]
    return result


def _compact_hot_sector_context(payload: Any, *, limit: int = 10) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {}
    return {
        "trade_date": payload.get("trade_date"),
        "lookback_days": payload.get("lookback_days"),
        "hot_industries": [_compact_hot_sector_for_llm(item) for item in list(payload.get("hot_industries") or [])[:limit]],
        "hot_concepts": [_compact_hot_sector_for_llm(item) for item in list(payload.get("hot_concepts") or [])[:limit]],
        "missing_fields": payload.get("missing_fields"),
        "data_sources": payload.get("data_sources"),
    }


def _build_llm(llm_factory: Callable[..., Any], *, settings: Settings) -> Any:
    return build_light_fallback_llm(
        llm_factory,
        light_model_name=_light_model_name(settings),
        default_model_name=_main_model_name(settings),
        timeout_seconds=_llm_timeout_seconds(settings),
    )


def _light_model_name(settings: Settings) -> str:
    return str(getattr(settings, "light_model_name", "") or getattr(settings, "openai_model", "") or "")


def _main_model_name(settings: Settings) -> str:
    return str(getattr(settings, "openai_model", "") or "")


def _llm_timeout_seconds(settings: Settings) -> int | None:
    value = getattr(settings, "llm_timeout_seconds", None)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _chat_json(llm: Any, messages: list[dict[str, str]]) -> dict[str, Any]:
    if hasattr(llm, "chat_validated"):
        return llm.chat_validated(messages, _parse_response_json)
    response = llm.chat(messages)
    return _parse_response_json(response)


def _parse_response_json(response: Any) -> dict[str, Any]:
    return _parse_json_object(str(getattr(response, "content", "") or ""))


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


def _text_or_dash(value: Any) -> str:
    text = str(value or "").strip()
    return text or "--"


def _display_ljust(text: Any, width: int) -> str:
    value = _truncate_to_width(str(text), width)
    return value + (" " * max(0, width - _display_width(value)))


def _truncate_to_width(text: str, width: int) -> str:
    value = str(text)
    if width <= 0 or _display_width(value) <= width:
        return value
    if width == 1:
        return "."
    result = ""
    used = 0
    for char in value:
        char_width = _display_width(char)
        if used + char_width > width - 1:
            break
        result += char
        used += char_width
    return result + "."


def _display_width(text: str) -> int:
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


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
