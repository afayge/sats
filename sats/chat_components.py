from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from sats.analysis.chan_chat_context import ChanChatContext, build_chan_chat_context
from sats.analysis.market_llm_context import (
    build_market_llm_context,
    is_market_question,
)
from sats.analysis.opportunity_discovery import (
    DEFAULT_CANDIDATE_LIMIT,
    extract_opportunity_discovery_limit,
    format_opportunity_discovery,
    is_llm_context_length_error,
)
from sats.analysis.quote_llm_context import build_stock_quote_llm_context
from sats.analysis.stock_llm_context import StockLLMContext, build_stock_llm_context, minute_curve_metadata
from sats.analysis.stock_picking_agent import format_stock_picking_agent_result, run_stock_picking_agent
from sats.analysis.stock_research_context import StockResearchContext, build_stock_research_context
from sats.chat_planner import ChatPlan, build_chat_plan
from sats.chat_preprocessor import ChatPreprocessResult, preprocess_chat_message
from sats.chat_reference import ChatReferenceContext
from sats.config import Settings
from sats.llm import ChatLLM, LLMResponse, build_light_fallback_llm, build_standard_llm
from sats.memory import ChatMemoryStore, MemoryRecord
from sats.minute_periods import extract_minute_periods, normalize_minute_periods
from sats.screening.registry import list_rules
from sats.screening.rule_composer import (
    GeneratedRuleResult,
    RuleGenerationPlan,
    compose_rule_generation_plan,
    compose_rule_generation_plan_from_spec,
    format_generated_rule_result,
    format_rule_generation_plan,
    generate_rule_code,
    is_rule_generation_request,
    parse_rule_generation_confirmation,
    revise_rule_generation_plan,
)
from sats.signals import analyze_signal_inputs
from sats.skill_routing import collections_for_skill_ids
from sats.skills import Skill, find_skill, match_skills, skill_summaries
from sats.stock_question import StockQuestion, extract_intraday_time, extract_trade_date, parse_stock_question
from sats.symbols import normalize_symbols


CHAT_ROUTE_GENERAL = "general_qa"
CHAT_ROUTE_STOCK = "stock_context"
CHAT_ROUTE_MARKET = "market_context"
CHAT_ROUTE_OPPORTUNITY = "opportunity_discovery"
CHAT_ROUTE_CHAN = "chan_context"
CHAT_ROUTE_RULE = "rule_generation"
CHAT_ROUTE_QUOTE = "quote_context"

COMPONENT_STOCK_CONTEXT = "stock_context"
COMPONENT_INDICATORS = "indicators"
COMPONENT_MARKET_CONTEXT = "market_context"
COMPONENT_OPPORTUNITY = "opportunity_discovery"
COMPONENT_CHAN_CONTEXT = "chan_context"
COMPONENT_KNOWLEDGE_CONTEXT = "knowledge_context"
COMPONENT_RULE_GENERATION = "rule_generation"
COMPONENT_QUOTE_CONTEXT = "quote_context"

RULE_GENERATION_ACTION_TYPE = "rule_generation"

SYNTHESIS_SYSTEM_PROMPT = (
    "你是 SATS CLI 助手。你只能基于下面提供的 SATS 真实数据证据、skills 方法论和知识库证据作答。"
    "当本轮 SATS 结构化数据与用户粘贴数据、历史消息或会话摘要冲突时，以本轮 SATS 结构化数据为准。"
    "不得编造价格、成交量、K线、quote、新闻、公告、题材、资金流或未注入的研究结论。"
    "如果数据缺失或组件未命中，必须明确写出限制。"
    "如果明确请求的股票未出现在摘要中，只能说明摘要未展开/未纳入摘要；只有 missing_fields、空指标 payload 或工具错误才可写成数据缺失/未命中。"
    "market_breadth.total_amount 若 amount_basis=intraday_cumulative，只能表述为截至当前累计成交额；"
    "只有 turnover_comparison.status=ok 时，才能写放量、缩量、成交萎缩或成交放大，且不得把盘中累计成交额直接与前一交易日全天成交额比较。"
    "涉及投资相关判断时，必须说明仅供研究，不构成投资建议。"
    "回答保持清晰、直接、可操作；不要输出内部编排日志。"
    "输出必须优先采用统一 Markdown 骨架：H1 标题、单句引用式核心结论、badge 元信息行、"
    "“结论摘要 / 关键证据 / 文字图表 / 风险与限制 / 下一步”这些一级段落。"
    "数据分析类问题必须包含表格或等价证据；文字图表优先使用 ASCII/Unicode 条形图、比例条或 sparkline。"
)

FULL_SKILL_CHAR_LIMIT = 2400
MAX_FULL_SKILLS = 4

STOCK_ANALYSIS_DEFAULT_RAG_COLLECTIONS = (
    "technical",
    "signals",
    "chan",
    "market",
    "sentiment",
    "fundamental",
    "risk",
    "stock-basic",
)
STOCK_ANALYSIS_DEFAULT_RAG_LIMIT = 12

ROUTE_COMPONENTS: dict[str, tuple[str, ...]] = {
    CHAT_ROUTE_GENERAL: (),
    CHAT_ROUTE_STOCK: (COMPONENT_STOCK_CONTEXT, COMPONENT_INDICATORS, COMPONENT_MARKET_CONTEXT),
    CHAT_ROUTE_MARKET: (COMPONENT_MARKET_CONTEXT,),
    CHAT_ROUTE_OPPORTUNITY: (COMPONENT_OPPORTUNITY,),
    CHAT_ROUTE_CHAN: (COMPONENT_CHAN_CONTEXT,),
    CHAT_ROUTE_RULE: (COMPONENT_RULE_GENERATION,),
    CHAT_ROUTE_QUOTE: (COMPONENT_QUOTE_CONTEXT,),
}


@dataclass(frozen=True, slots=True)
class ChatRequestRoute:
    route_kind: str
    intent: str
    reason: str = ""
    skills: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    trade_date: str | None = None
    as_of_time: str | None = None
    market_indices: tuple[str, ...] = ()
    market_dimensions: tuple[str, ...] = ()
    market_horizons: tuple[str, ...] = ()
    requested_limit: int | None = None
    required_components: tuple[str, ...] = ()
    knowledge_collections: tuple[str, ...] = ()
    explicit_knowledge: str | None = None
    requires_runtime: bool = False
    requires_confirmation: bool = False
    risk_level: str = "low"
    preprocess: ChatPreprocessResult | None = None
    plan: ChatPlan | None = None
    stock_question: StockQuestion | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_kind": self.route_kind,
            "intent": self.intent,
            "reason": self.reason,
            "skills": list(self.skills),
            "symbols": list(self.symbols),
            "trade_date": self.trade_date or "",
            "as_of_time": self.as_of_time or "",
            "market_indices": list(self.market_indices),
            "market_dimensions": list(self.market_dimensions),
            "market_horizons": list(self.market_horizons),
            "requested_limit": self.requested_limit,
            "required_components": list(self.required_components),
            "knowledge_collections": list(self.knowledge_collections),
            "explicit_knowledge": self.explicit_knowledge or "",
            "requires_runtime": self.requires_runtime,
            "requires_confirmation": self.requires_confirmation,
            "risk_level": self.risk_level,
        }

    def system_message(self) -> str:
        return "\n".join(
            [
                "SATS chat_route:",
                f"- route_kind: {self.route_kind}",
                f"- intent: {self.intent}",
                f"- reason: {self.reason}",
                f"- symbols: {', '.join(self.symbols) if self.symbols else 'none'}",
                f"- trade_date: {self.trade_date or 'none'}",
                f"- required_components: {', '.join(self.required_components) if self.required_components else 'none'}",
                f"- knowledge_collections: {', '.join(self.knowledge_collections) if self.knowledge_collections else 'none'}",
                f"- explicit_knowledge: {self.explicit_knowledge or 'none'}",
                f"- market_indices: {', '.join(self.market_indices) if self.market_indices else 'none'}",
                f"- market_dimensions: {', '.join(self.market_dimensions) if self.market_dimensions else 'none'}",
                f"- market_horizons: {', '.join(self.market_horizons) if self.market_horizons else 'none'}",
                f"- requested_limit: {self.requested_limit if self.requested_limit is not None else 'none'}",
                f"- requires_runtime: {self.requires_runtime}",
                f"- requires_confirmation: {self.requires_confirmation}",
                f"- risk_level: {self.risk_level}",
            ]
        )


@dataclass(frozen=True, slots=True)
class ChatEvidenceBundle:
    route: ChatRequestRoute
    stock_context: StockLLMContext | None = None
    indicators: dict[str, Any] | None = None
    market_context: Any | None = None
    opportunity_context: Any | None = None
    chan_context: ChanChatContext | None = None
    knowledge_context: StockResearchContext | None = None
    quote_context: Any | None = None
    reference_context: ChatReferenceContext | None = None
    rule_generation: dict[str, Any] | None = None
    data_names: tuple[str, ...] = ()
    sources: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    requires_confirmation: bool = False
    pending_action_id: str | None = None
    completion_trade_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.to_dict(),
            "data_names": list(self.data_names),
            "requires_confirmation": self.requires_confirmation,
            "pending_action_id": self.pending_action_id or "",
            "completion_trade_date": self.completion_trade_date,
            "artifacts": list(self.artifacts),
            "sources": list(self.sources),
            "has_stock_context": self.stock_context is not None,
            "has_indicators": self.indicators is not None,
            "has_market_context": self.market_context is not None,
            "has_opportunity_context": self.opportunity_context is not None,
            "has_chan_context": self.chan_context is not None,
            "has_knowledge_context": self.knowledge_context is not None,
            "has_quote_context": self.quote_context is not None,
            "has_rule_generation": self.rule_generation is not None,
        }


@dataclass(frozen=True, slots=True)
class ChatSynthesisResult:
    content: str
    tool_call_count: int = 0
    phase: str = "synthesis"
    model_policy: str = ""
    model_profile: str = ""
    model_name: str = ""


@dataclass(frozen=True, slots=True)
class RuleGenerationOutcome:
    content: str
    data_names: tuple[str, ...]
    payload: dict[str, Any] = field(default_factory=dict)
    pending_action_id: str | None = None
    requires_confirmation: bool = False
    artifacts: tuple[dict[str, Any], ...] = ()


