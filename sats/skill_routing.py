from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sats.skills import Skill, find_skill, match_skills


DSA_TECHNICAL_SKILLS = (
    "bull-trend",
    "shrink-pullback",
    "ma-golden-cross",
    "volume-breakout",
)
DSA_EXTENDED_TECHNICAL_SKILLS = (
    *DSA_TECHNICAL_SKILLS,
    "box-oscillation",
    "bottom-volume",
    "one-yang-three-yin",
    "elliott-wave",
)
MARKET_SKILLS = (
    "sats-market-assistant",
    "sentiment-analysis",
    "sector-rotation",
    "market-microstructure",
    "emotion-cycle",
)
STOCK_SHORT_TERM_SKILLS = (
    "sats-market-assistant",
    "technical-basic",
    "risk-analysis",
    *DSA_TECHNICAL_SKILLS,
)
STOCK_FUNDAMENTAL_SKILLS = (
    "deep-stock-analysis",
    "tushare-data",
    "financial-statement",
    "valuation-model",
    "fundamental-filter",
    "growth-quality",
    "risk-analysis",
)
DISCOVERY_SKILLS = (
    "serenity-stock-screen",
    "sats-market-assistant",
    "technical-basic",
    "risk-analysis",
    "hot-theme",
    "sector-rotation",
)
FACTOR_SKILLS = ("quant-factor-screener",)


@dataclass(frozen=True, slots=True)
class SkillRouteContext:
    message: str
    intent: str = ""
    symbols: tuple[str, ...] = ()
    planned_tools: tuple[str, ...] = ()
    observed_tools: tuple[str, ...] = ()
    internal_analysis_kinds: tuple[str, ...] = ()
    explicit_skill_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillSelection:
    skill: Skill
    load_mode: str = "summary"
    reason: str = ""
    score: int = 0


@dataclass(frozen=True, slots=True)
class SkillRoutingResult:
    selections: tuple[SkillSelection, ...] = ()
    collections: tuple[str, ...] = ()
    suggested_internal_analysis: tuple[str, ...] = ()

    @property
    def skills(self) -> tuple[Skill, ...]:
        return tuple(selection.skill for selection in self.selections)

    @property
    def skill_ids(self) -> tuple[str, ...]:
        return tuple(selection.skill.id for selection in self.selections)


def select_skills(context: SkillRouteContext, skills: Iterable[Skill], *, limit: int = 10) -> SkillRoutingResult:
    available = list(skills)
    if not available:
        return SkillRoutingResult()
    text = str(context.message or "")
    intent = _normalized_intent(context, text)
    planned_tools = set(context.planned_tools)
    observed_tools = set(context.observed_tools)
    internal_kinds = set(context.internal_analysis_kinds)
    evidence = _evidence_terms(planned_tools, observed_tools, internal_kinds)
    scores: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    modes: dict[str, str] = {}

    def add(skill_id: str, score: int, reason: str, *, mode: str | None = None) -> None:
        skill = find_skill(available, skill_id)
        if skill is None:
            return
        scores[skill.id] = scores.get(skill.id, 0) + score + int(skill.priority or 0)
        reasons.setdefault(skill.id, []).append(reason)
        modes[skill.id] = _stronger_mode(modes.get(skill.id, "summary"), mode or skill.auto_load)

    for explicit in context.explicit_skill_names:
        add(explicit, 500, "用户显式指定 skill")
    for skill in available:
        if _skill_named_in_text(skill, text):
            add(skill.id, 450, "用户问题命中 skill 名称或别名")

    for skill in available:
        applies = set(skill.applies_to)
        if intent and intent in applies:
            add(skill.id, 90, f"skill applies_to 匹配 {intent}")
        if applies and _compatible_intent(intent, applies):
            add(skill.id, 50, f"skill applies_to 兼容 {intent}")
        skill_evidence = set(skill.evidence)
        if skill_evidence and skill_evidence & evidence:
            add(skill.id, 70 + 5 * len(skill_evidence & evidence), "skill evidence 匹配已规划/已观察工具")

    for matched in match_skills(text, available, limit=12):
        add(matched.id, 30, "沿用 trigger/description 匹配")

    suggested_internal_analysis: list[str] = []
    if intent == "market_analysis":
        for skill_id in MARKET_SKILLS:
            add(skill_id, 120, "大盘/指数分析默认方法论")
    elif intent == "opportunity_discovery":
        for skill_id in DISCOVERY_SKILLS:
            add(skill_id, 120, "选股/机会发现默认方法论")
        if _is_factor_request(text) or "factor.pick" in planned_tools | observed_tools:
            for skill_id in FACTOR_SKILLS:
                add(skill_id, 110, "因子选股请求")
    elif intent == "financial_analysis":
        for skill_id in STOCK_FUNDAMENTAL_SKILLS:
            add(skill_id, 120, "基本面/估值/财务分析默认方法论")
    elif intent == "stock_analysis":
        for skill_id in STOCK_SHORT_TERM_SKILLS:
            add(skill_id, 120, "个股走势/短线分析默认方法论")
        if _is_financial_request(text):
            for skill_id in STOCK_FUNDAMENTAL_SKILLS:
                add(skill_id, 110, "个股综合/基本面分析补充")
        if _is_dsa_request(text) or "native_dsa" in internal_kinds:
            for skill_id in DSA_EXTENDED_TECHNICAL_SKILLS:
                add(skill_id, 120, "DSA/买卖点分析默认策略视角")
            if any(term in text for term in ("热点题材", "题材发酵", "热点", "龙头", "情绪周期")):
                for skill_id in ("hot-theme", "dragon-head", "emotion-cycle"):
                    add(skill_id, 115, "DSA 热点/情绪策略视角")
            if any(term in text for term in ("事件驱动", "公告", "并购", "订单", "政策催化")):
                add("event-driven-detector", 115, "事件驱动策略视角")
            if any(term in text for term in ("成长品质", "成长质量", "利润质量", "ROE", "预期重估", "预期差", "估值修复")):
                for skill_id in ("growth-quality", "expectation-repricing"):
                    add(skill_id, 115, "成长品质/预期重估策略视角")
            suggested_internal_analysis.append("native_dsa")
    elif intent == "factor_analysis" or _is_factor_request(text):
        for skill_id in FACTOR_SKILLS:
            add(skill_id, 120, "因子分析默认方法论")
    elif intent == "command_help":
        add("sats-market-assistant", 120, "SATS 命令帮助")

    selections = _rank_selections(available, scores, reasons, modes, limit=limit)
    collections = _collections_for_skill_ids([selection.skill.id for selection in selections], intent=intent)
    return SkillRoutingResult(
        selections=tuple(selections),
        collections=collections,
        suggested_internal_analysis=tuple(_dedupe(suggested_internal_analysis)),
    )


