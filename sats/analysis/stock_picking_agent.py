from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import pandas as pd

from sats.analysis.opportunity_discovery import (
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_DISCOVERY_LIMIT,
    DEFAULT_DISCOVERY_SIGNALS,
    LLMContextBudgetExceeded,
    LLMRankResult,
    OpportunityCandidate,
    OpportunityDiscoveryResult,
    extract_opportunity_discovery_limit,
    format_opportunity_discovery,
    is_generic_hot_sector_discovery_question,
    prepare_llm_context_prompt,
    run_opportunity_discovery,
    write_opportunity_report,
    _is_context_length_error,
    _resolve_trade_date,
)
from sats.analysis.stock_research_context import StockResearchContext, build_stock_research_context
from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.factors.profiles import DEFAULT_FACTOR_PROFILE
from sats.factors.service import snapshot_from_screening_inputs
from sats.llm import ChatLLM, build_light_fallback_llm
from sats.rag.knowledge import infer_stock_collections
from sats.skills import Skill, default_skills_dir, find_skill, load_skills, match_skills
from sats.storage.duckdb import DuckDBStorage
from sats.stock_basic_lookup import match_stock_name
from sats.symbols import normalize_symbols, normalize_ts_code


DEFAULT_AGENT_PROFILE = "technical_short_up"
STOCK_PICKING_ACTION = "stock_picking_agent"
ThemeUniverseSource = Literal["ths_sector", "sw_sector", "llm_theme_universe", "none"]
SW_SECTOR_TYPES = ("sw_l1", "sw_l2", "sw_l3")
_THEME_STOP_WORDS = {
    "a股",
    "个股",
    "大盘",
    "市场",
    "行业",
    "机会",
    "上涨",
    "未来几天",
    "未来几日",
    "短线",
    "长线",
    "股票",
    "板块",
    "概念",
    "题材",
    "热点",
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
    "根据今天的热点",
    "根据今日热点",
    "根据当前热点",
    "基本面",
    "低估值",
    "风险",
    "稳健",
    "资金流",
    "龙头",
    "st",
    "pe",
    "pb",
    "roe",
}
_GENERIC_SHORT_UP_QUANTIFIERS = (
    "一些",
    "几个",
    "几只",
    "几支",
    "若干",
    "多只",
    "十只",
    "十支",
    "10只",
    "10支",
)
_THEME_ALIASES = {
    "存储": "存储芯片",
    "内存": "存储芯片",
    "内存存储": "存储芯片",
    "内存和存储": "存储芯片",
    "内存与存储": "存储芯片",
    "半导体存储": "存储芯片",
    "存储设备": "存储芯片",
    "存储器": "存储芯片",
    "固态硬盘": "存储芯片",
    "闪存": "存储芯片",
    "dram": "存储芯片",
    "nand": "存储芯片",
    "flash": "存储芯片",
    "ssd": "存储芯片",
    "dram/nand": "存储芯片",
    "dramnand": "存储芯片",
    "dram和nand": "存储芯片",
    "dram与nand": "存储芯片",
}


@dataclass(frozen=True, slots=True)
class ThemeUniverseStock:
    ts_code: str
    name: str = ""
    reason: str = ""
    confidence: float = 0.0
    relation_type: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "reason": self.reason,
            "confidence": self.confidence,
            "relation_type": self.relation_type,
            "source": self.source,
        }

    def to_llm_context(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "reason": _short_text(self.reason, 120),
            "confidence": self.confidence,
            "relation_type": self.relation_type,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ThemeUniverse:
    theme: str = ""
    stocks: tuple[ThemeUniverseStock, ...] = ()
    matched_sector: str = ""
    source: ThemeUniverseSource = "none"
    confidence: float = 0.0
    warnings: tuple[str, ...] = ()

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(stock.ts_code for stock in self.stocks)

    @property
    def count(self) -> int:
        return len(self.stocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "count": self.count,
            "stocks": [stock.to_dict() for stock in self.stocks],
            "symbols": list(self.symbols),
            "matched_sector": self.matched_sector,
            "source": self.source,
            "confidence": self.confidence,
            "warnings": list(self.warnings),
        }

    def to_llm_context(self, *, stock_summary_limit: int = 50) -> dict[str, Any]:
        stocks = list(self.stocks)
        return {
            "theme": self.theme,
            "count": self.count,
            "stocks": [stock.to_llm_context() for stock in stocks[:stock_summary_limit]],
            "stock_summary_truncated_count": max(0, len(stocks) - stock_summary_limit),
            "symbols": list(self.symbols[: max(stock_summary_limit, 0)]),
            "matched_sector": self.matched_sector,
            "source": self.source,
            "confidence": self.confidence,
            "warnings": [_short_text(item, 160) for item in self.warnings[:20]],
        }


@dataclass(frozen=True, slots=True)
class StockPickingPlan:
    query: str
    profile: str
    horizon: str
    signals: str
    theme: str = ""
    skills: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    ranking_policy: str = ""
    data_requirements: tuple[str, ...] = ("market_context", "opportunity_discovery")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "profile": self.profile,
            "horizon": self.horizon,
            "signals": self.signals,
            "theme": self.theme,
            "skills": list(self.skills),
            "collections": list(self.collections),
            "constraints": list(self.constraints),
            "ranking_policy": self.ranking_policy,
            "data_requirements": list(self.data_requirements),
        }