def preprocess_chat_route(
    message: str,
    *,
    settings: Settings,
    skills: list[Skill],
    llm_factory: Callable[..., Any] | None = ChatLLM,
    preprocess_enabled: bool = True,
    reference_context: ChatReferenceContext | None = None,
    explicit_knowledge: str | None = None,
    last_stock_question: StockQuestion | None = None,
) -> tuple[ChatPreprocessResult, StockQuestion | None, ChatPlan, ChatRequestRoute]:
    preprocess = preprocess_chat_message(
        message,
        settings=settings,
        reference_context=reference_context,
        llm_factory=llm_factory,
        llm_enabled=preprocess_enabled and llm_factory is ChatLLM,
    )
    question = resolve_stock_question_from_preprocess(
        message,
        preprocess,
        reference_context=reference_context,
        last_stock_question=last_stock_question,
    )
    plan = build_chat_plan(
        message,
        skills=skills,
        stock_question=question,
        preprocess=preprocess,
    )
    route = build_chat_request_route(
        message,
        skills=skills,
        preprocess=preprocess,
        plan=plan,
        stock_question=question,
        explicit_knowledge=explicit_knowledge,
    )
    return preprocess, question, plan, route


def build_chat_request_route(
    message: str,
    *,
    skills: list[Skill],
    preprocess: ChatPreprocessResult,
    plan: ChatPlan,
    stock_question: StockQuestion | None,
    explicit_knowledge: str | None = None,
) -> ChatRequestRoute:
    text = str(message or "").strip()
    quote_only = bool(
        preprocess.needs_realtime_quote_context
        and not preprocess.needs_stock_context
        and stock_question is not None
        and stock_question.has_stock_question
    )
    market_requested = is_market_question(text)
    route_kind = CHAT_ROUTE_GENERAL
    if (
        parse_rule_generation_confirmation(text)
        or is_rule_generation_request(text)
        or _looks_like_rule_plan_revision(text)
        or plan.intent == "screening_rule_generation"
    ):
        route_kind = CHAT_ROUTE_RULE
    elif plan.needs_opportunity_discovery:
        route_kind = CHAT_ROUTE_OPPORTUNITY
    elif plan.needs_chan_context:
        route_kind = CHAT_ROUTE_CHAN
    elif quote_only:
        route_kind = CHAT_ROUTE_QUOTE
    elif stock_question is not None and stock_question.has_stock_question:
        route_kind = CHAT_ROUTE_STOCK
    elif market_requested or plan.intent == "market_analysis":
        route_kind = CHAT_ROUTE_MARKET

    required = list(ROUTE_COMPONENTS.get(route_kind, ()))
    if route_kind == CHAT_ROUTE_CHAN and stock_question is not None and stock_question.has_stock_question:
        required.extend([COMPONENT_STOCK_CONTEXT, COMPONENT_INDICATORS])
    if quote_only and COMPONENT_QUOTE_CONTEXT not in required:
        required.append(COMPONENT_QUOTE_CONTEXT)
    if _should_include_knowledge_context(route_kind, explicit_knowledge=explicit_knowledge, plan=plan):
        required.append(COMPONENT_KNOWLEDGE_CONTEXT)
    required = _dedupe(required)
    return ChatRequestRoute(
        route_kind=route_kind,
        intent=plan.intent,
        reason=plan.reason,
        skills=tuple(plan.skills),
        symbols=tuple(getattr(stock_question, "symbols", ()) or preprocess.symbols or ()),
        trade_date=getattr(stock_question, "trade_date", None) or preprocess.trade_date,
        as_of_time=getattr(stock_question, "as_of_time", None) or preprocess.as_of_time,
        market_indices=tuple(preprocess.market_indices or ()),
        market_dimensions=tuple(preprocess.market_dimensions or ()),
        market_horizons=tuple(preprocess.market_horizons or ()),
        requested_limit=preprocess.requested_limit,
        required_components=tuple(required),
        knowledge_collections=_knowledge_collections_for_route(route_kind, plan),
        explicit_knowledge=str(explicit_knowledge or "").strip() or None,
        requires_runtime=False,
        requires_confirmation=route_kind == CHAT_ROUTE_RULE,
        risk_level=plan.risk_level,
        preprocess=preprocess,
        plan=plan,
        stock_question=stock_question,
    )


def resolve_stock_question_from_preprocess(
    message: str,
    preprocess: ChatPreprocessResult,
    *,
    reference_context: ChatReferenceContext | None,
    last_stock_question: StockQuestion | None,
) -> StockQuestion | None:
    parsed = parse_stock_question(message)
    if parsed.has_stock_question:
        return _resolve_stock_followup(message, parsed, last_stock_question=last_stock_question)
    symbols = list(preprocess.symbols)
    if symbols:
        return _resolve_stock_followup(
            message,
            StockQuestion(
                symbols=symbols,
                trade_date=preprocess.trade_date or (reference_context.trade_date if reference_context else None),
                as_of_time=preprocess.as_of_time,
                has_stock_question=True,
            ),
            last_stock_question=last_stock_question,
        )
    if reference_context is not None and reference_context.symbols:
        return _resolve_stock_followup(
            message,
            StockQuestion(
                symbols=list(reference_context.symbols),
                trade_date=preprocess.trade_date or reference_context.trade_date,
                as_of_time=preprocess.as_of_time,
                has_stock_question=True,
            ),
            last_stock_question=last_stock_question,
        )
    return _resolve_stock_followup(message, parsed, last_stock_question=last_stock_question)


def collect_chat_evidence(
    message: str,
    *,
    route: ChatRequestRoute,
    settings: Settings,
    skills: list[Skill],
    explicit_knowledge: str | None = None,
    store: ChatMemoryStore | None = None,
    session_id: str = "",
    reference_context: ChatReferenceContext | None = None,
    progress: Any | None = None,
    recorder: Any | None = None,
) -> ChatEvidenceBundle:
    data_names: list[str] = []
    if reference_context is not None:
        data_names.append(reference_context.data_name)
        _emit_context_completed(
            recorder,
            "reference_context",
            payload={"data_name": reference_context.data_name, "symbols": list(reference_context.symbols)},
        )

    quote_context = None
    stock_context = None
    indicators = None
    market_context = None
    opportunity_context = None
    chan_context = None
    knowledge_context = None
    rule_generation = None
    artifacts: tuple[dict[str, Any], ...] = ()
    pending_action_id = None
    requires_confirmation = False
    completion_trade_date = ""

    if COMPONENT_QUOTE_CONTEXT in route.required_components and route.symbols:
        quote_context = _run_component(
            COMPONENT_QUOTE_CONTEXT,
            lambda: _build_quote_context(message, settings=settings, symbols=list(route.symbols)),
            progress=progress,
            recorder=recorder,
            payload={"symbols": list(route.symbols)},
            progress_label="实盘数据",
            progress_message="报价",
        )
        if quote_context is not None:
            data_names.append("实时报价")

    if COMPONENT_STOCK_CONTEXT in route.required_components and route.stock_question is not None:
        stock_context = _run_component(
            COMPONENT_STOCK_CONTEXT,
            lambda: build_stock_context_component(message, settings=settings, question=route.stock_question),
            progress=progress,
            recorder=recorder,
            payload={"symbols": list(route.symbols), "trade_date": route.trade_date or ""},
            progress_label="实盘数据",
            progress_message="个股",
        )
        if stock_context is not None:
            data_names.append("个股")
            context_question = getattr(stock_context, "question", route.stock_question)
            completion_trade_date = str(getattr(stock_context, "trade_date", "") or getattr(context_question, "trade_date", "") or "")

    if COMPONENT_INDICATORS in route.required_components and route.symbols:
        indicator_trade_date = completion_trade_date or route.trade_date or extract_trade_date(message) or ""
        indicators = _indicators_from_stock_context(
            stock_context,
            trade_date=indicator_trade_date,
        )
        if indicators and indicators.get("results"):
            data_names.append("指标")
            _emit_context_completed(
                recorder,
                COMPONENT_INDICATORS,
                payload={
                    "available": True,
                    "symbols": list(route.symbols),
                    "trade_date": indicator_trade_date,
                    "source": "stock_context",
                },
            )

    if COMPONENT_MARKET_CONTEXT in route.required_components:
        market_trade_date = completion_trade_date or route.trade_date
        market_context = _run_component(
            COMPONENT_MARKET_CONTEXT,
            lambda: build_market_context_component(
                message,
                settings=settings,
                trade_date=market_trade_date,
                indices=route.market_indices or None,
                dimensions=route.market_dimensions or None,
                horizons=route.market_horizons or None,
            ),
            progress=progress,
            recorder=recorder,
            payload={
                "indices": list(route.market_indices),
                "dimensions": list(route.market_dimensions),
                "horizons": list(route.market_horizons),
            },
            progress_label="实盘数据",
            progress_message="大盘",
        )
        if market_context is not None:
            data_names.append("大盘")

    if COMPONENT_OPPORTUNITY in route.required_components:
        opportunity_context = _run_component(
            COMPONENT_OPPORTUNITY,
            lambda: build_opportunity_component(
                message,
                settings=settings,
                skills=skills,
                trade_date=route.trade_date,
                limit=route.requested_limit,
                candidate_limit=DEFAULT_CANDIDATE_LIMIT,
                hot_sector_enabled=True,
                hot_sector_days=5,
                market_indices=route.market_indices or None,
                market_dimensions=route.market_dimensions or None,
                market_horizons=route.market_horizons or None,
                progress=progress,
            ),
            progress=progress,
            recorder=recorder,
            payload={"query": message, "requested_limit": route.requested_limit},
            progress_label="内部分析",
            progress_message="选股Agent",
        )
        if opportunity_context is not None:
            data_names.extend(["热点板块", "选股Agent"])

    if COMPONENT_CHAN_CONTEXT in route.required_components:
        chan_context = _run_component(
            COMPONENT_CHAN_CONTEXT,
            lambda: build_chan_context_component(message, skills=skills),
            progress=progress,
            recorder=recorder,
            payload={},
        )
        if chan_context is not None:
            data_names.append("缠论RAG")

    if COMPONENT_KNOWLEDGE_CONTEXT in route.required_components:
        knowledge_context = _run_component(
            COMPONENT_KNOWLEDGE_CONTEXT,
            lambda: build_knowledge_context_component(
                message,
                settings=settings,
                explicit_knowledge=explicit_knowledge or route.explicit_knowledge,
                collections=route.knowledge_collections,
            ),
            progress=progress,
            recorder=recorder,
            payload={"collections": list(route.knowledge_collections), "knowledge": explicit_knowledge or route.explicit_knowledge or ""},
        )
        if knowledge_context is not None:
            data_names.append("知识库RAG")

    if COMPONENT_RULE_GENERATION in route.required_components:
        if store is None:
            store = ChatMemoryStore(getattr(settings, "db_path", None))
        outcome = _run_component(
            COMPONENT_RULE_GENERATION,
            lambda: run_rule_generation_component(
                message,
                settings=settings,
                store=store,
                session_id=session_id or "default",
            ),
            progress=progress,
            recorder=recorder,
            payload={"session_id": session_id or "default"},
        )
        if outcome is not None:
            rule_generation = outcome.payload
            data_names.extend(outcome.data_names)
            artifacts = outcome.artifacts
            pending_action_id = outcome.pending_action_id
            requires_confirmation = outcome.requires_confirmation

    sources = tuple(getattr(knowledge_context, "sources", ()) or ()) if knowledge_context is not None else ()
    return ChatEvidenceBundle(
        route=route,
        stock_context=stock_context,
        indicators=indicators,
        market_context=market_context,
        opportunity_context=opportunity_context,
        chan_context=chan_context,
        knowledge_context=knowledge_context,
        quote_context=quote_context,
        reference_context=reference_context,
        rule_generation=rule_generation,
        data_names=tuple(_dedupe(data_names)),
        sources=sources,
        artifacts=artifacts,
        requires_confirmation=requires_confirmation,
        pending_action_id=pending_action_id,
        completion_trade_date=completion_trade_date or route.trade_date or "",
    )