def collections_for_skill_ids(skill_ids: Iterable[str], *, intent: str = "") -> tuple[str, ...]:
    return _collections_for_skill_ids(skill_ids, intent=intent)


def _normalized_intent(context: SkillRouteContext, text: str) -> str:
    explicit = str(context.intent or "").strip()
    tools = set(context.planned_tools) | set(context.observed_tools)
    if explicit in {"stock_picking_agent", "discovery"}:
        return "opportunity_discovery"
    if explicit:
        return explicit
    if "research.discover_opportunities" in tools:
        return "opportunity_discovery"
    if "research.serenity_screen" in tools:
        return "opportunity_discovery"
    if "research.market_context" in tools or _is_market_request(text):
        return "market_analysis"
    if "factor.pick" in tools or any(tool.startswith("factor.") for tool in tools) or _is_factor_request(text):
        return "factor_analysis"
    if "research.deep_stock_analysis" in tools:
        return "financial_analysis"
    if "research.stock_context" in tools or context.symbols or _is_stock_request(text):
        return "financial_analysis" if _is_financial_request(text) else "stock_analysis"
    if _is_command_help(text):
        return "command_help"
    return "general_qa"


def _evidence_terms(planned_tools: set[str], observed_tools: set[str], internal_kinds: set[str]) -> set[str]:
    terms = set(internal_kinds)
    for tool in planned_tools | observed_tools:
        terms.add(tool)
        if tool == "research.market_context":
            terms.update({"market_context", "market_breadth", "limit_sentiment", "hot_sectors"})
        elif tool == "research.stock_context":
            terms.add("stock_context")
        elif tool == "research.deep_stock_analysis":
            terms.update({"deep_stock_analysis", "stock_context", "indicators"})
        elif tool == "research.internal_analysis":
            terms.update({"indicators", "analyze_signals", "factor_summary"})
        elif tool == "research.discover_opportunities":
            terms.update({"opportunity_discovery", "analyze_signals", "hot_sectors"})
        elif tool == "research.serenity_screen":
            terms.update({"serenity_screen", "opportunity_discovery", "stock_context", "tushare_data"})
        elif tool.startswith("factor."):
            terms.add("factor")
    return terms


