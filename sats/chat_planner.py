from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sats.analysis.market_llm_context import is_market_question
from sats.analysis.opportunity_discovery import is_opportunity_discovery_question
from sats.screening.rule_composer import is_rule_generation_request
from sats.skills import Skill, find_skill, match_skills
from sats.stock_question import StockQuestion


@dataclass(frozen=True, slots=True)
class ChatPlan:
    intent: str
    skills: tuple[str, ...] = ()
    data_requirements: tuple[str, ...] = ()
    internal_actions: tuple[str, ...] = ()
    external_actions: tuple[str, ...] = ()
    risk_level: str = "low"
    reason: str = ""

    @property
    def needs_stock_context(self) -> bool:
        return "stock_context" in self.data_requirements

    @property
    def needs_market_context(self) -> bool:
        return "market_context" in self.data_requirements

    @property
    def needs_opportunity_discovery(self) -> bool:
        return "opportunity_discovery" in self.internal_actions

    @property
    def needs_chan_context(self) -> bool:
        return "chan_context" in self.data_requirements

    def system_message(self) -> str:
        return "\n".join(
            [
                "SATS chat_plan:",
                f"- intent: {self.intent}",
                f"- skills: {', '.join(self.skills) if self.skills else 'none'}",
                f"- data_requirements: {', '.join(self.data_requirements) if self.data_requirements else 'none'}",
                f"- internal_actions: {', '.join(self.internal_actions) if self.internal_actions else 'none'}",
                f"- external_actions: {', '.join(self.external_actions) if self.external_actions else 'none'}",
                f"- risk_level: {self.risk_level}",
                f"- reason: {self.reason}",
            ]
        )


