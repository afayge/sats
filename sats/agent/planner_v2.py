from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Iterable

import pandas as pd

from sats.agent.date_policy import resolve_agent_time_context, sanitize_agent_tool_arguments
from sats.agent.models import AgentExecutionPolicy, AgentPlan, AgentStep
from sats.agent.planner import build_agent_plan as build_legacy_agent_plan
from sats.agent.tools import AgentToolRegistry
from sats.agent.tools.base import find_fabricated_market_data, validate_tool_arguments
from sats.analysis.market_llm_context import DEFAULT_MARKET_DIMENSIONS, is_market_question
from sats.analysis.opportunity_discovery import is_opportunity_discovery_question
from sats.config import Settings
from sats.llm import ChatLLM, build_light_fallback_llm, extract_json_object
from sats.stock_basic_lookup import load_stock_basic_frame, names_from_stock_basic, resolve_stock_mentions, resolve_stock_names
from sats.stock_question import extract_intraday_time, extract_stock_symbols, extract_trade_date
from sats.symbols import normalize_symbols


@dataclass(frozen=True, slots=True)
class PlannerV2Intent:
    objective: str
    intent: str = "general_qa"
    domain: str = "general"
    task: str = "general_qa"
    ambiguity_level: str = "none"
    entities: dict[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()
    clarification_options: tuple[str, ...] = ()
    needs_clarification: bool = False
    risk_level: str = "low"
    output_preferences: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["constraints"] = list(self.constraints)
        payload["assumptions"] = list(self.assumptions)
        payload["ambiguities"] = list(self.ambiguities)
        payload["clarification_options"] = list(self.clarification_options)
        payload["output_preferences"] = list(self.output_preferences)
        return payload


@dataclass(frozen=True, slots=True)
class PlannerV2Grounding:
    selected_tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    knowledge: tuple[str, ...] = ()
    data_capabilities: tuple[str, ...] = ()
    reasoning_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["selected_tools"] = list(self.selected_tools)
        payload["skills"] = list(self.skills)
        payload["knowledge"] = list(self.knowledge)
        payload["data_capabilities"] = list(self.data_capabilities)
        return payload


@dataclass(frozen=True, slots=True)
class PlannerV2Result:
    intent: PlannerV2Intent
    grounding: PlannerV2Grounding
    plan: AgentPlan
    status: str = "ready"
    clarification_id: str = ""
    clarification_questions: tuple[str, ...] = ()
    normalized_message: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_clarification(self) -> bool:
        return bool(self.status == "clarification_required" or self.clarification_questions or self.intent.needs_clarification)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "grounding": self.grounding.to_dict(),
            "plan": self.plan.to_dict(),
            "status": self.status,
            "clarification_id": self.clarification_id,
            "clarification_questions": list(self.clarification_questions),
            "normalized_message": self.normalized_message,
            "diagnostics": dict(self.diagnostics),
        }