def _indicators_from_stock_context(
    context: StockLLMContext | None,
    *,
    trade_date: str = "",
) -> dict[str, Any] | None:
    if context is None:
        return None
    payload = getattr(context, "payload", None)
    if not isinstance(payload, dict):
        payload = _context_payload_for_llm(context)
    stocks = payload.get("stocks") if isinstance(payload, dict) and isinstance(payload.get("stocks"), list) else []
    results = []
    for item in stocks:
        if not isinstance(item, dict):
            continue
        indicator_result = item.get("indicator_result")
        if not isinstance(indicator_result, dict) or not indicator_result:
            continue
        results.append(
            {
                "ts_code": item.get("ts_code"),
                "name": item.get("name"),
                "trade_date": item.get("trade_date") or trade_date,
                "indicator_result": indicator_result,
                "period_returns": item.get("period_returns"),
                "missing_fields": item.get("missing_fields"),
            }
        )
    if not results:
        return None
    return {
        "kind": "indicators",
        "trade_date": str(getattr(context, "trade_date", "") or trade_date),
        "results": results,
        "source": "stock_context",
    }


def synthesize_chat_response(
    message: str,
    *,
    route: ChatRequestRoute,
    evidence: ChatEvidenceBundle,
    settings: Settings,
    llm_factory: Callable[..., Any] | None = ChatLLM,
    skills: list[Skill] | None = None,
    history: list[dict[str, str]] | None = None,
    memories: list[MemoryRecord] | None = None,
    session_summary: str = "",
    tool_chat: Callable[[list[dict[str, Any]]], tuple[Any, int] | tuple[Any, int, dict[str, str]]] | None = None,
    progress: Any | None = None,
) -> ChatSynthesisResult:
    if route.route_kind == CHAT_ROUTE_RULE and evidence.rule_generation is not None:
        content = str(evidence.rule_generation.get("content") or "").strip()
        return ChatSynthesisResult(content=content or "无响应", model_policy="none")

    matched_skills = _matched_skills_for_route(route, skills or [])
    messages = _build_synthesis_messages(
        message,
        route=route,
        evidence=evidence,
        skills=matched_skills,
        history=history or [],
        memories=memories or [],
        session_summary=session_summary,
    )
    if tool_chat is not None and route.route_kind == CHAT_ROUTE_GENERAL:
        try:
            chat_result = tool_chat(messages)
        except Exception as exc:
            if _is_timeout_exception(exc):
                return ChatSynthesisResult(
                    content=_timeout_message(_timeout_model_name(settings)),
                    model_policy="light",
                    model_profile="light",
                    model_name=_light_model_name(settings),
                )
            raise
        response, tool_count, model_meta = _normalize_tool_chat_result(chat_result, settings=settings)
        content = str(getattr(response, "content", "") or "").strip() or "无响应"
        return ChatSynthesisResult(content=content, tool_call_count=tool_count, **model_meta)
    if llm_factory is None:
        return ChatSynthesisResult(content=_fallback_chat_summary(route, evidence), model_policy="none")
    model_policy = _synthesis_model_policy(route)
    model_profile = "default" if model_policy == "standard" else "light"
    model_name = _main_model_name(settings) if model_policy == "standard" else _light_model_name(settings)
    try:
        if model_policy == "standard":
            llm = build_standard_llm(
                llm_factory,
                model_name=_main_model_name(settings),
                timeout_seconds=_llm_timeout_seconds(settings),
            )
        else:
            llm = build_light_fallback_llm(
                llm_factory,
                light_model_name=_light_model_name(settings),
                default_model_name=_main_model_name(settings),
                timeout_seconds=_llm_timeout_seconds(settings),
            )
        def call_llm() -> Any:
            try:
                return llm.chat(messages, timeout=_llm_timeout_seconds(settings))
            except TypeError:
                return llm.chat(messages)

        if progress is None:
            response = call_llm()
        else:
            with progress.step(f"{model_name} LLM") as step:
                response = call_llm()
                step.complete()
        model_profile = str(getattr(llm, "last_profile", "") or getattr(llm, "profile", "") or model_profile)
        model_name = str(getattr(llm, "last_model_name", "") or getattr(llm, "model_name", "") or model_name)
        content = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        if _is_timeout_exception(exc):
            timeout_name = _main_model_name(settings) if model_policy == "standard" else _timeout_model_name(settings)
            return ChatSynthesisResult(
                content=_timeout_message(timeout_name),
                model_policy=model_policy,
                model_profile=model_profile,
                model_name=model_name,
            )
        content = ""
    if not content:
        content = _fallback_chat_summary(route, evidence)
    if route.route_kind == CHAT_ROUTE_OPPORTUNITY and evidence.opportunity_context is not None:
        content = _opportunity_discovery_content_or_fallback(content, evidence.opportunity_context)
    return ChatSynthesisResult(
        content=content or "无响应",
        model_policy=model_policy,
        model_profile=model_profile,
        model_name=model_name,
    )


def _synthesis_model_policy(route: ChatRequestRoute) -> str:
    if route.route_kind in {CHAT_ROUTE_GENERAL, CHAT_ROUTE_QUOTE}:
        return "light"
    return "standard"


def _normalize_tool_chat_result(
    result: tuple[Any, int] | tuple[Any, int, dict[str, str]],
    *,
    settings: Settings,
) -> tuple[Any, int, dict[str, str]]:
    response = result[0]
    tool_count = int(result[1] if len(result) > 1 else 0)
    raw_meta = result[2] if len(result) > 2 and isinstance(result[2], dict) else {}
    meta = {
        "model_policy": str(raw_meta.get("model_policy") or "light"),
        "model_profile": str(raw_meta.get("model_profile") or "light"),
        "model_name": str(raw_meta.get("model_name") or _light_model_name(settings)),
    }
    return response, tool_count, meta


def build_stock_context_component(
    message: str,
    *,
    settings: Settings,
    question: StockQuestion,
    minute_periods: tuple[str, ...] = (),
) -> StockLLMContext | None:
    from sats import chat as chat_module

    builder = getattr(chat_module, "build_stock_llm_context", build_stock_llm_context)
    if minute_periods:
        try:
            return builder(message, settings=settings, question=question, minute_periods=minute_periods)
        except TypeError:
            return builder(message, settings=settings, question=question)
    return builder(message, settings=settings, question=question)


def build_market_context_component(
    message: str,
    *,
    settings: Settings,
    trade_date: str | None = None,
    indices: Iterable[str] | None = None,
    dimensions: Iterable[str] | None = None,
    horizons: Iterable[str] | None = None,
) -> Any | None:
    from sats import chat as chat_module

    builder = getattr(chat_module, "build_market_llm_context", build_market_llm_context)
    return builder(
        message,
        settings=settings,
        trade_date=trade_date,
        indices=tuple(indices or ()),
        dimensions=tuple(dimensions or ()),
        horizons=tuple(horizons or ()),
        force=True,
    )


def build_opportunity_component(
    message: str,
    *,
    settings: Settings,
    skills: list[Skill],
    trade_date: str | None = None,
    limit: int | None = None,
    candidate_limit: int | None = None,
    hot_sector_enabled: bool = True,
    hot_sector_days: int = 5,
    market_indices: Iterable[str] | None = None,
    market_dimensions: Iterable[str] | None = None,
    market_horizons: Iterable[str] | None = None,
    report: bool = True,
    progress: Any | None = None,
) -> Any:
    from sats import chat as chat_module
    from sats.storage.duckdb import DuckDBStorage

    runner = getattr(chat_module, "run_stock_picking_agent", run_stock_picking_agent)
    return runner(
        query=message,
        settings=settings,
        storage=DuckDBStorage(getattr(settings, "db_path", None) or "data/sats.duckdb"),
        skills=skills,
        trade_date=trade_date or extract_trade_date(message),
        limit=limit,
        candidate_limit=candidate_limit or DEFAULT_CANDIDATE_LIMIT,
        hot_sector_enabled=hot_sector_enabled,
        hot_sector_days=hot_sector_days,
        market_indices=tuple(market_indices or ()) or None,
        market_dimensions=tuple(market_dimensions or ()) or None,
        market_horizons=tuple(market_horizons or ()) or None,
        reports_dir=Path(getattr(settings, "project_root", ".")) / "reports",
        report=report,
        progress=progress,
    )