@dataclass(slots=True)
class StockPickingAgentResult:
    query: str
    plan: StockPickingPlan
    discovery: OpportunityDiscoveryResult
    theme_universe: ThemeUniverse = ThemeUniverse()
    evidence_sources: tuple[dict[str, Any], ...] = ()
    evidence_context: str = ""
    llm_unavailable: bool = False

    @property
    def candidates(self) -> list[OpportunityCandidate]:
        return self.discovery.candidates

    @property
    def report_path(self) -> str | None:
        return self.discovery.report_path

    @property
    def message(self) -> str:
        message = self.discovery.message
        if not message:
            return ""
        theme_line = _format_theme_universe_line(self.theme_universe)
        if theme_line and theme_line not in message:
            return f"{theme_line}\n{message}"
        return message

    def to_dict(self) -> dict[str, Any]:
        payload = self.discovery.to_dict()
        payload.update(
            {
                "query": self.query,
                "message": self.message,
                "agent_plan": self.plan.to_dict(),
                "theme_universe": self.theme_universe.to_dict(),
                "evidence_sources": list(self.evidence_sources),
                "llm_unavailable": bool(self.llm_unavailable or self.discovery.llm_unavailable),
                "opportunity_discovery": self.discovery.to_dict(),
                "data_policy": (
                    "SATS stock-picking Agent used local skills and DuckDB knowledge as methodology context; "
                    "real candidates came from structured SATS A-share data and Analyze signals. "
                    "Results are research candidates only, not trading instructions."
                ),
            }
        )
        return payload

    @property
    def system_message(self) -> str:
        return (
            "以下是 SATS 自然语言选股 Agent 基于真实本地数据、skills 和知识库证据生成的 A 股候选上下文。"
            "skills/RAG 只作为方法论和证据来源；不要编造未提供的行情、新闻、财务或题材，"
            "不要承诺未来必然上涨，必须给出触发条件、失效条件和风险提示。\n"
            + json.dumps(self.to_dict(), ensure_ascii=False, default=str)
        )

    def to_llm_context(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "message": self.message,
            "agent_plan": self.plan.to_dict(),
            "theme_universe": self.theme_universe.to_llm_context(),
            "evidence_sources": [_compact_evidence_source(item) for item in self.evidence_sources[:12]],
            "evidence_context": _short_text(self.evidence_context, 3000),
            "llm_unavailable": bool(self.llm_unavailable or self.discovery.llm_unavailable),
            "opportunity_discovery": self.discovery.to_llm_context(),
            "data_policy": (
                "SATS stock-picking Agent used local skills and DuckDB knowledge as methodology context; "
                "real candidates came from structured SATS A-share data and Analyze signals. "
                "Results are research candidates only, not trading instructions."
            ),
        }

    def system_message_for_llm(self) -> str:
        return (
            "以下是 SATS 自然语言选股 Agent 基于真实本地数据、skills 和知识库证据生成的 A 股候选精简上下文。"
            "skills/RAG 只作为方法论和证据来源；该上下文已移除原始长 K 线、全量热点成员映射、"
            "冗长因子明细和重复 discovery 嵌套。不要编造未提供的行情、新闻、财务或题材，"
            "不要承诺未来必然上涨，必须给出触发条件、失效条件和风险提示。\n"
            + json.dumps(self.to_llm_context(), ensure_ascii=False, default=str)
        )


def build_stock_picking_plan(
    query: str,
    *,
    skills: list[Skill],
    signals: str = DEFAULT_DISCOVERY_SIGNALS,
    hot_sector_enabled: bool = True,
) -> StockPickingPlan:
    text = str(query or "").strip()
    profile = _profile_from_query(text)
    matched = match_skills(text, skills, limit=5) if text else []
    skill_ids = _profile_skill_ids(profile, hot_sector_enabled=hot_sector_enabled)
    skill_ids = _dedupe([skill.id for skill in matched] + skill_ids)
    skill_ids = tuple(skill_id for skill_id in skill_ids if find_skill(skills, skill_id) is not None)
    collections = _collections_for_profile(text, profile, skill_ids)
    constraints = _constraints_from_query(text)
    return StockPickingPlan(
        query=text,
        profile=profile,
        horizon=_horizon_from_query(text),
        signals=str(signals or DEFAULT_DISCOVERY_SIGNALS).strip() or DEFAULT_DISCOVERY_SIGNALS,
        theme=_extract_theme_from_query(text),
        skills=skill_ids,
        collections=collections,
        constraints=constraints,
        ranking_policy=_ranking_policy(profile, constraints),
    )