class PlannerV2Engine:
    """Recoverable semantic planner state machine for SATS Agent."""

    def __init__(
        self,
        *,
        settings: Settings,
        policy: AgentExecutionPolicy,
        llm_factory: Callable[..., Any] | None,
        tool_registry: AgentToolRegistry | None,
        policy_message: str | None = None,
        reference_context: Any | None = None,
        previous_observations: Iterable[Any] = (),
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.llm_factory = llm_factory
        self.tool_registry = tool_registry
        self.policy_message = policy_message
        self.reference_context = reference_context
        self.previous_observations = tuple(previous_observations or ())

    def build(self, message: str) -> PlannerV2Result:
        text = str(message or "").strip()
        if _is_replan_request(text):
            return _build_replan_result(
                text,
                settings=self.settings,
                policy=self.policy,
                llm_factory=self.llm_factory,
                tool_registry=self.tool_registry,
                policy_message=self.policy_message,
                reference_context=self.reference_context,
                previous_observations=self.previous_observations,
            )
        intent = self.build_intent(text)
        intent = self.apply_defaults(intent)
        intent = self.detect_blocking_ambiguity(intent)
        normalized_message = _normalized_message(text, intent)
        if intent.needs_clarification:
            return self.clarify_or_ground(intent, normalized_message=normalized_message)
        plan = self.build_plan(intent, normalized_message=normalized_message)
        plan = self.validate(plan, original_message=text)
        grounding = _grounding_for_plan(plan, intent=intent)
        diagnostics = {
            "phase": "validate",
            "planner": "v2",
            "status": "ready",
            "normalized_entities": dict(intent.entities),
            "legacy_fallback": bool(plan.phase == "planner"),
        }
        return PlannerV2Result(
            intent=intent,
            grounding=grounding,
            plan=plan,
            status="ready",
            normalized_message=normalized_message,
            diagnostics=diagnostics,
        )

    def build_intent(self, message: str) -> PlannerV2Intent:
        return _understand(
            message,
            settings=self.settings,
            llm_factory=self.llm_factory,
            tool_registry=self.tool_registry,
            reference_context=self.reference_context,
        )

    def apply_defaults(self, intent: PlannerV2Intent) -> PlannerV2Intent:
        return apply_nonblocking_defaults(intent)

    def detect_blocking_ambiguity(self, intent: PlannerV2Intent) -> PlannerV2Intent:
        return detect_blocking_ambiguities(intent, reference_context=self.reference_context)

    def clarify_or_ground(self, intent: PlannerV2Intent, *, normalized_message: str) -> PlannerV2Result:
        plan = _empty_clarification_plan(intent)
        return PlannerV2Result(
            intent=intent,
            grounding=PlannerV2Grounding(reasoning_summary="planner_v2 stopped before planning because required entities are ambiguous."),
            plan=plan,
            status="clarification_required",
            clarification_id=_new_clarification_id(),
            clarification_questions=intent.ambiguities,
            normalized_message=normalized_message,
            diagnostics={"phase": "clarify", "planner": "v2", "status": "clarification_required"},
        )

    def ground_capability(self, intent: PlannerV2Intent, *, normalized_message: str) -> AgentPlan:
        return _plan_from_intent(
            intent,
            normalized_message=normalized_message,
            settings=self.settings,
            policy=self.policy,
            llm_factory=self.llm_factory,
            tool_registry=self.tool_registry,
            reference_context=self.reference_context,
        )

    def build_plan(self, intent: PlannerV2Intent, *, normalized_message: str) -> AgentPlan:
        return self.ground_capability(intent, normalized_message=normalized_message)

    def validate(self, plan: AgentPlan, *, original_message: str) -> AgentPlan:
        return _validate_and_repair_plan(plan, original_message=original_message, tool_registry=self.tool_registry)


def build_agent_plan_v2(
    message: str,
    *,
    settings: Settings,
    policy: AgentExecutionPolicy,
    llm_factory: Callable[..., Any] | None = ChatLLM,
    tool_registry: AgentToolRegistry | None = None,
    policy_message: str | None = None,
    reference_context: Any | None = None,
    previous_observations: Iterable[Any] = (),
) -> PlannerV2Result:
    """Build a Codex-like SATS Agent plan without changing the runtime contract."""

    text = str(message or "").strip()
    try:
        return PlannerV2Engine(
            settings=settings,
            policy=policy,
            llm_factory=llm_factory,
            tool_registry=tool_registry,
            policy_message=policy_message,
            reference_context=reference_context,
            previous_observations=previous_observations,
        ).build(text)
    except Exception as exc:
        plan = build_legacy_agent_plan(
            text,
            settings=settings,
            policy=policy,
            llm_factory=llm_factory,
            tool_registry=tool_registry,
            policy_message=policy_message,
            reference_context=reference_context,
        )
        intent = PlannerV2Intent(
            objective=text,
            intent="legacy_fallback",
            domain="general",
            task="legacy_fallback",
            assumptions=("planner_v2 failed before completion; legacy planner result is used.",),
        )
        return PlannerV2Result(
            intent=intent,
            grounding=_grounding_for_plan(plan, intent=intent),
            plan=plan,
            status="ready",
            normalized_message=text,
            diagnostics={"phase": "fallback", "planner": "legacy", "error": str(exc)},
        )


def _build_replan_result(
    message: str,
    *,
    settings: Settings,
    policy: AgentExecutionPolicy,
    llm_factory: Callable[..., Any] | None,
    tool_registry: AgentToolRegistry | None,
    policy_message: str | None,
    reference_context: Any | None,
    previous_observations: Iterable[Any],
) -> PlannerV2Result:
    objective = str(policy_message or message or "").strip()
    plan = build_legacy_agent_plan(
        message,
        settings=settings,
        policy=policy,
        llm_factory=llm_factory,
        tool_registry=tool_registry,
        policy_message=policy_message,
        reference_context=reference_context,
    )
    plan = _validate_and_repair_plan(plan, original_message=objective or message, tool_registry=tool_registry)
    intent = PlannerV2Intent(
        objective=objective or plan.objective,
        intent="replan",
        domain="general",
        task="replan",
        entities={"previous_observation_count": len(tuple(previous_observations or ()))},
        assumptions=("基于已有 observations 只规划后续替代步骤。",),
        risk_level=plan.risk_level,
    )
    return PlannerV2Result(
        intent=intent,
        grounding=_grounding_for_plan(plan, intent=intent),
        plan=plan,
        normalized_message=message,
        diagnostics={"phase": "replan", "planner": "v2"},
    )


def _understand(
    message: str,
    *,
    settings: Settings,
    llm_factory: Callable[..., Any] | None,
    tool_registry: AgentToolRegistry | None,
    reference_context: Any | None,
) -> PlannerV2Intent:
    text = str(message or "").strip()
    llm_payload = _llm_understand(
        text,
        settings=settings,
        llm_factory=llm_factory,
        tool_registry=tool_registry,
        reference_context=reference_context,
    )
    stock_basic = _load_stock_basic(settings)
    local = _local_intent(text, settings=settings, stock_basic=stock_basic, reference_context=reference_context)
    if not llm_payload:
        return local
    llm_intent = str(llm_payload.get("intent") or "").strip()
    objective = str(llm_payload.get("objective") or local.objective).strip() or local.objective
    llm_entities = llm_payload.get("entities") if isinstance(llm_payload.get("entities"), dict) else {}
    entities = _merge_entities(local.entities, llm_entities)
    domain = str(llm_payload.get("domain") or entities.get("domain") or local.domain).strip() or local.domain
    task = str(llm_payload.get("task") or entities.get("task") or local.task).strip() or local.task
    ambiguity_level = str(llm_payload.get("ambiguity_level") or entities.get("ambiguity_level") or local.ambiguity_level).strip() or local.ambiguity_level
    entities = {**entities, "domain": domain, "task": task, "ambiguity_level": ambiguity_level}
    if not entities.get("market_dimensions"):
        entities["market_dimensions"] = _market_dimensions_for_task(task, text)
    constraints = _dedupe([*local.constraints, *_string_list(llm_payload.get("constraints"))])
    assumptions = _dedupe([*local.assumptions, *_string_list(llm_payload.get("assumptions"))])
    ambiguities = _dedupe([*local.ambiguities, *_string_list(llm_payload.get("ambiguities"))])
    clarification_options = _dedupe([*local.clarification_options, *_string_list(llm_payload.get("clarification_options"))])
    output_preferences = _dedupe([*local.output_preferences, *_string_list(llm_payload.get("output_preferences"))])
    needs_clarification = local.needs_clarification or bool(ambiguities and ambiguity_level == "blocking")
    return replace(
        local,
        objective=objective,
        intent=llm_intent or local.intent,
        domain=domain,
        task=task,
        ambiguity_level=ambiguity_level,
        entities=entities,
        constraints=tuple(constraints),
        assumptions=tuple(assumptions),
        ambiguities=tuple(ambiguities),
        clarification_options=tuple(clarification_options),
        needs_clarification=needs_clarification,
        output_preferences=tuple(output_preferences),
    )


def _llm_understand(
    message: str,
    *,
    settings: Settings,
    llm_factory: Callable[..., Any] | None,
    tool_registry: AgentToolRegistry | None,
    reference_context: Any | None,
) -> dict[str, Any]:
    if not _can_use_llm_understand(llm_factory):
        return {}
    try:
        llm = build_light_fallback_llm(
            llm_factory,
            light_model_name=str(getattr(settings, "light_model_name", "") or getattr(settings, "openai_model", "") or ""),
            default_model_name=str(getattr(settings, "openai_model", "") or ""),
            timeout_seconds=_llm_timeout_seconds(settings),
        )
        response = llm.chat(
            _understand_messages(message, tool_registry=tool_registry, reference_context=reference_context),
            timeout=_llm_timeout_seconds(settings),
        )
    except Exception:
        return {}
    payload = extract_json_object(str(getattr(response, "content", "") or ""))
    if not isinstance(payload, dict) or not any(key in payload for key in ("intent", "domain", "task", "entities", "ambiguities")):
        return {}
    return payload


def _understand_messages(
    message: str,
    *,
    tool_registry: AgentToolRegistry | None,
    reference_context: Any | None,
) -> list[dict[str, str]]:
    reference = ""
    if reference_context is not None:
        symbols = ", ".join(str(item) for item in getattr(reference_context, "symbols", ()) or ())
        reference = f"\n上文引用上下文 symbols={symbols or 'none'} trade_date={getattr(reference_context, 'trade_date', '') or 'none'}。"
    return [
        {
            "role": "system",
            "content": (
                "你是 SATS Agent Planner V2 的自然语言理解阶段，只能输出 JSON 对象。"
                "不要规划工具，不要写结论，只抽取 objective、intent、domain、task、entities、"
                "constraints、assumptions、output_preferences、ambiguity_level、ambiguities、"
                "clarification_options。股票代码和日期最终会由 SATS 本地校验；"
                "只有不同解释会导致不同工具、数据源或副作用时才把 ambiguity_level 设为 blocking。"
            ),
        },
        {
            "role": "user",
            "content": (
                "可用能力摘要："
                f"{_tool_capability_summary(tool_registry)}\n"
                "按 JSON 输出字段：objective, intent, domain, task, entities, constraints, assumptions, "
                "output_preferences, ambiguity_level, ambiguities, clarification_options。"
                f"{reference}\n用户输入：{message}"
            ),
        },
    ]


def _can_use_llm_understand(llm_factory: Callable[..., Any] | None) -> bool:
    if llm_factory is None:
        return False
    if llm_factory is ChatLLM:
        return True
    module = str(getattr(llm_factory, "__module__", "") or "")
    return module.startswith("sats.llm")


def _merge_entities(local: dict[str, Any], llm_entities: dict[str, Any]) -> dict[str, Any]:
    merged = dict(local)
    for key, value in llm_entities.items():
        if value in (None, "", [], {}):
            continue
        if key in {"symbols", "stock_names", "explicit_dates", "forecast_horizons", "market_dimensions"}:
            merged[key] = _dedupe([*_string_list(merged.get(key)), *_string_list(value)])
        elif key in {"trade_date", "as_of_time", "requested_limit"}:
            if not merged.get(key):
                merged[key] = value
        else:
            merged[key] = value
    if merged.get("task") and not merged.get("market_dimensions"):
        merged["market_dimensions"] = _market_dimensions_for_task(str(merged.get("task") or ""), "")
    return merged


def _tool_capability_summary(tool_registry: AgentToolRegistry | None) -> str:
    names = ("research.market_context", "research.theme_stock_list", "research.discover_opportunities", "data.astock_catalog", "data.astock_fetch", "web.search", "web.social_hot", "web.hot_mentions")
    defaults = {
        "research.market_context": "获取真实 A 股大盘指数、市场宽度、涨跌停情绪、热点板块上下文。",
        "research.theme_stock_list": "解析主题相关 A 股股票池；不做短线机会筛选或预测。",
        "research.discover_opportunities": "运行自然语言 A 股机会发现，返回候选排序。",
        "web.search": "搜索公开网页证据；不能替代行情数据。",
        "web.social_hot": "获取公开社交热榜。",
        "web.hot_mentions": "按主题词在社交热榜中查命中。",
    }
    rows: list[str] = []
    for name in names:
        spec = tool_registry.get(name) if tool_registry is not None else None
        description = str(getattr(spec, "description", "") or defaults.get(name, "")).strip()
        if description:
            rows.append(f"{name}: {description}")
    return " | ".join(rows) if rows else "无工具摘要"


def _local_intent(
    message: str,
    *,
    settings: Settings,
    stock_basic: pd.DataFrame,
    reference_context: Any | None,
) -> PlannerV2Intent:
    text = str(message or "").strip()
    time_context = resolve_agent_time_context(text)
    explicit_symbols = normalize_symbols(extract_stock_symbols(text), required=False)
    reference_symbols = _reference_symbols(text, reference_context)
    mentioned_symbols = _resolve_stock_mentions(text, stock_basic)
    candidate_names = _candidate_stock_names(text, stock_basic=stock_basic)
    resolved_symbols, name_questions = _resolve_candidate_names(candidate_names, stock_basic)
    symbols = _dedupe([*mentioned_symbols, *explicit_symbols, *reference_symbols, *resolved_symbols])
    domain, task, intent = _semantic_classification(text, symbols=tuple(symbols))
    ambiguities = list(name_questions)
    if _needs_security_clarification(text, intent=intent, symbols=symbols, candidate_names=candidate_names):
        if not candidate_names:
            ambiguities.append("请补充明确的 6 位股票代码或唯一股票名称。")
        elif not stock_basic.empty and not name_questions:
            ambiguities.append("请确认要分析的是具体股票、指数/板块，还是普通概念解释。")
    trade_date = _safe_extract_trade_date(text)
    entities = {
        "symbols": symbols,
        "stock_names": candidate_names,
        "trade_date": trade_date or "",
        "explicit_dates": list(time_context.explicit_dates),
        "forecast_horizons": list(time_context.horizons),
        "analysis_periods": _analysis_periods(text),
        "market_dimensions": _market_dimensions_for_task(task, text),
        "domain": domain,
        "task": task,
        "as_of_time": extract_intraday_time(text) or "",
        "requested_limit": _requested_limit(text),
    }
    constraints = _constraints_from_text(text)
    output_preferences = _output_preferences(text)
    assumptions = ["市场数据必须来自 SATS 数据层或带 provenance 的工具结果。"]
    risk_level = "high" if _is_trade_text(text) else "medium" if intent != "general_qa" else "low"
    return PlannerV2Intent(
        objective=text,
        intent=intent,
        domain=domain,
        task=task,
        ambiguity_level="blocking" if ambiguities else "none",
        entities=entities,
        constraints=tuple(constraints),
        assumptions=tuple(assumptions),
        ambiguities=tuple(_dedupe(ambiguities)),
        clarification_options=tuple(_clarification_options_for_ambiguities(text, ambiguities)),
        needs_clarification=bool(ambiguities),
        risk_level=risk_level,
        output_preferences=tuple(output_preferences),
    )


def apply_nonblocking_defaults(intent: PlannerV2Intent) -> PlannerV2Intent:
    text = str(intent.objective or "")
    entities = dict(intent.entities or {})
    symbols = tuple(_string_list(entities.get("symbols")))
    local_domain, local_task, local_intent = _semantic_classification(text, symbols=symbols)
    if local_task in {"chan_analysis", "hot_sector_lookup", "theme_stock_list", "opportunity_discovery", "web_hot_sector_discussion"}:
        entities["domain"] = local_domain
        entities["task"] = local_task
        intent = replace(intent, domain=local_domain, task=local_task, intent=local_intent)
    if not entities.get("market_dimensions"):
        entities["market_dimensions"] = _market_dimensions_for_task(str(entities.get("task") or intent.task), text)
    assumptions = list(intent.assumptions)
    if "今天" in text:
        horizons = _string_list(entities.get("forecast_horizons"))
        if not horizons:
            horizons = ["today"]
        entities["forecast_horizons"] = horizons
        assumptions.append("“今天”按 SATS 日期策略解析为当前 Asia/Shanghai 日期和最近可用 A 股交易上下文。")
    if str(entities.get("task") or intent.task) == "chan_analysis":
        periods = _analysis_periods(text)
        if periods:
            entities["analysis_periods"] = periods
            entities["chan_period"] = periods[0]
        else:
            entities["analysis_periods"] = ["daily"]
            entities["chan_period"] = "daily"
            assumptions.append("缠论未指定周期时默认按日线/今日走势处理。")
    if str(entities.get("task") or intent.task) == "hot_sector_lookup":
        assumptions.append("“热点板块有哪些”默认解释为市场热点板块查询。")
    filtered_ambiguities = [
        item
        for item in intent.ambiguities
        if not _is_nonblocking_default_ambiguity(str(item or ""), intent=replace(intent, entities=entities), message=text)
    ]
    filtered_options = [
        item
        for item in intent.clarification_options
        if not _is_nonblocking_default_ambiguity(str(item or ""), intent=replace(intent, entities=entities), message=text)
    ]
    if not filtered_ambiguities:
        ambiguity_level = "none"
    elif intent.needs_clarification or intent.ambiguity_level == "blocking":
        ambiguity_level = "blocking"
    else:
        ambiguity_level = intent.ambiguity_level
    return replace(
        intent,
        entities=entities,
        assumptions=tuple(_dedupe(assumptions)),
        ambiguities=tuple(_dedupe(filtered_ambiguities)),
        clarification_options=tuple(_dedupe(filtered_options)),
        ambiguity_level=ambiguity_level,
        needs_clarification=bool(filtered_ambiguities and ambiguity_level == "blocking"),
    )


def _plan_from_intent(
    intent: PlannerV2Intent,
    *,
    normalized_message: str,
    settings: Settings,
    policy: AgentExecutionPolicy,
    llm_factory: Callable[..., Any] | None,
    tool_registry: AgentToolRegistry | None,
    reference_context: Any | None,
) -> AgentPlan:
    semantic_plan = _semantic_grounded_plan(intent, policy=policy)
    if semantic_plan is not None:
        return semantic_plan
    if _needs_astock_catalog_first(intent, llm_factory=llm_factory):
        return _astock_catalog_plan(intent, policy=policy)
    plan = build_legacy_agent_plan(
        normalized_message,
        settings=settings,
        policy=policy,
        llm_factory=llm_factory,
        tool_registry=tool_registry,
        reference_context=reference_context,
    )
    return _with_v2_plan_meta(plan, intent)


def _astock_catalog_plan(intent: PlannerV2Intent, *, policy: AgentExecutionPolicy) -> AgentPlan:
    text = intent.objective
    provider = _requested_provider(text)
    dataset = _explicit_dataset_id(text)
    steps = [
        AgentStep(
            step_id="astock_catalog",
            kind="tool",
            title="发现 AStock 数据接口",
            tool_name="data.astock_catalog",
            arguments={
                "provider": provider,
                "query": _catalog_query(text, dataset=dataset),
                "limit": 20,
                "compact": True,
            },
            side_effect="readonly",
            success_criteria="返回可由 data.astock_fetch 执行的白名单 operation 或 dataset。",
        )
    ]
    if dataset and provider in {"akshare", "tushare"}:
        steps.append(
            AgentStep(
                step_id="astock_fetch",
                kind="tool",
                title="按白名单数据集取数",
                tool_name="data.astock_fetch",
                arguments={
                    "operation": f"{provider}.dataset.fetch",
                    "params": {"dataset": dataset, "params": {}},
                    "limit": max(1, int(intent.entities.get("requested_limit") or 20)),
                },
                side_effect="write_db",
                success_criteria="取回有界数据并保留 provenance。",
            )
        )
    steps.append(AgentStep(step_id="final", kind="final", title="总结结果"))
    return AgentPlan(
        objective=intent.objective,
        success_criteria=("完成数据接口发现；如目录返回唯一可执行接口，则取回有界数据。",),
        assumptions=tuple(intent.assumptions),
        steps=tuple(steps),
        risk_level="high" if policy.auto_trade else intent.risk_level,
        requires_live_trading=bool(policy.live_trading),
        phase="planner_v2",
        model_policy="local",
        model_profile="local",
        model_name="local",
    )


def _semantic_grounded_plan(intent: PlannerV2Intent, *, policy: AgentExecutionPolicy) -> AgentPlan | None:
    task = str(intent.task or intent.entities.get("task") or "").strip()
    text = intent.objective
    if task == "hot_sector_lookup":
        return _single_tool_plan(
            intent,
            policy=policy,
            step=AgentStep(
                step_id="market_hot_sectors",
                kind="tool",
                title="获取热点板块上下文",
                tool_name="research.market_context",
                arguments={"horizons": _entity_horizons(intent), "dimensions": ["hot_sectors"]},
                side_effect="readonly",
                success_criteria="返回真实热点板块上下文和 provenance；缺失时明确说明数据缺口。",
            ),
            success_criteria=("获取今日热点板块上下文并总结。",),
        )
    if task == "theme_stock_list":
        return _single_tool_plan(
            intent,
            policy=policy,
            step=AgentStep(
                step_id="theme_stock_list",
                kind="tool",
                title="解析主题相关股票池",
                tool_name="research.theme_stock_list",
                arguments={"query": text, "limit": max(1, int(intent.entities.get("requested_limit") or 50))},
                side_effect="readonly",
            ),
            success_criteria=("返回主题相关股票池；不做短线机会预测。",),
        )
    if task == "opportunity_discovery":
        limit = int(intent.entities.get("requested_limit") or 0)
        return _single_tool_plan(
            intent,
            policy=policy,
            step=AgentStep(
                step_id="discover",
                kind="tool",
                title="运行机会发现",
                tool_name="research.discover_opportunities",
                arguments={"query": text, "limit": limit},
                side_effect="write_artifact",
            ),
            success_criteria=("运行自然语言机会发现并返回候选排序。",),
        )
    if task == "web_hot_sector_discussion":
        return _single_tool_plan(
            intent,
            policy=policy,
            step=AgentStep(
                step_id="web_hot_mentions",
                kind="tool",
                title="检索公开热榜中的热点板块讨论",
                tool_name="web.hot_mentions",
                arguments={"keyword": "热点板块", "platforms": [], "limit": 50, "extra_keywords": ["板块", "行业", "题材"]},
                side_effect="readonly",
                success_criteria="返回公开网络/社交热度证据；不得替代行情数据结论。",
            ),
            success_criteria=("检索公开网络或社交热榜中的热点板块讨论。",),
        )
    return None


def _single_tool_plan(
    intent: PlannerV2Intent,
    *,
    policy: AgentExecutionPolicy,
    step: AgentStep,
    success_criteria: tuple[str, ...],
) -> AgentPlan:
    return AgentPlan(
        objective=intent.objective,
        success_criteria=success_criteria,
        assumptions=tuple(intent.assumptions),
        steps=(step, AgentStep(step_id="final", kind="final", title="总结结果")),
        risk_level="high" if policy.auto_trade else intent.risk_level,
        requires_live_trading=bool(policy.live_trading),
        phase="planner_v2",
        model_policy="local",
        model_profile="local",
        model_name="local",
    )


def _validate_and_repair_plan(
    plan: AgentPlan,
    *,
    original_message: str,
    tool_registry: AgentToolRegistry | None,
) -> AgentPlan:
    if tool_registry is None:
        return _ensure_final_step(plan)
    repaired: list[AgentStep] = []
    diagnostics: list[dict[str, Any]] = []
    for step in plan.steps:
        if step.kind != "tool":
            repaired.append(step)
            continue
        spec = tool_registry.get(step.tool_name)
        if spec is None:
            diagnostics.append({"step_id": step.step_id, "issue": "unknown_tool", "tool_name": step.tool_name})
            continue
        sanitized = sanitize_agent_tool_arguments(step.tool_name, step.arguments, original_message)
        if sanitized.error:
            diagnostics.append({"step_id": step.step_id, "issue": "invalid_time_argument", "error": sanitized.error})
            continue
        error = validate_tool_arguments(spec, sanitized.arguments)
        if error:
            diagnostics.append({"step_id": step.step_id, "issue": "invalid_arguments", "error": error})
            continue
        fabricated = find_fabricated_market_data(sanitized.arguments, allow_trade_price=step.tool_name == "trade.submit_intent")
        if fabricated:
            diagnostics.append({"step_id": step.step_id, "issue": "fabricated_market_data", "path": fabricated})
            continue
        side_effect = step.side_effect or spec.side_effect
        repaired.append(replace(step, arguments=sanitized.arguments, side_effect=side_effect))
    if not repaired:
        repaired.append(
            AgentStep(
                step_id="chat",
                kind="tool",
                title="普通问答",
                tool_name="chat.answer",
                arguments={"message": original_message},
                side_effect="readonly",
            )
        )
    result = replace(plan, steps=tuple(repaired))
    result = _ensure_final_step(result)
    if diagnostics:
        checks = [*result.verification_checks, *({"name": item["issue"], "status": "repaired", "step_id": item.get("step_id", "")} for item in diagnostics)]
        result = replace(result, verification_checks=tuple(checks))
    return result


def _ensure_final_step(plan: AgentPlan) -> AgentPlan:
    if plan.steps and plan.steps[-1].kind == "final":
        return plan
    return replace(plan, steps=tuple([*plan.steps, AgentStep(step_id="final", kind="final", title="总结结果")]))


def _with_v2_plan_meta(plan: AgentPlan, intent: PlannerV2Intent) -> AgentPlan:
    assumptions = tuple(_dedupe([*plan.assumptions, *intent.assumptions]))
    risk_level = "high" if "high" in {plan.risk_level, intent.risk_level} else plan.risk_level or intent.risk_level
    return replace(
        plan,
        objective=intent.objective,
        assumptions=assumptions,
        risk_level=risk_level,
        phase="planner_v2",
    )


def _empty_clarification_plan(intent: PlannerV2Intent) -> AgentPlan:
    return AgentPlan(
        objective=intent.objective,
        success_criteria=("用户补充澄清信息后再生成执行计划。",),
        assumptions=tuple(intent.assumptions),
        steps=(),
        risk_level=intent.risk_level,
        phase="planner_v2",
        model_policy="local",
        model_profile="local",
        model_name="local",
    )


def _grounding_for_plan(plan: AgentPlan, *, intent: PlannerV2Intent) -> PlannerV2Grounding:
    tools = _dedupe([step.tool_name for step in plan.steps if step.kind == "tool" and step.tool_name])
    skills = _dedupe(_skill_hints(intent))
    knowledge = _dedupe(_knowledge_hints(intent.objective))
    data_capabilities = tuple(tool for tool in tools if tool.startswith("data."))
    summary = "planner_v2 grounded the normalized intent in registered Agent tools."
    if intent.needs_clarification:
        summary = "planner_v2 deferred grounding until clarification is complete."
    return PlannerV2Grounding(
        selected_tools=tuple(tools),
        skills=tuple(skills),
        knowledge=tuple(knowledge),
        data_capabilities=data_capabilities,
        reasoning_summary=summary,
    )


def _normalized_message(message: str, intent: PlannerV2Intent) -> str:
    entities = intent.entities
    fragments = [str(message or "").strip()]
    symbols = _string_list(entities.get("symbols"))
    trade_date = str(entities.get("trade_date") or "").strip()
    horizons = _string_list(entities.get("forecast_horizons"))
    as_of_time = str(entities.get("as_of_time") or "").strip()
    if symbols:
        fragments.append("已规范化股票代码：" + ",".join(symbols))
    if trade_date:
        fragments.append(f"已规范化交易日：{trade_date}")
    if horizons:
        fragments.append("预测周期：" + ",".join(horizons))
    if as_of_time:
        fragments.append(f"盘中时间：{as_of_time}")
    return "；".join(fragment for fragment in fragments if fragment)


def _load_stock_basic(settings: Settings) -> pd.DataFrame:
    try:
        return load_stock_basic_frame(settings)
    except Exception:
        return pd.DataFrame()


def _resolve_stock_mentions(message: str, stock_basic: pd.DataFrame) -> list[str]:
    try:
        return resolve_stock_mentions(message, stock_basic)
    except Exception:
        return []


def _resolve_candidate_names(names: list[str], stock_basic: pd.DataFrame) -> tuple[list[str], list[str]]:
    if not names:
        return [], []
    resolution = resolve_stock_names(names, stock_basic)
    return list(resolution.symbols), list(resolution.questions)


def _candidate_stock_names(message: str, *, stock_basic: pd.DataFrame) -> list[str]:
    text = str(message or "").strip()
    if not text or is_market_question(text) or is_opportunity_discovery_question(text) or _looks_like_market_scope(text):
        return []
    names = names_from_stock_basic(text, stock_basic)
    candidates: list[str] = [*names]
    for pattern in (
        r"(?:分析|研究|看看|评价|复盘|预测|查询)([\u4e00-\u9fffA-Za-z]{2,16})(?:的)?(?:技术面|基本面|财务|估值|走势|行情|公告|新闻|公司|股票)?$",
        r"^([\u4e00-\u9fffA-Za-z]{2,16})(?:技术面|基本面|财务|估值|走势|行情|公告|新闻|公司|股票)$",
    ):
        match = re.search(pattern, text)
        if match:
            candidates.append(match.group(1))
    clean: list[str] = []
    for candidate in candidates:
        value = _clean_stock_name_candidate(candidate)
        if value and value not in _GENERIC_STOCK_NAME_WORDS and not _looks_like_market_scope(value):
            clean.append(value)
    return _dedupe(clean)


def _clean_stock_name_candidate(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    text = re.sub(r"^(一下|这个|这只|帮我|请|今天|明天|下周)+", "", text)
    text = re.sub(r"(怎么看|怎么样|如何|好吗|可以吗)$", "", text)
    return text.strip("，。；;、:：")


def detect_blocking_ambiguities(intent: PlannerV2Intent, *, reference_context: Any | None) -> PlannerV2Intent:
    questions = list(intent.ambiguities)
    options = list(intent.clarification_options)
    text = str(intent.objective or "")
    symbols = _string_list(intent.entities.get("symbols") if isinstance(intent.entities, dict) else None)
    if _is_unresolved_reference_request(text, symbols=symbols, reference_context=reference_context):
        questions.append("请说明“这个/这只/它”指的是哪只股票、哪个板块/指数，或上一条结果。")
        options.extend(["补充 6 位股票代码或唯一股票名称", "说明要看的板块/指数", "引用上一条结果中的具体对象"])
    if _is_ambiguous_market_or_security_request(text, intent=intent, symbols=symbols):
        questions.append("请确认要分析的是具体股票、指数/板块，还是普通概念解释；如果是股票，请给出 6 位代码或唯一股票名称。")
        options.extend(["具体股票", "指数/板块", "普通概念解释"])
    if intent.ambiguity_level == "blocking" and not questions:
        questions.extend(options or ["请补充会影响工具选择的关键信息。"])
    questions = _dedupe(questions)
    options = _dedupe(options)
    if not questions:
        return replace(intent, ambiguity_level="none", clarification_options=tuple(options), needs_clarification=False)
    return replace(
        intent,
        ambiguities=tuple(questions),
        clarification_options=tuple(options),
        ambiguity_level="blocking",
        needs_clarification=True,
    )


def _semantic_classification(message: str, *, symbols: tuple[str, ...]) -> tuple[str, str, str]:
    text = str(message or "")
    lowered = text.lower()
    if _is_trade_text(text):
        return "trade", "trade", "trade"
    if any(term in text for term in ("缠论", "三买", "背驰", "中枢")) or "chan" in lowered:
        return "stock", "chan_analysis", "chan_analysis"
    if _is_web_hot_sector_discussion(text):
        return "web", "web_hot_sector_discussion", "web_research"
    if _is_hot_sector_opportunity_text(text) or is_opportunity_discovery_question(text):
        return "market", "opportunity_discovery", "opportunity_discovery"
    if _is_hot_sector_lookup_text(text):
        return "market", "hot_sector_lookup", "market_analysis"
    if _is_theme_stock_list_text(text):
        return "theme", "theme_stock_list", "theme_stock_list"
    if symbols and _is_stock_analysis_like(text):
        return "stock", "stock_analysis", "stock_analysis"
    if "筛选" in text:
        return "stock", "screening_or_workflow", "screening_or_workflow"
    if "a股" in lowered and any(term in text for term in ("表现", "市场", "走势", "复盘", "分析")):
        return "market", "market_review", "market_analysis"
    if is_market_question(text):
        return "market", "market_review", "market_analysis"
    if _needs_astock_catalog_text(text):
        return "data", "data_lookup", "data_lookup"
    if symbols or _is_stock_analysis_like(text):
        return "stock", "stock_analysis", "stock_analysis"
    if any(term in text for term in ("回测", "策略")) or "backtest" in lowered:
        return "strategy", "strategy_or_backtest", "strategy_or_backtest"
    if any(term in text for term in ("网页", "新闻", "公告", "最新", "搜索", "政策", "网上", "网络上", "社交", "社媒")) or "http" in lowered:
        return "web", "web_research", "web_research"
    return "general", "general_qa", "general_qa"


def _infer_intent(message: str, *, symbols: tuple[str, ...]) -> str:
    return _semantic_classification(message, symbols=symbols)[2]


def _is_hot_sector_lookup_text(message: str) -> bool:
    value = re.sub(r"[\s的]+", "", str(message or "").strip().lower())
    if not value:
        return False
    if _has_stock_list_target(value) or _has_opportunity_target(value):
        return False
    has_sector_subject = any(term in value for term in ("热点板块", "热门板块", "热点行业", "热门行业", "热点题材", "领涨板块", "领涨行业", "强势板块", "强势行业"))
    has_lookup_intent = any(term in value for term in ("有哪些", "哪个", "哪些", "排名", "榜", "排行", "领涨", "最强"))
    return has_sector_subject and has_lookup_intent


def _is_theme_stock_list_text(message: str) -> bool:
    value = re.sub(r"[\s的]+", "", str(message or "").strip().lower())
    if not value or _is_hot_sector_lookup_text(value):
        return False
    has_stock_target = _has_stock_list_target(value)
    has_list_intent = any(term in value for term in ("有哪些", "列出", "名单", "简单信息", "主营", "行业", "股票池"))
    has_theme_subject = any(
        term in value
        for term in ("概念", "板块", "行业", "题材", "产业链", "内存", "存储", "芯片", "半导体", "dram", "nand", "flash", "ssd")
    )
    return has_stock_target or (has_list_intent and has_theme_subject and "热点板块" not in value and "热门板块" not in value)


def _is_hot_sector_opportunity_text(message: str) -> bool:
    value = re.sub(r"[\s的]+", "", str(message or "").strip().lower())
    return any(term in value for term in ("热点板块", "热门板块", "热点题材", "强势板块", "领涨板块")) and _has_opportunity_target(value)


def _is_web_hot_sector_discussion(message: str) -> bool:
    value = re.sub(r"[\s的]+", "", str(message or "").strip().lower())
    if not any(term in value for term in ("网上", "网络上", "社交", "社媒", "热搜", "热榜", "讨论", "舆情")):
        return False
    return any(term in value for term in ("热点板块", "热门板块", "热点行业", "热点题材"))


def _has_stock_list_target(value: str) -> bool:
    return any(term in value for term in ("相关股票", "相关个股", "相关a股", "概念股", "产业链股票", "题材股", "哪些股票", "哪些个股", "有哪些股票", "有哪些个股"))


def _has_opportunity_target(value: str) -> bool:
    return any(term in value for term in ("机会", "上涨潜力", "大概率上涨", "候选", "推荐", "选股", "明天可能涨", "未来几天可能涨"))


def _market_dimensions_for_task(task: str, message: str) -> list[str]:
    if task == "hot_sector_lookup":
        return ["hot_sectors"]
    if task == "market_review":
        return list(DEFAULT_MARKET_DIMENSIONS)
    return []


def _entity_horizons(intent: PlannerV2Intent) -> list[str]:
    horizons = _string_list(intent.entities.get("forecast_horizons") if isinstance(intent.entities, dict) else None)
    return horizons or ["today"]


def _analysis_periods(message: str) -> list[str]:
    text = str(message or "")
    periods: list[str] = []
    for match in re.finditer(r"(\d{1,3})\s*(?:分钟|分|m|min)", text, flags=re.IGNORECASE):
        periods.append(f"{int(match.group(1))}min")
    if any(term in text for term in ("日线", "日K", "日k")):
        periods.append("daily")
    if any(term in text for term in ("周线", "周K", "周k")):
        periods.append("weekly")
    if any(term in text for term in ("月线", "月K", "月k")):
        periods.append("monthly")
    return _dedupe(periods)


def _is_nonblocking_default_ambiguity(question: str, *, intent: PlannerV2Intent, message: str) -> bool:
    text = str(question or "")
    compact = re.sub(r"\s+", "", text)
    entity_task = intent.entities.get("task") if isinstance(intent.entities, dict) else ""
    task = str(intent.task or entity_task or "")
    if "今天" in message and any(term in compact for term in ("今天", "日期", "交易日", "当前日期")):
        if any(term in compact for term in ("校验", "具体值", "解析", "确认", "默认", "SATS")):
            return True
    if task == "chan_analysis" and "周期" in compact and any(term in compact for term in ("未指定", "不同时间周期", "默认", "日线")):
        return True
    if task == "hot_sector_lookup" and "热点板块" in message and any(term in compact for term in ("股票", "个股", "板块")):
        return True
    return False


def _new_clarification_id() -> str:
    return f"clarify_{uuid.uuid4().hex[:12]}"


def _clarification_options_for_ambiguities(message: str, ambiguities: list[str]) -> list[str]:
    if not ambiguities:
        return []
    if any(term in str(message or "") for term in ("银行", "证券", "保险", "电力", "医药", "半导体")):
        return ["具体股票", "指数/板块", "普通概念解释"]
    return ["补充股票代码或唯一名称", "说明板块/指数", "说明普通概念"]


def _is_unresolved_reference_request(message: str, *, symbols: list[str], reference_context: Any | None) -> bool:
    if symbols or _has_reference_context(reference_context):
        return False
    text = str(message or "")
    has_reference = any(term in text for term in ("这个", "这只", "该股", "它", "它们", "这些", "上面", "上述", "刚才"))
    asks_analysis = any(term in text for term in ("看看", "分析", "怎么样", "怎么看", "如何", "走势", "机会", "风险"))
    return has_reference and asks_analysis


def _has_reference_context(reference_context: Any | None) -> bool:
    if reference_context is None:
        return False
    if getattr(reference_context, "symbols", ()) or getattr(reference_context, "data_name", "") or getattr(reference_context, "source", ""):
        return True
    return False


def _is_ambiguous_market_or_security_request(message: str, *, intent: PlannerV2Intent, symbols: list[str]) -> bool:
    if symbols or intent.needs_clarification:
        return False
    text = re.sub(r"\s+", "", str(message or ""))
    if not any(term in text for term in ("分析", "研究", "看看", "评价", "复盘", "预测")):
        return False
    if any(term in text for term in ("热点板块", "热门板块", "领涨行业", "AI板块", "概念股", "相关股票")):
        return False
    ambiguous_terms = ("银行", "证券", "保险", "电力", "医药", "半导体", "新能源", "军工", "消费")
    return any(term in text for term in ambiguous_terms) and intent.task in {"stock_analysis", "general_qa"}


def _needs_security_clarification(message: str, *, intent: str, symbols: list[str], candidate_names: list[str]) -> bool:
    if symbols:
        return False
    if intent not in {"stock_analysis", "trade", "chan_analysis"}:
        return False
    if any(term in message for term in ("筛选", "选股", "交易计划", "观察名单")):
        return False
    if any(term in message for term in ("均线", "指标", "解释", "是什么", "含义")) and not candidate_names:
        return False
    return bool(candidate_names or any(term in message for term in ("这只", "该股", "股票", "个股")))


def _needs_astock_catalog_first(intent: PlannerV2Intent, *, llm_factory: Callable[..., Any] | None) -> bool:
    if _explicit_dataset_id(intent.objective) and _needs_astock_catalog_text(intent.objective):
        return True
    if llm_factory is not None and llm_factory is not ChatLLM:
        return False
    return intent.intent == "data_lookup" or intent.task == "data_lookup" or _needs_astock_catalog_text(intent.objective)


def _needs_astock_catalog_text(message: str) -> bool:
    text = str(message or "")
    lowered = text.lower()
    if any(term in lowered for term in ("akshare", "tushare", "dataset", "data set")):
        return True
    return any(term in text for term in ("数据集", "数据接口", "白名单接口", "融资融券", "申万成分", "宏观数据", "CPI", "PMI"))


def _requested_provider(message: str) -> str:
    lowered = str(message or "").lower()
    if "akshare" in lowered or "ak share" in lowered:
        return "akshare"
    if "tushare" in lowered or "tu share" in lowered:
        return "tushare"
    if "tickflow" in lowered:
        return "tickflow"
    return "astock"


def _explicit_dataset_id(message: str) -> str:
    candidates = re.findall(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+){2,}\b", str(message or ""), flags=re.IGNORECASE)
    return candidates[0] if candidates else ""


def _catalog_query(message: str, *, dataset: str) -> str:
    if dataset:
        return dataset
    text = re.sub(r"\b(?:akshare|tushare|tickflow|dataset)\b", " ", str(message or ""), flags=re.IGNORECASE)
    for token in ("查询", "获取", "数据", "接口", "用", "看", "查", "的", "一下"):
        text = text.replace(token, " ")
    return re.sub(r"\s+", " ", text).strip() or str(message or "").strip()


def _reference_symbols(message: str, reference_context: Any | None) -> list[str]:
    if reference_context is None:
        return []
    if not any(term in str(message or "") for term in ("上面", "上述", "刚才", "这些", "它", "它们")):
        return []
    return normalize_symbols(list(getattr(reference_context, "symbols", ()) or ()), required=False)


def _safe_extract_trade_date(message: str) -> str:
    try:
        return extract_trade_date(message) or ""
    except ValueError:
        return ""


def _requested_limit(message: str) -> int | None:
    match = re.search(r"(?:top|前)\s*(\d{1,3})", str(message or ""), flags=re.IGNORECASE)
    if match:
        return max(1, min(200, int(match.group(1))))
    if "几只" in str(message or ""):
        return 5
    return None


def _constraints_from_text(message: str) -> list[str]:
    constraints: list[str] = []
    text = str(message or "")
    if "不联网" in text or "不要联网" in text:
        constraints.append("不使用 web 搜索。")
    if "只看" in text:
        constraints.append("优先遵守用户指定的分析范围。")
    if "实盘" in text:
        constraints.append("涉及实盘交易时必须经过交易权限和确认门控。")
    return constraints


def _output_preferences(message: str) -> list[str]:
    result: list[str] = []
    text = str(message or "")
    if "报告" in text:
        result.append("report")
    if "表格" in text:
        result.append("table")
    if any(term in text for term in ("保存", "导出")):
        result.append("artifact")
    return result


def _skill_hints(intent: PlannerV2Intent) -> list[str]:
    if intent.intent == "market_analysis" or intent.task in {"hot_sector_lookup", "market_review"}:
        return ["sats-market-assistant", "tickflow"]
    if intent.intent in {"stock_analysis", "chan_analysis"}:
        return ["tickflow", "technical-basic"]
    if intent.intent == "opportunity_discovery" or intent.task == "opportunity_discovery":
        return ["sats-market-assistant", "technical-basic", "risk-analysis"]
    return []


def _knowledge_hints(message: str) -> list[str]:
    text = str(message or "")
    hints: list[str] = []
    if any(term in text for term in ("缠论", "三买", "背驰", "中枢")):
        hints.append("chan")
    if any(term in text for term in ("知识库", "资料", "方法论")):
        hints.append("auto")
    return hints


def _is_stock_analysis_like(message: str) -> bool:
    text = str(message or "").lower()
    return any(
        term in text
        for term in (
            "分析",
            "评价",
            "研究",
            "走势",
            "技术面",
            "基本面",
            "财务",
            "估值",
            "公告",
            "新闻",
            "股价",
            "怎么看",
            "怎么样",
        )
    )


def _is_trade_text(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(term in str(message or "") for term in ("买入", "卖出", "下单", "委托")) or any(term in lowered for term in ("buy", "sell"))


def _looks_like_market_scope(value: str) -> bool:
    text = str(value or "")
    return any(term in text for term in ("大盘", "指数", "板块", "行业", "市场", "概念", "A股", "a股", "表现"))


def _is_replan_request(message: str) -> bool:
    return "SATS Agent error recovery replan" in message or "SATS Agent catalog replan" in message


def _llm_timeout_seconds(settings: Settings) -> int | None:
    try:
        value = int(getattr(settings, "llm_timeout_seconds", 0) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable):
        values = list(value)
    else:
        values = [value]
    return [str(item or "").strip() for item in values if str(item or "").strip()]


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


_GENERIC_STOCK_NAME_WORDS = {
    "股票",
    "个股",
    "公司",
    "市场",
    "大盘",
    "指数",
    "今天",
    "明天",
    "下周",
    "未来",
    "技术面",
    "基本面",
}
