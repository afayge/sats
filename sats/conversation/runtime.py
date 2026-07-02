from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from sats.agent.command_runner import AgentCommandRunner
from sats.agent.date_policy import agent_today, normalize_agent_date, resolve_agent_time_context, sanitize_agent_tool_arguments
from sats.agent.models import AgentExecutionPolicy, AgentObservation, AgentPlan, AgentStep
from sats.agent.synthesis import collect_agent_sources, synthesize_agent_result
from sats.agent.tools import AgentToolContext, AgentToolResult, build_default_tool_registry
from sats.agent.trading import AgentTradingExecutor
from sats.chat_events import ChatEventSink, ChatTurnRecorder
from sats.config import Settings, load_settings
from sats.data.resolver import MarketDataResolver
from sats.llm import ChatLLM, build_standard_llm, extract_json_object
from sats.memory import ChatMemoryStore
from sats.skills import default_skills_dir, load_skills
from sats.storage.duckdb import DuckDBStorage
from sats.stock_basic_lookup import load_stock_basic_frame, resolve_stock_mentions
from sats.stock_question import extract_stock_symbols
from sats.symbols import normalize_ts_code


CONVERSATION_ACTION_TYPE = "conversation_tool"
CONVERSATION_CLARIFICATION_ACTION_TYPE = "conversation_clarification"
CONFIRM_SIDE_EFFECTS = {"live_trade"}
SUBAGENT_KINDS = {"subagent", "delegate"}
MAX_SUBAGENT_STEPS = 4
MAX_INVALID_ACTIONS = 2
CONVERSATION_ACTIONS = {"call_tool", "ask_clarification", "request_confirmation", "final_answer"}
PLAN_MODE_FORBIDDEN_SIDE_EFFECTS = {"write_db", "write_artifact", "command", "long_running", "live_trade"}
CONVERSATION_RESEARCH_TIME_TOOLS = {
    "research.stock_context",
    "research.internal_analysis",
    "research.deep_stock_analysis",
}
TODAY_AS_OF_TERMS = ("今天", "今日", "当天", "当日")
RECENT_AS_OF_TERMS = ("过去几天", "过去几日", "最近几天", "最近几日", "近几天", "近几日", "这几天", "这几日")
CAPABILITY_REQUEST_TERMS = (
    "skill",
    "skills",
    "技能",
    "能力",
    "capability",
    "capabilities",
    "tool",
    "tools",
    "工具",
    "能做什么",
)
CAPABILITY_INVENTORY_TERMS = (
    "列出",
    "支持",
    "哪些",
    "有什么",
    "有哪些",
    "查看",
    "目录",
    "总览",
    "概览",
    "清单",
    "能做什么",
    "可以做什么",
    "list",
    "show",
    "available",
    "supported",
)


@dataclass(frozen=True, slots=True)
class ConversationRunSpec:
    message: str
    policy: AgentExecutionPolicy
    reference_context: dict[str, Any] = field(default_factory=dict)
    available_tools: tuple[str, ...] = ()
    observations: tuple[dict[str, Any], ...] = ()
    phase: str = "conversation_loop"

    def to_plan(self) -> AgentPlan:
        """Compatibility shell for ConversationResult.plan.

        The conversation runtime no longer pre-generates tool steps. Keep only
        the objective and phase so older callers can still read result.plan
        without treating it as an executable plan.
        """

        return AgentPlan(objective=self.message, phase=self.phase)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "policy": self.policy.to_dict(),
            "reference_context": dict(self.reference_context),
            "available_tools": list(self.available_tools),
            "observations": [dict(item) for item in self.observations],
            "phase": self.phase,
        }


@dataclass(frozen=True, slots=True)
class _PlanInterruption:
    content: str
    requires_confirmation: bool = False
    requires_clarification: bool = False
    action_id: str = ""
    clarification_prompt: str = ""
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ConversationAction:
    action: str
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    content: str = ""
    question: str = ""
    missing_fields: tuple[str, ...] = ()
    side_effect: str = ""


@dataclass(frozen=True, slots=True)
class _LoopOutcome:
    interruption: _PlanInterruption | None = None
    final_content: str = ""
    synthesize_final: bool = False
    model_policy: str = ""
    model_profile: str = ""
    model_name: str = ""


@dataclass(frozen=True, slots=True)
class ConversationResult:
    content: str
    plan: AgentPlan
    observations: tuple[AgentObservation, ...] = ()
    data_names: tuple[str, ...] = ("Conversation",)
    skill_names: tuple[str, ...] = ()
    tool_call_count: int = 0
    artifacts: tuple[dict[str, Any], ...] = ()
    sources: tuple[dict[str, Any], ...] = ()
    requires_confirmation: bool = False
    pending_action_id: str | None = None
    requires_clarification: bool = False
    clarification_id: str | None = None
    clarification_prompt: str = ""
    missing_fields: tuple[str, ...] = ()
    turn_id: str | None = None
    session_id: str = "conversation"
    model_policy: str = ""
    model_profile: str = ""
    model_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "plan": self.plan.to_dict(),
            "observations": [item.to_dict() for item in self.observations],
            "data_names": list(self.data_names),
            "skill_names": list(self.skill_names),
            "tool_call_count": self.tool_call_count,
            "artifacts": list(self.artifacts),
            "sources": list(self.sources),
            "requires_confirmation": self.requires_confirmation,
            "pending_action_id": self.pending_action_id or "",
            "requires_clarification": self.requires_clarification,
            "clarification_id": self.clarification_id or "",
            "clarification_prompt": self.clarification_prompt,
            "missing_fields": list(self.missing_fields),
            "turn_id": self.turn_id or "",
            "session_id": self.session_id,
            "model_policy": self.model_policy,
            "model_profile": self.model_profile,
            "model_name": self.model_name,
        }


def run_conversation_once(
    message: str,
    *,
    settings: Settings | None = None,
    policy: AgentExecutionPolicy | None = None,
    session_id: str = "conversation",
    event_sink: ChatEventSink | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
    reference_context: Any | None = None,
) -> ConversationResult:
    settings = settings or load_settings()
    policy = policy or AgentExecutionPolicy()
    text = str(message or "").strip()
    if not text:
        raise ValueError("conversation message is required")

    store = ChatMemoryStore(settings.db_path)
    recorder = ChatTurnRecorder(session_id=session_id, request=text, store=store, event_sink=event_sink)
    started = time.monotonic()
    recorder.start(payload={"conversation_engine": "codex_style", "policy": policy.to_dict()})
    user_message_id = ""
    assistant_message_id = ""
    observations: list[AgentObservation] = []
    artifacts: list[dict[str, Any]] = []
    data_names: list[str] = ["Conversation"]
    tool_call_count = 0

    try:
        context = _tool_context(
            settings=settings,
            store=store,
            policy=policy,
            session_id=session_id,
            turn_id=recorder.turn_id,
            message=text,
            llm_factory=llm_factory,
        )
        registry = build_default_tool_registry()
        run_spec = _conversation_run_spec(text, policy=policy, registry=registry, reference_context=reference_context)
        plan = run_spec.to_plan()
        recorder.emit(
            "plan_ready",
            item_type="run_spec",
            item_name="conversation",
            status="done",
            content=plan.objective,
            payload={"run_spec": run_spec.to_dict(), "plan": plan.to_dict()},
        )
        _preseed_capability_observations(
            text,
            plan=plan,
            registry=registry,
            context=context,
            recorder=recorder,
            observations=observations,
            artifacts=artifacts,
            data_names=data_names,
        )

        if llm_factory is not None:
            loop = _run_conversation_loop(
                message=text,
                plan=plan,
                registry=registry,
                context=context,
                recorder=recorder,
                observations=observations,
                artifacts=artifacts,
                data_names=data_names,
                settings=settings,
                llm_factory=llm_factory,
                reference_context=reference_context,
            )
            interruption = loop.interruption
        else:
            content = "conversation loop requires an LLM action model; no tools were executed."
            observations.append(
                AgentObservation(
                    step_id="llm_unavailable",
                    kind="runtime",
                    status="error",
                    content=content,
                    payload={"phase": "conversation_loop", "reason": "llm_factory_none"},
                )
            )
            loop = _LoopOutcome(final_content=content, synthesize_final=False, model_policy="none")
            interruption = None
        if interruption is not None:
            content = interruption.content
            action_id = interruption.action_id
            user_message_id, assistant_message_id = _persist_messages(store, session_id, text, content)
            tool_call_count = len([item for item in observations if item.kind == "tool"])
            result = ConversationResult(
                content=content,
                plan=plan,
                observations=tuple(observations),
                data_names=tuple(dict.fromkeys(data_names)),
                tool_call_count=tool_call_count,
                artifacts=tuple(artifacts),
                requires_confirmation=interruption.requires_confirmation,
                pending_action_id=action_id if interruption.requires_confirmation else None,
                requires_clarification=interruption.requires_clarification,
                clarification_id=action_id if interruption.requires_clarification else None,
                clarification_prompt=interruption.clarification_prompt,
                missing_fields=interruption.missing_fields,
                turn_id=recorder.turn_id,
                session_id=session_id,
                model_policy=loop.model_policy or "none",
                model_profile=loop.model_profile,
                model_name=loop.model_name,
            )
            _complete_turn(recorder, result, started, user_message_id=user_message_id, assistant_message_id=assistant_message_id)
            return result

        tool_call_count = len([item for item in observations if item.kind == "tool"])
        sources = tuple(collect_agent_sources(tuple(observations)))
        if loop.final_content and not loop.synthesize_final:
            content = loop.final_content
            skill_names: tuple[str, ...] = ()
            model_policy = loop.model_policy or "standard"
            model_profile = loop.model_profile
            model_name = loop.model_name
        else:
            synthesis_started = time.monotonic()
            recorder.emit(
                "context_started",
                item_type="agent_synthesis",
                item_name="final_synthesis",
                status="running",
                payload={"phase": "synthesis"},
            )
            try:
                synthesis = synthesize_agent_result(
                    message=text,
                    plan=plan,
                    observations=tuple(observations),
                    skills=tuple(context.skills),
                    settings=settings,
                    llm_factory=llm_factory,
                )
            except Exception as exc:
                recorder.emit(
                    "context_completed",
                    item_type="agent_synthesis",
                    item_name="final_synthesis",
                    status="error",
                    content=str(exc),
                    payload={"phase": "synthesis", "error_type": exc.__class__.__name__, "error_message": str(exc)},
                    duration_seconds=max(0.0, time.monotonic() - synthesis_started),
                )
                raise
            _emit_synthesis_error(recorder, synthesis)
            content = synthesis.content or _fallback_content(observations)
            skill_names = synthesis.skill_names
            model_policy = synthesis.model_policy
            model_profile = synthesis.model_profile
            model_name = synthesis.model_name
            recorder.emit(
                "context_completed",
                item_type="agent_synthesis",
                item_name="final_synthesis",
                status="done",
                content=content,
                payload={
                    "used_llm": bool(getattr(synthesis, "used_llm", False)),
                    "skills": list(getattr(synthesis, "skill_names", ()) or ()),
                    "phase": str(getattr(synthesis, "phase", "synthesis") or "synthesis"),
                    "model_policy": str(getattr(synthesis, "model_policy", "") or ""),
                    "model_profile": str(getattr(synthesis, "model_profile", "") or ""),
                    "model_name": str(getattr(synthesis, "model_name", "") or ""),
                    "prompt_chars": int(getattr(synthesis, "prompt_chars", 0) or 0),
                    "prompt_budget_chars": int(getattr(synthesis, "prompt_budget_chars", 0) or 0),
                    "compact_mode": str(getattr(synthesis, "compact_mode", "") or ""),
                    "retry_count": int(getattr(synthesis, "retry_count", 0) or 0),
                    "base_url_class": str(getattr(synthesis, "base_url_class", "") or ""),
                },
                duration_seconds=max(0.0, time.monotonic() - synthesis_started),
            )
        user_message_id, assistant_message_id = _persist_messages(store, session_id, text, content)
        result = ConversationResult(
            content=content,
            plan=plan,
            observations=tuple(observations),
            data_names=tuple(dict.fromkeys(data_names)),
            skill_names=skill_names,
            tool_call_count=tool_call_count,
            artifacts=tuple(artifacts),
            sources=sources,
            turn_id=recorder.turn_id,
            session_id=session_id,
            model_policy=model_policy,
            model_profile=model_profile,
            model_name=model_name,
        )
        _complete_turn(recorder, result, started, user_message_id=user_message_id, assistant_message_id=assistant_message_id)
        return result
    except Exception as exc:
        recorder.fail(exc, duration_seconds=max(0.0, time.monotonic() - started), meta={"phase": "conversation"})
        raise