def run_stock_picking_agent(
    *,
    query: str = "",
    settings: Settings,
    storage: DuckDBStorage,
    provider: Any | None = None,
    skills: list[Skill] | None = None,
    trade_date: str | None = None,
    signals: str = DEFAULT_DISCOVERY_SIGNALS,
    limit: int | None = None,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    lookback_days: int = 180,
    score_threshold: float = 58.0,
    reports_dir: Path | None = None,
    report: bool = True,
    llm_enabled: bool = True,
    llm_factory: Callable[..., Any] | None = None,
    hot_sector_enabled: bool = True,
    hot_sector_days: int = 5,
    factor_enabled: bool = True,
    factor_profile: str = DEFAULT_FACTOR_PROFILE,
    factor_weight: float = 2.0,
    market_indices: list[str] | tuple[str, ...] | None = None,
    market_dimensions: list[str] | tuple[str, ...] | None = None,
    market_horizons: list[str] | tuple[str, ...] | None = None,
    market_plan_source: str | None = None,
    progress: Any | None = None,
) -> StockPickingAgentResult:
    resolved_skills = skills if skills is not None else _load_project_skills(settings)
    plan = build_stock_picking_plan(
        query,
        skills=resolved_skills,
        signals=signals,
        hot_sector_enabled=hot_sector_enabled,
    )
    provider = provider or AStockDataProvider(settings)
    resolved_trade_date = trade_date
    if resolved_trade_date is None:
        try:
            resolved_trade_date = _resolve_trade_date(storage=storage, provider=provider)
        except Exception:
            resolved_trade_date = None
    theme_universe = resolve_theme_universe(
        query,
        provider,
        storage,
        llm_factory or ChatLLM,
        settings=settings,
        trade_date=resolved_trade_date,
        hot_sector_days=hot_sector_days,
        llm_enabled=llm_enabled,
    )
    if plan.theme != theme_universe.theme:
        plan = StockPickingPlan(
            query=plan.query,
            profile=plan.profile,
            horizon=plan.horizon,
            signals=plan.signals,
            theme=theme_universe.theme,
            skills=plan.skills,
            collections=plan.collections,
            constraints=plan.constraints,
            ranking_policy=plan.ranking_policy,
            data_requirements=plan.data_requirements,
        )
    research_context = _safe_research_context(query or _profile_query(plan), settings=settings, collections=plan.collections)
    parsed_limit = extract_opportunity_discovery_limit(query)
    final_limit = max(1, int(limit if limit is not None else parsed_limit or DEFAULT_DISCOVERY_LIMIT))
    scan_limit = max(final_limit, int(candidate_limit))
    if theme_universe.theme and not theme_universe.symbols:
        source_line = _format_theme_universe_line(theme_universe)
        discovery = OpportunityDiscoveryResult(
            trade_date=str(resolved_trade_date or ""),
            signals=plan.signals,
            candidates=[],
            candidate_count=0,
            scanned_count=0,
            missing_fields=list(theme_universe.warnings),
            message=source_line or f"主题股票池: 未能确认 {theme_universe.theme} 相关 A 股股票，不关联无关板块",
        )
        return StockPickingAgentResult(
            query=str(query or "").strip(),
            plan=plan,
            discovery=discovery,
            theme_universe=theme_universe,
            evidence_sources=research_context.sources if research_context is not None else (),
            evidence_context=research_context.system_message if research_context is not None else "",
            llm_unavailable=False,
        )
    discovery = run_opportunity_discovery(
        settings=settings,
        storage=storage,
        provider=provider,
        symbols=list(theme_universe.symbols) if theme_universe.symbols else None,
        trade_date=resolved_trade_date,
        signals=plan.signals,
        limit=scan_limit,
        candidate_limit=scan_limit,
        lookback_days=lookback_days,
        score_threshold=score_threshold,
        reports_dir=None,
        report=False,
        llm_enabled=False,
        hot_sector_enabled=hot_sector_enabled,
        hot_sector_days=hot_sector_days,
        market_indices=market_indices,
        market_dimensions=market_dimensions,
        market_horizons=market_horizons,
        market_plan_source=market_plan_source,
        progress=progress,
    )
    llm_unavailable = False
    if discovery.candidates:
        ranked_local = _rank_locally(discovery.candidates, plan=plan)
        if factor_enabled and resolved_trade_date:
            _apply_factor_overlay(
                ranked_local[:scan_limit],
                provider=provider,
                storage=storage,
                trade_date=str(resolved_trade_date),
                profile=factor_profile,
                weight=factor_weight,
                lookback_days=max(lookback_days, 260),
            )
            ranked_local = sorted(ranked_local, key=lambda item: (-item.ranking_score, -item.local_score, item.ts_code))
        discovery.candidates = ranked_local[:final_limit]
        discovery.llm_pool_count = 0
        if llm_enabled:
            try:
                rank_result = _rank_with_agent_llm(
                    ranked_local[:scan_limit],
                    all_candidates=ranked_local[:scan_limit],
                    settings=settings,
                    plan=plan,
                    theme_universe=theme_universe,
                    research_context=research_context,
                    limit=final_limit,
                    llm_factory=llm_factory or ChatLLM,
                )
                discovery.candidates = rank_result.candidates
                discovery.llm_pool_count = rank_result.llm_pool_count
                if rank_result.warnings:
                    discovery.missing_fields = list(_dedupe([*discovery.missing_fields, *rank_result.warnings]))
                if rank_result.llm_unavailable:
                    llm_unavailable = True
                    discovery.llm_unavailable = True
            except LLMContextBudgetExceeded as exc:
                llm_unavailable = True
                discovery.llm_unavailable = True
                discovery.llm_pool_count = 0
                discovery.missing_fields = list(_dedupe([*discovery.missing_fields, *exc.warnings]))
            except Exception:
                llm_unavailable = True
                discovery.llm_unavailable = True
                discovery.llm_pool_count = 0
        if report and reports_dir is not None:
            discovery.report_path = str(write_opportunity_report(discovery, reports_dir=reports_dir))
    return StockPickingAgentResult(
        query=str(query or "").strip(),
        plan=plan,
        discovery=discovery,
        theme_universe=theme_universe,
        evidence_sources=research_context.sources if research_context is not None else (),
        evidence_context=research_context.system_message if research_context is not None else "",
        llm_unavailable=llm_unavailable,
    )


def format_stock_picking_agent_result(result: StockPickingAgentResult) -> str:
    theme_line = _format_theme_universe_line(result.theme_universe)
    if result.message:
        if theme_line and theme_line not in result.message:
            return f"{theme_line}\n{result.message}"
        return result.message
    if not result.candidates:
        if theme_line:
            return f"{theme_line}\n无结果"
        return "无结果"
    header = [
        f"选股Agent: {result.plan.profile} / {result.plan.horizon}",
    ]
    if theme_line:
        header.append(theme_line)
    if result.plan.skills:
        header.append(f"使用skill: {', '.join(result.plan.skills)}")
    if result.plan.collections:
        header.append(f"知识库: {', '.join(result.plan.collections)}")
    if result.plan.constraints:
        header.append(f"约束: {', '.join(result.plan.constraints)}")
    return "\n".join([*header, "", format_opportunity_discovery(result.discovery)])


def resolve_theme_universe(
    query: str,
    provider: Any,
    storage: DuckDBStorage,
    llm_factory: Callable[..., Any] | None,
    *,
    settings: Settings | None = None,
    trade_date: str | None = None,
    hot_sector_days: int = 5,
    llm_enabled: bool = True,
    max_symbols: int = 30,
) -> ThemeUniverse:
    theme = _canonical_theme(_extract_theme_from_query(query))
    if not theme:
        return ThemeUniverse()

    ths_universe = _resolve_ths_theme_universe(
        theme,
        provider=provider,
        storage=storage,
        trade_date=trade_date,
        hot_sector_days=hot_sector_days,
        max_symbols=max_symbols,
    )
    if ths_universe.source == "ths_sector" and ths_universe.symbols:
        return ths_universe
    sw_universe = _resolve_sw_theme_universe(
        theme,
        provider=provider,
        storage=storage,
        max_symbols=max_symbols,
    )
    if sw_universe.source == "sw_sector" and sw_universe.symbols:
        return sw_universe
    if not llm_enabled or llm_factory is None:
        warnings = [*ths_universe.warnings, *sw_universe.warnings, "llm_theme_universe: disabled"]
        return ThemeUniverse(
            theme=theme,
            matched_sector=ths_universe.matched_sector or sw_universe.matched_sector,
            source="none",
            warnings=tuple(_dedupe(warnings)),
        )
    matched_sector = ths_universe.matched_sector or sw_universe.matched_sector
    matched_sector_source = "ths" if ths_universe.matched_sector else "sw" if sw_universe.matched_sector else ""
    llm_universe = _resolve_llm_theme_universe(
        theme,
        provider=provider,
        storage=storage,
        llm_factory=llm_factory,
        settings=settings,
        prior_warnings=tuple(_dedupe([*ths_universe.warnings, *sw_universe.warnings])),
        matched_sector=matched_sector,
        matched_sector_source=matched_sector_source,
    )
    return llm_universe


