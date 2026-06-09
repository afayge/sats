from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sats.agent.models import AgentObservation, AgentPlan
from sats.chat_artifacts import save_markdown_artifact
from sats.llm import ChatLLM
from sats.natural_output import format_meter, format_sparkline
from sats.skill_routing import SkillRouteContext, SkillSelection, select_skills
from sats.skills import Skill, match_skills
from sats.symbols import normalize_symbols


FULL_SKILL_CHAR_LIMIT = 2400
MAX_FULL_SKILLS = 4

SYNTHESIS_SYSTEM_PROMPT = (
    "你是 SATS Agent 的最终分析器。你只能基于下面提供的 SATS 工具 observations、skills 方法论和真实数据 provenance 作答；"
    "不得编造股票/指数价格、成交量、K线、quote、新闻、公告、题材或资金流。"
    "如果数据缺失或工具失败，必须明确说明限制。"
    "如果明确请求的股票未出现在摘要中，只能说明摘要未展开/未纳入摘要；只有 missing_fields、空指标 payload 或工具错误才可写成数据缺失/未命中。"
    "对投资相关判断必须说明仅供研究，不构成投资建议。"
    "回答要像排版清晰的研究分析正文，而不是工具执行日志；不要输出 step id、[done]、原始 JSON 大段内容。"
    "优先使用中文 Markdown 标题、表格、引用块和粗体突出结论。"
    "输出必须尽量遵循统一骨架：H1 标题、引用式核心结论、badge 元信息、"
    "“结论摘要 / 关键证据 / 文字图表 / 风险与限制 / 下一步”这些一级段落。"
    "文字图表优先使用 ASCII/Unicode 比例条或 sparkline。"
)

REPORT_TERMS = ("报告", "保存", "导出", "写成", "生成研究")


@dataclass(frozen=True, slots=True)
class AgentSynthesisResult:
    content: str
    skill_names: tuple[str, ...] = ()
    messages: tuple[dict[str, Any], ...] = ()
    used_llm: bool = False


def synthesize_agent_result(
    *,
    message: str,
    plan: AgentPlan,
    observations: tuple[AgentObservation, ...],
    skills: tuple[Skill, ...],
    settings: Any,
    llm_factory: Callable[..., Any] | None = ChatLLM,
) -> AgentSynthesisResult:
    matched_skill_selections = _matched_skill_selections(message, observations, skills)
    matched_skills = [selection.skill for selection in matched_skill_selections]
    if _is_chat_answer_only(observations):
        return AgentSynthesisResult(
            content=_first_observation_content(observations) or "无响应",
            skill_names=tuple(skill.name for skill in matched_skills),
            used_llm=False,
        )
    if not _needs_synthesis(observations):
        return AgentSynthesisResult(
            content=_fallback_summary(plan.objective, observations, []),
            skill_names=tuple(skill.name for skill in matched_skills),
            used_llm=False,
        )
    messages = _synthesis_messages(
        message=message,
        plan=plan,
        observations=observations,
        matched_skill_selections=matched_skill_selections,
    )
    if llm_factory is None:
        return AgentSynthesisResult(
            content=_fallback_summary(plan.objective, observations, matched_skills),
            skill_names=tuple(skill.name for skill in matched_skills),
            messages=tuple(messages),
            used_llm=False,
        )
    try:
        llm = _make_llm(llm_factory, settings)
        try:
            response = llm.chat(messages, timeout=getattr(settings, "llm_timeout_seconds", None))
        except TypeError:
            response = llm.chat(messages)
        content = str(getattr(response, "content", "") or "").strip()
    except Exception:
        content = ""
    if not content:
        content = _fallback_summary(plan.objective, observations, matched_skills)
        return AgentSynthesisResult(content=content, skill_names=tuple(skill.name for skill in matched_skills), messages=tuple(messages), used_llm=False)
    return AgentSynthesisResult(content=content, skill_names=tuple(skill.name for skill in matched_skills), messages=tuple(messages), used_llm=True)


def should_write_agent_report(message: str, plan: AgentPlan, observations: tuple[AgentObservation, ...]) -> bool:
    text = str(message or "")
    if any(term in text for term in REPORT_TERMS):
        return True
    for step in plan.steps:
        if step.kind == "tool" and step.tool_name == "research.write_report":
            return True
    return any(_is_deferred_report_observation(item) for item in observations)