def build_chat_plan(
    message: str,
    *,
    skills: list[Skill],
    stock_question: StockQuestion | None = None,
    preprocess: Any | None = None,
) -> ChatPlan:
    text = str(message or "").strip()
    lowered = text.lower()
    matched = match_skills(text, skills)
    skill_ids = _skill_ids(matched)
    requirements: list[str] = []
    actions: list[str] = []
    external_actions: list[str] = []
    reasons: list[str] = []
    intent = "general_qa"
    risk_level = "low"

    rule_generation = is_rule_generation_request(text)
    if rule_generation:
        intent = "screening_rule_generation"
        skill_ids.append("sats-market-assistant")
        reasons.append("检测到自然语言创建筛选规则请求，需要先生成规则计划并等待用户二次确认。")
        risk_level = "medium"

    quote_only = bool(
        preprocess is not None
        and getattr(preprocess, "needs_realtime_quote_context", False)
        and not getattr(preprocess, "needs_stock_context", False)
    )
    has_stock = bool(stock_question and stock_question.has_stock_question)
    if has_stock and not rule_generation:
        intent = "stock_quote" if quote_only else "stock_analysis"
        requirements.extend(["quote_context"] if quote_only else ["stock_context", "market_context"])
        skill_ids.extend(["tickflow"] if quote_only else ["tickflow", "technical-basic"])
        reasons.append("检测到 A 股股票代码，需要先获取真实实时报价。" if quote_only else "检测到 A 股股票代码，需要先获取真实个股行情和大盘背景。")
        risk_level = "medium"

    if is_market_question(text) and not rule_generation:
        intent = "market_analysis" if not has_stock else intent
        requirements.append("market_context")
        skill_ids.extend(["sats-market-assistant", "tickflow", "market-microstructure"])
        reasons.append("检测到 A 股大盘/指数/走势问题，需要注入真实大盘上下文。")
        risk_level = "medium"

    if is_opportunity_discovery_question(text) and not has_stock and not rule_generation:
        intent = "opportunity_discovery"
        requirements.append("market_context")
        actions.append("opportunity_discovery")
        skill_ids.extend(["sats-market-assistant", "technical-basic", "risk-analysis"])
        reasons.append("检测到自然语言选股问题，需要先运行短线机会发现。")
        risk_level = "medium"

    if _is_chan_question(text):
        intent = "chan_analysis" if not has_stock else intent
        requirements.append("chan_context")
        skill_ids.append("chan-theory")
        reasons.append("检测到缠论关键词，需要加载本地缠论 skill/RAG。")
        risk_level = "medium"

    if _is_financial_question(text):
        intent = "financial_analysis" if intent == "general_qa" else intent
        skill_ids.extend(["tushare-data", "financial-statement", "valuation-model", "risk-analysis"])
        if has_stock:
            requirements.append("stock_context")
        reasons.append("检测到财务/估值/资金流问题，需要优先使用 Tushare 相关上下文。")
        risk_level = "medium"

    if _is_command_help(text):
        intent = "command_help"
        skill_ids.append("sats-market-assistant")
        reasons.append("检测到 SATS 命令/用法问题，只需要命令和 skill 上下文。")

    if _mentions_external_analysis(text):
        external_actions.append("daily_stock_analysis_bridge")
        reasons.append("外部 daily_stock_analysis 只能通过 SATS 已封装桥接能力使用，不开放任意 shell。")

    if preprocess is not None:
        pre_intent = str(getattr(preprocess, "intent", "") or "").strip()
        if pre_intent and pre_intent != "general_qa" and intent == "general_qa":
            intent = pre_intent
        if bool(getattr(preprocess, "needs_stock_context", False)):
            requirements.extend(["stock_context", "market_context"])
            skill_ids.extend(["tickflow", "technical-basic"])
            reasons.append("LLM 预处理识别到个股研究需求，已转为本地真实个股/大盘数据准备。")
            risk_level = "medium"
        if bool(getattr(preprocess, "needs_market_context", False)):
            requirements.append("market_context")
            skill_ids.extend(["sats-market-assistant", "tickflow"])
            reasons.append("LLM 预处理识别到大盘/市场背景需求。")
            risk_level = "medium"
        if bool(getattr(preprocess, "needs_opportunity_discovery", False)) and not has_stock:
            intent = "opportunity_discovery"
            requirements.append("market_context")
            actions.append("opportunity_discovery")
            skill_ids.extend(["sats-market-assistant", "technical-basic", "risk-analysis"])
            reasons.append("LLM 预处理识别到自然语言选股/推荐需求。")
            risk_level = "medium"
        if bool(getattr(preprocess, "needs_indicators", False)) and (has_stock or bool(getattr(preprocess, "symbols", ()))):
            requirements.append("stock_context")
            skill_ids.append("technical-basic")
            reasons.append("LLM 预处理识别到指标计算需求，指标由个股上下文预处理完成。")
            risk_level = "medium"
        skill_ids.extend(str(item) for item in getattr(preprocess, "skill_hints", ()) or ())

    skill_ids = _existing_skill_ids(skill_ids, skills)
    requirements = _dedupe(requirements)
    actions = _dedupe(actions)
    external_actions = _dedupe(external_actions)
    if not reasons:
        reasons.append("未检测到需要真实行情或内部分析的关键词，按普通问答处理。")
    return ChatPlan(
        intent=intent,
        skills=tuple(skill_ids),
        data_requirements=tuple(requirements),
        internal_actions=tuple(actions),
        external_actions=tuple(external_actions),
        risk_level=risk_level,
        reason=" ".join(reasons),
    )


def skills_for_plan(plan: ChatPlan, skills: list[Skill], matched: Iterable[Skill] = ()) -> list[Skill]:
    result: list[Skill] = []
    seen: set[str] = set()
    for skill in matched:
        if skill.id not in seen:
            result.append(skill)
            seen.add(skill.id)
    for skill_id in plan.skills:
        skill = find_skill(skills, skill_id)
        if skill is not None and skill.id not in seen:
            result.append(skill)
            seen.add(skill.id)
    return result[:5]


def _is_chan_question(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("缠论", "三买", "二买", "一买", "三卖", "背驰", "中枢", "chan"))


def _is_financial_question(text: str) -> bool:
    lowered = text.lower()
    return any(
        term in lowered
        for term in (
            "财报",
            "财务",
            "估值",
            "pe",
            "pb",
            "roe",
            "资金流",
            "基本面",
            "利润",
            "现金流",
            "资产负债",
        )
    )


def _is_command_help(text: str) -> bool:
    lowered = text.lower()
    return "sats" in lowered and any(term in lowered for term in ("命令", "用法", "怎么用", "参数", "cli"))


def _mentions_external_analysis(text: str) -> bool:
    lowered = text.lower()
    return "daily_stock_analysis" in lowered or "analyze-dsa" in lowered


def _skill_ids(skills: Iterable[Skill]) -> list[str]:
    return [skill.id for skill in skills]


def _existing_skill_ids(skill_ids: Iterable[str], skills: list[Skill]) -> list[str]:
    available = {skill.id for skill in skills}
    return [skill_id for skill_id in _dedupe(skill_ids) if skill_id in available]


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