def _resolve_ths_theme_universe(
    theme: str,
    *,
    provider: Any,
    storage: DuckDBStorage,
    trade_date: str | None,
    hot_sector_days: int,
    max_symbols: int,
) -> ThemeUniverse:
    warnings: list[str] = []
    sectors = _load_ths_sector_basic(provider=provider, storage=storage, trade_date=trade_date, hot_sector_days=hot_sector_days)
    if sectors.empty:
        return ThemeUniverse(theme=theme, source="none", warnings=("ths_sector: no_sector_basic",))
    match = _match_ths_sector(theme, sectors)
    if match is None:
        return ThemeUniverse(theme=theme, source="none")
    sector_code = str(match.get("sector_code") or "").strip()
    matched_sector = str(match.get("name") or theme).strip()
    if not sector_code:
        return ThemeUniverse(
            theme=theme,
            matched_sector=matched_sector,
            source="ths_sector",
            confidence=0.95,
            warnings=("ths_sector: missing_sector_code",),
        )
    members = _load_ths_sector_members(provider=provider, storage=storage, sector_codes=[sector_code])
    if members.empty:
        warnings.append(f"ths_sector_members: empty:{matched_sector}")
    stocks = _theme_stocks_from_ths_members(members, source="ths_sector", max_symbols=max_symbols)
    if not stocks:
        warnings.append(f"ths_sector_members: no_valid_symbols:{matched_sector}")
    return ThemeUniverse(
        theme=theme,
        stocks=tuple(stocks),
        matched_sector=matched_sector,
        source="ths_sector",
        confidence=0.98,
        warnings=tuple(_dedupe(warnings)),
    )


def _load_ths_sector_basic(
    *,
    provider: Any,
    storage: DuckDBStorage,
    trade_date: str | None,
    hot_sector_days: int,
) -> pd.DataFrame:
    if hasattr(provider, "load_ths_sector_basic"):
        try:
            frame = provider.load_ths_sector_basic(storage=storage)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        except Exception:
            pass
    try:
        frame = storage.get_sector_basic(sector_types=["industry", "concept"])
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            return frame
    except Exception:
        pass
    if trade_date and hasattr(provider, "load_hot_sector_context"):
        try:
            provider.load_hot_sector_context(
                trade_date,
                storage=storage,
                lookback_days=max(3, min(5, int(hot_sector_days))),
            )
        except Exception:
            pass
        try:
            frame = storage.get_sector_basic(sector_types=["industry", "concept"])
            if isinstance(frame, pd.DataFrame):
                return frame
        except Exception:
            pass
    return pd.DataFrame()


def _load_ths_sector_members(*, provider: Any, storage: DuckDBStorage, sector_codes: list[str]) -> pd.DataFrame:
    if hasattr(provider, "load_ths_sector_members"):
        try:
            frame = provider.load_ths_sector_members(sector_codes, storage=storage)
            if isinstance(frame, pd.DataFrame):
                return frame
        except Exception:
            pass
    try:
        return storage.get_sector_members(sector_codes)
    except Exception:
        return pd.DataFrame()


def _resolve_sw_theme_universe(
    theme: str,
    *,
    provider: Any,
    storage: DuckDBStorage,
    max_symbols: int,
) -> ThemeUniverse:
    warnings: list[str] = []
    sectors = _load_sw_sector_basic(provider=provider, storage=storage)
    if sectors.empty:
        return ThemeUniverse(theme=theme, source="none", warnings=("sw_sector: no_sector_basic",))
    match = _match_ths_sector(theme, sectors)
    if match is None:
        return ThemeUniverse(theme=theme, source="none")
    sector_code = str(match.get("sector_code") or "").strip()
    matched_sector = str(match.get("name") or theme).strip()
    if not sector_code:
        return ThemeUniverse(
            theme=theme,
            matched_sector=matched_sector,
            source="sw_sector",
            confidence=0.9,
            warnings=("sw_sector: missing_sector_code",),
        )
    members = _load_sw_sector_members(provider=provider, storage=storage, sector_codes=[sector_code])
    if members.empty:
        warnings.append(f"sw_sector_members: empty:{matched_sector}")
    stocks = _theme_stocks_from_ths_members(members, source="sw_sector", max_symbols=max_symbols)
    if not stocks:
        warnings.append(f"sw_sector_members: no_valid_symbols:{matched_sector}")
    return ThemeUniverse(
        theme=theme,
        stocks=tuple(stocks),
        matched_sector=matched_sector,
        source="sw_sector",
        confidence=0.94,
        warnings=tuple(_dedupe(warnings)),
    )


def _load_sw_sector_basic(*, provider: Any, storage: DuckDBStorage) -> pd.DataFrame:
    if hasattr(provider, "load_sw_sector_basic"):
        try:
            frame = provider.load_sw_sector_basic(storage=storage)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        except Exception:
            pass
    try:
        frame = storage.get_sector_basic(sector_types=list(SW_SECTOR_TYPES))
        if isinstance(frame, pd.DataFrame):
            return frame
    except Exception:
        pass
    return pd.DataFrame()


def _load_sw_sector_members(*, provider: Any, storage: DuckDBStorage, sector_codes: list[str]) -> pd.DataFrame:
    if hasattr(provider, "load_sw_sector_members"):
        try:
            frame = provider.load_sw_sector_members(sector_codes, storage=storage)
            if isinstance(frame, pd.DataFrame):
                return frame
        except Exception:
            pass
    try:
        return storage.get_sector_members(sector_codes)
    except Exception:
        return pd.DataFrame()


def _theme_stocks_from_ths_members(
    members: pd.DataFrame,
    *,
    source: str,
    max_symbols: int,
) -> list[ThemeUniverseStock]:
    if members is None or members.empty:
        return []
    stocks: list[ThemeUniverseStock] = []
    seen: set[str] = set()
    relation_type = "sw_member" if source == "sw_sector" else "ths_member"
    for _, row in members.iterrows():
        ts_code = normalize_ts_code(str(row.get("ts_code") or "").strip())
        if not _is_a_share_ts_code(ts_code) or ts_code in seen:
            continue
        seen.add(ts_code)
        stocks.append(
            ThemeUniverseStock(
                ts_code=ts_code,
                name=str(row.get("name") or "").strip(),
                relation_type=relation_type,
                source=source,
                confidence=0.98,
            )
        )
        if len(stocks) >= max_symbols:
            break
    return stocks


def _match_ths_sector(theme: str, sectors: pd.DataFrame) -> dict[str, Any] | None:
    if sectors.empty or "name" not in sectors.columns:
        return None
    theme_key = _theme_key(theme)
    if not theme_key:
        return None
    best: dict[str, Any] | None = None
    best_score = 0
    for _, row in sectors.iterrows():
        name = str(row.get("name") or "").strip()
        name_key = _theme_key(name)
        if not name_key:
            continue
        score = 0
        if name_key == theme_key:
            score = 100
        elif _strip_sector_suffix(name_key) == _strip_sector_suffix(theme_key):
            score = 96
        elif len(theme_key) >= 3 and theme_key in name_key:
            score = 92
        if score > best_score:
            best_score = score
            best = row.dropna().to_dict()
    return best if best_score >= 90 else None


