from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from sats.agent.command_runner import AgentCommandRunner
from sats.agent.models import AgentExecutionPolicy, AgentObservation, AgentPlan, AgentStep
from sats.agent.planner import _is_sector_return_ranking_request, _sector_return_ranking_step, build_agent_plan
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
from sats.stock_question import extract_stock_symbols


CONVERSATION_ACTION_TYPE = "conversation_tool"
CONVERSATION_CLARIFICATION_ACTION_TYPE = "conversation_clarification"
CONFIRM_SIDE_EFFECTS = {"write_db", "long_running", "command", "live_trade"}
SUBAGENT_KINDS = {"subagent", "delegate"}
MAX_SUBAGENT_STEPS = 4
MAX_INVALID_ACTIONS = 2
CONVERSATION_ACTIONS = {"call_tool", "ask_clarification", "request_confirmation", "final_answer"}
SECTOR_RANKING_MISROUTE_TOOLS = {
    "research.theme_stock_returns",
    "research.theme_stock_list",
    "research.discover_opportunities",
    "web.search",
    "web.open",
    "web.hot_mentions",
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
        plan = build_agent_plan(
            text,
            settings=settings,
            policy=policy,
            llm_factory=None,
            tool_registry=registry,
            reference_context=reference_context,
        )
        plan = replace(plan, phase="conversation_planner", model_policy="local", model_profile="local", model_name="local")
        plan = _normalize_conversation_plan(plan, text)
        recorder.emit("plan_ready", item_type="plan", item_name="conversation", status="done", content=plan.objective, payload=plan.to_dict())

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
            loop = _LoopOutcome(model_policy="none")
            interruption = _execute_plan_until_confirmation(
                plan,
                registry=registry,
                context=context,
                recorder=recorder,
                observations=observations,
                artifacts=artifacts,
                data_names=data_names,
            )
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
            synthesis = synthesize_agent_result(
                message=text,
                plan=plan,
                observations=tuple(observations),
                skills=tuple(context.skills),
                settings=settings,
                llm_factory=llm_factory,
            )
            content = synthesis.content or _fallback_content(observations)
            skill_names = synthesis.skill_names
            model_policy = synthesis.model_policy
            model_profile = synthesis.model_profile
            model_name = synthesis.model_name
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


def format_conversation_plan(
    message: str,
    *,
    settings: Settings,
    policy: AgentExecutionPolicy | None = None,
    reference_context: Any | None = None,
) -> str:
    registry = build_default_tool_registry()
    plan = build_agent_plan(
        message,
        settings=settings,
        policy=policy or AgentExecutionPolicy(),
        llm_factory=None,
        tool_registry=registry,
        reference_context=reference_context,
    )
    plan = _normalize_conversation_plan(plan, message)
    lines = ["SATS Conversation Plan", "", f"目标: {plan.objective}"]
    if plan.analysis_mode:
        lines.append(f"分析模式: {plan.analysis_mode}")
    if plan.assumptions:
        lines.extend(["", "假设:"])
        lines.extend(f"- {item}" for item in plan.assumptions)
    lines.extend(["", "步骤:"])
    for index, step in enumerate(plan.steps, start=1):
        if step.kind == "final":
            lines.append(f"{index}. final: {step.title or '总结结果'}")
            continue
        if step.kind in SUBAGENT_KINDS:
            lines.append(f"{index}. subagent - {step.title or step.step_id}")
            continue
        tool = f" {step.tool_name}" if step.tool_name else ""
        side_effect = f" [{step.side_effect}]" if step.side_effect else ""
        lines.append(f"{index}. {step.kind}:{tool}{side_effect} - {step.title or step.step_id}")
        if step.arguments:
            lines.append(f"   参数: {json.dumps(step.arguments, ensure_ascii=False, sort_keys=True, default=str)}")
    if plan.verification_checks:
        lines.extend(["", "验证:"])
        for item in plan.verification_checks:
            lines.append(f"- {item.get('name') or item.get('kind') or item}")
    return "\n".join(lines)


def _normalize_conversation_plan(plan: AgentPlan, message: str) -> AgentPlan:
    if not _is_sector_return_ranking_request(message):
        if _is_ambiguous_stock_analysis_request(message):
            tool_steps = [step for step in plan.steps if step.kind == "tool"]
            if len(tool_steps) == 1 and tool_steps[0].tool_name == "chat.answer":
                return replace(
                    plan,
                    steps=(
                        AgentStep(
                            step_id="stock_context",
                            kind="tool",
                            title="获取个股上下文",
                            tool_name="research.stock_context",
                            arguments={},
                            side_effect="readonly",
                        ),
                        AgentStep(step_id="final", kind="final", title="总结结果"),
                    ),
                )
        return plan
    tool_steps = [step for step in plan.steps if step.kind == "tool"]
    if len(tool_steps) == 1 and tool_steps[0].tool_name == "research.sector_return_ranking":
        return plan
    if not tool_steps or any(step.tool_name in SECTOR_RANKING_MISROUTE_TOOLS for step in tool_steps):
        return replace(
            plan,
            steps=(
                _sector_return_ranking_step(message),
                AgentStep(step_id="final", kind="final", title="总结结果"),
            ),
        )
    return plan


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

        if action.action == "final_answer":
            issue = _final_answer_blocking_issue(message=message, plan=plan, observations=tuple(observations))
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
) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 SATS conversation agent loop。你只能输出一个 JSON 对象，不要输出 Markdown。"
                "每次只选择一个动作：call_tool、ask_clarification、request_confirmation、final_answer。"
                "所有真实 A 股行情、财务、K线、报价、资金流和 provider 数据必须通过 SATS 注册工具获取；"
                "不确定 A 股数据接口时，先调用 data.astock_catalog，再调用 data.astock_fetch。"
                "不要编造股票代码、价格、成交量、K线、财务字段或第三方 provider 方法名。"
                "readonly 和 write_artifact 工具可以直接 call_tool；write_db、long_running、command、live_trade 必须 request_confirmation。"
                "如果缺少必要股票、日期、搜索词或 operation，输出 ask_clarification。"
                "大盘、市场或指数走势/预测类问题，final_answer 前必须先 call_tool research.market_context；"
                "用户要求下周或未来一周时，research.market_context 参数必须包含 horizons=[\"next_week\"]。"
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
                f"初始计划：{json.dumps(plan.to_dict(), ensure_ascii=False, default=str)}\n"
                f"policy={json.dumps(policy.to_dict(), ensure_ascii=False, default=str)}\n"
                f"reference_context={json.dumps(_reference_context_payload(reference_context), ensure_ascii=False, default=str)}\n"
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
    requires_confirmation = bool(force_confirmation or step.requires_confirmation or getattr(spec, "requires_confirmation", False) or side_effect in CONFIRM_SIDE_EFFECTS)
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