def build_chan_context_component(message: str, *, skills: list[Skill]) -> ChanChatContext | None:
    from sats import chat as chat_module

    builder = getattr(chat_module, "build_chan_chat_context", build_chan_chat_context)
    return builder(message, skills=skills)


def build_knowledge_context_component(
    message: str,
    *,
    settings: Settings,
    explicit_knowledge: str | None = None,
    collections: Iterable[str] = (),
) -> StockResearchContext | None:
    if not explicit_knowledge and not tuple(collections):
        return None
    from sats import chat as chat_module

    builder = getattr(chat_module, "build_stock_research_context", build_stock_research_context)
    return builder(
        message,
        settings=settings,
        knowledge=explicit_knowledge,
        collections=collections,
        limit=_knowledge_context_limit(tuple(collections), explicit_knowledge=explicit_knowledge),
    )


def run_internal_analysis_component(settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    from sats import chat as chat_module
    from sats.analysis.dsa_native import run_dsa_analysis
    from sats.data.astock_provider import AStockDataProvider
    from sats.factors.profiles import DEFAULT_FACTOR_PROFILE
    from sats.factors.service import snapshot_from_screening_inputs, summarize_factor_exposure
    from sats.storage.duckdb import DuckDBStorage

    kind = str(arguments.get("kind") or "").strip()
    if kind not in {"indicators", "analyze_signals", "native_dsa", "factor_summary", "company_fundamentals"}:
        raise ValueError(f"unsupported internal analysis kind: {kind}")
    symbols = normalize_symbols(arguments.get("symbols") if isinstance(arguments.get("symbols"), list) else [], required=True)
    trade_date = str(arguments.get("trade_date") or "").strip() or extract_trade_date(" ".join(symbols))
    message = str(arguments.get("_message") or arguments.get("message") or " ".join(symbols))
    minute_periods = normalize_minute_periods(arguments.get("minute_periods") or ()) or extract_minute_periods(message)
    if kind == "company_fundamentals":
        storage = DuckDBStorage(getattr(settings, "db_path", None) or "data/sats.duckdb")
        provider_cls = getattr(chat_module, "AStockDataProvider", AStockDataProvider)
        provider = provider_cls(settings)
        resolved_trade_date = trade_date or _today_yyyymmdd()
        companies = provider.load_company_fundamentals(
            symbols,
            trade_date=resolved_trade_date,
            storage=storage,
            periods=4,
        )
        return {
            "kind": kind,
            "trade_date": resolved_trade_date,
            "companies": [companies[symbol] for symbol in symbols if symbol in companies],
        }
    if kind == "native_dsa":
        storage = DuckDBStorage(getattr(settings, "db_path", None) or "data/sats.duckdb")
        resolved_trade_date = trade_date or _today_yyyymmdd()
        result = run_dsa_analysis(
            symbols,
            settings=settings,
            storage=storage,
            trade_date=resolved_trade_date,
            reports_dir=Path(getattr(settings, "project_root", ".")) / "reports",
            report=False,
            llm_enabled=False,
        )
        return {
            "kind": kind,
            "trade_date": resolved_trade_date,
            "rankings": [asdict(ranking) for ranking in result.rankings],
            "analyses": [asdict(analysis) for analysis in result.analyses],
            "message": result.message,
            "llm_unavailable": result.llm_unavailable,
        }
    if kind == "factor_summary":
        storage = DuckDBStorage(getattr(settings, "db_path", None) or "data/sats.duckdb")
        provider_cls = getattr(chat_module, "AStockDataProvider", AStockDataProvider)
        snapshot_builder = getattr(chat_module, "snapshot_from_screening_inputs", snapshot_from_screening_inputs)
        exposure_builder = getattr(chat_module, "summarize_factor_exposure", summarize_factor_exposure)
        provider = provider_cls(settings)
        resolved_trade_date = trade_date or _today_yyyymmdd()
        inputs = provider.load_screening_inputs(
            symbols,
            resolved_trade_date,
            storage=storage,
            trade_days=max(1, int(arguments.get("lookback_days") or 260)),
            rule_name="factor_summary",
        )
        profile = str(arguments.get("profile") or DEFAULT_FACTOR_PROFILE)
        snapshot, _panel_result = snapshot_builder(
            inputs,
            storage=storage,
            trade_date=resolved_trade_date,
            profile=profile,
            lookback_days=max(1, int(arguments.get("lookback_days") or 260)),
        )
        summary = exposure_builder(snapshot, symbols)
        snapshot_payload = snapshot.to_dict(symbols=symbols) if hasattr(snapshot, "to_dict") else {}
        return {
            "kind": kind,
            "trade_date": resolved_trade_date,
            "profile": profile,
            "summary": summary,
            "snapshot": snapshot_payload,
        }
    stock_context = build_stock_context_component(
        message,
        settings=settings,
        question=StockQuestion(symbols=symbols, trade_date=trade_date or None, has_stock_question=True),
        minute_periods=minute_periods,
    )
    if stock_context is None:
        raise ValueError("stock context unavailable")
    payload = getattr(stock_context, "payload", {}) if isinstance(getattr(stock_context, "payload", {}), dict) else {}
    stocks = payload.get("stocks") if isinstance(payload.get("stocks"), list) else []
    if not stocks:
        empty_rows: list[dict[str, Any]] = []
        if kind == "indicators":
            return {"kind": kind, "trade_date": str(getattr(stock_context, "trade_date", "") or trade_date), "results": empty_rows}
        return {"kind": kind, "trade_date": str(getattr(stock_context, "trade_date", "") or trade_date), "results": empty_rows}
    if kind == "indicators":
        return {
            "kind": kind,
            "trade_date": stock_context.trade_date,
            "results": [
                {
                    "ts_code": item.get("ts_code"),
                    "name": item.get("name"),
                    "trade_date": item.get("trade_date"),
                    "indicator_result": item.get("indicator_result"),
                    "period_returns": item.get("period_returns"),
                    "missing_fields": item.get("missing_fields"),
                }
                for item in stocks
                if isinstance(item, dict)
            ],
        }
    inputs = [_signal_input_from_stock_payload(item) for item in stocks if isinstance(item, dict)]
    run = analyze_signal_inputs(
        inputs,
        selected_signals=str(arguments.get("signals") or "short_up"),
        trade_date=stock_context.trade_date,
        report=False,
    )
    return {
        "kind": kind,
        "trade_date": run.trade_date,
        "results": [result.to_dict() for result in run.results],
    }


def run_rule_generation_component(
    message: str,
    *,
    settings: Settings,
    store: ChatMemoryStore,
    session_id: str,
    action: str = "auto",
    rule_name: str = "",
    semantic_spec: dict[str, Any] | None = None,
) -> RuleGenerationOutcome:
    resolved_action = str(action or "auto").strip() or "auto"
    confirmed_rule = parse_rule_generation_confirmation(message)
    if resolved_action == "auto":
        if confirmed_rule is not None:
            resolved_action = "confirm"
            rule_name = confirmed_rule
        elif is_rule_generation_request(message):
            resolved_action = "plan"
        elif _is_save_latest_semantic_rule_request(message):
            resolved_action = "plan"
        elif _looks_like_rule_plan_revision(message):
            resolved_action = "revise"
        else:
            raise ValueError("not a rule generation request")
    if resolved_action == "plan":
        selected_spec = dict(semantic_spec or {}) or _latest_semantic_screen_spec(store, session_id=session_id)
        if selected_spec:
            plan = compose_rule_generation_plan_from_spec(selected_spec, existing_rule_names=list_rules())
        else:
            plan = compose_rule_generation_plan(message, existing_rule_names=list_rules())
        action_id = _persist_rule_plan(store, session_id=session_id, plan=plan, title=plan.decision_name)
        content = format_rule_generation_plan(plan)
        return RuleGenerationOutcome(
            content=content,
            data_names=("规则计划",),
            payload={"action": "plan", "plan": _rule_plan_payload(plan), "content": content},
            pending_action_id=action_id,
            requires_confirmation=True,
        )
    if resolved_action == "revise":
        pending = _find_latest_pending_rule_plan(store, session_id=session_id)
        if pending is None:
            content = "当前没有待生成规则。请先描述要新增的筛选规则。"
            return RuleGenerationOutcome(content=content, data_names=(), payload={"action": "revise", "content": content})
        plan = _rule_plan_from_payload(pending.get("payload", {}).get("plan"))
        revised = revise_rule_generation_plan(plan, message, existing_rule_names=list_rules())
        _replace_rule_plan(store, action_id=str(pending["action_id"]), plan=revised, title=revised.decision_name)
        content = format_rule_generation_plan(revised)
        return RuleGenerationOutcome(
            content=content,
            data_names=("规则计划",),
            payload={"action": "revise", "plan": _rule_plan_payload(revised), "content": content},
            pending_action_id=str(pending["action_id"]),
            requires_confirmation=True,
        )
    if resolved_action == "confirm":
        target_rule = str(rule_name or confirmed_rule or "").strip().replace("-", "_")
        pending = _find_pending_rule_plan(store, session_id=session_id, rule_name=target_rule)
        if pending is None:
            content = "当前没有待生成规则。请先描述要新增的筛选规则。"
            return RuleGenerationOutcome(content=content, data_names=(), payload={"action": "confirm", "content": content})
        plan = _rule_plan_from_payload(pending.get("payload", {}).get("plan"))
        if target_rule != plan.rule_name:
            content = f"确认的规则名 {target_rule} 与当前计划 {plan.rule_name} 不一致，请重新确认。"
            return RuleGenerationOutcome(content=content, data_names=(), payload={"action": "confirm", "content": content})
        if plan.questions:
            content = "规则计划仍有待确认问题，暂不能生成代码。\n\n" + format_rule_generation_plan(plan)
            return RuleGenerationOutcome(
                content=content,
                data_names=("规则计划",),
                payload={"action": "confirm", "plan": _rule_plan_payload(plan), "content": content},
                pending_action_id=str(pending["action_id"]),
                requires_confirmation=True,
            )
        if plan.rule_name in list_rules():
            content = f"规则名 {plan.rule_name} 已存在，请换一个 rule_name 后重新生成计划。"
            return RuleGenerationOutcome(content=content, data_names=(), payload={"action": "confirm", "content": content})
        from sats import chat as chat_module

        generator = getattr(chat_module, "generate_rule_code", generate_rule_code)
        generated = generator(plan)
        content = format_generated_rule_result(generated)
        artifact = {"kind": "generated_rule", "title": generated.rule_name, "path": str(generated.path), "mime_type": "text/x-python"}
        store.update_pending_action(
            str(pending["action_id"]),
            status="done",
            result={"generated_rule": _generated_rule_payload(generated), "content": content},
        )
        return RuleGenerationOutcome(
            content=content,
            data_names=("生成规则",),
            payload={
                "action": "confirm",
                "generated_rule": _generated_rule_payload(generated),
                "content": content,
            },
            artifacts=(artifact,),
        )
    raise ValueError(f"unsupported rule generation action: {resolved_action}")


def build_plain_chat_answer(
    message: str,
    *,
    settings: Settings,
    skills: list[Skill] | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
    knowledge: str | None = None,
    history: list[dict[str, str]] | None = None,
    memories: list[MemoryRecord] | None = None,
    session_summary: str = "",
) -> ChatSynthesisResult:
    route = ChatRequestRoute(
        route_kind=CHAT_ROUTE_GENERAL,
        intent="general_qa",
        explicit_knowledge=str(knowledge or "").strip() or None,
        required_components=(COMPONENT_KNOWLEDGE_CONTEXT,) if knowledge else (),
        knowledge_collections=(),
    )
    evidence = ChatEvidenceBundle(
        route=route,
        knowledge_context=build_knowledge_context_component(
            message,
            settings=settings,
            explicit_knowledge=str(knowledge or "").strip() or None,
            collections=(),
        )
        if knowledge
        else None,
        data_names=("知识库RAG",) if knowledge else (),
    )
    return synthesize_chat_response(
        message,
        route=route,
        evidence=evidence,
        settings=settings,
        llm_factory=llm_factory,
        skills=skills or [],
        history=history,
        memories=memories,
        session_summary=session_summary,
    )


def match_skills_for_route(route: ChatRequestRoute, skills: list[Skill], matched: Iterable[Skill] = ()) -> list[Skill]:
    result: list[Skill] = []
    seen: set[str] = set()
    for skill in matched:
        if skill.id not in seen:
            result.append(skill)
            seen.add(skill.id)
    for skill_id in route.skills:
        skill = find_skill(skills, skill_id)
        if skill is not None and skill.id not in seen:
            result.append(skill)
            seen.add(skill.id)
    return result[:10]


def skill_context(skills: list[Skill]) -> str:
    blocks = [
        "以下是自动匹配到的 SATS skills 摘要。skill 只提供方法论，不代表已经执行真实行情或分析。"
    ]
    for skill in skills:
        blocks.append(
            "\n".join(
                [
                    f"Skill: {skill.name}",
                    f"Category: {skill.category}",
                    f"Description: {skill.description}",
                    f"Triggers: {', '.join(skill.triggers) if skill.triggers else '无'}",
                ]
            )
        )
    return "\n\n".join(blocks)


def skill_digest(skills: list[Skill]) -> list[dict[str, Any]]:
    rows = []
    full_used = 0
    for skill in skills:
        mode = skill.auto_load if skill.auto_load in {"summary", "full", "never"} else "summary"
        if mode == "full" and full_used >= MAX_FULL_SKILLS:
            mode = "summary"
        item = {
            "id": skill.id,
            "name": skill.name,
            "category": skill.category,
            "description": skill.description,
            "triggers": list(skill.triggers),
            "mode": mode,
            "source": skill.source,
        }
        if mode == "full":
            full_used += 1
            item["content"] = skill.content[:FULL_SKILL_CHAR_LIMIT]
            item["truncated"] = len(skill.content) > FULL_SKILL_CHAR_LIMIT
        rows.append(item)
    return rows


def chat_tool_definitions(skills: list[Skill]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_skills",
                "description": "列出 SATS 本地 skills 的分类摘要。",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "按 skill 名称或 id 加载完整 SKILL.md 内容。",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "Skill name or id"}},
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_a_share_market_context",
                "description": "获取真实 A 股大盘指数和市场宽度上下文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "trade_date": {"type": "string"},
                        "horizon": {"type": "string"},
                        "horizons": {"type": "array", "items": {"type": "string"}},
                        "indices": {"type": "array", "items": {"type": "string"}},
                        "dimensions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "discover_a_share_opportunities",
                "description": "基于 SATS 真实数据运行 A 股机会发现。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "trade_date": {"type": "string"},
                        "query": {"type": "string"},
                        "signals": {"type": "string"},
                        "limit": {"type": "integer"},
                        "candidate_limit": {"type": "integer"},
                        "hot_sector": {"type": "boolean"},
                        "hot_sector_days": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_stock_research_context",
                "description": "获取指定 A 股个股真实研究上下文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbols": {"type": "array", "items": {"type": "string"}},
                        "trade_date": {"type": "string"},
                    },
                    "required": ["symbols"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_internal_analysis",
                "description": "运行 SATS 白名单内部分析。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["indicators", "analyze_signals", "native_dsa", "factor_summary", "company_fundamentals"],
                        },
                        "symbols": {"type": "array", "items": {"type": "string"}},
                        "trade_date": {"type": "string"},
                        "signals": {"type": "string"},
                        "profile": {"type": "string"},
                        "lookback_days": {"type": "integer"},
                    },
                    "required": ["kind", "symbols"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_chan_context",
                "description": "获取本地缠论 skill 与知识库上下文。",
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_knowledge_context",
                "description": "按知识库或 collection 构建本地研究上下文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "knowledge": {"type": "string"},
                        "collections": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["message"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_rule_generation",
                "description": "运行筛选规则计划/修订/确认生成。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "action": {"type": "string", "enum": ["auto", "plan", "revise", "confirm"]},
                        "rule_name": {"type": "string"},
                        "session_id": {"type": "string"},
                    },
                    "required": ["message"],
                },
            },
        },
    ]