def _resolve_llm_theme_universe(
    theme: str,
    *,
    provider: Any,
    storage: DuckDBStorage,
    llm_factory: Callable[..., Any],
    settings: Settings | None,
    prior_warnings: tuple[str, ...],
    matched_sector: str = "",
    matched_sector_source: str = "",
) -> ThemeUniverse:
    warnings = list(prior_warnings)
    stock_basic = _load_stock_basic_for_theme(provider=provider, storage=storage)
    if stock_basic.empty:
        warnings.append("stock_basic: unavailable")
    if matched_sector:
        matched_label = "申万" if matched_sector_source == "sw" else "同花顺"
        lookup_instruction = (
            f"{matched_label}命中了“{matched_sector}”板块，但板块成分股查询没有得到可校验的中国 A 股代码。"
            f"请回答“{matched_sector}板块有哪些 A 股股票？”。\n"
        )
    else:
        lookup_instruction = f"THS/同花顺和申万都没有找到“{theme}”相关行业/概念板块。请回答“{theme} 相关 A 股股票有哪些？”。\n"
    prompt = (
        "你是 SATS 的 A 股主题股票池解析器，只提供候选股票池线索，不做行情、热度、买卖或上涨判断。"
        f"{lookup_instruction}"
        "只返回你明确知道与该主题相关的具体中国 A 股上市公司；不要为了数量补充弱相关股票。"
        "不要返回板块名、行业名、ETF、指数、港股、美股，也不要从行业/板块名自行扩展成分股。"
        "返回严格 JSON，不能有 Markdown，结构必须是："
        '{"theme":"主题","stocks":[{"ts_code":"000000.SZ","name":"股票简称","reason":"与主题的直接关系",'
        '"relation_type":"manufacturer|materials|equipment|component_platform|other","confidence":0.0}],'
        '"uncertainties":["..."]}。不确定时 stocks 返回空数组。'
    )
    try:
        llm = _build_llm(llm_factory, settings=settings or object())
        data = _chat_json(llm, [{"role": "user", "content": prompt}])
    except Exception as exc:
        warnings.append(f"llm_theme_universe: {exc}")
        return ThemeUniverse(theme=theme, matched_sector=matched_sector, source="none", warnings=tuple(_dedupe(warnings)))
    stocks, validation_warnings = _validate_llm_theme_stocks(data, stock_basic=stock_basic)
    warnings.extend(validation_warnings)
    uncertainties = data.get("uncertainties")
    if isinstance(uncertainties, list):
        warnings.extend(f"llm_uncertainty: {item}" for item in uncertainties[:5] if str(item).strip())
    if not stocks:
        return ThemeUniverse(theme=theme, matched_sector=matched_sector, source="none", warnings=tuple(_dedupe(warnings)))
    return ThemeUniverse(
        theme=str(data.get("theme") or theme).strip() or theme,
        stocks=tuple(stocks),
        matched_sector=matched_sector,
        source="llm_theme_universe",
        confidence=0.75,
        warnings=tuple(_dedupe(warnings)),
    )


def _load_stock_basic_for_theme(*, provider: Any, storage: DuckDBStorage) -> pd.DataFrame:
    if hasattr(provider, "load_stock_basic"):
        try:
            frame = provider.load_stock_basic(storage=storage)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        except Exception:
            pass
    try:
        frame = storage.get_stock_basic()
        if isinstance(frame, pd.DataFrame):
            return frame
    except Exception:
        pass
    return pd.DataFrame()


def _validate_llm_theme_stocks(
    data: dict[str, Any],
    *,
    stock_basic: pd.DataFrame,
) -> tuple[list[ThemeUniverseStock], list[str]]:
    rows = data.get("stocks")
    if not isinstance(rows, list):
        return [], ["llm_theme_universe: stocks_not_list"]
    basic = _clean_theme_stock_basic(stock_basic)
    by_code = {str(row["ts_code"]): row for _, row in basic.iterrows()} if not basic.empty else {}
    stocks: list[ThemeUniverseStock] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            warnings.append(f"llm_stock_{index}: invalid_row")
            continue
        confidence = _float(row.get("confidence"), default=0.0)
        if confidence < 0.45:
            warnings.append(f"llm_stock_{index}: low_confidence")
        raw_code = str(row.get("ts_code") or "").strip()
        raw_name = str(row.get("name") or "").strip()
        ts_code = normalize_ts_code(raw_code)
        resolved = ""
        local_name = ""
        if _is_a_share_ts_code(ts_code) and ts_code in by_code:
            if raw_name and _name_conflicts_with_code(raw_name, ts_code, basic):
                matched = match_stock_name(raw_name, basic)
                if len(matched) == 1:
                    candidate = str(matched.iloc[0]["ts_code"])
                    if _is_a_share_ts_code(candidate):
                        warnings.append(f"llm_stock_{index}: code_name_conflict:{ts_code}:{raw_name}->resolved_by_name:{candidate}")
                        resolved = candidate
                        local_name = str(matched.iloc[0].get("name") or "").strip()
                if not resolved:
                    warnings.append(f"llm_stock_{index}: code_name_conflict:{ts_code}:{raw_name}:rejected")
                    continue
            else:
                resolved = ts_code
                local_name = str(by_code.get(ts_code, {}).get("name") or "").strip()
        elif raw_name:
            matched = match_stock_name(raw_name, basic)
            if len(matched) == 1:
                candidate = str(matched.iloc[0]["ts_code"])
                if _is_a_share_ts_code(candidate):
                    resolved = candidate
                    local_name = str(matched.iloc[0].get("name") or "").strip()
        if not resolved:
            label = raw_code or raw_name or f"row_{index}"
            warnings.append(f"llm_stock_{index}: unrecognized:{label}")
            continue
        if resolved in seen:
            warnings.append(f"llm_stock_{index}: duplicate:{resolved}")
            continue
        seen.add(resolved)
        stocks.append(
            ThemeUniverseStock(
                ts_code=resolved,
                name=local_name or raw_name,
                reason=str(row.get("reason") or "").strip(),
                confidence=confidence,
                relation_type=str(row.get("relation_type") or "").strip() or "other",
                source="llm_theme_universe",
            )
        )
    return stocks, warnings