def _rank_selections(
    skills: list[Skill],
    scores: dict[str, int],
    reasons: dict[str, list[str]],
    modes: dict[str, str],
    *,
    limit: int,
) -> list[SkillSelection]:
    by_id = {skill.id: skill for skill in skills}
    rows = []
    for skill_id, score in scores.items():
        skill = by_id.get(skill_id)
        if skill is None or score <= 0 or skill.auto_load == "never":
            continue
        rows.append(
            SkillSelection(
                skill=skill,
                load_mode=_stronger_mode("summary", modes.get(skill_id, skill.auto_load)),
                reason="；".join(_dedupe(reasons.get(skill_id, ())))[:220],
                score=score,
            )
        )
    rows.sort(key=lambda item: (-item.score, -int(item.skill.priority or 0), item.skill.id))
    return rows[: max(1, int(limit))]


def _collections_for_skill_ids(skill_ids: Iterable[str], *, intent: str) -> tuple[str, ...]:
    skills = set(skill_ids)
    collections: list[str] = []
    dsa_market = {"dragon-head", "hot-theme", "emotion-cycle"}
    dsa_fundamental = {"expectation-repricing", "growth-quality"}
    if intent == "stock_analysis":
        collections.extend(["technical", "signals", "chan", "market", "sentiment", "fundamental", "risk"])
    if intent == "market_analysis":
        collections.extend(["market", "sentiment"])
    if intent == "opportunity_discovery":
        collections.extend(["market", "sentiment", "technical", "signals", "risk"])
    if "chan-theory" in skills:
        collections.append("chan")
    if "technical-basic" in skills or set(DSA_EXTENDED_TECHNICAL_SKILLS) & skills:
        collections.extend(["stock-basic", "technical", "signals"])
    if "sats-market-assistant" in skills or {"sector-rotation"} & skills or dsa_market & skills:
        collections.extend(["market", "sentiment"])
    if {"financial-statement", "valuation-model", "fundamental-filter", "quant-factor-screener"} & skills or dsa_fundamental & skills:
        collections.append("fundamental")
    if {"risk-analysis", "portfolio-health-check", "risk-adjusted-return-optimizer", "suitability-report-generator"} & skills:
        collections.append("risk")
    return tuple(_dedupe(collections))


def _skill_named_in_text(skill: Skill, text: str) -> bool:
    lowered = text.lower()
    candidates = (skill.id, skill.name, *skill.aliases)
    return any(len(str(candidate or "")) >= 2 and str(candidate).lower() in lowered for candidate in candidates)


def _compatible_intent(intent: str, applies: set[str]) -> bool:
    if intent == "stock_analysis":
        return bool(applies & {"financial_analysis", "opportunity_discovery"})
    if intent == "financial_analysis":
        return "stock_analysis" in applies
    if intent == "opportunity_discovery":
        return "stock_analysis" in applies
    return False


def _stronger_mode(current: str, candidate: str | None) -> str:
    order = {"never": 0, "summary": 1, "full": 2}
    current_mode = current if current in order else "summary"
    candidate_mode = candidate if candidate in order else "summary"
    return candidate_mode if order[candidate_mode] > order[current_mode] else current_mode


def _is_market_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered or term in text for term in ("大盘", "指数", "市场", "上证", "沪指", "深成指", "创业板", "沪深300", "a股"))


def _is_stock_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("analyze", "analysis", "signal", "dsa", "das")) or any(
        term in text
        for term in (
            "个股",
            "股票",
            "分析",
            "走势",
            "技术面",
            "买卖点",
            "DSA",
            "DAS",
            "缠论",
            "波浪理论",
            "热点题材",
            "事件驱动",
            "成长品质",
            "预期重估",
        )
    )


def _is_financial_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("pe", "pb", "roe")) or any(term in text for term in ("财报", "财务", "估值", "基本面", "利润", "现金流", "负债", "盈利"))


def _is_dsa_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("dsa", "das", "daily_stock_analysis", "elliott", "wave")) or any(
        term in text
        for term in (
            "买卖点",
            "交易策略",
            "多头趋势",
            "回踩低吸",
            "放量突破",
            "均线金叉",
            "缠论",
            "波浪理论",
            "热点题材",
            "题材发酵",
            "事件驱动",
            "成长品质",
            "成长质量",
            "预期重估",
            "预期差",
        )
    )


def _is_factor_request(text: str) -> bool:
    lowered = text.lower()
    return "因子" in text or "factor" in lowered or "alpha101" in lowered or "gtja" in lowered or "barra" in lowered


def _is_command_help(text: str) -> bool:
    lowered = text.lower()
    return "sats" in lowered and any(term in lowered for term in ("命令", "用法", "怎么用", "参数", "cli"))


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