def _conversation_run_spec(
    message: str,
    *,
    policy: AgentExecutionPolicy,
    registry: Any,
    reference_context: Any | None,
    observations: tuple[AgentObservation, ...] = (),
) -> ConversationRunSpec:
    return ConversationRunSpec(
        message=message,
        policy=policy,
        reference_context=_reference_context_payload(reference_context),
        available_tools=_registry_tool_names(registry),
        observations=tuple(_compact_observations(observations)),
    )


def _emit_synthesis_error(recorder: ChatTurnRecorder, synthesis: Any) -> None:
    error_message = str(getattr(synthesis, "error_message", "") or "").strip()
    if not error_message:
        return
    error_type = str(getattr(synthesis, "error_type", "") or "").strip()
    model_policy = str(getattr(synthesis, "model_policy", "") or "")
    model_profile = str(getattr(synthesis, "model_profile", "") or "")
    model_name = str(getattr(synthesis, "model_name", "") or "")
    attempt_errors = getattr(synthesis, "attempt_errors", ()) or ()
    recorder.emit(
        "llm_completed",
        item_type="agent_synthesis",
        item_name="final_synthesis",
        status="error",
        content=f"final synthesis LLM failed: {error_message}",
        payload={
            "phase": "synthesis",
            "model_policy": model_policy,
            "model_profile": model_profile,
            "model_name": model_name,
            "error_type": error_type,
            "error_message": error_message,
            "prompt_chars": int(getattr(synthesis, "prompt_chars", 0) or 0),
            "prompt_budget_chars": int(getattr(synthesis, "prompt_budget_chars", 0) or 0),
            "compact_mode": str(getattr(synthesis, "compact_mode", "") or ""),
            "retry_count": int(getattr(synthesis, "retry_count", 0) or 0),
            "base_url_class": str(getattr(synthesis, "base_url_class", "") or ""),
            "attempt_errors": [dict(item) for item in attempt_errors if isinstance(item, dict)],
            "fallback": "local_summary",
        },
    )


def run_plan_mode_once(
    message: str,
    *,
    settings: Settings,
    policy: AgentExecutionPolicy | None = None,
    reference_context: Any | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
) -> str:
    text = str(message or "").strip()
    if not text:
        raise ValueError("plan mode message is required")
    settings = settings or load_settings()
    policy = policy or AgentExecutionPolicy()
    registry = build_default_tool_registry()
    run_spec = _conversation_run_spec(text, policy=policy, registry=registry, reference_context=reference_context)
    allowed_tools = _plan_mode_allowed_tools(registry)
    if llm_factory is None:
        return _fallback_plan_mode_result(text, allowed_tools=allowed_tools)
    try:
        llm = _make_conversation_llm(llm_factory, settings)
        response = llm.chat(
            _plan_mode_messages(
                message=text,
                run_spec=run_spec,
                registry=registry,
                allowed_tools=allowed_tools,
                reference_context=reference_context,
            ),
            timeout=getattr(settings, "llm_timeout_seconds", None),
        )
        content = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        return _fallback_plan_mode_result(text, allowed_tools=allowed_tools, note=f"Plan mode LLM unavailable: {exc}")
    if not content:
        return _fallback_plan_mode_result(text, allowed_tools=allowed_tools, note="Plan mode LLM returned empty content.")
    return _normalize_plan_mode_result(content, objective=text)


def format_plan_mode_result(
    message: str,
    *,
    settings: Settings,
    policy: AgentExecutionPolicy | None = None,
    reference_context: Any | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
) -> str:
    return run_plan_mode_once(
        message,
        settings=settings,
        policy=policy,
        reference_context=reference_context,
        llm_factory=llm_factory,
    )


def format_conversation_plan(
    message: str,
    *,
    settings: Settings,
    policy: AgentExecutionPolicy | None = None,
    reference_context: Any | None = None,
) -> str:
    """Backward-compatible wrapper for the legacy public name."""

    return format_plan_mode_result(message, settings=settings, policy=policy, reference_context=reference_context)


def _is_ambiguous_stock_analysis_request(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if extract_stock_symbols(text):
        return False
    if any(term in text for term in ("大盘", "市场", "A股", "指数", "板块", "行业", "概念")):
        return False
    has_analysis = any(term in text for term in ("分析", "看看", "评价", "判断"))
    has_stock_shape = any(term in text for term in ("走势", "技术面", "买点", "卖点", "支撑", "压力"))
    return has_analysis and has_stock_shape


def _resolved_stock_mentions_context(message: str, *, settings: Settings) -> tuple[dict[str, str], ...]:
    text = str(message or "").strip()
    if not text:
        return ()
    try:
        stock_basic = load_stock_basic_frame(settings)
    except Exception:
        stock_basic = None
    try:
        symbols = resolve_stock_mentions(text, stock_basic) if _has_stock_basic_rows(stock_basic) else extract_stock_symbols(text)
    except Exception:
        symbols = extract_stock_symbols(text)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        symbol = normalize_ts_code(str(raw_symbol or "").strip())
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        name = _stock_basic_name_for_symbol(stock_basic, symbol)
        item = {"ts_code": symbol}
        if name:
            item["name"] = name
        rows.append(item)
        if len(rows) >= 8:
            break
    return tuple(rows)


def _has_stock_basic_rows(stock_basic: Any) -> bool:
    try:
        return bool(stock_basic is not None and not stock_basic.empty and "ts_code" in stock_basic.columns)
    except Exception:
        return False


def _stock_basic_name_for_symbol(stock_basic: Any, symbol: str) -> str:
    if not _has_stock_basic_rows(stock_basic) or "name" not in stock_basic.columns:
        return ""
    normalized = normalize_ts_code(str(symbol or "").strip())
    if not normalized:
        return ""
    try:
        data = stock_basic.copy()
        data["__ts_code"] = data["ts_code"].fillna("").astype(str).map(normalize_ts_code)
        matched = data[data["__ts_code"] == normalized]
        if matched.empty and "symbol" in data.columns:
            matched = data[data["symbol"].fillna("").astype(str) == normalized[:6]]
        if matched.empty:
            return ""
        return str(matched.iloc[0].get("name") or "").strip()
    except Exception:
        return ""


def _registry_tool_names(registry: Any) -> tuple[str, ...]:
    try:
        names = registry.names() if hasattr(registry, "names") else ()
    except Exception:
        names = ()
    return tuple(str(name) for name in names if str(name or "").strip())


def _plan_mode_allowed_tools(registry: Any) -> tuple[str, ...]:
    names = _registry_tool_names(registry)
    allowed: list[str] = []
    for name in names:
        spec = registry.get(name) if hasattr(registry, "get") else None
        side_effect = str(getattr(spec, "side_effect", "") or "readonly")
        requires_confirmation = bool(getattr(spec, "requires_confirmation", False))
        if side_effect in PLAN_MODE_FORBIDDEN_SIDE_EFFECTS or requires_confirmation:
            continue
        allowed.append(name)
    return tuple(allowed)


def _plan_mode_messages(
    *,
    message: str,
    run_spec: ConversationRunSpec,
    registry: Any,
    allowed_tools: tuple[str, ...],
    reference_context: Any | None,
) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 SATS Plan mode。只做非执行规划，不调用工具、不写库、不写文件、不下单、不启动命令。"
                "你可以基于用户输入、reference_context、可用工具摘要和已知约束，输出可交付计划或澄清问题。"
                "禁止把计划写成已执行结果；禁止编造行情、价格、K线、财务字段或 provider 数据。"
                "如果需要真实 A 股数据，计划中必须说明执行阶段应先通过注册 SATS 工具获取；"
                "不确定 A 股数据接口时，应建议先 data.astock_catalog，再 data.astock_fetch。"
                "如果信息不足以形成可执行计划，输出澄清问题，不要编造步骤。"
                "固定输出 Markdown 结构：# SATS Plan Mode、## 目标、## 当前判断、## 建议步骤、## 测试/验收、## 假设。"
                "如需澄清，额外包含 ## 需要澄清。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户输入：{message}\n"
                f"run_spec={json.dumps(run_spec.to_dict(), ensure_ascii=False, default=str)}\n"
                f"reference_context={json.dumps(_reference_context_payload(reference_context), ensure_ascii=False, default=str)}\n"
                f"允许在计划中引用的只读/非执行工具名={json.dumps(list(allowed_tools), ensure_ascii=False)}\n"
                f"完整工具摘要（用于理解能力，不代表可以执行）={_registry_context(registry)}"
            ),
        },
    ]


def _fallback_plan_mode_result(message: str, *, allowed_tools: tuple[str, ...], note: str = "") -> str:
    tool_hint = "、".join(allowed_tools[:8]) if allowed_tools else "当前未发现只读工具"
    lines = [
        "# SATS Plan Mode",
        "",
        "## 目标",
        f"- {message}",
        "",
        "## 当前判断",
        "- Plan mode 只生成计划，不执行工具、不写入数据库、不写报告、不下单。",
        "- 默认执行入口应由 conversation loop 逐轮选择动作并交给 runtime 守卫。",
        "",
        "## 建议步骤",
        "- 明确需要的股票、日期、数据范围和输出形式。",
        "- 执行阶段先使用注册 SATS 工具获取真实上下文；不确定 A 股数据接口时先 catalog 再 fetch。",
        "- 涉及写库、命令、长任务或交易时，在执行阶段请求确认。",
        "",
        "## 测试/验收",
        "- 计划本身不产生副作用。",
        "- 后续执行结果必须能追溯到真实工具 observation。",
        "",
        "## 假设",
        f"- 可参考的只读工具包括：{tool_hint}。",
    ]
    if note:
        lines.extend(["", "## 需要澄清", f"- {note}"])
    return "\n".join(lines)


