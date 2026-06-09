from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from sats.analysis.market_llm_context import (
    DEFAULT_MARKET_DIMENSIONS,
    extract_explicit_market_indices,
    extract_market_horizons,
    is_market_question,
    resolve_market_dimensions,
    resolve_market_horizons,
    resolve_market_indices,
)
from sats.analysis.opportunity_discovery import (
    extract_opportunity_discovery_limit,
    is_generic_hot_sector_discovery_question,
    is_opportunity_discovery_question,
)
from sats.chat_reference import ChatReferenceContext, is_reference_question
from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.llm import ChatLLM, build_light_fallback_llm, extract_json_object
from sats.skill_routing import DISCOVERY_SKILLS, MARKET_SKILLS, STOCK_FUNDAMENTAL_SKILLS, STOCK_SHORT_TERM_SKILLS
from sats.stock_basic_lookup import (
    load_stock_basic_frame,
    names_from_stock_basic,
    resolve_stock_names,
)
from sats.stock_question import extract_intraday_time, extract_stock_symbols, extract_trade_date
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols


@dataclass(frozen=True, slots=True)
class ChatPreprocessResult:
    intent: str = "general_qa"
    symbols: tuple[str, ...] = ()
    stock_names: tuple[str, ...] = ()
    trade_date: str | None = None
    as_of_time: str | None = None
    reference_needed: bool = False
    needs_stock_context: bool = False
    needs_market_context: bool = False
    needs_opportunity_discovery: bool = False
    needs_indicators: bool = False
    needs_realtime_quote_context: bool = False
    market_indices: tuple[str, ...] = ()
    market_dimensions: tuple[str, ...] = ()
    market_horizons: tuple[str, ...] = ()
    requested_limit: int | None = None
    market_plan_source: str = ""
    skill_hints: tuple[str, ...] = ()
    confidence: float = 0.0
    missing_questions: tuple[str, ...] = ()
    source: str = "local"

    def system_message(self) -> str:
        return "\n".join(
            [
                "SATS chat_preprocess:",
                f"- source: {self.source}",
                f"- intent: {self.intent}",
                f"- symbols: {', '.join(self.symbols) if self.symbols else 'none'}",
                f"- stock_names: {', '.join(self.stock_names) if self.stock_names else 'none'}",
                f"- trade_date: {self.trade_date or 'none'}",
                f"- as_of_time: {self.as_of_time or 'none'}",
                f"- reference_needed: {self.reference_needed}",
                (
                    "- data_flags: "
                    f"stock={self.needs_stock_context}, market={self.needs_market_context}, "
                    f"opportunity={self.needs_opportunity_discovery}, indicators={self.needs_indicators}, "
                    f"quote={self.needs_realtime_quote_context}"
                ),
                f"- market_indices: {', '.join(self.market_indices) if self.market_indices else 'none'}",
                f"- market_dimensions: {', '.join(self.market_dimensions) if self.market_dimensions else 'none'}",
                f"- market_horizons: {', '.join(self.market_horizons) if self.market_horizons else 'none'}",
                f"- requested_limit: {self.requested_limit if self.requested_limit is not None else 'none'}",
                f"- market_plan_source: {self.market_plan_source or 'none'}",
                f"- skill_hints: {', '.join(self.skill_hints) if self.skill_hints else 'none'}",
                f"- confidence: {self.confidence:.2f}",
                "- policy: LLM 预处理只用于理解意图和抽取候选实体；股票代码、名称和数据需求已由 SATS 本地校验。",
            ]
        )