def execute_chat_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    skills: list[Skill],
    settings: Settings,
) -> str:
    try:
        return _execute_chat_tool(name, arguments, skills=skills, settings=settings)
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


def _execute_chat_tool(name: str, arguments: dict[str, Any], *, skills: list[Skill], settings: Settings) -> str:
    if name == "list_skills":
        return json.dumps({"status": "ok", "skills": skill_summaries(skills)}, ensure_ascii=False)
    if name == "load_skill":
        skill = find_skill(skills, str(arguments.get("name") or ""))
        if skill is None:
            return json.dumps({"status": "error", "error": "unknown skill", "available": [item.name for item in skills]}, ensure_ascii=False)
        return json.dumps(
            {
                "status": "ok",
                "name": skill.name,
                "category": skill.category,
                "description": skill.description,
                "content": skill.content,
            },
            ensure_ascii=False,
        )
    if name == "get_a_share_market_context":
        from sats import chat as chat_module

        raw_builder = getattr(chat_module, "get_a_share_market_context", None)
        if callable(raw_builder):
            payload = raw_builder(
                settings=settings,
                trade_date=str(arguments.get("trade_date") or "").strip() or None,
                horizon=str(arguments.get("horizon") or "").strip() or None,
                horizons=arguments.get("horizons") if isinstance(arguments.get("horizons"), list) else None,
                indices=arguments.get("indices") if isinstance(arguments.get("indices"), list) else None,
                dimensions=arguments.get("dimensions") if isinstance(arguments.get("dimensions"), list) else None,
            )
        else:
            context = build_market_context_component(
                str(arguments.get("message") or "需要大盘上下文"),
                settings=settings,
                trade_date=str(arguments.get("trade_date") or "").strip() or None,
                indices=arguments.get("indices") if isinstance(arguments.get("indices"), list) else (),
                dimensions=arguments.get("dimensions") if isinstance(arguments.get("dimensions"), list) else (),
                horizons=arguments.get("horizons") if isinstance(arguments.get("horizons"), list) else ([arguments.get("horizon")] if arguments.get("horizon") else ()),
            )
            payload = _context_payload_for_llm(context) if context is not None else {}
        return json.dumps({"status": "ok", "market_context": payload}, ensure_ascii=False)
    if name == "discover_a_share_opportunities":
        query = str(arguments.get("query") or "")
        result = build_opportunity_component(
            query,
            settings=settings,
            skills=skills,
            trade_date=str(arguments.get("trade_date") or "").strip() or None,
            limit=int(arguments.get("limit")) if arguments.get("limit") is not None else extract_opportunity_discovery_limit(query),
            candidate_limit=int(arguments.get("candidate_limit") or DEFAULT_CANDIDATE_LIMIT),
            hot_sector_enabled=bool(arguments.get("hot_sector", True)),
            hot_sector_days=int(arguments.get("hot_sector_days") or 5),
        )
        payload = _context_payload_for_llm(result)
        legacy = result.discovery.to_llm_context() if hasattr(result, "discovery") else payload
        return json.dumps({"status": "ok", "stock_picking_agent": payload, "opportunity_discovery": legacy}, ensure_ascii=False)
    if name == "get_stock_research_context":
        symbols = arguments.get("symbols") if isinstance(arguments.get("symbols"), list) else []
        question = StockQuestion(
            symbols=normalize_symbols(symbols, required=True),
            trade_date=str(arguments.get("trade_date") or "").strip() or None,
            has_stock_question=True,
        )
        context = build_stock_context_component(" ".join(question.symbols), settings=settings, question=question)
        payload = context.payload if context is not None else {}
        return json.dumps({"status": "ok", "stock_context": payload}, ensure_ascii=False)
    if name == "run_internal_analysis":
        payload = run_internal_analysis_component(settings, arguments)
        return json.dumps({"status": "ok", "analysis": payload}, ensure_ascii=False)
    if name == "get_chan_context":
        context = build_chan_context_component(str(arguments.get("message") or ""), skills=skills)
        payload = {"payload": context.payload, "system_message": context.system_message} if context is not None else {}
        return json.dumps({"status": "ok", "chan_context": payload}, ensure_ascii=False)
    if name == "get_knowledge_context":
        context = build_knowledge_context_component(
            str(arguments.get("message") or ""),
            settings=settings,
            explicit_knowledge=str(arguments.get("knowledge") or "").strip() or None,
            collections=arguments.get("collections") if isinstance(arguments.get("collections"), list) else (),
        )
        payload = {
            "collections": list(context.collections),
            "system_message": context.system_message,
            "sources": list(context.sources),
        } if context is not None else {}
        return json.dumps({"status": "ok", "knowledge_context": payload}, ensure_ascii=False)
    if name == "run_rule_generation":
        store = ChatMemoryStore(getattr(settings, "db_path", None))
        outcome = run_rule_generation_component(
            str(arguments.get("message") or ""),
            settings=settings,
            store=store,
            session_id=str(arguments.get("session_id") or "chat_tool"),
            action=str(arguments.get("action") or "auto"),
            rule_name=str(arguments.get("rule_name") or ""),
        )
        return json.dumps(
            {
                "status": "ok",
                "rule_generation": {
                    "content": outcome.content,
                    "data_names": list(outcome.data_names),
                    "payload": outcome.payload,
                    "pending_action_id": outcome.pending_action_id or "",
                    "requires_confirmation": outcome.requires_confirmation,
                    "artifacts": list(outcome.artifacts),
                },
            },
            ensure_ascii=False,
        )
    raise ValueError(f"unknown tool: {name}")