def save_agent_report(
    *,
    content: str,
    message: str,
    settings: Any,
    store: Any | None,
    session_id: str,
    turn_id: str,
) -> dict[str, Any]:
    write = save_markdown_artifact(
        project_root=Path(getattr(settings, "project_root", ".")),
        session_id=session_id,
        turn_id=turn_id or "agent",
        title="SATS Agent 研究报告",
        content=content,
        filename="agent_report.md",
        report=True,
        summary="Agent 研究报告",
        meta={"request": message},
    )
    artifact = write.to_dict()
    if store is not None:
        try:
            artifact["artifact_id"] = store.add_chat_artifact(
                session_id=session_id,
                turn_id=turn_id or "agent",
                kind=write.kind,
                title=write.title,
                path=str(write.path),
                mime_type=write.mime_type,
                summary=write.summary,
                meta=write.meta or {},
            )
        except Exception:
            artifact["artifact_id"] = ""
    return artifact


def _synthesis_messages(
    *,
    message: str,
    plan: AgentPlan,
    observations: tuple[AgentObservation, ...],
    matched_skill_selections: list[SkillSelection],
) -> list[dict[str, Any]]:
    analysis_style = _analysis_style(message, observations)
    style_guide = _style_guide(analysis_style)
    context = {
        "objective": plan.objective,
        "success_criteria": list(plan.success_criteria),
        "risk_level": plan.risk_level,
        "analysis_style": analysis_style,
        "style_guide": style_guide,
        "skills": _skill_context(matched_skill_selections),
        "evidence_digest": _evidence_digest(observations),
        "observations": [_observation_for_llm(item) for item in observations if not _is_deferred_report_observation(item)],
    }
    return [
        {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
        {
            "role": "system",
            "content": "以下是 SATS Agent 已经获取/计算的真实上下文和方法论摘要：\n" + json.dumps(context, ensure_ascii=False, default=str),
        },
        {
            "role": "user",
            "content": (
                f"用户问题：{message}\n"
                "请严格按 style_guide 输出详细中文 Markdown 分析。必须直接给结论和表格化证据，"
                "并尽量保持统一骨架：标题、核心结论引用、badge 元信息、结论摘要、关键证据、文字图表、风险与限制、下一步。"
                "并把缺失数据写成“数据缺失/未命中”。不要输出工具执行日志。"
            ),
        },
    ]


def _analysis_style(message: str, observations: tuple[AgentObservation, ...]) -> str:
    tools = {str(item.payload.get("tool_name") or "") for item in observations}
    text = str(message or "")
    if "research.discover_opportunities" in tools or any(term in text for term in ("推荐", "筛选", "候选", "短线机会")):
        return "discovery"
    if "research.backtest" in tools:
        return "backtest"
    if "research.market_context" in tools or any(term in text for term in ("大盘", "指数", "市场")):
        return "market_analysis"
    if "research.chan_context" in tools:
        return "stock_analysis"
    if "research.stock_context" in tools or any(term in text for term in ("个股", "股票", "走势", "技术面")):
        return "stock_analysis"
    return "general_research"


def _style_guide(style: str) -> dict[str, Any]:
    common = {
        "format": "中文 Markdown；用表格呈现指标和证据；少用长段落；不要输出工具日志。",
        "required_footer": "必须以“以上仅供研究，不构成投资建议。”或同义风险提示收尾。",
        "data_policy": "所有价格、成交量、K线、quote、指标、信号和因子证据只能来自 observations/evidence_digest；只有 requested_dimensions/missing_fields、真实空 payload 或工具错误表明缺失时，才写数据缺失或未命中；明确请求股票未出现在摘要中只能写摘要未展开/未纳入摘要，不要写成数据缺失；不要把摘要字段名差异当成数据缺失。",
    }
    if style == "stock_analysis":
        return {
            **common,
            "title": "用股票名称或代码写 H1 标题。",
            "sections": [
                "数据截止与免责声明",
                "核心结论",
                "今日/近期盘面表",
                "关键变化对比",
                "技术指标与 Analyze 信号表",
                "因子/资金/风险证据",
                "结构/趋势研判",
                "关键价位",
                "未来情景与触发条件",
                "综合判断",
                "最关键观察项",
                "一句话总结",
            ],
            "table_hints": ["盘面表", "关键变化对比表", "Analyze 信号表", "关键价位表", "情景推演表", "综合判断表", "观察清单表"],
        }
    if style == "market_analysis":
        return {
            **common,
            "title": "用 A股大盘或指数主题写 H1 标题。",
            "sections": ["数据截止与免责声明", "核心结论", "核心指数表", "市场宽度/情绪", "板块轮动", "下周情景", "风险触发位", "一句话总结"],
            "table_hints": ["核心指数表", "宽度/情绪表", "板块轮动表", "情景推演表"],
        }
    if style == "discovery":
        return {
            **common,
            "title": "用短线机会发现或候选观察名单写 H1 标题。",
            "sections": ["数据截止与免责声明", "核心结论", "候选排序表", "入选理由", "触发条件", "失效条件", "风险过滤", "观察名单"],
            "table_hints": ["候选排序表", "触发/失效条件表", "风险过滤表"],
        }
    if style == "backtest":
        return {
            **common,
            "title": "用策略回测报告写 H1 标题。",
            "sections": ["核心结论", "策略规则", "回测指标", "收益/回撤解释", "适用条件", "风险与改进"],
            "table_hints": ["回测指标表", "交易/信号统计表"],
        }
    return {
        **common,
        "title": "用用户问题主题写 H1 标题。",
        "sections": ["核心结论", "已获取证据", "分析判断", "风险与限制", "下一步观察"],
        "table_hints": ["证据表", "风险表"],
    }


def _evidence_digest(observations: tuple[AgentObservation, ...]) -> dict[str, Any]:
    digest: dict[str, Any] = {
        "data_cutoff": "",
        "provenance": [],
        "quotes": [],
        "stock_context": [],
        "market_context": {},
        "indicators": [],
        "indicator_coverage": {},
        "analyze_signals": [],
        "factor_summary": {},
        "discovery": {},
        "chan_context": {},
        "knowledge_context": {},
        "rule_generation": {},
        "backtest": {},
        "errors": [],
    }
    for obs in observations:
        tool_name = str(obs.payload.get("tool_name") or "")
        if obs.status == "error":
            digest["errors"].append({"tool_name": tool_name or obs.kind, "message": _truncate(obs.content, 240)})
            continue
        payload = _result_payload(obs)
        _collect_provenance(digest, payload)
        if tool_name == "data.realtime_quotes":
            digest["quotes"].extend(_compact_rows(payload, ("ts_code", "name", "price", "pct_chg", "change", "volume", "amount", "fetched_at")))
        elif tool_name == "research.stock_context":
            digest["stock_context"].extend(_stock_context_digest(payload, requested_symbols=_observation_symbols(obs)))
        elif tool_name == "research.market_context":
            digest["market_context"] = _market_context_digest(payload)
        elif tool_name == "research.chan_context":
            context = payload.get("chan_context") if isinstance(payload.get("chan_context"), dict) else payload
            digest["chan_context"] = _trim_payload(context, max_chars=5000)
        elif tool_name == "research.knowledge_context":
            context = payload.get("knowledge_context") if isinstance(payload.get("knowledge_context"), dict) else payload
            digest["knowledge_context"] = _trim_payload(context, max_chars=5000)
        elif tool_name == "research.rule_generation":
            context = payload.get("rule_generation") if isinstance(payload.get("rule_generation"), dict) else payload
            digest["rule_generation"] = _trim_payload(context, max_chars=7000)
        elif tool_name == "research.internal_analysis":
            analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else payload
            kind = str(analysis.get("kind") or "")
            if kind == "indicators":
                items = analysis.get("indicators") if isinstance(analysis.get("indicators"), list) else analysis.get("results")
                requested_symbols = _observation_symbols(obs)
                digest["indicators"].extend(_indicator_items(items, requested_symbols=requested_symbols))
                coverage = _indicator_coverage(items, requested_symbols=requested_symbols)
                if coverage:
                    digest["indicator_coverage"] = coverage
            elif kind == "analyze_signals":
                digest["analyze_signals"].extend(_signal_digest(analysis))
            elif kind == "factor_summary":
                digest["factor_summary"] = _trim_payload(analysis, max_chars=7000)
        elif tool_name == "research.discover_opportunities":
            digest["discovery"] = _trim_payload(payload, max_chars=9000)
        elif tool_name == "research.backtest":
            digest["backtest"] = _trim_payload(payload, max_chars=7000)
        digest["data_cutoff"] = _latest_cutoff(digest["data_cutoff"], _payload_cutoff(payload))
    digest["provenance"] = _dedupe_dicts(digest["provenance"])[:12]
    return digest


def _matched_skill_selections(message: str, observations: tuple[AgentObservation, ...], skills: tuple[Skill, ...]) -> list[SkillSelection]:
    if not skills:
        return []
    observed_tools = {str(item.payload.get("tool_name") or "") for item in observations}
    internal_kinds = []
    for item in observations:
        if str(item.payload.get("tool_name") or "") != "research.internal_analysis":
            continue
        payload = _result_payload(item)
        analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else payload
        kind = str(analysis.get("kind") or "")
        if kind:
            internal_kinds.append(kind)
    route = select_skills(
        SkillRouteContext(
            message=message,
            intent=_analysis_style(message, observations),
            observed_tools=tuple(observed_tools),
            internal_analysis_kinds=tuple(internal_kinds),
        ),
        skills,
        limit=12,
    )
    if route.selections:
        return list(route.selections)
    return [SkillSelection(skill=skill, load_mode=skill.auto_load, reason="沿用 trigger/description 匹配", score=1) for skill in match_skills(message, list(skills), limit=8)]


def _skill_context(selections: list[SkillSelection]) -> str:
    if not selections:
        return "无匹配 skill。"
    blocks = [
        "以下 skill 只提供分析方法论；价格、成交量、K线、quote、因子和信号必须来自 observations/provenance。",
        "自动选择结果：",
    ]
    full_used = 0
    for selection in selections:
        skill = selection.skill
        mode = selection.load_mode if selection.load_mode in {"summary", "full", "never"} else "summary"
        if mode == "full" and full_used >= MAX_FULL_SKILLS:
            mode = "summary"
        blocks.append(
            f"- {skill.name} [{skill.category}] mode={mode} source={skill.source}; reason={selection.reason or 'auto'}; "
            f"description={skill.description}"
        )
        if mode == "full":
            full_used += 1
            content = _truncate(skill.content, FULL_SKILL_CHAR_LIMIT)
            suffix = "\n[skill content truncated]" if len(skill.content) > FULL_SKILL_CHAR_LIMIT else ""
            blocks.append(f"  full_content:\n{content}{suffix}")
    return "\n".join(blocks)


def _observation_for_llm(observation: AgentObservation) -> dict[str, Any]:
    result = observation.payload.get("result") if isinstance(observation.payload.get("result"), dict) else {}
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else observation.payload
    return {
        "step_id": observation.step_id,
        "kind": observation.kind,
        "status": observation.status,
        "tool_name": observation.payload.get("tool_name") or "",
        "data_names": list(observation.payload.get("data_names") or result.get("data_names") or []),
        "content": _truncate(observation.content, 1200),
        "payload": _trim_payload(payload),
    }


def _result_payload(observation: AgentObservation) -> dict[str, Any]:
    result = observation.payload.get("result") if isinstance(observation.payload.get("result"), dict) else {}
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else observation.payload
    return payload if isinstance(payload, dict) else {}


def _collect_provenance(digest: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in ("provenance", "market_data_provenance"):
        rows = payload.get(key)
        if isinstance(rows, list):
            digest["provenance"].extend(item for item in rows if isinstance(item, dict))
    for value in payload.values():
        if isinstance(value, dict):
            _collect_provenance(digest, value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _collect_provenance(digest, item)


def _stock_context_digest(payload: dict[str, Any], *, requested_symbols: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
    context = payload.get("stock_context") if isinstance(payload.get("stock_context"), dict) else payload
    stocks = context.get("stocks") if isinstance(context.get("stocks"), list) else []
    if not stocks and isinstance(context.get("symbols"), list):
        stocks = [context]
    return _stock_context_rows(stocks, requested_symbols=requested_symbols)


def _stock_context_rows(stocks: Any, *, requested_symbols: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    requested = _normalized_symbol_list(requested_symbols)
    if requested:
        stocks = _matching_symbol_rows(stocks, requested)
    elif isinstance(stocks, list):
        stocks = stocks[:6]
    else:
        stocks = []
    rows: list[dict[str, Any]] = []
    for item in stocks:
        if not isinstance(item, dict):
            continue
        latest_daily = _last_dict(item.get("daily_tail"))
        indicator = item.get("indicator_result") if isinstance(item.get("indicator_result"), dict) else {}
        rows.append(
            _drop_empty(
                {
                    "ts_code": item.get("ts_code") or item.get("symbol"),
                    "name": item.get("name"),
                    "trade_date": item.get("trade_date") or latest_daily.get("trade_date"),
                    "latest_daily": _pick_fields(latest_daily, ("trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg")),
                    "indicator": _pick_fields(
                        indicator,
                        ("close", "ma5", "ma10", "ma20", "ma60", "macd", "macd_dif", "macd_dea", "rsi6", "rsi12", "kdj_j", "boll_upper", "boll_mid", "boll_lower"),
                    ),
                    "missing_fields": item.get("missing_fields"),
                }
            )
        )
    return rows


def _market_context_digest(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("market_context") if isinstance(payload.get("market_context"), dict) else payload
    hot_sector_context = _hot_sector_context_digest(context.get("hot_sector_context"))
    return _drop_empty(
        {
            "trade_date": context.get("trade_date"),
            "core_indices": _market_index_rows(context.get("indices") or context.get("core_indices")),
            "market_breadth": _trim_payload(context.get("market_breadth") or {}, max_chars=2500),
            "limit_sentiment": _trim_payload(context.get("limit_sentiment") or {}, max_chars=2500),
            "hot_sector_context": hot_sector_context,
            "hot_sectors": _hot_sector_rows(
                hot_sector_context,
                context.get("hot_sectors") or context.get("sector_rotation"),
            ),
            "requested_indices": context.get("requested_indices"),
            "requested_dimensions": context.get("requested_dimensions"),
            "requested_horizons": context.get("requested_horizons"),
            "missing_fields": context.get("missing_fields"),
            "data_sources": context.get("data_sources"),
        }
    )


def _market_index_rows(value: Any) -> list[dict[str, Any]]:
    rows = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        latest = item.get("latest") if isinstance(item.get("latest"), dict) else {}
        technical = item.get("technical") if isinstance(item.get("technical"), dict) else {}
        rows.append(
            _drop_empty(
                {
                    "ts_code": item.get("ts_code"),
                    "name": item.get("name"),
                    "trade_date": item.get("trade_date") or latest.get("trade_date"),
                    "close": item.get("close") if item.get("close") is not None else latest.get("close"),
                    "pct_chg": item.get("pct_chg") if item.get("pct_chg") is not None else latest.get("pct_chg"),
                    "amount": item.get("amount") if item.get("amount") is not None else latest.get("amount"),
                    "vol": item.get("vol") if item.get("vol") is not None else latest.get("vol"),
                    "latest": _pick_fields(latest, ("close", "pct_chg", "amount", "vol")),
                    "technical": _trim_payload(technical, max_chars=1800),
                    "missing_fields": item.get("missing_fields"),
                }
            )
        )
    return rows[:8]


def _hot_sector_context_digest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty(
        {
            "trade_date": value.get("trade_date"),
            "lookback_days": value.get("lookback_days"),
            "hot_industries": _compact_items(value.get("hot_industries"), limit=10),
            "hot_concepts": _compact_items(value.get("hot_concepts"), limit=10),
            "missing_fields": value.get("missing_fields"),
            "data_sources": value.get("data_sources"),
        }
    )


def _hot_sector_rows(context: dict[str, Any], legacy: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, label in (("hot_industries", "industry"), ("hot_concepts", "concept")):
        for item in context.get(key) if isinstance(context.get(key), list) else []:
            if not isinstance(item, dict):
                continue
            pct_chg = item.get("pct_chg") if item.get("pct_chg") is not None else item.get("latest_pct_chg")
            score = item.get("score") if item.get("score") is not None else item.get("heat_score")
            rows.append(
                _drop_empty(
                    {
                        "name": item.get("name"),
                        "sector": item.get("sector_type") or label,
                        "pct_chg": pct_chg,
                        "score": score,
                        "reason": item.get("reason") or item.get("rank_reason") or item.get("concept"),
                    }
                )
            )
    if rows:
        return rows[:10]
    return _legacy_hot_sector_rows(legacy)


def _legacy_hot_sector_rows(value: Any) -> list[dict[str, Any]]:
    rows = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            _drop_empty(
                {
                    "name": item.get("name"),
                    "sector": item.get("sector") or item.get("sector_type"),
                    "pct_chg": item.get("pct_chg") or item.get("latest_pct_chg"),
                    "score": item.get("score") or item.get("heat_score"),
                    "reason": item.get("reason") or item.get("rank_reason"),
                }
            )
        )
    return rows[:10]


def _signal_digest(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in analysis.get("results") if isinstance(analysis.get("results"), list) else []:
        if not isinstance(item, dict):
            continue
        events = item.get("events") if isinstance(item.get("events"), list) else []
        rows.append(
            _drop_empty(
                {
                    "ts_code": item.get("ts_code"),
                    "name": item.get("name"),
                    "trade_date": item.get("trade_date"),
                    "close": item.get("close"),
                    "score": item.get("score"),
                    "decision": item.get("decision"),
                    "trend": item.get("trend"),
                    "selected_signals": item.get("selected_signals"),
                    "key_levels": item.get("key_levels"),
                    "events": [
                        _pick_fields(event, ("signal_id", "label", "category", "side", "confidence", "score", "reason", "risk_flags"))
                        for event in events[:6]
                        if isinstance(event, dict)
                    ],
                }
            )
        )
    return rows


def _compact_rows(payload: dict[str, Any], fields: tuple[str, ...], *, limit: int = 10) -> list[dict[str, Any]]:
    sample = payload.get("sample") if isinstance(payload.get("sample"), list) else []
    return [_pick_fields(item, fields) for item in sample[:limit] if isinstance(item, dict)]


def _compact_items(value: Any, *, limit: int = 8) -> list[Any]:
    if not isinstance(value, list):
        return []
    rows = []
    for item in value[:limit]:
        if isinstance(item, dict):
            rows.append(_trim_payload(item, max_chars=2500))
        else:
            rows.append(item)
    return rows


def _indicator_items(value: Any, *, requested_symbols: list[str] | tuple[str, ...]) -> list[Any]:
    requested = _normalized_symbol_list(requested_symbols)
    if not requested:
        return _compact_items(value, limit=8)
    return [_trim_payload(item, max_chars=2500) for item in _matching_symbol_rows(value, requested)]


def _indicator_coverage(value: Any, *, requested_symbols: list[str] | tuple[str, ...]) -> dict[str, Any]:
    requested = _normalized_symbol_list(requested_symbols)
    if not requested:
        return {}
    rows = value if isinstance(value, list) else []
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


def _observation_symbols(observation: AgentObservation) -> list[str]:
    arguments = observation.payload.get("arguments") if isinstance(observation.payload.get("arguments"), dict) else {}
    return _normalized_symbol_list(arguments.get("symbols") if isinstance(arguments, dict) else [])


def _normalized_symbol_list(values: Any) -> list[str]:
    return normalize_symbols(values or [], required=False)


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


def _payload_cutoff(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("trade_date", "fetched_at", "datetime", "date"):
        value = payload.get(key)
        if value not in (None, ""):
            candidates.append(str(value))
    for row in _compact_rows(payload, ("trade_date", "fetched_at", "datetime"), limit=20):
        for value in row.values():
            if value not in (None, ""):
                candidates.append(str(value))
    for key in ("market_context", "stock_context", "analysis"):
        value = payload.get(key)
        if isinstance(value, dict):
            nested = _payload_cutoff(value)
            if nested:
                candidates.append(nested)
    return max(candidates) if candidates else ""


def _latest_cutoff(current: str, candidate: str) -> str:
    if not candidate:
        return current
    if not current:
        return candidate
    return max(str(current), str(candidate))


def _last_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        for item in reversed(value):
            if isinstance(item, dict):
                return item
    return {}


def _pick_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty({field: value.get(field) for field in fields})


def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _dedupe_dicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _trim_payload(value: Any, *, max_chars: int = 18000) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return value
    return {"truncated_json": text[:max_chars], "truncated": True}


def _fallback_summary(objective: str, observations: tuple[AgentObservation, ...], matched_skills: list[Skill]) -> str:
    style = _analysis_style(objective, observations)
    digest = _evidence_digest(observations)
    lines = [f"# {_fallback_title(style, objective)}", ""]
    cutoff = str(digest.get("data_cutoff") or "以 SATS 已获取数据为准")
    lines.extend([f"> 已围绕“{objective}”完成 SATS 真实数据工具调用；数据截止 {cutoff}。", "", "`风格: 研究输出`", ""])
    lines.extend(["## 结论摘要", "", f"- 已围绕“{objective}”完成 SATS 真实数据工具调用。", "- 以下结论只基于已返回的 observations/provenance。", "- 仅供研究，不构成投资建议。", ""])
    successes = [item for item in observations if item.status == "done" and item.kind != "final" and not _is_deferred_report_observation(item)]
    errors = [item for item in observations if item.status == "error"]
    if matched_skills:
        lines.extend(["## 使用的方法论", "", "、".join(skill.name for skill in matched_skills), ""])
    lines.extend(["## 文字图表", "", f"- 数据覆盖: {format_meter(min(5, max(1, len(successes))))}", f"- 风险等级: {format_meter(4 if errors else 2)}", f"- 信息密度: {format_sparkline([len(successes), len(errors), len(matched_skills), len(digest.get('provenance') or []), len(digest.get('quotes') or [])])}", ""])
    if style == "stock_analysis":
        _append_stock_fallback(lines, digest)
    elif style == "market_analysis":
        _append_market_fallback(lines, digest)
    elif style == "discovery":
        _append_discovery_fallback(lines, digest)
    elif style == "backtest":
        _append_backtest_fallback(lines, digest)
    _append_observation_summary(lines, successes)
    if errors:
        lines.append("## 风险与限制")
        lines.append("")
        for obs in errors:
            lines.append(f"- {_observation_label(obs)}: {_observation_detail(obs)}")
        lines.append("")
    else:
        lines.extend(["## 风险与限制", "", "- 当前结论仅基于已注入 observations/provenance。", "- 若关键字段缺失，需要先补齐 SATS 数据后再判断。", ""])
    lines.extend(["## 下一步", "", "- 继续跟踪已列出的真实数据源；若关键字段缺失，需要先补齐 SATS 数据后再判断。", "- 不将模型表达作为交易指令，交易动作仍需单独授权和风控校验。", "", "以上仅供研究，不构成投资建议。"])
    return "\n".join(lines).strip()


def _fallback_title(style: str, objective: str) -> str:
    if style == "stock_analysis":
        return "个股走势研究报告"
    if style == "market_analysis":
        return "A股大盘走势研究报告"
    if style == "discovery":
        return "短线机会发现报告"
    if style == "backtest":
        return "策略回测研究报告"
    return str(objective or "SATS Agent 分析结果")


def _append_stock_fallback(lines: list[str], digest: dict[str, Any]) -> None:
    lines.extend(["## 今日/近期盘面", ""])
    quote_rows = digest.get("quotes") if isinstance(digest.get("quotes"), list) else []
    stock_rows = digest.get("stock_context") if isinstance(digest.get("stock_context"), list) else []
    rows = []
    for row in (quote_rows or stock_rows)[:6]:
        if isinstance(row, dict):
            latest = row.get("latest_daily") if isinstance(row.get("latest_daily"), dict) else {}
            rows.append(
                {
                    "代码": row.get("ts_code"),
                    "名称": row.get("name"),
                    "日期": row.get("trade_date") or latest.get("trade_date"),
                    "价格/收盘": row.get("price") or latest.get("close"),
                    "涨跌幅": row.get("pct_chg"),
                    "成交量": row.get("volume") or latest.get("vol"),
                }
            )
    lines.extend(_markdown_table(rows, ("代码", "名称", "日期", "价格/收盘", "涨跌幅", "成交量")) or ["数据缺失：未返回可展示的 quote 或日 K 摘要。"])
    lines.extend(["", "## 技术指标与 Analyze 信号", ""])
    signal_rows = []
    for item in digest.get("analyze_signals") if isinstance(digest.get("analyze_signals"), list) else []:
        labels = "、".join(str(event.get("label") or event.get("signal_id") or "") for event in item.get("events", []) if isinstance(event, dict)) or "未命中"
        signal_rows.append({"代码": item.get("ts_code"), "评分": item.get("score"), "判断": item.get("decision"), "趋势": item.get("trend"), "信号": labels})
    lines.extend(_markdown_table(signal_rows, ("代码", "评分", "判断", "趋势", "信号")) or ["Analyze 信号数据缺失或未命中。"])
    lines.extend(["", "## 因子/资金/风险证据", ""])
    factor = digest.get("factor_summary")
    lines.append(_compact_json_line(factor) if factor else "因子画像数据缺失。")
    lines.extend(["", "## 关键价位", ""])
    key_rows = []
    for item in digest.get("analyze_signals") if isinstance(digest.get("analyze_signals"), list) else []:
        levels = item.get("key_levels") if isinstance(item.get("key_levels"), dict) else {}
        if levels:
            key_rows.append({"代码": item.get("ts_code"), "支撑": levels.get("support"), "压力": levels.get("resistance"), "说明": levels.get("note")})
    lines.extend(_markdown_table(key_rows, ("代码", "支撑", "压力", "说明")) or ["关键价位数据缺失。"])
    lines.extend(["", "## 未来情景与触发条件", "", "| 情景 | 触发条件 | 观察重点 |", "|---|---|---|", "| 偏强 | 已有信号继续改善且关键支撑不破 | 量能、趋势评分、触发信号是否延续 |", "| 震荡 | 信号分歧或量能不足 | 支撑/压力区间内反复确认 |", "| 转弱 | Analyze 信号转弱或关键支撑失守 | 风险信号、成交量和大盘共振 |", ""])


def _append_market_fallback(lines: list[str], digest: dict[str, Any]) -> None:
    market = digest.get("market_context") if isinstance(digest.get("market_context"), dict) else {}
    lines.extend(["## 核心指数表", ""])
    rows = market.get("core_indices") if isinstance(market.get("core_indices"), list) else []
    lines.extend(_markdown_table(rows, ("ts_code", "name", "trade_date", "close", "pct_chg")) or ["核心指数数据缺失。"])
    lines.extend(["", "## 市场宽度/情绪", "", _compact_json_line({"market_breadth": market.get("market_breadth"), "limit_sentiment": market.get("limit_sentiment")}), "", "## 板块轮动", ""])
    sector_rows = market.get("hot_sectors") if isinstance(market.get("hot_sectors"), list) else []
    lines.extend(_markdown_table(sector_rows, ("name", "sector", "pct_chg", "score", "reason")) or ["板块轮动数据缺失。"])
    lines.extend(["", "## 下周情景", "", "| 情景 | 触发条件 | 风险触发位 |", "|---|---|---|", "| 偏强 | 核心指数放量修复且宽度改善 | 以 observation 中真实指数位为准 |", "| 震荡 | 宽度和情绪分歧 | 关注成交量和涨跌停情绪 |", "| 转弱 | 指数与情绪同步走弱 | 不补造点位，等待真实数据确认 |", ""])


def _append_discovery_fallback(lines: list[str], digest: dict[str, Any]) -> None:
    discovery = digest.get("discovery") if isinstance(digest.get("discovery"), dict) else {}
    lines.extend(["## 候选排序表", "", _compact_json_line(discovery) if discovery else "候选排序数据缺失。", "", "## 触发条件与失效条件", "", "| 项目 | 说明 |", "|---|---|", "| 触发条件 | 以候选股返回的 Analyze 信号、趋势评分和真实成交数据为准 |", "| 失效条件 | 信号未延续、关键支撑失守或市场情绪恶化 |", "| 风险过滤 | 不保证上涨；仅作为观察名单 |", ""])


def _append_backtest_fallback(lines: list[str], digest: dict[str, Any]) -> None:
    backtest = digest.get("backtest") if isinstance(digest.get("backtest"), dict) else {}
    lines.extend(["## 回测指标", "", _compact_json_line(backtest) if backtest else "回测指标数据缺失。", ""])


def _append_observation_summary(lines: list[str], successes: list[AgentObservation]) -> None:
    if not successes:
        return
    lines.extend(["## 已获取证据", ""])
    for obs in successes:
        tool = _observation_label(obs)
        names = ", ".join(str(item) for item in (obs.payload.get("data_names") or []))
        lines.append(f"- {tool}: {names or _observation_detail(obs)}")
    lines.append("")


def _markdown_table(rows: list[Any], columns: tuple[str, ...]) -> list[str]:
    clean_rows = [row for row in rows if isinstance(row, dict)]
    if not clean_rows:
        return []
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    for row in clean_rows:
        lines.append("| " + " | ".join(_table_cell(row.get(column)) for column in columns) + " |")
    return lines


def _table_cell(value: Any) -> str:
    if value in (None, ""):
        return "数据缺失"
    if isinstance(value, (dict, list)):
        return _truncate(json.dumps(value, ensure_ascii=False, default=str), 80)
    return _truncate(str(value), 80)


def _compact_json_line(value: Any) -> str:
    if value in (None, "", {}, []):
        return "数据缺失。"
    return _truncate(json.dumps(value, ensure_ascii=False, default=str), 700)


def _observation_label(observation: AgentObservation) -> str:
    tool_name = str(observation.payload.get("tool_name") or "").strip()
    if tool_name:
        return tool_name
    argv = observation.payload.get("argv")
    if observation.kind == "command" and isinstance(argv, list) and argv:
        return "sats " + " ".join(str(item) for item in argv)
    return observation.kind or observation.step_id


def _observation_detail(observation: AgentObservation) -> str:
    content = _truncate(observation.content, 180)
    if content:
        return content
    argv = observation.payload.get("argv")
    if observation.kind == "command" and isinstance(argv, list):
        returncode = observation.payload.get("returncode")
        return f"argv={argv}, returncode={returncode}"
    result = observation.payload.get("result") if isinstance(observation.payload.get("result"), dict) else {}
    result_content = _truncate(result.get("content"), 180)
    return result_content or observation.status or "无更多明细"


def _make_llm(llm_factory: Callable[..., Any], settings: Any) -> Any:
    try:
        return llm_factory(
            model_name=getattr(settings, "openai_model", None),
            profile="default",
            timeout_seconds=getattr(settings, "llm_timeout_seconds", None),
        )
    except TypeError:
        return llm_factory()


def _needs_synthesis(observations: tuple[AgentObservation, ...]) -> bool:
    for obs in observations:
        tool = str(obs.payload.get("tool_name") or "")
        if tool.startswith(("research.", "data.", "factor.", "trade.")) or obs.kind in {"python", "trade"}:
            return True
    return False


def _is_chat_answer_only(observations: tuple[AgentObservation, ...]) -> bool:
    meaningful = [item for item in observations if item.kind != "final"]
    return len(meaningful) == 1 and str(meaningful[0].payload.get("tool_name") or "") == "chat.answer"


def _first_observation_content(observations: tuple[AgentObservation, ...]) -> str:
    for item in observations:
        if item.content:
            return item.content
    return ""


def _is_deferred_report_observation(observation: AgentObservation) -> bool:
    result = observation.payload.get("result") if isinstance(observation.payload.get("result"), dict) else {}
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    return bool(payload.get("deferred_report"))


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."