def preprocess_chat_message(
    message: str,
    *,
    settings: Settings,
    reference_context: ChatReferenceContext | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
    llm_enabled: bool = True,
    timeout_seconds: int | None = None,
    storage_factory: Callable[[Path], DuckDBStorage] = DuckDBStorage,
    provider_factory: Callable[[Settings], AStockDataProvider] = AStockDataProvider,
) -> ChatPreprocessResult:
    text = str(message or "").strip()
    llm_payload = _run_llm_preprocess(
        text,
        settings=settings,
        reference_context=reference_context,
        llm_factory=llm_factory,
        llm_enabled=llm_enabled,
        timeout_seconds=timeout_seconds if timeout_seconds is not None else _llm_timeout_seconds(settings),
    )
    local = _local_preprocess(text, reference_context=reference_context)
    result = _merge_llm_and_local(text, llm_payload, local, reference_context=reference_context)
    return _resolve_and_validate_entities(
        text,
        result,
        settings=settings,
        reference_context=reference_context,
        storage_factory=storage_factory,
        provider_factory=provider_factory,
    )


def _run_llm_preprocess(
    message: str,
    *,
    settings: Settings,
    reference_context: ChatReferenceContext | None,
    llm_factory: Callable[..., Any] | None,
    llm_enabled: bool,
    timeout_seconds: int | None,
) -> dict[str, Any]:
    if not llm_enabled or llm_factory is None:
        return {}
    try:
        llm = build_light_fallback_llm(
            llm_factory,
            light_model_name=_light_model_name(settings),
            default_model_name=_main_model_name(settings),
            timeout_seconds=timeout_seconds,
        )
        payload = llm.chat_validated(
            _preprocess_messages(message, reference_context=reference_context),
            _parse_required_json_response,
            timeout=timeout_seconds,
        )
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _preprocess_messages(message: str, *, reference_context: ChatReferenceContext | None = None) -> list[dict[str, str]]:
    reference_summary = _reference_context_summary(reference_context)
    return [
        {
            "role": "system",
            "content": (
                "你是 SATS 聊天任务预处理器，只能输出一个 JSON 对象，不能输出分析结论。"
                "任务是从用户自然语言中抽取意图、A股代码/名称、日期、是否引用上文、需要的数据和 skill hints。"
                "如果不确定，字段留空或降低 confidence，不要猜股票代码。"
                "当用户提到上面、上述、刚才、上一个、这些股票，并且引用上下文中已有 symbols 时，"
                "必须把这些 symbols 当作本轮股票输入，不得要求用户再次提供股票代码或名称。"
                "当用户询问大盘、指数或市场走势时，不要要求用户先确认单一指数或具体绝对日期；"
                "应从 A 股核心指数池中给出 market_indices，并给出 market_dimensions 与 market_horizons。"
                "大盘、市场、指数、沪指、创业板指、沪深300 等市场范围词不得放入 stock_names。"
                "例如“分析今天的大盘走势，预测明天走势”应输出 intent=market_analysis, "
                "stock_names=[], needs_market_context=true, market_horizons=[\"today\",\"tomorrow\"]。"
                "当用户在选股请求中提到热点板块、强势板块、市场主线或上涨较好的行业概念时，"
                "应理解为让 SATS 自动获取真实热点板块上下文，不得要求用户指定单一板块。"
                "市场和选股问题的 missing_questions 默认留空，除非存在本地无法唯一确认的股票名称歧义。"
            ),
        },
        {
            "role": "user",
            "content": (
                "按以下 JSON 字段输出：intent, symbols, stock_names, trade_date, as_of_time, "
                "reference_needed, needs_stock_context, needs_market_context, needs_opportunity_discovery, "
                "needs_indicators, needs_realtime_quote_context, market_indices, market_dimensions, market_horizons, "
                "requested_limit, skill_hints, confidence, missing_questions。\n"
                f"{reference_summary}"
                f"用户输入：{message}"
            ),
        },
    ]