def _build_synthesis_messages(
    message: str,
    *,
    route: ChatRequestRoute,
    evidence: ChatEvidenceBundle,
    skills: list[Skill],
    history: list[dict[str, str]],
    memories: list[MemoryRecord],
    session_summary: str,
) -> list[dict[str, Any]]:
    evidence_digest = _chat_evidence_digest(evidence)
    context = {
        "route": route.to_dict(),
        "skills": skill_digest(skills),
        "evidence_digest": evidence_digest,
        "sources": list(evidence.sources),
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
        {"role": "system", "content": route.system_message()},
        {
            "role": "system",
            "content": "以下是 SATS 已获取/计算的真实上下文和方法论摘要：\n" + json.dumps(context, ensure_ascii=False, default=str),
        },
    ]
    if evidence.reference_context is not None:
        messages.append({"role": "system", "content": evidence.reference_context.system_message})
    if memories:
        messages.append({"role": "system", "content": _memory_context(memories)})
    if session_summary:
        messages.append({"role": "system", "content": f"当前会话摘要：\n{session_summary}"})
    messages.extend(history)
    messages.append(
        {
            "role": "user",
            "content": (
                f"用户问题：{message}\n"
                "请直接给出中文 Markdown 研究输出。"
                "必须包含：标题、核心结论引用、badge 元信息、结论摘要、关键证据、文字图表、风险与限制、下一步。"
                "逐股票条目只能来自 reference_symbol_policy.allowed_symbols 或真实结构化数据中的有效 ts_code，且必须同时有有效股票代码和名称。"
                "【...】、章节标题、策略标签、风险标签、分组名不是股票；没有代码的文本标签只能放到分类/风险说明/限制里，不能作为股票条目。"
                "如果 evidence_digest 中有 period_returns，应直接使用其中的 start_trade_date、end_trade_date 和 pct_change 回答模糊时间段涨跌幅；"
                "不要因为用户的自然日端点不是交易日而说区间涨跌幅缺失。"
                "对于 opportunity_context 候选，candidate_summary.omitted_count=0 时不得声称排名靠后的原始发现结果或技术数据被截断。"
                "若本轮 SATS 结构化数据与用户粘贴数据、历史消息或会话摘要冲突，必须采用本轮结构化数据。"
                "market_breadth.total_amount 若 amount_basis=intraday_cumulative，只能写截至当前累计；"
                "只有 turnover_comparison.status=ok 时才能定性放量/缩量/成交萎缩，禁止盘中累计额直接对比前日全天。"
                "若缺少真实数据或对应组件未命中，明确写“数据缺失/未命中”。"
            ),
        }
    )
    return messages


def _chat_evidence_digest(evidence: ChatEvidenceBundle) -> dict[str, Any]:
    requested_symbols = evidence.route.symbols
    digest: dict[str, Any] = {
        "quote_context": _quote_context_digest(evidence.quote_context),
        "stock_context": _stock_context_digest(evidence.stock_context, requested_symbols=requested_symbols),
        "indicators": _indicator_digest(evidence.indicators, requested_symbols=requested_symbols),
        "indicator_coverage": _indicator_coverage(evidence.indicators, requested_symbols=requested_symbols),
        "reference_symbol_policy": _reference_symbol_policy(evidence),
        "market_context": _market_context_digest(evidence.market_context),
        "opportunity_context": _opportunity_digest(evidence.opportunity_context),
        "chan_context": _chan_context_digest(evidence.chan_context),
        "knowledge_context": _knowledge_context_digest(
            evidence.knowledge_context,
            collections=evidence.route.knowledge_collections,
        ),
        "rule_generation": _trim_payload(evidence.rule_generation or {}, max_chars=8000),
        "data_names": list(evidence.data_names),
    }
    return digest


def _reference_symbol_policy(evidence: ChatEvidenceBundle) -> dict[str, Any]:
    allowed = _allowed_reference_symbols(evidence)
    if not allowed:
        return {}
    return {
        "allowed_symbols": allowed,
        "stock_item_policy": "逐股票条目只能来自 allowed_symbols 或真实结构化数据中的有效 ts_code，且必须同时有有效股票代码和名称。",
        "non_stock_labels_policy": "【...】、章节标题、策略标签、风险标签、分组名不是股票；没有有效股票代码的标签只能用于分类或风险说明，不得生成股票名称/代码为空的逐股票条目。",
    }


def _allowed_reference_symbols(evidence: ChatEvidenceBundle) -> list[str]:
    values: list[str] = []
    values.extend(evidence.route.symbols or ())
    if evidence.reference_context is not None:
        values.extend(evidence.reference_context.symbols or [])
    values.extend(
        item.get("ts_code")
        for item in _stock_context_digest(evidence.stock_context, requested_symbols=evidence.route.symbols)
        if isinstance(item, dict)
    )
    return _valid_symbol_list(values)


def _stock_context_digest(context: StockLLMContext | None, *, requested_symbols: tuple[str, ...] | list[str] = ()) -> list[dict[str, Any]]:
    if context is None:
        return []
    payload = _context_payload_for_llm(context)
    payload = payload if isinstance(payload, dict) else {}
    stocks = payload.get("stocks") if isinstance(payload.get("stocks"), list) else []
    if not stocks and payload.get("system_message"):
        return [{"system_message": payload["system_message"]}]
    requested = _normalized_symbol_list(requested_symbols)
    if requested:
        stocks = _matching_symbol_rows(stocks, requested)
    else:
        stocks = stocks[:6]
    rows: list[dict[str, Any]] = []
    for item in stocks:
        if not isinstance(item, dict):
            continue
        rows.append(
            _drop_empty(
                {
                    "ts_code": item.get("ts_code"),
                    "name": item.get("name"),
                    "requested_trade_date": item.get("requested_trade_date"),
                    "trade_date": item.get("trade_date"),
                    "price_context": _pick_fields(item.get("price_context") or {}, ("close", "pct_chg", "change", "source")),
                    "indicator_result": _pick_fields(
                        item.get("indicator_result") or {},
                        ("close", "ma5", "ma10", "ma20", "ma60", "macd", "macd_dif", "macd_dea", "rsi6", "rsi12", "kdj_j"),
                    ),
                    "period_returns": _trim_payload(item.get("period_returns") or {}, max_chars=2500),
                    "missing_fields": item.get("missing_fields"),
                }
            )
        )
    return rows


def _indicator_digest(payload: dict[str, Any] | None, *, requested_symbols: tuple[str, ...] | list[str] = ()) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("results") if isinstance(payload.get("results"), list) else []
    requested = _normalized_symbol_list(requested_symbols)
    if requested:
        rows = _matching_symbol_rows(rows, requested)
    else:
        rows = rows[:8]
    digest = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        digest.append(
            _drop_empty(
                {
                    "ts_code": item.get("ts_code"),
                    "name": item.get("name"),
                    "trade_date": item.get("trade_date"),
                    "indicator_result": _trim_payload(item.get("indicator_result") or {}, max_chars=1600),
                    "period_returns": _trim_payload(item.get("period_returns") or {}, max_chars=2500),
                    "missing_fields": item.get("missing_fields"),
                }
            )
        )
    return digest