def _normalize_plan_mode_result(content: str, *, objective: str) -> str:
    text = str(content or "").strip()
    if not text:
        return _fallback_plan_mode_result(objective, allowed_tools=())
    if "# SATS Plan Mode" not in text:
        text = "# SATS Plan Mode\n\n" + text
    required = ("## 目标", "## 当前判断", "## 建议步骤", "## 测试/验收", "## 假设")
    missing = [section for section in required if section not in text]
    if missing:
        suffix = ["", ""] if not text.endswith("\n") else [""]
        for section in missing:
            suffix.extend([section, "- 待补充。", ""])
        text = text.rstrip() + "\n" + "\n".join(suffix).rstrip()
    return text


def _preseed_capability_observations(
    message: str,
    *,
    plan: AgentPlan,
    registry: Any,
    context: AgentToolContext,
    recorder: ChatTurnRecorder,
    observations: list[AgentObservation],
    artifacts: list[dict[str, Any]],
    data_names: list[str],
) -> None:
    if not _is_capability_inventory_request(message):
        return
    steps = [
        AgentStep(
            step_id="capability_catalog_summary",
            kind="tool",
            title="SATS 能力目录总览",
            tool_name="catalog.capabilities",
            arguments={"section": "summary", "limit": 50},
            side_effect="readonly",
        )
    ]
    if _is_skill_inventory_request(message):
        steps.append(
            AgentStep(
                step_id="capability_catalog_skills",
                kind="tool",
                title="SATS skills 首页",
                tool_name="catalog.capabilities",
                arguments={"section": "skills", "limit": 12},
                side_effect="readonly",
            )
        )
    for step in steps:
        _execute_conversation_tool_step(
            step,
            plan=plan,
            registry=registry,
            context=context,
            recorder=recorder,
            observations=observations,
            artifacts=artifacts,
            data_names=data_names,
        )