def _reference_context_summary(reference_context: ChatReferenceContext | None) -> str:
    if reference_context is None or not reference_context.symbols:
        return ""
    return (
        "可用上文引用上下文："
        f"source={reference_context.source}; "
        f"data_name={reference_context.data_name}; "
        f"trade_date={reference_context.trade_date or 'none'}; "
        f"symbols={', '.join(reference_context.symbols)}。\n"
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


def _parse_required_json_response(response: Any) -> dict[str, Any]:
    payload = extract_json_object(str(getattr(response, "content", "") or ""))
    if not isinstance(payload, dict):
        raise ValueError("LLM response did not contain a JSON object")
    return payload


def _local_preprocess(message: str, *, reference_context: ChatReferenceContext | None) -> ChatPreprocessResult:
    explicit_symbols = extract_stock_symbols(message)
    reference_needed = is_reference_question(message)
    has_reference_symbols = bool(reference_needed and reference_context is not None and reference_context.symbols)
    has_stock = bool(explicit_symbols or has_reference_symbols)
    market = is_market_question(message)
    opportunity = is_opportunity_discovery_question(message)
    chan = _is_chan_question(message)
    financial = _is_financial_question(message)
    indicators = _is_indicator_question(message)
    quote = _is_quote_question(message)
    quote_only = quote and not _needs_full_stock_context(message)
    needs_stock_context = bool(has_stock and not quote_only)
    needs_market_context = bool(market or opportunity or needs_stock_context)
    market_indices = extract_explicit_market_indices(message) if needs_market_context else ()
    market_horizons = extract_market_horizons(message) if needs_market_context else ()
    market_dimensions = DEFAULT_MARKET_DIMENSIONS if needs_market_context else ()
    requested_limit = extract_opportunity_discovery_limit(message) if opportunity else None
    intent = "general_qa"
    if opportunity and not has_stock:
        intent = "opportunity_discovery"
    elif has_stock:
        intent = "stock_analysis"
    elif market:
        intent = "market_analysis"
    elif chan:
        intent = "chan_analysis"
    elif financial:
        intent = "financial_analysis"
    skill_hints = _skill_hints(
        has_stock=has_stock,
        market=market,
        opportunity=opportunity,
        chan=chan,
        financial=financial,
    )
    return ChatPreprocessResult(
        intent=intent,
        symbols=tuple(explicit_symbols),
        trade_date=_safe_extract_trade_date(message),
        as_of_time=extract_intraday_time(message),
        reference_needed=reference_needed,
        needs_stock_context=needs_stock_context,
        needs_market_context=needs_market_context,
        needs_opportunity_discovery=bool(opportunity and not has_stock),
        needs_indicators=bool(indicators or (needs_stock_context and not financial)),
        needs_realtime_quote_context=bool(quote and has_stock),
        market_indices=tuple(market_indices),
        market_dimensions=tuple(market_dimensions),
        market_horizons=tuple(market_horizons),
        requested_limit=requested_limit,
        market_plan_source="local_default" if needs_market_context else "",
        skill_hints=tuple(skill_hints),
        confidence=0.75 if intent != "general_qa" else 0.45,
        source="local",
    )


def _merge_llm_and_local(
    message: str,
    payload: dict[str, Any],
    local: ChatPreprocessResult,
    *,
    reference_context: ChatReferenceContext | None,
) -> ChatPreprocessResult:
    llm_intent = _clean_string(payload.get("intent"))
    intent = local.intent if local.intent != "general_qa" else llm_intent or local.intent
    market = local.needs_market_context or _bool(payload.get("needs_market_context"))
    opportunity = local.needs_opportunity_discovery or _bool(payload.get("needs_opportunity_discovery"))
    stock_names = _filter_market_scope_stock_names(
        message,
        _filter_opportunity_stock_names(
            message,
            _dedupe(
                [
                    *_string_list(payload.get("stock_names")),
                    *([] if _looks_like_symbol_followup(message) else _names_from_message(message)),
                ]
            ),
            opportunity=opportunity,
        ),
        market=market,
    )
    llm_symbols = normalize_symbols(_string_list(payload.get("symbols")), required=False)
    reference_needed = local.reference_needed or _bool(payload.get("reference_needed"))
    reference_symbols = (
        list(reference_context.symbols)
        if reference_needed and reference_context is not None and reference_context.symbols
        else []
    )
    symbols = _dedupe([*local.symbols, *reference_symbols, *llm_symbols])
    has_stock = bool(symbols or stock_names)
    indicators = local.needs_indicators or _bool(payload.get("needs_indicators")) or _is_indicator_question(message)
    quote = local.needs_realtime_quote_context or _bool(payload.get("needs_realtime_quote_context")) or _is_quote_question(message)
    quote_only = bool(quote and not _needs_full_stock_context(message))
    skill_hints = _dedupe([*local.skill_hints, *_string_list(payload.get("skill_hints"))])
    raw_market_indices = _string_list(payload.get("market_indices"))
    raw_market_dimensions = _string_list(payload.get("market_dimensions"))
    raw_market_horizons = _string_list(payload.get("market_horizons"))
    requested_limit = local.requested_limit or _positive_int(payload.get("requested_limit"))
    llm_market_indices = resolve_market_indices(raw_market_indices)
    llm_market_dimensions = tuple(resolve_market_dimensions(raw_market_dimensions)) if raw_market_dimensions else ()
    llm_market_horizons = tuple(resolve_market_horizons(raw_market_horizons)) if raw_market_horizons else ()
    market_indices = tuple(_dedupe([*local.market_indices, *llm_market_indices]))
    market_dimensions = tuple(llm_market_dimensions) if llm_market_dimensions else local.market_dimensions
    market_horizons = tuple(local.market_horizons or llm_market_horizons)
    missing = _sanitize_missing_questions(
        _string_list(payload.get("missing_questions")),
        message=message,
        intent=intent or local.intent,
        has_stock=has_stock,
        has_symbols=bool(symbols),
        needs_market_context=bool(market or symbols or opportunity),
    )
    confidence = _confidence(payload.get("confidence"), default=local.confidence)
    source = "llm+local" if payload else "local"
    if has_stock and intent in {"", "general_qa"}:
        intent = "stock_analysis"
    return ChatPreprocessResult(
        intent=intent or "general_qa",
        symbols=tuple(symbols),
        stock_names=tuple(stock_names),
        trade_date=_safe_extract_trade_date(message) or _clean_string(payload.get("trade_date")) or local.trade_date,
        as_of_time=extract_intraday_time(message) or _clean_string(payload.get("as_of_time")) or local.as_of_time,
        reference_needed=reference_needed,
        needs_stock_context=bool((local.needs_stock_context or _bool(payload.get("needs_stock_context")) or symbols) and not quote_only),
        needs_market_context=bool(market or (symbols and not quote_only) or opportunity),
        needs_opportunity_discovery=bool(opportunity and not symbols),
        needs_indicators=bool(indicators and (has_stock or local.needs_stock_context)),
        needs_realtime_quote_context=bool(quote and symbols),
        market_indices=market_indices,
        market_dimensions=market_dimensions,
        market_horizons=market_horizons,
        requested_limit=requested_limit,
        market_plan_source=_market_plan_source(local, payload, market_indices),
        skill_hints=tuple(skill_hints),
        confidence=confidence,
        missing_questions=tuple(missing),
        source=source,
    )


def _resolve_and_validate_entities(
    message: str,
    result: ChatPreprocessResult,
    *,
    settings: Settings,
    reference_context: ChatReferenceContext | None,
    storage_factory: Callable[[Path], DuckDBStorage],
    provider_factory: Callable[[Settings], AStockDataProvider],
) -> ChatPreprocessResult:
    stock_basic = pd.DataFrame()
    names = list(result.stock_names)
    if _should_scan_stock_names(message, result):
        stock_basic = _load_stock_basic(settings, storage_factory=storage_factory, provider_factory=provider_factory)
        names = _dedupe([*names, *_names_from_stock_basic(message, stock_basic)])
    elif names:
        stock_basic = _load_stock_basic(settings, storage_factory=storage_factory, provider_factory=provider_factory)

    resolved_symbols: list[str] = []
    questions = list(result.missing_questions)
    if names:
        resolved_symbols, name_questions = _resolve_stock_names(names, stock_basic)
        questions.extend(name_questions)

    reference_symbols = (
        list(reference_context.symbols)
        if result.reference_needed and reference_context is not None and reference_context.symbols
        else []
    )
    symbols = _dedupe([*result.symbols, *reference_symbols, *resolved_symbols])
    has_stock = bool(symbols)
    if has_stock:
        questions = _filter_missing_stock_identifier_questions(questions)
    quote_only = bool(result.needs_realtime_quote_context and not _needs_full_stock_context(message))
    skill_hints = list(result.skill_hints)
    if has_stock:
        skill_hints.extend(["tickflow", "technical-basic"])
    if result.needs_indicators and has_stock:
        skill_hints.append("technical-basic")
    if result.needs_opportunity_discovery:
        skill_hints.extend(["sats-market-assistant", "technical-basic", "risk-analysis"])
    if result.needs_market_context or has_stock:
        skill_hints.extend(["sats-market-assistant", "tickflow"])
    return ChatPreprocessResult(
        intent="stock_analysis" if has_stock and result.intent == "general_qa" else result.intent,
        symbols=tuple(symbols),
        stock_names=tuple(names),
        trade_date=result.trade_date,
        as_of_time=result.as_of_time,
        reference_needed=result.reference_needed,
        needs_stock_context=bool((result.needs_stock_context or has_stock) and not quote_only),
        needs_market_context=bool(result.needs_market_context or (has_stock and not quote_only)),
        needs_opportunity_discovery=bool(result.needs_opportunity_discovery and not has_stock),
        needs_indicators=bool(result.needs_indicators and has_stock and not quote_only),
        needs_realtime_quote_context=bool(result.needs_realtime_quote_context and has_stock),
        market_indices=result.market_indices,
        market_dimensions=result.market_dimensions,
        market_horizons=result.market_horizons,
        requested_limit=result.requested_limit,
        market_plan_source=result.market_plan_source,
        skill_hints=tuple(_dedupe(skill_hints)),
        confidence=result.confidence,
        missing_questions=tuple(_dedupe(questions)),
        source=result.source,
    )


def _load_stock_basic(
    settings: Settings,
    *,
    storage_factory: Callable[[Path], DuckDBStorage],
    provider_factory: Callable[[Settings], AStockDataProvider],
) -> pd.DataFrame:
    return load_stock_basic_frame(
        settings,
        storage_factory=storage_factory,
        provider_factory=provider_factory,
    )


def _resolve_stock_names(names: Iterable[str], stock_basic: pd.DataFrame) -> tuple[list[str], list[str]]:
    resolution = resolve_stock_names(names, stock_basic)
    return list(resolution.symbols), list(resolution.questions)


def _match_stock_name(name: str, stock_basic: pd.DataFrame) -> pd.DataFrame:
    from sats.stock_basic_lookup import match_stock_name

    return match_stock_name(name, stock_basic)


def _should_scan_stock_names(message: str, result: ChatPreprocessResult) -> bool:
    if result.stock_names:
        return True
    if _looks_like_symbol_followup(message):
        return False
    if result.reference_needed or result.needs_opportunity_discovery:
        return False
    if result.symbols:
        return _is_stock_analysis_like(message) and _has_possible_named_stock(message)
    return _is_stock_analysis_like(message)


def _names_from_stock_basic(message: str, stock_basic: pd.DataFrame) -> list[str]:
    return names_from_stock_basic(message, stock_basic)


def _names_from_message(message: str) -> list[str]:
    text = str(message or "")
    names: list[str] = []
    for pattern in (
        r"分析([\u4e00-\u9fffA-Za-z]{2,12}?)(?=技术面|基本面|财务|估值|走势|缠论|指标|$)",
        r"看看([\u4e00-\u9fffA-Za-z]{2,12}?)(?=技术面|基本面|财务|估值|走势|缠论|指标|$)",
    ):
        for match in re.finditer(pattern, text):
            candidate = match.group(1).strip()
            if candidate and candidate not in _GENERIC_NAME_WORDS:
                names.append(candidate)
    return _dedupe(names)


def _skill_hints(
    *,
    has_stock: bool,
    market: bool,
    opportunity: bool,
    chan: bool,
    financial: bool,
) -> list[str]:
    hints: list[str] = []
    if has_stock:
        hints.extend(["tickflow", *STOCK_SHORT_TERM_SKILLS])
    if market:
        hints.extend(["tickflow", *MARKET_SKILLS])
    if opportunity:
        hints.extend(DISCOVERY_SKILLS)
    if chan:
        hints.append("chan-theory")
    if financial:
        hints.extend(STOCK_FUNDAMENTAL_SKILLS)
    return _dedupe(hints)


def _is_stock_analysis_like(text: str) -> bool:
    lowered = str(text or "").lower()
    if is_market_question(text) or is_opportunity_discovery_question(text):
        return False
    return any(
        term in lowered
        for term in (
            "分析",
            "看看",
            "技术面",
            "基本面",
            "走势",
            "财务",
            "估值",
            "缠论",
            "指标",
            "均线",
            "macd",
            "kdj",
            "rsi",
            "买入",
            "卖出",
            "怎么看",
            "怎么样",
        )
    )


def _looks_like_symbol_followup(text: str) -> bool:
    value = str(text or "")
    return any(term in value for term in ("继续", "它", "它们", "这只", "这些", "上面", "刚才"))


def _has_possible_named_stock(text: str) -> bool:
    value = re.sub(r"[034689]\d{5}(?:\.(?:SH|SZ|BJ))?", "", str(text or ""), flags=re.IGNORECASE)
    for word in (
        "分析",
        "看看",
        "和",
        "与",
        "以及",
        "技术面",
        "基本面",
        "财务",
        "估值",
        "走势",
        "缠论",
        "指标",
        "买入",
        "卖出",
        "怎么看",
        "怎么样",
    ):
        value = value.replace(word, "")
    return bool(re.search(r"[\u4e00-\u9fff]{2,}", value))


def _is_indicator_question(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in ("指标", "均线", "ma", "macd", "kdj", "rsi", "boll", "量能", "换手"))


def _is_quote_question(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in ("实时报价", "报价", "当前价格", "实时价格", "最新价", "涨跌幅", "最新价格"))


def _needs_full_stock_context(text: str) -> bool:
    return _is_stock_analysis_like(text) or _is_indicator_question(text) or _is_chan_question(text) or _is_financial_question(text)


def _is_chan_question(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in ("缠论", "三买", "二买", "一买", "三卖", "背驰", "中枢", "chan"))


def _is_financial_question(text: str) -> bool:
    lowered = str(text or "").lower()
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


def _safe_extract_trade_date(text: str) -> str | None:
    try:
        return extract_trade_date(text)
    except ValueError:
        return None


def _sanitize_missing_questions(
    questions: Iterable[str],
    *,
    message: str,
    intent: str,
    has_stock: bool,
    has_symbols: bool = False,
    needs_market_context: bool,
) -> list[str]:
    result = _dedupe([str(item or "").strip() for item in questions if str(item or "").strip()])
    if has_symbols:
        result = _filter_missing_stock_identifier_questions(result)
    if intent == "market_analysis" and needs_market_context and not has_stock:
        return []
    if intent == "opportunity_discovery" and needs_market_context and not has_stock:
        return [
            question
            for question in result
            if not _is_missing_stock_identifier_question(question)
            and not _is_missing_market_scope_question(question)
        ]
    if is_generic_hot_sector_discovery_question(message):
        result = [question for question in result if not _is_missing_market_scope_question(question)]
    return result


def _filter_opportunity_stock_names(message: str, names: Iterable[str], *, opportunity: bool) -> list[str]:
    result = _dedupe([str(item or "").strip() for item in names if str(item or "").strip()])
    if not opportunity or not is_generic_hot_sector_discovery_question(message):
        return result
    return [name for name in result if not _is_generic_market_scope_value(name)]


def _filter_market_scope_stock_names(message: str, names: Iterable[str], *, market: bool) -> list[str]:
    result = _dedupe([str(item or "").strip() for item in names if str(item or "").strip()])
    if not market:
        return result
    return [name for name in result if not _is_market_scope_stock_name(name)]


def _is_market_scope_stock_name(value: str) -> bool:
    text = re.sub(r"\s+", "", str(value or "").strip().lower())
    text = re.sub(r"^(分析|看看|预测|判断|研究|复盘)+", "", text)
    text = re.sub(r"(走势|行情|表现|涨跌|怎么走|怎么看|如何|分析|预测)+$", "", text)
    text = text.strip("，。；;、:：")
    if not text:
        return False
    for prefix in ("今天", "今日", "当前", "现在", "盘中", "盘后", "明天", "次日", "后天", "明后", "明后天", "下周", "未来"):
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix) :]
            break
    text = re.sub(r"^(的|之)", "", text)
    exact = {
        "a股",
        "a股大盘",
        "大盘",
        "市场",
        "a股市场",
        "整体市场",
        "全市场",
        "指数",
        "大盘指数",
        "核心指数",
        "上证",
        "上证指数",
        "沪指",
        "深成指",
        "深证成指",
        "创业板",
        "创业板指",
        "深证100",
        "深100",
        "沪深300",
        "中证500",
        "科创50",
        "北证50",
    }
    return text in exact