def _indicator_coverage(payload: dict[str, Any] | None, *, requested_symbols: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    requested = _normalized_symbol_list(requested_symbols)
    if not requested:
        return {}
    rows = payload.get("results") if isinstance(payload, dict) and isinstance(payload.get("results"), list) else []
    included = _matching_symbol_rows(rows, requested)
    included_symbols = _normalized_symbol_list(item.get("ts_code") or item.get("symbol") for item in included if isinstance(item, dict))
    missing_from_summary = [symbol for symbol in requested if symbol not in included_symbols]
    return _drop_empty(
        {
            "requested_count": len(requested),
            "included_count": len(included_symbols),
            "omitted_count": max(0, len(rows) - len(included)),
            "missing_requested_symbols": missing_from_summary,
            "policy": "明确请求股票的指标摘要应全部保留；未出现在摘要不等于数据缺失，除非 missing_fields 或空指标 payload 明确标记。",
        }
    )


def _normalized_symbol_list(values: Any) -> list[str]:
    return normalize_symbols(values or [], required=False)


def _valid_symbol_list(values: Any) -> list[str]:
    return [symbol for symbol in _normalized_symbol_list(values) if _is_valid_ts_code(symbol)]


def _is_valid_ts_code(value: Any) -> bool:
    text = str(value or "").strip().upper()
    return len(text) == 9 and text[:6].isdigit() and text[6] == "." and text[7:] in {"SH", "SZ", "BJ"}


def _matching_symbol_rows(rows: Any, requested_symbols: list[str]) -> list[Any]:
    if not requested_symbols or not isinstance(rows, list):
        return []
    by_symbol: dict[str, Any] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = _normalized_symbol_list([item.get("ts_code") or item.get("symbol")])
        if symbol and symbol[0] not in by_symbol:
            by_symbol[symbol[0]] = item
    return [by_symbol[symbol] for symbol in requested_symbols if symbol in by_symbol]


def _market_context_digest(context: Any | None) -> dict[str, Any]:
    payload = _context_payload_for_llm(context) if context is not None else {}
    if not isinstance(payload, dict):
        return {}
    return _drop_empty(
        {
            "trade_date": payload.get("trade_date"),
            "requested_indices": payload.get("requested_indices"),
            "requested_dimensions": payload.get("requested_dimensions"),
            "requested_horizons": payload.get("requested_horizons"),
            "indices": _trim_payload(payload.get("indices") or [], max_chars=5000),
            "market_breadth": _trim_payload(payload.get("market_breadth") or {}, max_chars=2500),
            "limit_sentiment": _trim_payload(payload.get("limit_sentiment") or {}, max_chars=2500),
            "fund_flow": _trim_payload(payload.get("fund_flow") or {}, max_chars=2500),
            "hot_sector_context": _trim_payload(payload.get("hot_sector_context") or {}, max_chars=2500),
            "hot_sectors": _trim_payload(payload.get("hot_sectors") or [], max_chars=2500),
            "catalysts": _trim_payload(payload.get("catalysts") or {}, max_chars=3000),
            "missing_fields": payload.get("missing_fields"),
            "system_message": payload.get("system_message"),
        }
    )


def _opportunity_digest(context: Any | None) -> dict[str, Any]:
    payload = _context_payload_for_llm(context) if context is not None else {}
    return _chat_discovery_digest(payload) if payload else {}


def _chat_discovery_digest(payload: dict[str, Any], *, candidate_limit: int = 10) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    compact_message = str(payload.get("system_message_for_llm") or "").strip()
    system_message = str(payload.get("system_message") or "").strip()
    discovery = payload.get("opportunity_discovery") if isinstance(payload.get("opportunity_discovery"), dict) else payload
    raw_candidates = _list_items(discovery.get("candidates"))
    if not raw_candidates and (compact_message or system_message):
        return {"system_message_for_llm": compact_message or _truncate_text(system_message, 9000)}
    candidates = [
        _compact_chat_discovery_candidate(item, rank=index)
        for index, item in enumerate(raw_candidates[:candidate_limit], start=1)
    ]
    candidates = [item for item in candidates if item]
    omitted_count = max(0, len(raw_candidates) - len(candidates))
    return _drop_empty(
        {
            "query": payload.get("query"),
            "trade_date": discovery.get("trade_date"),
            "signals": discovery.get("signals"),
            "candidate_count": discovery.get("candidate_count"),
            "scanned_count": discovery.get("scanned_count"),
            "llm_pool_count": discovery.get("llm_pool_count"),
            "llm_unavailable": payload.get("llm_unavailable") or discovery.get("llm_unavailable"),
            "report_path": discovery.get("report_path") or payload.get("report_path"),
            "message": discovery.get("message"),
            "candidate_summary": {
                "returned_count": len(raw_candidates),
                "included_count": len(candidates),
                "omitted_count": omitted_count,
                "policy": (
                    "candidates contains compact technical details for every included row; "
                    "when omitted_count is 0, do not claim later-ranked candidates or original discovery data were truncated."
                ),
            },
            "candidates": candidates,
            "missing_fields": _list_items(discovery.get("missing_fields"))[:20],
            "data_policy": discovery.get("data_policy") or payload.get("data_policy"),
        }
    )


def _compact_chat_discovery_candidate(value: Any, *, rank: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty(
        {
            "rank": rank,
            "ts_code": value.get("ts_code"),
            "name": value.get("name"),
            "trade_date": value.get("trade_date"),
            "ranking_score": value.get("ranking_score") or value.get("score"),
            "local_score": value.get("local_score"),
            "close": value.get("close"),
            "decision": value.get("decision"),
            "trend": value.get("trend"),
            "events": [_compact_chat_discovery_event(item) for item in _list_items(value.get("events"))[:3]],
            "key_levels": value.get("key_levels"),
            "indicator": _compact_chat_discovery_indicator(value.get("indicator")),
            "hot_sectors": [_compact_chat_hot_sector(item) for item in _list_items(value.get("hot_sectors"))[:3]],
            "entry_trigger": _truncate_text(value.get("entry_trigger"), 140),
            "invalidation": _truncate_text(value.get("invalidation"), 140),
            "risk": _truncate_text(value.get("risk"), 180),
        }
    )


def _compact_chat_discovery_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"label": _truncate_text(value, 80)}
    return _drop_empty(
        {
            "signal_id": value.get("signal_id"),
            "label": value.get("label"),
            "category": value.get("category"),
            "side": value.get("side"),
            "confidence": value.get("confidence"),
            "score": value.get("score"),
            "reason": _truncate_text(value.get("reason"), 180),
            "risk_flags": [_truncate_text(item, 80) for item in _list_items(value.get("risk_flags"))[:4]],
        }
    )


def _compact_chat_discovery_indicator(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = _drop_empty(
        {
            "technical": value.get("technical"),
            "volume": value.get("volume"),
            "support_resistance": value.get("support_resistance"),
            "moneyflow": value.get("moneyflow"),
            "fundamentals": value.get("fundamentals"),
            "data_sources": value.get("data_sources"),
        }
    )
    factor = value.get("factor")
    if isinstance(factor, dict):
        result["factor"] = _drop_empty(
            {
                "profile": factor.get("profile"),
                "score": factor.get("score"),
                "coverage": factor.get("coverage"),
                "missing_factors": _list_items(factor.get("missing_factors"))[:8],
            }
        )
    return result


def _compact_chat_hot_sector(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"name": _truncate_text(value, 60)}
    return _drop_empty(
        {
            "name": value.get("name"),
            "sector_type": value.get("sector_type") or value.get("type"),
            "latest_pct_chg": value.get("latest_pct_chg") or value.get("pct_chg"),
            "heat_score": value.get("heat_score") or value.get("score"),
        }
    )


def _chan_context_digest(context: ChanChatContext | None) -> dict[str, Any]:
    if context is None:
        return {}
    return _trim_payload(context.payload, max_chars=5000)


def _knowledge_context_digest(
    context: StockResearchContext | None,
    *,
    collections: Iterable[str] = (),
) -> dict[str, Any]:
    if context is None:
        return {}
    payload = _context_payload_for_llm(context)
    result = {
        "collections": list(getattr(context, "collections", ()) or collections),
        "sources": list(getattr(context, "sources", ()) or ()),
    }
    if isinstance(payload, dict):
        if not result["collections"] and isinstance(payload.get("collections"), (list, tuple)):
            result["collections"] = list(payload["collections"])
        if not result["sources"] and isinstance(payload.get("sources"), (list, tuple)):
            result["sources"] = list(payload["sources"])
        if payload.get("system_message"):
            result["system_message"] = payload["system_message"]
    return _drop_empty(result)


def _quote_context_digest(context: Any | None) -> dict[str, Any]:
    payload = _context_payload_for_llm(context) if context is not None else {}
    return _trim_payload(payload, max_chars=2500) if payload else {}


def _fallback_chat_summary(route: ChatRequestRoute, evidence: ChatEvidenceBundle) -> str:
    if route.route_kind == CHAT_ROUTE_OPPORTUNITY and evidence.opportunity_context is not None:
        return _opportunity_discovery_content_or_fallback("", evidence.opportunity_context)
    if route.route_kind == CHAT_ROUTE_MARKET and evidence.market_context is not None:
        payload = _context_payload_for_llm(evidence.market_context)
        return f"已获取 A 股大盘真实上下文，交易日 {payload.get('trade_date') or route.trade_date or '未知'}。以上仅供研究，不构成投资建议。"
    if route.route_kind == CHAT_ROUTE_STOCK and evidence.stock_context is not None:
        return f"已获取 {', '.join(route.symbols) or '目标股票'} 的真实个股上下文和指标。以上仅供研究，不构成投资建议。"
    if route.route_kind == CHAT_ROUTE_CHAN and evidence.chan_context is not None:
        return "已加载缠论本地规则与知识卡证据。以上仅供研究，不构成投资建议。"
    return "无响应"


def _matched_skills_for_route(route: ChatRequestRoute, skills: list[Skill]) -> list[Skill]:
    if not skills:
        return []
    matched = match_skills(route.intent or route.reason, skills)
    return match_skills_for_route(route, skills, matched=matched)


def _resolve_stock_followup(message: str, parsed: StockQuestion, *, last_stock_question: StockQuestion | None) -> StockQuestion | None:
    if parsed.has_stock_question:
        return parsed
    if last_stock_question is None or not _is_stock_followup(message):
        return None
    return StockQuestion(
        symbols=list(last_stock_question.symbols),
        trade_date=extract_trade_date(message) or last_stock_question.trade_date,
        as_of_time=extract_intraday_time(message) or last_stock_question.as_of_time,
        has_stock_question=True,
    )


def _should_include_knowledge_context(route_kind: str, *, explicit_knowledge: str | None, plan: ChatPlan) -> bool:
    if explicit_knowledge:
        return True
    if route_kind not in {CHAT_ROUTE_STOCK, CHAT_ROUTE_MARKET, CHAT_ROUTE_OPPORTUNITY, CHAT_ROUTE_CHAN}:
        return False
    return bool(_knowledge_collections_for_route(route_kind, plan))


def _knowledge_collections_for_route(route_kind: str, plan: ChatPlan) -> tuple[str, ...]:
    collections: list[str] = []
    skill_ids = set(getattr(plan, "skills", ()) or ())
    if route_kind == CHAT_ROUTE_STOCK:
        collections.extend(STOCK_ANALYSIS_DEFAULT_RAG_COLLECTIONS)
    elif route_kind == CHAT_ROUTE_MARKET:
        collections.extend(["market", "sentiment"])
    elif route_kind == CHAT_ROUTE_OPPORTUNITY:
        collections.extend(["signals", "market", "sentiment"])
    elif route_kind == CHAT_ROUTE_CHAN:
        collections.append("chan")
    if {"financial-statement", "valuation-model", "fundamental-filter", "growth-quality"} & skill_ids:
        collections.append("fundamental")
    if {"risk-analysis", "portfolio-health-check", "risk-adjusted-return-optimizer", "suitability-report-generator"} & skill_ids:
        collections.append("risk")
    collections.extend(collections_for_skill_ids(skill_ids, intent=route_kind))
    return tuple(_dedupe(collections))


def _knowledge_context_limit(plan_collections: tuple[str, ...], *, explicit_knowledge: str | None) -> int:
    if explicit_knowledge:
        return 6
    if set(STOCK_ANALYSIS_DEFAULT_RAG_COLLECTIONS).issubset(plan_collections):
        return STOCK_ANALYSIS_DEFAULT_RAG_LIMIT
    return 6


def _context_payload_for_llm(context: Any) -> Any:
    builder = getattr(context, "to_llm_context", None)
    if callable(builder):
        return builder()
    payload = getattr(context, "payload", None)
    if payload is not None:
        return payload
    compact_builder = getattr(context, "system_message_for_llm", None)
    if callable(compact_builder):
        return {"system_message_for_llm": str(compact_builder() or "")}
    to_dict = getattr(context, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    system_message = str(getattr(context, "system_message", "") or "").strip()
    if system_message:
        return {"system_message": system_message}
    return {}


def _run_component(
    name: str,
    runner: Callable[[], Any],
    *,
    progress: Any | None,
    recorder: Any | None,
    payload: dict[str, Any],
    progress_label: str = "",
    progress_message: str = "",
) -> Any:
    started = time.monotonic()
    _emit_context_started(recorder, name, payload=payload)
    if progress is None or not progress_label:
        result = runner()
    else:
        with progress.step(progress_label) as step:
            result = runner()
            step.complete(message=progress_message or None)
    _emit_context_completed(
        recorder,
        name,
        payload={"available": result is not None, **payload},
        duration_seconds=max(0.0, time.monotonic() - started),
    )
    return result


def _emit_context_started(recorder: Any | None, name: str, *, payload: dict[str, Any] | None = None) -> None:
    if recorder is not None:
        recorder.emit(
            "context_started",
            item_type="context",
            item_name=name,
            status="running",
            payload=payload or {},
        )


def _emit_context_completed(recorder: Any | None, name: str, *, payload: dict[str, Any] | None = None, duration_seconds: float | None = None) -> None:
    if recorder is not None:
        recorder.emit(
            "context_completed",
            item_type="context",
            item_name=name,
            status="done",
            payload=payload or {},
            duration_seconds=duration_seconds,
        )


def _persist_rule_plan(store: ChatMemoryStore, *, session_id: str, plan: RuleGenerationPlan, title: str) -> str:
    pending = _find_latest_pending_rule_plan(store, session_id=session_id)
    payload = {"plan": _rule_plan_payload(plan)}
    if pending is None:
        return store.create_pending_action(
            session_id=session_id,
            action_type=RULE_GENERATION_ACTION_TYPE,
            title=title,
            payload=payload,
        )
    _replace_rule_plan(store, action_id=str(pending["action_id"]), plan=plan, title=title)
    return str(pending["action_id"])


def _replace_rule_plan(store: ChatMemoryStore, *, action_id: str, plan: RuleGenerationPlan, title: str) -> None:
    store.update_pending_action_payload(
        action_id,
        payload={"plan": _rule_plan_payload(plan)},
        title=title,
        status="pending",
    )


def _find_latest_pending_rule_plan(store: ChatMemoryStore, *, session_id: str) -> dict[str, Any] | None:
    rows = store.list_pending_actions(session_id=session_id, action_type=RULE_GENERATION_ACTION_TYPE, status="pending", limit=1)
    return rows[0] if rows else None


def _latest_semantic_screen_spec(store: ChatMemoryStore, *, session_id: str) -> dict[str, Any]:
    rows = store.list_pending_actions(session_id=session_id, action_type="semantic_screen_spec", status="pending", limit=1)
    if not rows:
        return {}
    payload = rows[0].get("payload") if isinstance(rows[0].get("payload"), dict) else {}
    spec = payload.get("semantic_spec") if isinstance(payload.get("semantic_spec"), dict) else {}
    return dict(spec)


def _is_save_latest_semantic_rule_request(message: str) -> bool:
    text = str(message or "")
    return any(term in text for term in ("保存这个规则", "保存该规则", "保存刚才的规则", "把这个规则保存", "持久化这个规则"))


def _find_pending_rule_plan(store: ChatMemoryStore, *, session_id: str, rule_name: str) -> dict[str, Any] | None:
    normalized = str(rule_name or "").strip().replace("-", "_")
    rows = store.list_pending_actions(session_id=session_id, action_type=RULE_GENERATION_ACTION_TYPE, status="pending", limit=20)
    for row in rows:
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            continue
        plan = payload.get("plan", {})
        if isinstance(plan, dict) and str(plan.get("rule_name") or "").strip().replace("-", "_") == normalized:
            return row
    return None


def _rule_plan_payload(plan: RuleGenerationPlan) -> dict[str, Any]:
    return {
        "decision_name": plan.decision_name,
        "rule_name": plan.rule_name,
        "goal": plan.goal,
        "data_dependencies": list(plan.data_dependencies),
        "conditions": [dict(item) for item in plan.conditions],
        "pass_condition": plan.pass_condition,
        "risk_notes": list(plan.risk_notes),
        "questions": list(plan.questions),
        "unsupported_requirements": list(plan.unsupported_requirements),
        "source_text": plan.source_text,
    }


def _rule_plan_from_payload(payload: Any) -> RuleGenerationPlan:
    data = dict(payload or {})
    return RuleGenerationPlan(
        decision_name=str(data.get("decision_name") or ""),
        rule_name=str(data.get("rule_name") or ""),
        goal=str(data.get("goal") or ""),
        data_dependencies=tuple(str(item) for item in data.get("data_dependencies") or ()),
        conditions=tuple(dict(item) for item in data.get("conditions") or ()),
        pass_condition=str(data.get("pass_condition") or ""),
        risk_notes=tuple(str(item) for item in data.get("risk_notes") or ()),
        questions=tuple(str(item) for item in data.get("questions") or ()),
        unsupported_requirements=tuple(str(item) for item in data.get("unsupported_requirements") or ()),
        source_text=str(data.get("source_text") or ""),
    )


def _generated_rule_payload(result: GeneratedRuleResult) -> dict[str, Any]:
    return {
        "rule_name": result.rule_name,
        "class_name": result.class_name,
        "path": str(result.path),
    }


def _build_quote_context(message: str, *, settings: Settings, symbols: list[str]) -> Any:
    from sats import chat as chat_module

    builder = getattr(chat_module, "build_stock_quote_llm_context", build_stock_quote_llm_context)
    return builder(message, settings=settings, symbols=symbols)


def _signal_input_from_stock_payload(item: dict[str, Any]):
    from sats.signals import SignalInput

    return SignalInput(
        ts_code=str(item.get("ts_code") or ""),
        trade_date=str(item.get("trade_date") or ""),
        daily=pd.DataFrame(item.get("daily_tail") or []),
        stock_basic={"name": item.get("name") or ""},
        metadata=minute_curve_metadata(
            item.get("minute_curves") or {},
            preferred_period=str(item.get("chan_minute_period") or ""),
        ),
    )


def _memory_context(memories: list[MemoryRecord]) -> str:
    lines = ["以下是 SATS 本地长期记忆，可能与当前问题相关；如与用户当前输入冲突，以当前输入为准："]
    for memory in memories:
        tags = f" tags={','.join(memory.tags)}" if memory.tags else ""
        lines.append(f"- [{memory.memory_type}] {memory.content}{tags}")
    return "\n".join(lines)


def _dedupe(values: Iterable[Any]) -> list[Any]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _pick_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty({field: value.get(field) for field in fields})


def _list_items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def _trim_payload(value: Any, *, max_chars: int = 18000) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return value
    return {"truncated_json": text[:max_chars], "truncated": True}


def _main_model_name(settings: Settings) -> str:
    return str(getattr(settings, "openai_model", "") or "LLM")


def _light_model_name(settings: Settings) -> str:
    return str(getattr(settings, "light_model_name", "") or getattr(settings, "openai_model", "") or "LLM")


def _timeout_model_name(settings: Settings) -> str:
    light = _light_model_name(settings)
    main = _main_model_name(settings)
    return light if light == main else f"{light} / {main}"


def _llm_timeout_seconds(settings: Settings) -> int | None:
    value = getattr(settings, "llm_timeout_seconds", None)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _today_yyyymmdd() -> str:
    return time.strftime("%Y%m%d")


def _is_timeout_exception(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc or "").lower()
    return "timeout" in text or "timed out" in text


def _timeout_message(model_name: str) -> str:
    return (
        f"{model_name} 请求超时。请缩小问题范围、切换更快的模型，或稍后重试。"
        "本次未返回可用的大模型分析结果。"
    )


_STOCK_FOLLOWUP_REFERENCES = ("继续", "它", "它们", "这只", "这些", "刚才", "上面")
_STOCK_FOLLOWUP_INTENTS = (
    "分析",
    "走势",
    "怎么看",
    "怎么样",
    "缠论",
    "结构",
    "风险",
    "买",
    "卖",
    "背驰",
    "中枢",
    "指标",
    "均线",
    "macd",
    "kdj",
    "rsi",
)
_RULE_PLAN_REVISION_TERMS = ("改", "调整", "补充", "去掉", "忽略", "不需要", "不要", "加上", "规则名", "rule_name")


def _is_stock_followup(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    return any(term in text for term in _STOCK_FOLLOWUP_REFERENCES) and any(term.lower() in text for term in _STOCK_FOLLOWUP_INTENTS)


def _looks_like_rule_plan_revision(message: str) -> bool:
    text = str(message or "").strip()
    return bool(text) and any(term in text for term in _RULE_PLAN_REVISION_TERMS)


def _opportunity_discovery_content_or_fallback(content: str, opportunity_context: Any | None) -> str:
    text = str(content or "").strip()
    if _is_substantive_opportunity_answer(text):
        return text
    if opportunity_context is None:
        return text
    try:
        if hasattr(opportunity_context, "discovery"):
            fallback = format_stock_picking_agent_result(opportunity_context)
        else:
            fallback = format_opportunity_discovery(opportunity_context)
    except Exception:
        return text
    report_path = str(getattr(opportunity_context, "report_path", "") or "").strip()
    if report_path and "报告:" not in fallback:
        fallback = f"{fallback}\n报告: {report_path}"
    return fallback


def _is_substantive_opportunity_answer(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    if len(text) < 80 and text.lstrip().startswith("#") and "\n\n" not in text:
        return False
    return any(term in text for term in ("触发", "失效", "风险", "报告:", "不构成投资建议"))