def _loop_final_needs_synthesis(observations: tuple[AgentObservation, ...]) -> bool:
    for item in observations:
        tool_name = str(item.payload.get("tool_name") or "")
        if tool_name.startswith(("research.", "data.", "factor.", "trade.", "web.")):
            return True
        if item.kind in {"python", "trade"}:
            return True
    return False


def _final_answer_blocking_issue(*, message: str, plan: AgentPlan, observations: tuple[AgentObservation, ...]) -> str:
    if not _requires_market_context_before_final(message, plan):
        return ""
    market_observation = _successful_market_context_observation(observations)
    if market_observation is None:
        return "final_answer blocked: 大盘/市场/指数走势或预测类回答必须先调用 research.market_context 获取真实上下文。"
    if _requires_next_week_horizon(message) and "next_week" not in _market_context_horizons(market_observation):
        return "final_answer blocked: 下周/未来一周预测必须先调用 research.market_context 且 requested_horizons 包含 next_week。"
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
        rows.append(
            {
                "step_id": item.step_id,
                "kind": item.kind,
                "status": item.status,
                "tool_name": payload.get("tool_name") or "",
                "content": _truncate_text(item.content or result.get("content") or "", 900),
                "payload_keys": sorted(str(key) for key in payload.keys())[:12],
            }
        )
    return rows


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
        requires_confirmation = bool(step.requires_confirmation or getattr(spec, "requires_confirmation", False) or side_effect in CONFIRM_SIDE_EFFECTS)
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
    child_plan = build_agent_plan(
        task,
        settings=context.settings,
        policy=context.policy,
        llm_factory=None,
        tool_registry=registry,
    )
    child_context = replace(context, message=task)
    executed = 0
    skipped: list[str] = []
    child_observations: list[AgentObservation] = []
    for child_step in child_plan.steps:
        if child_step.kind == "final":
            break
        if executed >= MAX_SUBAGENT_STEPS:
            skipped.append("max_steps")
            break
        if child_step.kind != "tool":
            skipped.append(child_step.step_id)
            continue
        if not _is_subagent_readonly_tool(child_step, registry=registry, allowed_tools=allowed_tools):
            skipped.append(child_step.tool_name or child_step.step_id)
            continue
        issue = _conversation_argument_issue(child_step, spec=registry.get(child_step.tool_name), message=child_context.message, observations=tuple(observations))
        if issue:
            skipped.append(f"{child_step.tool_name or child_step.step_id}: {issue}")
            continue
        tool_item_id = recorder.start_item("conversation_subagent_tool", child_step.tool_name, input_payload=child_step.to_dict())
        tool_started = time.monotonic()
        recorder.emit(
            "tool_started",
            item_type="conversation_subagent_tool",
            item_name=child_step.tool_name,
            status="running",
            payload=child_step.to_dict(),
        )
        result = registry.execute(child_step.tool_name, child_step.arguments, child_context.with_observations(tuple(observations)))
        executed += 1
        data_names.extend(result.data_names)
        artifacts.extend(result.artifacts)
        observation = AgentObservation(
            step_id=f"{step.step_id}.{child_step.step_id}",
            kind="tool",
            status=result.status,
            content=result.content,
            payload={"tool_name": child_step.tool_name, "arguments": child_step.arguments, "result": result.to_dict(), "subagent": step.step_id},
        )
        observations.append(observation)
        child_observations.append(observation)
        recorder.complete_item(
            tool_item_id,
            status="done" if result.status == "done" else "error",
            output_payload=observation.to_dict(),
            artifact_paths=[str(item.get("path") or "") for item in result.artifacts],
            duration_seconds=max(0.0, time.monotonic() - tool_started),
        )
        recorder.emit(
            "tool_completed",
            item_type="conversation_subagent_tool",
            item_name=child_step.tool_name,
            status="done" if result.status == "done" else "error",
            content=result.content,
            payload=observation.payload,
            duration_seconds=max(0.0, time.monotonic() - tool_started),
        )
        if result.status != "done":
            break
    status = "done" if executed else "skipped"
    content = _subagent_summary(task, child_observations=child_observations, skipped=skipped)
    observation = AgentObservation(
        step_id=step.step_id,
        kind="subagent",
        status=status,
        content=content,
        payload={
            "task": task,
            "executed_tools": [item.payload.get("tool_name") for item in child_observations],
            "skipped": skipped,
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
        return f"只读子任务未执行工具: {task}"
    lines = [f"只读子任务完成: {task}"]
    for item in child_observations:
        tool_name = item.payload.get("tool_name") if isinstance(item.payload, dict) else ""
        prefix = f"- {tool_name}: " if tool_name else "- "
        lines.append(prefix + (item.content or item.status))
    if skipped:
        lines.append("跳过: " + ", ".join(skipped))
    return "\n".join(lines)


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

    if tool_name == "research.sector_return_ranking":
        required = ["query", "sector_type", "period", "direction", "limit"]
        missing_business = [key for key in required if not _argument_present(arguments.get(key))]
        if missing_business:
            return _missing_arguments_issue(tool_name, missing_business)

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
    if any(term in text for term in ("昨天", "昨日", "前天", "复盘", "回顾", "收盘", "盘后", "当日", "当天")):
        return True
    return any(char.isdigit() for char in text) and any(term in text for term in ("大盘", "市场", "A股", "指数"))


def _is_historical_analysis_request(message: str) -> bool:
    text = str(message or "")
    return any(term in text for term in ("昨天", "昨日", "前天", "复盘", "回顾", "历史", "当日", "当天", "收盘", "盘后"))


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
        phase=str(payload.get("phase") or "conversation_planner"),
        model_policy=str(payload.get("model_policy") or "local"),
        model_profile=str(payload.get("model_profile") or "local"),
        model_name=str(payload.get("model_name") or "local"),
        natural_task=dict(payload.get("natural_task") or {}) if isinstance(payload.get("natural_task"), dict) else {},
        analysis_mode=str(payload.get("analysis_mode") or ""),
        verification_checks=tuple(payload.get("verification_checks") or ()),
    )