def _is_generic_market_scope_value(value: str) -> bool:
    text = re.sub(r"\s+", "", str(value or "").strip().lower())
    if not text:
        return False
    exact = {
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
        "主线",
        "主线板块",
        "强势板块",
        "强势行业",
        "强势概念",
        "行业概念",
        "行业板块",
        "概念板块",
    }
    return text in exact or bool(re.search(r"(上涨|涨幅|表现).{0,8}(较好|比较好|最好|靠前|居前)", text))


def _filter_missing_stock_identifier_questions(questions: Iterable[str]) -> list[str]:
    return [question for question in questions if not _is_missing_stock_identifier_question(question)]


def _is_missing_stock_identifier_question(question: str) -> bool:
    text = str(question or "").strip()
    if not text:
        return False
    has_request = any(term in text for term in ("提供", "指定", "输入", "确认", "补充", "缺少", "需要", "明确"))
    has_stock_target = any(term in text for term in ("股票", "代码", "名称", "证券", "标的"))
    return has_request and has_stock_target


def _is_missing_market_scope_question(question: str) -> bool:
    text = str(question or "").strip()
    if not text:
        return False
    has_request = any(term in text for term in ("提供", "指定", "输入", "确认", "补充", "缺少", "需要", "明确", "选择"))
    has_market_scope = any(term in text for term in ("热点", "板块", "行业", "概念", "领域", "题材", "方向", "赛道", "主线", "范围", "主题"))
    return has_request and has_market_scope


def _market_plan_source(local: ChatPreprocessResult, payload: dict[str, Any], market_indices: tuple[str, ...]) -> str:
    if market_indices and payload:
        return "llm+local_market_plan"
    if market_indices:
        return "explicit_market_indices"
    if local.needs_market_context:
        return "local_default"
    return ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in (_clean_string(item) for item in value) if item]


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是", "需要"}
    return bool(value)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _confidence(value: Any, *, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen or item in _GENERIC_NAME_WORDS:
            continue
        seen.add(item)
        result.append(item)
    return result


_GENERIC_NAME_WORDS = {
    "股票",
    "个股",
    "大盘",
    "市场",
    "技术面",
    "基本面",
    "未来几天",
    "上涨趋势",
    "上面列表",
}