def _is_capability_inventory_request(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    lower = text.lower()
    has_subject = any(term in lower or term in text for term in CAPABILITY_REQUEST_TERMS)
    if not has_subject:
        return False
    return any(term in lower or term in text for term in CAPABILITY_INVENTORY_TERMS)


def _is_skill_inventory_request(message: str) -> bool:
    text = str(message or "").strip()
    lower = text.lower()
    return any(term in lower or term in text for term in ("skill", "skills", "技能"))


def _run_conversation_loop(
    *,
    message: str,
    plan: AgentPlan,
    registry: Any,
    context: AgentToolContext,
    recorder: ChatTurnRecorder,
    observations: list[AgentObservation],
    artifacts: list[dict[str, Any]],
    data_names: list[str],
    settings: Settings,
    llm_factory: Callable[..., Any],
    reference_context: Any | None = None,
) -> _LoopOutcome:
    llm = _make_conversation_llm(llm_factory, settings)
    model_meta = _conversation_model_meta(settings, llm=llm)
    repeated_errors: dict[str, int] = {}
    max_iterations = max(1, int(getattr(context.policy, "max_iterations", 6) or 6))
    resolved_stock_mentions = _resolved_stock_mentions_context(message, settings=settings)

    for iteration in range(1, max_iterations + 1):
        iteration_key = f"iteration_{iteration}"
        recorder.emit(
            "runtime_iteration_started",
            item_type="conversation_action",
            item_name=iteration_key,
            status="running",
            content=f"第 {iteration} 轮：根据已获取数据选择工具、澄清或总结",
            payload={"iteration": iteration, "max_iterations": max_iterations, "title": "决定下一步"},
        )
        try:
            response = llm.chat(
                _conversation_action_messages(
                    message=message,
                    plan=plan,
                    registry=registry,
                    policy=context.policy,
                    observations=tuple(observations),
                    reference_context=reference_context,
                    resolved_stock_mentions=resolved_stock_mentions,
                ),
                timeout=getattr(settings, "llm_timeout_seconds", None),
            )
            raw_content = str(getattr(response, "content", "") or "").strip()
        except Exception as exc:
            raw_content = ""
            error = f"conversation action LLM failed: {exc}"
            if _record_loop_error(
                error,
                observations=observations,
                recorder=recorder,
                repeated_errors=repeated_errors,
                iteration=iteration,
            ):
                return _LoopOutcome(final_content=error, **model_meta)
            continue

        action, error = _parse_conversation_action(raw_content)
        if error:
            if _record_loop_error(
                error,
                observations=observations,
                recorder=recorder,
                repeated_errors=repeated_errors,
                iteration=iteration,
                raw_content=raw_content,
            ):
                return _LoopOutcome(final_content=error, **model_meta)
            continue

        recorder.emit(
            "llm_completed",
            item_type="conversation_action",
            item_name=iteration_key,
            status="done",
            content=_conversation_action_detail(action),
            payload={"action": _conversation_action_payload(action), "iteration": iteration},
        )

        repeated_stock_basic_issue = _repeated_stock_basic_lookup_issue(action, tuple(observations))
        if repeated_stock_basic_issue:
            if _record_action_blocked(
                repeated_stock_basic_issue,
                action=action,
                observations=observations,
                recorder=recorder,
                repeated_errors=repeated_errors,
                iteration=iteration,
            ):
                return _LoopOutcome(final_content=repeated_stock_basic_issue, **model_meta)
            continue

        if action.action == "final_answer":
            issue = _final_answer_blocking_issue(message=message, plan=plan, observations=tuple(observations), content=action.content)
            if issue:
                if _record_action_blocked(
                    issue,
                    action=action,
                    observations=observations,
                    recorder=recorder,
                    repeated_errors=repeated_errors,
                    iteration=iteration,
                ):
                    return _LoopOutcome(final_content=issue, **model_meta)
                continue
            content = action.content or _fallback_content(observations) or "无响应"
            synthesize_final = _loop_final_needs_synthesis(tuple(observations))
            observations.append(
                AgentObservation(
                    step_id=f"loop_{iteration}_final",
                    kind="final",
                    status="done",
                    content=content,
                    payload={"action": _conversation_action_payload(action)},
                )
            )
            return _LoopOutcome(final_content=content, synthesize_final=synthesize_final, **model_meta)

        if action.action == "ask_clarification":
            step = _step_from_action(action, iteration=iteration, registry=registry)
            issue = action.question or "需要补充信息后继续。"
            missing_fields = action.missing_fields or tuple(_issue_missing_fields(issue))
            clarification_id = _require_clarification(
                step,
                plan=plan,
                context=context,
                recorder=recorder,
                issue=issue,
                missing_fields=missing_fields,
            )
            observation = _argument_guard_observation(step, issue, clarification_id=clarification_id, missing_fields=missing_fields)
            observations.append(observation)
            recorder.emit(
                "clarification_required",
                item_type="conversation_action",
                item_name=step.tool_name or step.step_id,
                status="done",
                content=issue,
                payload=observation.payload,
            )
            return _LoopOutcome(
                interruption=_PlanInterruption(
                    _clarification_message(step, issue, clarification_id=clarification_id),
                    requires_clarification=True,
                    action_id=clarification_id,
                    clarification_prompt=_clarification_prompt(step, issue),
                    missing_fields=missing_fields,
                ),
                **model_meta,
            )

        step = _step_from_action(action, iteration=iteration, registry=registry)
        if action.action == "request_confirmation":
            interruption = _execute_conversation_tool_step(
                step,
                plan=plan,
                registry=registry,
                context=context,
                recorder=recorder,
                observations=observations,
                artifacts=artifacts,
                data_names=data_names,
            )
            if interruption is not None:
                return _LoopOutcome(interruption=interruption, **model_meta)
            continue

        interruption = _execute_conversation_tool_step(
            step,
            plan=plan,
            registry=registry,
            context=context,
            recorder=recorder,
            observations=observations,
            artifacts=artifacts,
            data_names=data_names,
        )
        if interruption is not None:
            return _LoopOutcome(interruption=interruption, **model_meta)

        if observations and observations[-1].status == "error":
            signature = _loop_error_signature(observations[-1].content or observations[-1].step_id)
            repeated_errors[signature] = repeated_errors.get(signature, 0) + 1
            if repeated_errors[signature] >= MAX_INVALID_ACTIONS:
                content = f"conversation loop stopped after repeated error: {observations[-1].content}"
                observations.append(AgentObservation(step_id="repeated_error", kind="runtime", status="error", content=content))
                return _LoopOutcome(final_content=content, **model_meta)

    content = f"conversation loop reached max_iterations={max_iterations}"
    observations.append(AgentObservation(step_id="max_iterations", kind="runtime", status="error", content=content))
    return _LoopOutcome(**model_meta)


def _conversation_action_messages(
    *,
    message: str,
    plan: AgentPlan,
    registry: Any,
    policy: AgentExecutionPolicy,
    observations: tuple[AgentObservation, ...],
    reference_context: Any | None,
    resolved_stock_mentions: tuple[dict[str, str], ...] = (),
) -> list[dict[str, Any]]:
    today = agent_today()
    yesterday = normalize_agent_date("yesterday", today=today)
    return [
        {
            "role": "system",
            "content": (
                "你是 SATS conversation agent loop。你只能输出一个 JSON 对象，不要输出 Markdown。"
                "每次只选择一个动作：call_tool、ask_clarification、request_confirmation、final_answer。"
                "所有真实 A 股行情、财务、K线、报价、资金流和 provider 数据必须通过 SATS 注册工具获取；"
                "不确定 A 股数据接口时，先调用 data.astock_catalog，再调用 data.astock_fetch。"
                "不要编造股票代码、价格、成交量、K线、财务字段或第三方 provider 方法名。"
                "普通研究工具只返回数据和证据，不直接写 reports；只有用户明确要求保存/导出时才调用 research.write_report。"
                "非交易工具（readonly、write_artifact、write_db、long_running、command）可以直接 call_tool；"
                "只有 live_trade 或 requires_trade_permission 的实盘交易工具必须 request_confirmation。"
                "如果缺少必要股票、日期、搜索词或 operation，输出 ask_clarification。"
                "用户询问支持哪些 skills、工具、能力或可做什么时，应基于 catalog.capabilities 和 chat.list_skills 的 observation "
                "解释本地 skills 方法论层与 Agent tools 执行层，不能只输出“支持的 skills 包括”这类空泛句子。"
                "大盘、市场或指数走势/预测类问题，final_answer 前必须先 call_tool research.market_context；"
                "用户要求下周或未来一周时，research.market_context 参数必须包含 horizons=[\"next_week\"]。"
                f"当前日期是 {today}（Asia/Shanghai）；日期字段禁止输出 today、yesterday、current 等自然语言值，"
                f"昨天/yesterday 必须写成 {yesterday}，今天/today/current 必须写成 {today}。"
                "用户说今天、昨日、明天、未来几天、过去几天等相对时间时，可以直接调用工具；"
                "runtime 会在执行前兜底归一化为 trade_date 或 horizons，明确日期仍优先。"
                "预测范围继续使用 horizons 的 today/tomorrow/day_after_tomorrow/next_week 枚举，不把 horizons 当作日期字段改写。"
                "调用 analysis.python_program 时，允许导入已安装模块，但禁止文件、进程、网络和动态执行等危险行为；"
                "优先使用 resolver、context 和安全 builtins。"
                "读取前序工具结果时，优先使用 context['observations_by_step'][step_id]['payload']['result']['payload']，"
                "不要为了读取前序结果而解析 observation content 字符串；例如从 market_context observation 的结构化 payload 读取 hot_sectors。"
                "如果 resolved_stock_mentions 提供股票名称到 ts_code 的映射，调用需要 symbols 的工具时必须直接使用这些 ts_code；"
                "不要为同一股票名称重复调用 data.stock_basic。"
                "data.stock_basic 只用于名称未解析、需要消歧或专门查询股票基础信息；"
                "一旦 observation.stock_matches 出现唯一匹配，下一步必须复用其中 ts_code 继续研究。"
                "如果已有 observations 足以回答，输出 final_answer。"
            ),
        },
        {
            "role": "user",
            "content": (
                "动作 JSON 协议：\n"
                '{"action":"call_tool","tool_name":"...","arguments":{},"title":"..."}\n'
                '{"action":"request_confirmation","tool_name":"...","arguments":{},"title":"..."}\n'
                '{"action":"ask_clarification","question":"...","missing_fields":["symbols"]}\n'
                '{"action":"final_answer","content":"..."}\n\n'
                f"用户输入：{message}\n"
                f"运行规格（兼容 plan 字段，不含预生成工具步骤）：{json.dumps(plan.to_dict(), ensure_ascii=False, default=str)}\n"
                f"policy={json.dumps(policy.to_dict(), ensure_ascii=False, default=str)}\n"
                f"reference_context={json.dumps(_reference_context_payload(reference_context), ensure_ascii=False, default=str)}\n"
                f"resolved_stock_mentions={json.dumps(list(resolved_stock_mentions), ensure_ascii=False, default=str)}\n"
                f"可用工具={_registry_context(registry)}\n"
                f"observations={json.dumps(_compact_observations(observations), ensure_ascii=False, default=str)}"
            ),
        },
    ]


def _parse_conversation_action(content: str) -> tuple[_ConversationAction | None, str]:
    payload = extract_json_object(content)
    if not isinstance(payload, dict):
        return None, "conversation action must be a JSON object"
    action = str(payload.get("action") or payload.get("type") or "").strip()
    if action not in CONVERSATION_ACTIONS:
        return None, f"conversation action must be one of {', '.join(sorted(CONVERSATION_ACTIONS))}"
    arguments = payload.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return None, "conversation action arguments must be an object"
    missing_fields = payload.get("missing_fields")
    if isinstance(missing_fields, str):
        missing = tuple(item.strip() for item in missing_fields.split(",") if item.strip())
    elif isinstance(missing_fields, (list, tuple)):
        missing = tuple(str(item).strip() for item in missing_fields if str(item).strip())
    else:
        missing = ()
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
    if action in {"call_tool", "request_confirmation"} and not tool_name:
        return None, f"{action} requires tool_name"
    return (
        _ConversationAction(
            action=action,
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            title=str(payload.get("title") or ""),
            content=str(payload.get("content") or payload.get("answer") or ""),
            question=str(payload.get("question") or payload.get("content") or ""),
            missing_fields=missing,
            side_effect=str(payload.get("side_effect") or ""),
        ),
        "",
    )


def _step_from_action(action: _ConversationAction, *, iteration: int, registry: Any) -> AgentStep:
    spec = registry.get(action.tool_name) if action.tool_name else None
    side_effect = str(action.side_effect or getattr(spec, "side_effect", "") or "readonly")
    safe_name = (action.tool_name or action.action or "conversation").replace(".", "_").replace("-", "_")
    return AgentStep(
        step_id=f"loop_{iteration}_{safe_name}",
        kind="tool",
        title=action.title or action.tool_name or action.action,
        tool_name=action.tool_name,
        arguments=dict(action.arguments or {}),
        side_effect=side_effect,
    )


def _execute_conversation_tool_step(
    step: AgentStep,
    *,
    plan: AgentPlan,
    registry: Any,
    context: AgentToolContext,
    recorder: ChatTurnRecorder,
    observations: list[AgentObservation],
    artifacts: list[dict[str, Any]],
    data_names: list[str],
    force_confirmation: bool = False,
) -> _PlanInterruption | None:
    spec = registry.get(step.tool_name)
    if spec is None:
        content = f"unknown agent tool: {step.tool_name}"
        observation = AgentObservation(
            step_id=step.step_id,
            kind="tool",
            status="error",
            content=content,
            payload={"tool_name": step.tool_name, "arguments": step.arguments},
        )
        observations.append(observation)
        recorder.emit("tool_completed", item_type="conversation_tool", item_name=step.tool_name, status="error", content=content, payload=observation.payload)
        return None

    step, normalization_issue, normalization_meta = _normalize_conversation_tool_step(step, message=context.message)
    if normalization_issue:
        missing_fields = ("trade_date",)
        clarification_id = _require_clarification(
            step,
            plan=plan,
            context=context,
            recorder=recorder,
            issue=normalization_issue,
            missing_fields=missing_fields,
        )
        prompt = _clarification_prompt(step, normalization_issue)
        observation = _argument_guard_observation(step, normalization_issue, clarification_id=clarification_id, missing_fields=missing_fields)
        observations.append(observation)
        recorder.emit(
            "clarification_required",
            item_type="conversation_tool",
            item_name=step.tool_name,
            status="done",
            content=normalization_issue,
            payload=observation.payload,
        )
        return _PlanInterruption(
            _clarification_message(step, normalization_issue, clarification_id=clarification_id),
            requires_clarification=True,
            action_id=clarification_id,
            clarification_prompt=prompt,
            missing_fields=missing_fields,
        )
    if normalization_meta:
        recorder.emit(
            "tool_arguments_normalized",
            item_type="conversation_tool",
            item_name=step.tool_name,
            status="done",
            content="已根据用户自然语言时间补齐工具参数",
            payload={"tool_name": step.tool_name, "arguments": step.arguments, "normalization": normalization_meta},
        )

    issue = _conversation_argument_issue(step, spec=spec, message=context.message, observations=tuple(observations))
    if issue:
        missing_fields = tuple(_issue_missing_fields(issue))
        clarification_id = _require_clarification(
            step,
            plan=plan,
            context=context,
            recorder=recorder,
            issue=issue,
            missing_fields=missing_fields,
        )
        prompt = _clarification_prompt(step, issue)
        observation = _argument_guard_observation(step, issue, clarification_id=clarification_id, missing_fields=missing_fields)
        observations.append(observation)
        recorder.emit(
            "clarification_required",
            item_type="conversation_tool",
            item_name=step.tool_name,
            status="done",
            content=issue,
            payload=observation.payload,
        )
        return _PlanInterruption(
            _clarification_message(step, issue, clarification_id=clarification_id),
            requires_clarification=True,
            action_id=clarification_id,
            clarification_prompt=prompt,
            missing_fields=missing_fields,
        )

    side_effect = str(step.side_effect or getattr(spec, "side_effect", "") or "readonly")
    requires_confirmation = _conversation_step_requires_confirmation(
        step,
        spec=spec,
        side_effect=side_effect,
        force_confirmation=force_confirmation,
    )
    if requires_confirmation:
        action_id = _require_step_confirmation(step, plan=plan, context=context, recorder=recorder, side_effect=side_effect)
        observations.append(_approval_observation(step, action_id))
        return _PlanInterruption(_pending_message(step, action_id), requires_confirmation=True, action_id=action_id)

    item_id = recorder.start_item("conversation_tool", step.tool_name, input_payload=step.to_dict())
    step_started = time.monotonic()
    recorder.emit("tool_started", item_type="conversation_tool", item_name=step.tool_name, status="running", payload=step.to_dict())
    result = registry.execute(step.tool_name, step.arguments, context.with_observations(tuple(observations)))
    data_names.extend(result.data_names)
    artifacts.extend(result.artifacts)
    observation = AgentObservation(
        step_id=step.step_id,
        kind="tool",
        status=result.status,
        content=result.content,
        payload={"tool_name": step.tool_name, "arguments": step.arguments, "result": result.to_dict()},
    )
    observations.append(observation)
    recorder.complete_item(
        item_id,
        status="done" if result.status == "done" else "error",
        output_payload=observation.to_dict(),
        artifact_paths=[str(item.get("path") or "") for item in result.artifacts],
        duration_seconds=max(0.0, time.monotonic() - step_started),
    )
    recorder.emit(
        "tool_completed",
        item_type="conversation_tool",
        item_name=step.tool_name,
        status="done" if result.status == "done" else "error",
        content=result.content,
        payload=observation.payload,
        duration_seconds=max(0.0, time.monotonic() - step_started),
    )
    return None


def _conversation_action_detail(action: _ConversationAction) -> str:
    if action.action == "call_tool":
        args = _action_arguments_summary(action.arguments)
        return f"决定调用 {action.tool_name}{(' ' + args) if args else ''}"
    if action.action == "request_confirmation":
        args = _action_arguments_summary(action.arguments)
        return f"请求确认 {action.tool_name}{(' ' + args) if args else ''}"
    if action.action == "ask_clarification":
        return action.question or "需要补充信息后继续"
    if action.action == "final_answer":
        return "进入最终分析合成"
    return action.action


def _action_arguments_summary(arguments: dict[str, Any]) -> str:
    if not isinstance(arguments, dict) or not arguments:
        return ""
    preferred = ("query", "symbols", "trade_date", "horizon", "horizons", "dimensions", "indices", "kind")
    keys = [key for key in preferred if key in arguments]
    keys.extend(key for key in arguments if key not in keys)
    parts: list[str] = []
    for key in keys[:4]:
        value = arguments.get(key)
        if isinstance(value, (list, tuple)):
            text = "[" + ",".join(str(item) for item in list(value)[:4]) + (",..." if len(value) > 4 else "") + "]"
        elif isinstance(value, dict):
            text = "{" + ",".join(str(item) for item in list(value)[:4]) + ("..." if len(value) > 4 else "") + "}"
        else:
            text = str(value or "").strip()
        if text:
            parts.append(f"{key}={text}")
    return " ".join(parts)


def _conversation_step_requires_confirmation(
    step: AgentStep,
    *,
    spec: Any,
    side_effect: str,
    force_confirmation: bool = False,
) -> bool:
    return bool(
        force_confirmation
        or side_effect in CONFIRM_SIDE_EFFECTS
        or getattr(spec, "requires_trade_permission", False)
    )


def _loop_final_needs_synthesis(observations: tuple[AgentObservation, ...]) -> bool:
    for item in observations:
        tool_name = str(item.payload.get("tool_name") or "")
        if tool_name.startswith(("research.", "data.", "factor.", "trade.", "web.", "catalog.")):
            return True
        if tool_name in {"chat.list_skills", "chat.load_skill"}:
            return True
        if item.kind in {"python", "trade"}:
            return True
    return False


def _final_answer_blocking_issue(
    *,
    message: str,
    plan: AgentPlan,
    observations: tuple[AgentObservation, ...],
    content: str = "",
) -> str:
    if _requires_market_context_before_final(message, plan):
        market_observation = _successful_market_context_observation(observations)
        if market_observation is None:
            return "final_answer blocked: 大盘/市场/指数走势或预测类回答必须先调用 research.market_context 获取真实上下文。"
        if _requires_next_week_horizon(message) and "next_week" not in _market_context_horizons(market_observation):
            return "final_answer blocked: 下周/未来一周预测必须先调用 research.market_context 且 requested_horizons 包含 next_week。"
    if _requires_python_program_before_final(plan) and _successful_python_program_observation(observations) is None:
        return "final_answer blocked: 当前计划需要先调用 analysis.python_program 完成有界只读计算。"
    fabricated_issue = _fabricated_market_data_issue(message=message, content=content, observations=observations)
    if fabricated_issue:
        return fabricated_issue
    return ""


def _requires_market_context_before_final(message: str, plan: AgentPlan) -> bool:
    if any(step.kind == "tool" and step.tool_name == "research.market_context" for step in plan.steps):
        return True
    text = str(message or "")
    has_market_subject = any(term in text for term in ("大盘", "市场", "指数", "上证", "深证", "创业板", "沪深"))
    has_analysis_intent = any(term in text for term in ("分析", "走势", "预测", "判断", "怎么看", "今天", "本周", "下周", "未来一周", "情况"))
    return has_market_subject and has_analysis_intent


def _requires_next_week_horizon(message: str) -> bool:
    text = str(message or "")
    return "下周" in text or "未来一周" in text


def _successful_market_context_observation(observations: tuple[AgentObservation, ...]) -> AgentObservation | None:
    for item in reversed(observations):
        if item.kind == "tool" and item.status == "done" and str(item.payload.get("tool_name") or "") == "research.market_context":
            return item
    return None


def _requires_python_program_before_final(plan: AgentPlan) -> bool:
    return any(step.kind == "tool" and step.tool_name == "analysis.python_program" for step in plan.steps)


def _successful_python_program_observation(observations: tuple[AgentObservation, ...]) -> AgentObservation | None:
    for item in reversed(observations):
        if item.kind == "tool" and item.status == "done" and str(item.payload.get("tool_name") or "") == "analysis.python_program":
            return item
    return None


def _fabricated_market_data_issue(*, message: str, content: str, observations: tuple[AgentObservation, ...]) -> str:
    if _has_successful_data_observation(observations):
        return ""
    text = str(content or "")
    if not text:
        return ""
    has_concrete_number = bool(re.search(r"\d+(?:\.\d+)?\s*(?:元|%|万|亿|倍|手|股|点)?", text))
    if not has_concrete_number:
        return ""
    stockish = bool(extract_stock_symbols(text) or extract_stock_symbols(message))
    marketish = any(term in text for term in ("上证", "深证", "创业板", "沪深", "大盘", "指数", "A股"))
    fieldish = any(
        term in text
        for term in (
            "最新价",
            "现价",
            "收盘价",
            "开盘价",
            "最高价",
            "最低价",
            "涨跌幅",
            "成交量",
            "成交额",
            "换手率",
            "市盈率",
            "PE",
            "PB",
            "K线",
            "均线",
            "MA5",
            "MA10",
            "MA20",
            "营收",
            "净利润",
            "每股收益",
        )
    )
    if fieldish and (stockish or marketish):
        return "final_answer blocked: 未观察到真实行情/财务工具结果，不能输出具体价格、K线、成交量、涨跌幅或财务字段。"
    return ""


def _has_successful_data_observation(observations: tuple[AgentObservation, ...]) -> bool:
    for item in observations:
        if item.kind != "tool" or item.status != "done":
            continue
        tool_name = str(item.payload.get("tool_name") or "")
        if tool_name.startswith(("research.", "data.", "factor.", "web.")):
            return True
    return False


def _market_context_horizons(observation: AgentObservation) -> set[str]:
    payload = observation.payload if isinstance(observation.payload, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    result_payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    market_context = result_payload.get("market_context") if isinstance(result_payload.get("market_context"), dict) else result_payload
    candidates = [
        market_context.get("requested_horizons") if isinstance(market_context, dict) else None,
        result_payload.get("requested_horizons"),
        payload.get("arguments", {}).get("horizons") if isinstance(payload.get("arguments"), dict) else None,
    ]
    horizons: list[str] = []
    for value in candidates:
        if isinstance(value, str):
            horizons.append(value)
        elif isinstance(value, (list, tuple, set)):
            horizons.extend(str(item) for item in value)
    return {item.strip() for item in horizons if item and item.strip()}


def _record_action_blocked(
    issue: str,
    *,
    action: _ConversationAction,
    observations: list[AgentObservation],
    recorder: ChatTurnRecorder,
    repeated_errors: dict[str, int],
    iteration: int,
) -> bool:
    signature = _loop_error_signature(issue)
    repeated_errors[signature] = repeated_errors.get(signature, 0) + 1
    observation = AgentObservation(
        step_id=f"loop_{iteration}_action_blocked",
        kind="runtime",
        status="error",
        content=issue,
        payload={"action": _conversation_action_payload(action), "iteration": iteration, "repeated": repeated_errors[signature]},
    )
    observations.append(observation)
    recorder.emit(
        "action_blocked",
        item_type="conversation_action",
        item_name=f"iteration_{iteration}",
        status="error",
        content=issue,
        payload=observation.payload,
    )
    return repeated_errors[signature] >= MAX_INVALID_ACTIONS


def _record_loop_error(
    error: str,
    *,
    observations: list[AgentObservation],
    recorder: ChatTurnRecorder,
    repeated_errors: dict[str, int],
    iteration: int,
    raw_content: str = "",
) -> bool:
    signature = _loop_error_signature(error)
    repeated_errors[signature] = repeated_errors.get(signature, 0) + 1
    observation = AgentObservation(
        step_id=f"loop_{iteration}_invalid_action",
        kind="runtime",
        status="error",
        content=error,
        payload={"raw_content": raw_content, "iteration": iteration, "repeated": repeated_errors[signature]},
    )
    observations.append(observation)
    recorder.emit(
        "llm_completed",
        item_type="conversation_action",
        item_name=f"iteration_{iteration}",
        status="error",
        content=error,
        payload=observation.payload,
    )
    return repeated_errors[signature] >= MAX_INVALID_ACTIONS


def _loop_error_signature(value: str) -> str:
    return str(value or "").strip().lower()[:240]


def _conversation_action_payload(action: _ConversationAction) -> dict[str, Any]:
    return {
        "action": action.action,
        "tool_name": action.tool_name,
        "arguments": action.arguments,
        "title": action.title,
        "content": action.content,
        "question": action.question,
        "missing_fields": list(action.missing_fields),
        "side_effect": action.side_effect,
    }


def _repeated_stock_basic_lookup_issue(action: _ConversationAction, observations: tuple[AgentObservation, ...]) -> str:
    if action.action not in {"call_tool", "request_confirmation"} or action.tool_name != "data.stock_basic":
        return ""
    current_key = _stock_basic_lookup_key(action.arguments)
    if not current_key:
        return ""
    for item in reversed(observations):
        if item.kind != "tool" or item.status != "done":
            continue
        payload = item.payload if isinstance(item.payload, dict) else {}
        if payload.get("tool_name") != "data.stock_basic":
            continue
        if _stock_basic_lookup_key(payload.get("arguments")) != current_key:
            continue
        matches = _stock_basic_matches_from_result(payload)
        if not matches:
            continue
        first = matches[0]
        label = str(first.get("name") or current_key).strip()
        symbol = str(first.get("ts_code") or "").strip()
        if not symbol:
            continue
        return (
            f"data.stock_basic 已经查询过“{label}”并命中 {symbol}。"
            f"下一步请使用 symbols=[\"{symbol}\"] 调用研究或行情工具，不要重复查询同一股票名称。"
        )
    return ""


def _stock_basic_lookup_key(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in ("name", "query"):
        value = str(arguments.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    symbols = arguments.get("symbols")
    if isinstance(symbols, (list, tuple)):
        normalized = [normalize_ts_code(str(item or "").strip()) for item in symbols]
        normalized = [item for item in normalized if item]
        if normalized:
            return "symbols:" + ",".join(normalized)
    return ""


def _make_conversation_llm(llm_factory: Callable[..., Any], settings: Any) -> Any:
    return build_standard_llm(
        llm_factory,
        model_name=str(getattr(settings, "openai_model", "") or "LLM"),
        timeout_seconds=getattr(settings, "llm_timeout_seconds", None),
    )


def _conversation_model_meta(settings: Any, llm: Any | None = None) -> dict[str, str]:
    return {
        "model_policy": "standard",
        "model_profile": str(getattr(llm, "profile", "") if llm is not None else "") or "default",
        "model_name": str(getattr(llm, "model_name", "") if llm is not None else "") or str(getattr(settings, "openai_model", "") or "LLM"),
    }


def _registry_context(registry: Any) -> str:
    try:
        if hasattr(registry, "planner_context"):
            return str(registry.planner_context())
        if hasattr(registry, "summaries"):
            return json.dumps(registry.summaries(), ensure_ascii=False, default=str)
        if hasattr(registry, "names"):
            return json.dumps([{"name": name} for name in registry.names()], ensure_ascii=False)
    except Exception:
        pass
    return "[]"


def _compact_observations(observations: tuple[AgentObservation, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in observations[-8:]:
        payload = item.payload if isinstance(item.payload, dict) else {}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        result_payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        row = {
            "step_id": item.step_id,
            "kind": item.kind,
            "status": item.status,
            "tool_name": payload.get("tool_name") or "",
            "content": _truncate_text(item.content or result.get("content") or "", 900),
            "payload_keys": sorted(str(key) for key in payload.keys())[:12],
            "result_status": result.get("status") or "",
            "result_payload_keys": sorted(str(key) for key in result_payload.keys())[:12],
        }
        stock_matches = _stock_basic_matches_from_result(payload)
        if stock_matches:
            row["stock_matches"] = stock_matches
        rows.append(row)
    return rows


def _stock_basic_matches_from_result(payload: dict[str, Any]) -> list[dict[str, str]]:
    if payload.get("tool_name") != "data.stock_basic":
        return []
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    result_payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    sample = result_payload.get("sample") if isinstance(result_payload.get("sample"), list) else []
    matches: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in sample:
        if not isinstance(row, dict):
            continue
        symbol = normalize_ts_code(str(row.get("ts_code") or row.get("symbol") or "").strip())
        name = str(row.get("name") or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        match = {"ts_code": symbol}
        if name:
            match["name"] = name
            match["display"] = f"{name}({symbol})"
        else:
            match["display"] = symbol
        matches.append(match)
        if len(matches) >= 6:
            break
    return matches


def _reference_context_payload(reference_context: Any | None) -> dict[str, Any]:
    if reference_context is None:
        return {}
    return {
        "data_name": str(getattr(reference_context, "data_name", "") or ""),
        "symbols": list(getattr(reference_context, "symbols", ()) or ()),
        "trade_date": str(getattr(reference_context, "trade_date", "") or ""),
        "source": str(getattr(reference_context, "source", "") or ""),
    }


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def confirm_pending_conversation_action(
    action_id: str,
    *,
    settings: Settings,
    store: ChatMemoryStore | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
) -> ConversationResult:
    store = store or ChatMemoryStore(settings.db_path)
    action = store.get_pending_action(action_id)
    if action is None:
        raise ValueError(f"未找到待确认动作 {action_id}")
    if str(action.get("action_type") or "") != CONVERSATION_ACTION_TYPE:
        raise ValueError(f"not a conversation pending action: {action.get('action_type')}")
    if action["status"] != "pending":
        raise ValueError(f"动作 {action_id} 当前状态为 {action['status']}，不能确认执行")
    payload = dict(action.get("payload") or {})
    step_payload = dict(payload.get("step") or {})
    plan_payload = dict(payload.get("plan") or {})
    message = str(payload.get("message") or "")
    session_id = str(action.get("session_id") or payload.get("session_id") or "conversation")
    turn_id = str(action.get("turn_id") or "")
    policy = _policy_from_payload(payload.get("policy"))
    context = _tool_context(
        settings=settings,
        store=store,
        policy=policy,
        session_id=session_id,
        turn_id=turn_id,
        message=message,
        llm_factory=llm_factory,
    )
    registry = build_default_tool_registry()
    step = _step_from_payload(step_payload)
    result = registry.execute(step.tool_name, step.arguments, context)
    store.update_pending_action(action_id, status="done" if result.status == "done" else "error", result=result.to_dict())
    plan = _plan_from_payload(plan_payload, fallback_message=message, step=step)
    content = result.content or ("已执行待确认动作。" if result.status == "done" else "待确认动作执行失败。")
    if result.status != "done":
        content = f"执行失败: {content}"
    return ConversationResult(
        content=content,
        plan=plan,
        observations=(
            AgentObservation(
                step_id=step.step_id,
                kind="tool",
                status=result.status,
                content=result.content,
                payload={"tool_name": step.tool_name, "result": result.to_dict(), "arguments": step.arguments},
            ),
        ),
        data_names=("Conversation", *result.data_names),
        tool_call_count=1,
        artifacts=result.artifacts,
        turn_id=turn_id,
        session_id=session_id,
        model_policy="none",
    )


def continue_conversation_after_clarification(
    clarification_id: str,
    answer: str,
    *,
    settings: Settings,
    store: ChatMemoryStore | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
    event_sink: ChatEventSink | None = None,
) -> ConversationResult:
    store = store or ChatMemoryStore(settings.db_path)
    action = store.get_pending_action(clarification_id)
    if action is None:
        raise ValueError(f"未找到待澄清问题 {clarification_id}")
    if str(action.get("action_type") or "") != CONVERSATION_CLARIFICATION_ACTION_TYPE:
        raise ValueError(f"not a conversation clarification: {action.get('action_type')}")
    if action["status"] != "pending":
        raise ValueError(f"澄清问题 {clarification_id} 当前状态为 {action['status']}，不能继续")
    clean_answer = str(answer or "").strip()
    if not clean_answer:
        raise ValueError("clarification answer is required")
    payload = dict(action.get("payload") or {})
    message = str(payload.get("message") or "")
    session_id = str(action.get("session_id") or payload.get("session_id") or "conversation")
    policy = _policy_from_payload(payload.get("policy"))
    merged_message = _merge_clarification_answer(
        message,
        clean_answer,
        missing_fields=tuple(str(item) for item in (payload.get("missing_fields") or ()) if str(item or "").strip()),
    )
    result = run_conversation_once(
        merged_message,
        settings=settings,
        policy=policy,
        session_id=session_id,
        llm_factory=llm_factory,
        event_sink=event_sink,
    )
    store.update_pending_action(
        clarification_id,
        status="done",
        result={"answer": clean_answer, "merged_message": merged_message, "result": result.to_dict()},
    )
    return result


def reject_pending_conversation_action(
    action_id: str,
    *,
    settings: Settings,
    store: ChatMemoryStore | None = None,
) -> ConversationResult:
    store = store or ChatMemoryStore(settings.db_path)
    action = store.get_pending_action(action_id)
    if action is None:
        raise ValueError(f"未找到待确认动作 {action_id}")
    action_type = str(action.get("action_type") or "")
    if action_type not in {CONVERSATION_ACTION_TYPE, CONVERSATION_CLARIFICATION_ACTION_TYPE}:
        raise ValueError(f"not a conversation pending action: {action.get('action_type')}")
    store.update_pending_action(action_id, status="rejected", result={"message": "用户拒绝执行"})
    if action_type == CONVERSATION_CLARIFICATION_ACTION_TYPE:
        message = f"已取消澄清问题 {action_id}: {action.get('title') or action.get('action_type')}"
    else:
        message = f"已拒绝待确认动作 {action_id}: {action.get('title') or action.get('action_type')}"
    return ConversationResult(
        content=message,
        plan=AgentPlan(objective=message),
        data_names=("Conversation", "待确认动作"),
        turn_id=str(action.get("turn_id") or ""),
        session_id=str(action.get("session_id") or "conversation"),
        model_policy="none",
    )


def _execute_plan_until_confirmation(
    plan: AgentPlan,
    *,
    registry: Any,
    context: AgentToolContext,
    recorder: ChatTurnRecorder,
    observations: list[AgentObservation],
    artifacts: list[dict[str, Any]],
    data_names: list[str],
) -> _PlanInterruption | None:
    for step in plan.steps:
        if step.kind == "final":
            break
        if step.kind in SUBAGENT_KINDS:
            _execute_readonly_subagent(
                step,
                registry=registry,
                context=context,
                recorder=recorder,
                observations=observations,
                artifacts=artifacts,
                data_names=data_names,
            )
            continue
        if step.kind != "tool":
            action_id = _require_step_confirmation(step, plan=plan, context=context, recorder=recorder)
            observations.append(_approval_observation(step, action_id))
            return _PlanInterruption(_pending_message(step, action_id), requires_confirmation=True, action_id=action_id)
        spec = registry.get(step.tool_name)
        issue = _conversation_argument_issue(step, spec=spec, message=context.message, observations=tuple(observations))
        if issue:
            missing_fields = tuple(_issue_missing_fields(issue))
            clarification_id = _require_clarification(
                step,
                plan=plan,
                context=context,
                recorder=recorder,
                issue=issue,
                missing_fields=missing_fields,
            )
            prompt = _clarification_prompt(step, issue)
            observation = _argument_guard_observation(step, issue, clarification_id=clarification_id, missing_fields=missing_fields)
            observations.append(observation)
            recorder.emit(
                "tool_argument_guard",
                item_type="conversation_tool",
                item_name=step.tool_name,
                status="blocked",
                content=issue,
                payload=observation.payload,
            )
            return _PlanInterruption(
                _clarification_message(step, issue, clarification_id=clarification_id),
                requires_clarification=True,
                action_id=clarification_id,
                clarification_prompt=prompt,
                missing_fields=missing_fields,
            )
        side_effect = str(step.side_effect or getattr(spec, "side_effect", "") or "readonly")
        requires_confirmation = _conversation_step_requires_confirmation(step, spec=spec, side_effect=side_effect)
        if requires_confirmation:
            action_id = _require_step_confirmation(step, plan=plan, context=context, recorder=recorder, side_effect=side_effect)
            observations.append(_approval_observation(step, action_id))
            return _PlanInterruption(_pending_message(step, action_id), requires_confirmation=True, action_id=action_id)
        item_id = recorder.start_item("conversation_tool", step.tool_name, input_payload=step.to_dict())
        step_started = time.monotonic()
        recorder.emit(
            "tool_started",
            item_type="conversation_tool",
            item_name=step.tool_name,
            status="running",
            payload=step.to_dict(),
        )
        result = registry.execute(step.tool_name, step.arguments, context.with_observations(tuple(observations)))
        data_names.extend(result.data_names)
        artifacts.extend(result.artifacts)
        observation = AgentObservation(
            step_id=step.step_id,
            kind="tool",
            status=result.status,
            content=result.content,
            payload={"tool_name": step.tool_name, "arguments": step.arguments, "result": result.to_dict()},
        )
        observations.append(observation)
        recorder.complete_item(
            item_id,
            status="done" if result.status == "done" else "error",
            output_payload=observation.to_dict(),
            artifact_paths=[str(item.get("path") or "") for item in result.artifacts],
            duration_seconds=max(0.0, time.monotonic() - step_started),
        )
        recorder.emit(
            "tool_completed",
            item_type="conversation_tool",
            item_name=step.tool_name,
            status="done" if result.status == "done" else "error",
            content=result.content,
            payload=observation.payload,
            duration_seconds=max(0.0, time.monotonic() - step_started),
        )
        if result.status != "done":
            break
    return None


def _execute_readonly_subagent(
    step: AgentStep,
    *,
    registry: Any,
    context: AgentToolContext,
    recorder: ChatTurnRecorder,
    observations: list[AgentObservation],
    artifacts: list[dict[str, Any]],
    data_names: list[str],
) -> None:
    task = _subagent_task(step, context.message)
    allowed_tools = _subagent_allowed_tools(step.arguments)
    item_id = recorder.start_item("conversation_subagent", step.title or step.step_id, input_payload=step.to_dict())
    started = time.monotonic()
    recorder.emit(
        "subagent_started",
        item_type="conversation_subagent",
        item_name=step.title or step.step_id,
        status="running",
        content=task,
        payload=step.to_dict(),
    )
    skipped = tuple(sorted(allowed_tools)) if allowed_tools else ()
    status = "skipped"
    content = _subagent_summary(task, child_observations=[], skipped=[*skipped, "conversation loop does not generate planner subplans"])
    observation = AgentObservation(
        step_id=step.step_id,
        kind="subagent",
        status=status,
        content=content,
        payload={
            "task": task,
            "executed_tools": [],
            "skipped": list(skipped),
            "reason": "planner_removed_from_conversation_runtime",
        },
    )
    observations.append(observation)
    recorder.complete_item(
        item_id,
        status=status,
        output_payload=observation.to_dict(),
        artifact_paths=[],
        duration_seconds=max(0.0, time.monotonic() - started),
    )
    recorder.emit(
        "subagent_completed",
        item_type="conversation_subagent",
        item_name=step.title or step.step_id,
        status=status,
        content=content,
        payload=observation.payload,
        duration_seconds=max(0.0, time.monotonic() - started),
    )


def _subagent_task(step: AgentStep, fallback: str) -> str:
    arguments = step.arguments if isinstance(step.arguments, dict) else {}
    return str(arguments.get("task") or arguments.get("message") or step.title or fallback or "").strip()


def _subagent_allowed_tools(arguments: Any) -> set[str]:
    if not isinstance(arguments, dict):
        return set()
    raw = arguments.get("tools") or arguments.get("allowed_tools")
    if isinstance(raw, str):
        return {item.strip() for item in raw.split(",") if item.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip() for item in raw if str(item).strip()}
    return set()


def _is_subagent_readonly_tool(step: AgentStep, *, registry: Any, allowed_tools: set[str]) -> bool:
    tool_name = str(step.tool_name or "").strip()
    if not tool_name:
        return False
    if allowed_tools and tool_name not in allowed_tools:
        return False
    spec = registry.get(tool_name)
    if spec is None:
        return False
    side_effect = str(step.side_effect or getattr(spec, "side_effect", "") or "readonly")
    requires_confirmation = bool(step.requires_confirmation or getattr(spec, "requires_confirmation", False))
    return side_effect == "readonly" and not requires_confirmation


def _subagent_summary(
    task: str,
    *,
    child_observations: list[AgentObservation],
    skipped: list[str],
) -> str:
    if not child_observations:
        lines = [f"只读子任务未执行工具: {task}"]
        if skipped:
            lines.append("跳过: " + ", ".join(skipped))
        return "\n".join(lines)
    lines = [f"只读子任务完成: {task}"]
    for item in child_observations:
        tool_name = item.payload.get("tool_name") if isinstance(item.payload, dict) else ""
        prefix = f"- {tool_name}: " if tool_name else "- "
        lines.append(prefix + (item.content or item.status))
    if skipped:
        lines.append("跳过: " + ", ".join(skipped))
    return "\n".join(lines)


def _normalize_conversation_tool_step(step: AgentStep, *, message: str) -> tuple[AgentStep, str, dict[str, Any]]:
    tool_name = str(step.tool_name or "").strip()
    if not tool_name:
        return step, "", {}
    today = agent_today()
    sanitized = sanitize_agent_tool_arguments(tool_name, step.arguments, message, today=today)
    if sanitized.error:
        return step, f"工具 {tool_name} 日期参数无效: {sanitized.error}", {"error": sanitized.error}

    original_arguments = dict(step.arguments or {}) if isinstance(step.arguments, dict) else {}
    arguments = dict(sanitized.arguments or {})
    changes = list((sanitized.metadata or {}).get("changes") or ())
    time_context = resolve_agent_time_context(message, today=today, arguments=arguments)

    if tool_name == "research.market_context":
        _apply_conversation_market_time_arguments(arguments, message=message, time_context=time_context, changes=changes)
    elif tool_name in CONVERSATION_RESEARCH_TIME_TOOLS:
        _apply_conversation_research_time_arguments(arguments, message=message, time_context=time_context, changes=changes)

    if arguments == original_arguments and not changes:
        return step, "", {}

    normalized_step = replace(step, arguments=arguments)
    metadata = dict(sanitized.metadata or {})
    if changes:
        metadata["changes"] = _dedupe_strings([str(item) for item in changes if str(item or "").strip()])
    metadata["time_context"] = {
        "today": time_context.today,
        "explicit_dates": list(time_context.explicit_dates),
        "horizons": list(_conversation_forecast_horizons(message, time_context=time_context)),
        "is_forecast": bool(_conversation_forecast_horizons(message, time_context=time_context)),
        "requires_intraday": time_context.requires_intraday,
    }
    return normalized_step, "", metadata


def _apply_conversation_market_time_arguments(
    arguments: dict[str, Any],
    *,
    message: str,
    time_context: Any,
    changes: list[str],
) -> None:
    if not _argument_present(arguments.get("trade_date")):
        as_of_date = _conversation_as_of_trade_date(message, time_context=time_context)
        if as_of_date:
            arguments["trade_date"] = as_of_date
            changes.append("set market trade_date from user relative date")

    has_horizon = _argument_present(arguments.get("horizons")) or _argument_present(arguments.get("horizon"))
    if not has_horizon:
        horizons = _conversation_forecast_horizons(message, time_context=time_context)
        if horizons:
            arguments["horizons"] = list(horizons)
            arguments.pop("horizon", None)
            changes.append("set market horizons from user relative forecast")
        elif not _argument_present(arguments.get("trade_date")) and _is_market_context_request(message):
            arguments["horizons"] = ["today"]
            changes.append("defaulted market horizon to today")


def _apply_conversation_research_time_arguments(
    arguments: dict[str, Any],
    *,
    message: str,
    time_context: Any,
    changes: list[str],
) -> None:
    horizons = _conversation_forecast_horizons(message, time_context=time_context)
    if not _argument_present(arguments.get("trade_date")):
        as_of_date = _conversation_as_of_trade_date(message, time_context=time_context)
        if as_of_date:
            arguments["trade_date"] = as_of_date
            changes.append("set research trade_date from user relative date")
        elif horizons:
            arguments["trade_date"] = time_context.today
            changes.append("set research trade_date to current as-of date for forecast")
    if horizons and not _argument_present(arguments.get("horizons")) and not _argument_present(arguments.get("horizon")):
        arguments["horizons"] = list(horizons)
        changes.append("set research horizons from user relative forecast")


def _conversation_as_of_trade_date(message: str, *, time_context: Any) -> str:
    if getattr(time_context, "explicit_dates", ()):
        return str(time_context.explicit_dates[0])
    text = str(message or "")
    if any(term in text for term in (*TODAY_AS_OF_TERMS, *RECENT_AS_OF_TERMS)) or re.search(
        r"(?:过去|最近|近)\s*(?:[0-9]+|[一二两三四五六七八九十几数]+)\s*[天日]",
        text,
    ):
        return str(time_context.today)
    return ""


def _conversation_forecast_horizons(message: str, *, time_context: Any) -> tuple[str, ...]:
    text = str(message or "")
    horizons = list(getattr(time_context, "horizons", ()) or ())
    if any(term in text for term in ("明后天", "明后", "明天后天", "未来两天", "未来几天", "未来几日", "未来数天", "未来三天", "未来三日")) or re.search(
        r"未来\s*(?:[0-9]+|[一二两三四五六七八九十几数]+)\s*[天日]",
        text,
    ):
        horizons.extend(["tomorrow", "day_after_tomorrow"])
    else:
        if "明天" in text or "次日" in text:
            horizons.append("tomorrow")
        if "后天" in text:
            horizons.append("day_after_tomorrow")
    if "下周" in text or "未来一周" in text:
        horizons.append("next_week")
    return tuple(item for item in _dedupe_strings(horizons) if item in {"today", "tomorrow", "day_after_tomorrow", "next_week"})


def _is_market_context_request(message: str) -> bool:
    text = str(message or "")
    has_subject = any(term in text for term in ("大盘", "市场", "A股", "指数", "上证", "深证", "创业板", "沪深", "热点板块"))
    has_intent = any(term in text for term in ("分析", "走势", "预测", "判断", "评价", "怎么看", "情况", "热点", "板块"))
    return has_subject and has_intent


def _dedupe_strings(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _conversation_argument_issue(
    step: AgentStep,
    *,
    spec: Any,
    message: str,
    observations: tuple[AgentObservation, ...],
) -> str:
    tool_name = str(step.tool_name or "").strip()
    arguments = dict(step.arguments or {}) if isinstance(step.arguments, dict) else {}
    missing = _missing_schema_arguments(spec, arguments)
    if missing:
        return _missing_arguments_issue(tool_name, missing)

    if tool_name == "research.market_context":
        has_trade_date = _argument_present(arguments.get("trade_date"))
        has_horizon = _argument_present(arguments.get("horizons")) or _argument_present(arguments.get("horizon"))
        if _is_historical_market_request(message) and not has_trade_date:
            return _missing_arguments_issue(tool_name, ["trade_date"])
        if not has_trade_date and not has_horizon:
            return _missing_arguments_issue(tool_name, ["trade_date 或 horizons"])

    if tool_name in {"research.stock_context", "research.internal_analysis", "research.deep_stock_analysis"}:
        if not _argument_present(arguments.get("symbols")):
            return _missing_arguments_issue(tool_name, ["symbols"])
        if _is_historical_analysis_request(message) and not _argument_present(arguments.get("trade_date")):
            return _missing_arguments_issue(tool_name, ["trade_date"])

    if tool_name == "data.astock_fetch":
        operation = str(arguments.get("operation") or "").strip()
        if operation:
            operation_issue = _astock_operation_issue(operation, observations)
            if operation_issue:
                return operation_issue

    return ""


def _missing_schema_arguments(spec: Any, arguments: dict[str, Any]) -> list[str]:
    schema = getattr(spec, "input_schema", None) if spec is not None else None
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    missing: list[str] = []
    for key in required:
        name = str(key or "").strip()
        if name and not _argument_present(arguments.get(name)):
            missing.append(name)
    return missing


def _argument_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _missing_arguments_issue(tool_name: str, missing: list[str]) -> str:
    fields = "、".join(missing)
    hint = _argument_hint(missing)
    return f"工具 {tool_name} 缺少明确参数: {fields}。{hint}"


def _argument_hint(missing: list[str]) -> str:
    fields = set(missing)
    if "trade_date 或 horizons" in fields:
        return "请说明要复盘的交易日期，或说明要预测的时间范围。"
    if "symbols" in fields:
        return "请说明要分析的股票代码或名称。"
    if "trade_date" in fields:
        return "请说明交易日期，例如 20260625，或使用昨天/昨日/前天/今天。"
    if "query" in fields:
        return "请补充搜索或检索关键词。"
    if "operation" in fields:
        return "请先通过 data.astock_catalog 确认可用 operation。"
    return "请补充这些参数后再执行。"


def _is_historical_market_request(message: str) -> bool:
    text = str(message or "")
    if _has_conversation_forecast_terms(text):
        return False
    if any(term in text for term in ("昨天", "昨日", "前天", "复盘", "回顾", "收盘", "盘后", "当日", "当天")):
        return True
    return any(char.isdigit() for char in text) and any(term in text for term in ("大盘", "市场", "A股", "指数"))


def _is_historical_analysis_request(message: str) -> bool:
    text = str(message or "")
    if _has_conversation_forecast_terms(text):
        return False
    return any(term in text for term in ("昨天", "昨日", "前天", "复盘", "回顾", "历史", "当日", "当天", "收盘", "盘后"))


def _has_conversation_forecast_terms(text: str) -> bool:
    value = str(text or "")
    if any(term in value for term in ("明天", "次日", "后天", "明后天", "明后", "下周", "未来一周", "未来几天", "未来几日", "未来数天")):
        return True
    return bool(re.search(r"未来\s*(?:[0-9]+|[一二两三四五六七八九十几数]+)\s*[天日]", value))


def _astock_operation_issue(operation: str, observations: tuple[AgentObservation, ...]) -> str:
    try:
        from sats.data.astock_operations import get_astock_operation

        get_astock_operation(operation)
    except Exception:
        return f"工具 data.astock_fetch 的 operation 未登记: {operation}。请先通过 data.astock_catalog 发现可用 operation。"
    if not _catalog_returned_operation(operation, observations):
        return f"工具 data.astock_fetch 需要先通过 data.astock_catalog 确认 operation: {operation}。"
    return ""


def _catalog_returned_operation(operation: str, observations: tuple[AgentObservation, ...]) -> bool:
    for item in observations:
        if item.kind != "tool" or item.payload.get("tool_name") != "data.astock_catalog":
            continue
        result = item.payload.get("result") if isinstance(item.payload, dict) else None
        if not isinstance(result, dict):
            continue
        payload = result.get("payload")
        if not isinstance(payload, dict):
            continue
        catalog = payload.get("astock_catalog")
        if not isinstance(catalog, dict):
            continue
        for row in catalog.get("items") or ():
            if isinstance(row, dict) and str(row.get("operation") or "").strip() == operation:
                return True
    return False


def _issue_missing_fields(issue: str) -> list[str]:
    marker = "缺少明确参数:"
    if marker not in issue:
        return []
    tail = issue.split(marker, 1)[1].split("。", 1)[0]
    return [item.strip() for item in tail.split("、") if item.strip()]


def _argument_guard_observation(
    step: AgentStep,
    issue: str,
    *,
    clarification_id: str = "",
    missing_fields: tuple[str, ...] = (),
) -> AgentObservation:
    return AgentObservation(
        step_id=step.step_id,
        kind="clarification",
        status="blocked",
        content=issue,
        payload={
            "tool_name": step.tool_name,
            "arguments": step.arguments,
            "issue": issue,
            "clarification_id": clarification_id,
            "missing_fields": list(missing_fields),
        },
    )


def _clarification_prompt(step: AgentStep, issue: str) -> str:
    tool = step.tool_name or step.step_id
    hint = _clarification_question(tuple(_issue_missing_fields(issue)))
    return f"需要先补充信息，暂不调用 {tool}。\n\n{issue}\n\n{hint}"


def _clarification_message(step: AgentStep, issue: str, *, clarification_id: str) -> str:
    prompt = _clarification_prompt(step, issue)
    continuation = f"sats chat --answer {clarification_id} \"...\"" if clarification_id else "sats chat --answer CLARIFICATION_ID \"...\""
    return f"{prompt}\n\nclarification_id: {clarification_id}\n继续: {continuation}"


def _clarification_question(missing_fields: tuple[str, ...]) -> str:
    fields = set(missing_fields)
    if "symbols" in fields:
        return "请直接输入要分析的股票代码或名称。"
    if "trade_date" in fields:
        return "请直接输入交易日期，例如 20260625，或输入昨天/昨日/前天/今天。"
    if "trade_date 或 horizons" in fields:
        return "请直接输入要复盘的交易日期，或要预测的时间范围。"
    if "query" in fields:
        return "请直接输入检索关键词。"
    return "请直接补充缺失参数。"


def _require_clarification(
    step: AgentStep,
    *,
    plan: AgentPlan,
    context: AgentToolContext,
    recorder: ChatTurnRecorder,
    issue: str,
    missing_fields: tuple[str, ...],
) -> str:
    if context.store is None:
        return ""
    payload = {
        "message": context.message,
        "session_id": context.session_id,
        "plan": plan.to_dict(),
        "step": step.to_dict(),
        "tool_name": step.tool_name,
        "arguments": step.arguments,
        "issue": issue,
        "missing_fields": list(missing_fields),
        "policy": context.policy.to_dict(),
    }
    return context.store.create_pending_action(
        session_id=context.session_id,
        turn_id=recorder.turn_id,
        action_type=CONVERSATION_CLARIFICATION_ACTION_TYPE,
        title=step.title or step.tool_name or step.step_id,
        payload=payload,
    )


def _merge_clarification_answer(message: str, answer: str, *, missing_fields: tuple[str, ...]) -> str:
    clean_message = str(message or "").strip()
    clean_answer = str(answer or "").strip()
    if not clean_message:
        return clean_answer
    fields = "、".join(missing_fields)
    if fields:
        return f"{clean_message}\n\n补充信息（{fields}）: {clean_answer}"
    return f"{clean_message}\n\n补充信息: {clean_answer}"


def _tool_context(
    *,
    settings: Settings,
    store: ChatMemoryStore,
    policy: AgentExecutionPolicy,
    session_id: str,
    turn_id: str,
    message: str,
    llm_factory: Callable[..., Any] | None,
) -> AgentToolContext:
    storage = DuckDBStorage(settings.db_path)
    resolver = MarketDataResolver(settings, storage=storage)
    skills = tuple(load_skills(default_skills_dir(Path(getattr(settings, "project_root", ".")))))
    trader = AgentTradingExecutor(settings=settings, storage=storage, resolver=resolver, policy=policy)
    return AgentToolContext(
        settings=settings,
        storage=storage,
        resolver=resolver,
        policy=policy,
        command_runner=AgentCommandRunner(policy=policy),
        trader=trader,
        store=store,
        skills=skills,
        llm_factory=llm_factory,
        session_id=session_id,
        turn_id=turn_id,
        message=message,
    )


def _require_step_confirmation(
    step: AgentStep,
    *,
    plan: AgentPlan,
    context: AgentToolContext,
    recorder: ChatTurnRecorder,
    side_effect: str | None = None,
) -> str:
    payload = {
        "message": context.message,
        "session_id": context.session_id,
        "plan": plan.to_dict(),
        "step": step.to_dict(),
        "tool_name": step.tool_name,
        "arguments": step.arguments,
        "side_effect": side_effect or step.side_effect,
        "policy": context.policy.to_dict(),
    }
    return recorder.require_approval(
        action_type=CONVERSATION_ACTION_TYPE,
        title=step.title or step.tool_name or step.step_id,
        payload=payload,
    )


def _approval_observation(step: AgentStep, action_id: str) -> AgentObservation:
    return AgentObservation(
        step_id=step.step_id,
        kind="approval",
        status="done",
        content=f"requires confirmation: {action_id}",
        payload={"pending_action_id": action_id, "tool_name": step.tool_name, "side_effect": step.side_effect},
    )


def _pending_message(step: AgentStep, action_id: str) -> str:
    return (
        "需要确认后才能执行该动作。\n\n"
        f"- 动作: {step.title or step.tool_name or step.step_id}\n"
        f"- 工具: {step.tool_name or step.kind}\n"
        f"- 副作用: {step.side_effect or 'unknown'}\n"
        f"- 确认: sats chat --confirm {action_id}\n"
        f"- 拒绝: sats chat --reject {action_id}"
    )


def _persist_messages(store: ChatMemoryStore, session_id: str, user: str, assistant: str) -> tuple[str, str]:
    try:
        user_id = store.add_message(session_id, "user", user)
        assistant_id = store.add_message(session_id, "assistant", assistant)
        return user_id, assistant_id
    except Exception:
        return "", ""


def _complete_turn(
    recorder: ChatTurnRecorder,
    result: ConversationResult,
    started: float,
    *,
    user_message_id: str = "",
    assistant_message_id: str = "",
) -> None:
    recorder.complete(
        content=result.content,
        intent="conversation",
        data_names=result.data_names,
        skill_names=result.skill_names,
        model_name=result.model_name,
        tool_call_count=result.tool_call_count,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        duration_seconds=max(0.0, time.monotonic() - started),
        meta={
            "phase": "conversation",
            "model_policy": result.model_policy,
            "model_profile": result.model_profile,
            "model_name": result.model_name,
            "requires_confirmation": result.requires_confirmation,
            "pending_action_id": result.pending_action_id or "",
            "requires_clarification": result.requires_clarification,
            "clarification_id": result.clarification_id or "",
        },
    )


def _fallback_content(observations: list[AgentObservation]) -> str:
    for item in reversed(observations):
        if item.content:
            return item.content
    return "无响应"


def _policy_from_payload(value: Any) -> AgentExecutionPolicy:
    if not isinstance(value, dict):
        return AgentExecutionPolicy()
    return AgentExecutionPolicy(
        auto_trade=tuple(value.get("auto_trade") or ()),
        broker=str(value.get("broker") or "noop"),
        live_trading=bool(value.get("live_trading") or False),
        dry_run=bool(value.get("dry_run") or False),
        max_order_value=float(value.get("max_order_value") or 20000.0),
        max_position_pct=float(value.get("max_position_pct") or 0.2),
        sell_ratio=float(value.get("sell_ratio") or 1.0),
        max_iterations=int(value.get("max_iterations") or 6),
        command_timeout=int(value.get("command_timeout") or 120),
        python_timeout=int(value.get("python_timeout") or 30),
    )


def _step_from_payload(payload: dict[str, Any]) -> AgentStep:
    return AgentStep(
        step_id=str(payload.get("step_id") or "confirmed_step"),
        kind=str(payload.get("kind") or "tool"),
        title=str(payload.get("title") or ""),
        tool_name=str(payload.get("tool_name") or ""),
        arguments=dict(payload.get("arguments") or {}) if isinstance(payload.get("arguments"), dict) else {},
        command=tuple(payload.get("command") or ()),
        code=str(payload.get("code") or ""),
        trade=dict(payload.get("trade") or {}) if isinstance(payload.get("trade"), dict) else {},
        requires_confirmation=bool(payload.get("requires_confirmation") or False),
        side_effect=str(payload.get("side_effect") or "readonly"),
        success_criteria=str(payload.get("success_criteria") or ""),
    )


def _plan_from_payload(payload: dict[str, Any], *, fallback_message: str, step: AgentStep) -> AgentPlan:
    raw_steps = payload.get("steps")
    steps = tuple(_step_from_payload(item) for item in raw_steps if isinstance(item, dict)) if isinstance(raw_steps, list) else (step,)
    return AgentPlan(
        objective=str(payload.get("objective") or fallback_message or step.title or step.step_id),
        success_criteria=tuple(payload.get("success_criteria") or ()),
        assumptions=tuple(payload.get("assumptions") or ()),
        steps=steps,
        risk_level=str(payload.get("risk_level") or "medium"),
        requires_live_trading=bool(payload.get("requires_live_trading") or False),
        phase=str(payload.get("phase") or "conversation_loop"),
        model_policy=str(payload.get("model_policy") or ""),
        model_profile=str(payload.get("model_profile") or ""),
        model_name=str(payload.get("model_name") or ""),
        natural_task=dict(payload.get("natural_task") or {}) if isinstance(payload.get("natural_task"), dict) else {},
        analysis_mode=str(payload.get("analysis_mode") or ""),
        verification_checks=tuple(payload.get("verification_checks") or ()),
    )