def _clean_theme_stock_basic(stock_basic: pd.DataFrame) -> pd.DataFrame:
    if stock_basic is None or stock_basic.empty:
        return pd.DataFrame(columns=["ts_code", "symbol", "name"])
    data = stock_basic.copy()
    for column in ("ts_code", "symbol", "name"):
        if column not in data.columns:
            data[column] = ""
        data[column] = data[column].fillna("").astype(str)
    data["ts_code"] = data["ts_code"].map(normalize_ts_code)
    data["symbol"] = data["symbol"].where(data["symbol"].astype(bool), data["ts_code"].str[:6])
    data = data[data["ts_code"].map(_is_a_share_ts_code)]
    return data.drop_duplicates(subset=["ts_code"]).reset_index(drop=True)


def _name_conflicts_with_code(name: str, ts_code: str, stock_basic: pd.DataFrame) -> bool:
    matched = match_stock_name(name, stock_basic)
    if matched.empty:
        return False
    return not any(str(value) == ts_code for value in matched["ts_code"].astype(str).tolist())


def _extract_theme_from_query(query: str) -> str:
    text = str(query or "").strip()
    if not text:
        return ""
    if is_generic_hot_sector_discovery_question(text) or _is_generic_short_up_discovery_query(text):
        return ""
    patterns = [
        r"([A-Za-z][A-Za-z0-9+\-_/]{1,24})\s*(?:相关(?:股票|个股|A股)?|概念股|概念|板块|题材|产业链)",
        r"([\u4e00-\u9fffA-Za-z0-9+\-_/]{2,18})\s*(?:相关(?:股票|个股|A股)?|概念股|概念|板块|题材|产业链)",
        r"(?:找|筛|推荐|给出|分析|看看|关注)\s*([\u4e00-\u9fffA-Za-z0-9+\-_/]{2,18})(?:相关|概念股|产业链)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        theme = _clean_theme_candidate(match.group(1))
        if theme:
            return theme
    if any(term in text for term in ("股票", "个股", "概念", "题材", "板块", "上涨", "机会")):
        for match in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9+\-_/]{2,16})(?![A-Za-z0-9])", text):
            theme = _clean_theme_candidate(match.group(1))
            if theme:
                return theme
    return ""


def _is_generic_short_up_discovery_query(value: str) -> bool:
    text = re.sub(r"\s+", "", str(value or "").strip())
    if not text:
        return False
    has_action = any(term in text for term in ("给出", "推荐", "筛选", "找出", "选出", "挑选", "列出", "返回"))
    has_stock = any(term in text for term in ("股票", "个股", "标的", "候选"))
    has_upside = any(term in text for term in ("上涨", "涨", "短线", "机会", "强势", "未来几天", "未来几日", "明天", "下周"))
    has_quantity = any(term in text for term in _GENERIC_SHORT_UP_QUANTIFIERS) or bool(
        re.search(r"(?:top|前)?[0-9一二两三四五六七八九十百]+(?:只|支|个|名)?", text, flags=re.IGNORECASE)
    )
    has_explicit_theme_marker = any(term in text for term in ("相关", "概念股", "概念", "板块", "题材", "产业链", "行业", "赛道"))
    return has_action and has_stock and has_upside and has_quantity and not has_explicit_theme_marker


def _clean_theme_candidate(value: str) -> str:
    text = str(value or "").strip(" ，,。.!！?？:：；;、（）()[]【】“”\"'")
    for prefix in (
        "根据今天的",
        "根据今日的",
        "根据当前的",
        "根据近期的",
        "根据今天",
        "根据今日",
        "根据当前",
        "根据近期",
        "今天的",
        "今日的",
        "当前的",
        "近期的",
        "帮我",
        "请",
        "优先",
        "偏向",
        "找",
        "筛",
        "推荐",
        "给出",
        "分析",
        "看看",
        "关注",
        "有没有",
    ):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    for suffix in ("相关股票", "相关个股", "相关A股", "概念股", "相关", "股票", "个股", "概念", "板块", "题材", "产业链", "的"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    if not text or _theme_key(text) in _THEME_STOP_WORDS or _is_generic_short_up_theme_phrase(text):
        return ""
    if len(text) > 24:
        return ""
    canonical = _canonical_theme(text)
    if canonical:
        return canonical
    return text.upper() if re.fullmatch(r"[A-Za-z0-9+\-_/]+", text) else text


def _canonical_theme(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    alias = _THEME_ALIASES.get(_theme_alias_key(text))
    return alias or text


def _theme_alias_key(value: str) -> str:
    text = _theme_key(value)
    return re.sub(r"[，,、\s]+", "", text)


def _is_generic_short_up_theme_phrase(value: str) -> bool:
    text = _theme_key(value)
    if not text:
        return True
    if any(term in text for term in ("上涨", "短线", "机会", "可能涨", "未来几天", "未来几日", "大概率", "强势")):
        return True
    return text in {"一些", "几个", "几只", "几支", "十只", "十支", "候选", "标的"}


def _theme_key(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _strip_sector_suffix(value: str) -> str:
    text = _theme_key(value)
    for suffix in ("概念指数", "行业指数", "概念板块", "行业板块", "概念", "板块", "指数", "行业"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text


def _is_a_share_ts_code(value: str) -> bool:
    return bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(value or "").strip().upper()))


def _format_theme_universe_line(universe: ThemeUniverse) -> str:
    if not universe.theme:
        return ""
    if universe.source == "ths_sector" and universe.symbols:
        sector = universe.matched_sector or universe.theme
        return f"主题股票池: THS 板块 {sector}，共 {universe.count} 只"
    if universe.source == "sw_sector" and universe.symbols:
        sector = universe.matched_sector or universe.theme
        return f"主题股票池: 申万行业 {sector}，共 {universe.count} 只"
    if universe.source == "llm_theme_universe" and universe.symbols:
        return f"主题股票池: LLM 主题线索 {universe.theme}，经本地 stock_basic 校验，共 {universe.count} 只"
    if universe.source == "ths_sector":
        sector = universe.matched_sector or universe.theme
        return f"主题股票池: THS 板块 {sector}，但未取得有效成分股"
    if universe.source == "sw_sector":
        sector = universe.matched_sector or universe.theme
        return f"主题股票池: 申万行业 {sector}，但未取得有效成分股"
    return f"主题股票池: 未能确认 {universe.theme} 相关 A 股股票，不关联无关板块"


def _load_project_skills(settings: Settings) -> list[Skill]:
    root = Path(getattr(settings, "project_root", "."))
    return load_skills(default_skills_dir(root))


def _safe_research_context(
    query: str,
    *,
    settings: Settings,
    collections: tuple[str, ...],
) -> StockResearchContext | None:
    if not query and not collections:
        return None
    if getattr(settings, "db_path", None) is None:
        return None
    try:
        return build_stock_research_context(query, settings=settings, collections=collections, limit=6)
    except Exception:
        return None


def _profile_from_query(query: str) -> str:
    text = query.lower()
    if any(term in text for term in ("缠论", "三买", "二买", "一买", "背驰", "中枢", "chan")):
        return "chan_structure"
    if any(term in text for term in ("基本面", "财务", "估值", "roe", "pe", "pb", "高股息", "低估", "成长", "现金流")):
        return "fundamental_quality"
    if any(term in text for term in ("避开", "不要st", "非st", "风险", "稳健", "低波动", "回撤", "监管", "退市")):
        return "risk_first"
    if any(term in text for term in ("热点", "题材", "板块", "情绪", "龙头", "涨停", "赚钱效应")):
        return "hot_sector_momentum"
    return DEFAULT_AGENT_PROFILE


def _horizon_from_query(query: str) -> str:
    text = query.lower()
    if any(term in text for term in ("中线", "几周", "波段", "月线")):
        return "swing"
    if any(term in text for term in ("长线", "长期", "价值投资")):
        return "position"
    if any(term in text for term in ("今天", "明天", "未来几天", "短线", "几日", "几天")):
        return "short_term"
    return "short_term"


def _profile_skill_ids(profile: str, *, hot_sector_enabled: bool) -> list[str]:
    mapping = {
        "technical_short_up": ["sats-market-assistant", "technical-basic", "risk-analysis"],
        "hot_sector_momentum": ["sats-market-assistant", "technical-basic", "sector-rotation", "hot-theme", "risk-analysis"],
        "chan_structure": ["chan-theory", "technical-basic", "risk-analysis"],
        "fundamental_quality": [
            "quant-factor-screener",
            "fundamental-filter",
            "financial-statement",
            "valuation-model",
            "risk-analysis",
        ],
        "risk_first": ["risk-analysis", "ashare-pre-st-filter", "regulatory-knowledge", "sats-market-assistant"],
    }
    result = list(mapping.get(profile, mapping[DEFAULT_AGENT_PROFILE]))
    if hot_sector_enabled and "sats-market-assistant" not in result:
        result.append("sats-market-assistant")
    return result


def _collections_for_profile(query: str, profile: str, skill_ids: Iterable[str]) -> tuple[str, ...]:
    explicit: list[str] = ["stock-basic", "signals", "technical", "market", "sentiment", "risk"]
    if profile == "chan_structure":
        explicit.append("chan")
    if profile == "fundamental_quality":
        explicit.append("fundamental")
    if profile == "hot_sector_momentum":
        explicit.extend(["market", "sentiment"])
    if profile == "risk_first":
        explicit.append("risk")
    if any(skill in set(skill_ids) for skill in ("quant-factor-screener", "financial-statement", "valuation-model")):
        explicit.append("fundamental")
    return tuple(_dedupe([*infer_stock_collections(query, explicit=explicit)]))


def _constraints_from_query(query: str) -> tuple[str, ...]:
    text = query.lower()
    checks = [
        ("避开ST/退市风险", ("避开st", "非st", "不要st", "退市", "st")),
        ("热点板块共振", ("热点", "题材", "板块")),
        ("资金流改善", ("资金流", "主力资金", "北向")),
        ("低估值/基本面", ("低估", "估值", "pe", "pb", "基本面")),
        ("小盘成长", ("小盘", "成长")),
        ("缠论结构", ("缠论", "三买", "中枢", "背驰")),
        ("稳健风控", ("稳健", "低波动", "回撤", "风险")),
    ]
    result = []
    for label, terms in checks:
        if any(term in text for term in terms):
            result.append(label)
    return tuple(_dedupe(result))


def _ranking_policy(profile: str, constraints: tuple[str, ...]) -> str:
    base = {
        "technical_short_up": "优先技术中短期上涨信号、趋势强度和明确触发/失效位。",
        "hot_sector_momentum": "优先技术信号与 3-5 日持续热点板块共振，热点只做软加权。",
        "chan_structure": "优先缠论买点、中枢低吸、背驰确认和关键位清晰的候选。",
        "fundamental_quality": "优先技术候选中财务/估值/成长质量更可解释的股票。",
        "risk_first": "优先风险可控、缺失字段少、强卖出信号弱的候选。",
    }.get(profile, "优先真实结构化数据支持的候选。")
    if constraints:
        return f"{base} 同时尊重用户约束：{', '.join(constraints)}。"
    return base


def _profile_query(plan: StockPickingPlan) -> str:
    return " ".join([plan.profile, *plan.collections, *plan.constraints])


def _rank_locally(candidates: list[OpportunityCandidate], *, plan: StockPickingPlan) -> list[OpportunityCandidate]:
    ranked = list(candidates)
    for candidate in ranked:
        candidate.ranking_score = round(_profile_score(candidate, plan=plan), 4)
    return sorted(ranked, key=lambda item: (-item.ranking_score, -item.local_score, item.ts_code))


def _apply_factor_overlay(
    candidates: list[OpportunityCandidate],
    *,
    provider: Any,
    storage: DuckDBStorage,
    trade_date: str,
    profile: str,
    weight: float,
    lookback_days: int,
) -> None:
    if not candidates:
        return
    symbols = [candidate.ts_code for candidate in candidates]
    try:
        inputs = provider.load_screening_inputs(
            symbols,
            trade_date,
            storage=storage,
            trade_days=max(1, int(lookback_days)),
            rule_name="factor_overlay",
        )
        snapshot, _panel_result = snapshot_from_screening_inputs(
            inputs,
            storage=storage,
            trade_date=trade_date,
            profile=profile,
            lookback_days=max(1, int(lookback_days)),
        )
    except Exception as exc:
        warning = f"factor_overlay: {exc}"
        for candidate in candidates:
            candidate.missing_fields = _dedupe([*candidate.missing_fields, warning])
        return
    warnings = list(snapshot.warnings)
    for candidate in candidates:
        exposure = snapshot.exposure_for(candidate.ts_code)
        score = exposure.get("score")
        factor_payload = {
            "profile": snapshot.profile,
            "score": score,
            "coverage": exposure.get("coverage"),
            "factor_values": exposure.get("factor_values") or {},
            "missing_factors": exposure.get("missing_factors") or [],
            "warnings": warnings,
        }
        candidate.indicator["factor"] = factor_payload
        if score is None:
            candidate.missing_fields = _dedupe([*candidate.missing_fields, "factor_overlay: no_score", *warnings])
            continue
        candidate.ranking_score = round(
            float(candidate.ranking_score or candidate.local_score or 0.0)
            + max(-3.0, min(3.0, float(score))) * float(weight),
            4,
        )
        if warnings:
            candidate.missing_fields = _dedupe([*candidate.missing_fields, *warnings])


def _profile_score(candidate: OpportunityCandidate, *, plan: StockPickingPlan) -> float:
    score = float(candidate.ranking_score or candidate.local_score or 0.0)
    labels = " ".join(str(event.get("label") or "") for event in candidate.events)
    categories = {str(event.get("category") or "") for event in candidate.events}
    if plan.profile == "hot_sector_momentum":
        score += min(6.0, float(candidate.hot_sector_score or 0.0) * 0.5)
    if plan.profile == "chan_structure" and ("chan" in categories or "缠" in labels):
        score += 5.0
    if plan.profile == "fundamental_quality" and candidate.indicator.get("fundamentals"):
        score += 3.0
    if plan.profile == "risk_first":
        score -= min(6.0, len(candidate.missing_fields) * 1.5)
        score -= sum(0.75 for event in candidate.events for _ in event.get("risk_flags") or [])
    if "热点板块共振" in plan.constraints:
        score += min(4.0, float(candidate.hot_sector_score or 0.0) * 0.35)
    if "缠论结构" in plan.constraints and ("chan" in categories or "缠" in labels):
        score += 3.0
    if "低估值/基本面" in plan.constraints and candidate.indicator.get("fundamentals"):
        score += 2.0
    if "稳健风控" in plan.constraints:
        score -= min(4.0, len(candidate.missing_fields))
    return score


def _rank_with_agent_llm(
    candidates: list[OpportunityCandidate],
    *,
    all_candidates: list[OpportunityCandidate],
    settings: Settings,
    plan: StockPickingPlan,
    theme_universe: ThemeUniverse,
    research_context: StockResearchContext | None,
    limit: int,
    llm_factory: Callable[..., Any],
) -> LLMRankResult:
    def build_prompt(pool: list[OpportunityCandidate]) -> str:
        return _build_agent_llm_prompt(
            pool,
            plan=plan,
            theme_universe=theme_universe,
            research_context=research_context,
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
    ranked = _parse_agent_llm_rankings(data, pool, all_candidates=all_candidates, limit=limit)
    return LLMRankResult(
        candidates=ranked,
        warnings=tuple(_dedupe(warnings)),
        llm_pool_count=len(pool),
    )


def _build_agent_llm_prompt(
    candidates: list[OpportunityCandidate],
    *,
    plan: StockPickingPlan,
    theme_universe: ThemeUniverse,
    research_context: StockResearchContext | None,
) -> str:
    payload = {
        "agent_plan": plan.to_dict(),
        "knowledge_policy": "skills/RAG 只提供方法论证据，真实行情和结论必须来自 candidates 结构化字段。",
        "chan_policy": "chan_score 来自 SATS Analyze 的结构化缠论买点/共振信号，只能作为排序证据，不能保证上涨。",
        "theme_universe": theme_universe.to_llm_context(),
        "theme_universe_policy": (
            "theme_universe 只限定研究股票池；source=llm_theme_universe 时只能称为 LLM 主题线索，"
            "不能称为同花顺概念板块，也不能把相关性当作上涨理由。"
        ),
        "evidence_sources": [_compact_evidence_source(item) for item in research_context.sources[:12]]
        if research_context is not None
        else (),
        "evidence_context": _short_text(research_context.system_message, 3000) if research_context is not None else "",
        "candidates": [candidate.to_llm_context() for candidate in candidates],
    }
    return (
        "你是 SATS 自然语言选股 Agent 的排序器。请只基于 JSON 中的真实结构化候选和本地方法论证据排序，"
        "chan_score/chan_signals 只代表 SATS Analyze 已识别的缠论买点证据，不能写成上涨保证。"
        "不要编造新闻、题材、价格或财务字段。若候选来自 LLM 主题股票池，只能把它作为纳入研究池的线索，"
        "不能声称属于同花顺概念板块或把主题相关性当作上涨理由。返回严格 JSON："
        '{"rankings":[{"ts_code":"000001.SZ","reason":"...","entry_trigger":"...",'
        '"invalidation":"...","risk":"...","evidence_refs":["..."],"data_limits":"..."}]}。'
        "最多返回 limit 只。\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


def _short_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _compact_evidence_source(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"source": _short_text(item, 120)}
    compact: dict[str, Any] = {}
    for key in ("collection", "source", "skill", "title", "path"):
        if key in item:
            compact[key] = _short_text(item.get(key), 160)
    if not compact:
        compact["source"] = _short_text(item, 160)
    return compact


def _parse_agent_llm_rankings(
    data: dict[str, Any],
    candidates: list[OpportunityCandidate],
    *,
    all_candidates: list[OpportunityCandidate],
    limit: int,
) -> list[OpportunityCandidate]:
    rankings = data.get("rankings")
    if not isinstance(rankings, list):
        raise ValueError("LLM did not return rankings")
    by_code = {candidate.ts_code: candidate for candidate in all_candidates}
    allowed = {candidate.ts_code for candidate in candidates}
    ranked: list[OpportunityCandidate] = []
    seen: set[str] = set()
    for row in rankings:
        if not isinstance(row, dict):
            continue
        ts_code = str(row.get("ts_code") or "").strip().upper()
        candidate = by_code.get(ts_code)
        if candidate is None or ts_code in seen or ts_code not in allowed:
            continue
        seen.add(ts_code)
        candidate.llm_reason = _merge_reason(row)
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
    return ranked[:limit]


def _merge_reason(row: dict[str, Any]) -> str:
    reason = str(row.get("reason") or "").strip()
    refs = row.get("evidence_refs")
    limits = str(row.get("data_limits") or "").strip()
    parts = [reason]
    if isinstance(refs, list) and refs:
        parts.append("证据 " + ",".join(str(item) for item in refs[:3] if str(item).strip()))
    if limits:
        parts.append("限制 " + limits)
    return "；".join(part for part in parts if part)


def _float(value: Any, *, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)
