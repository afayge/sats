"""OBSOLETE: autonomous Agent runtime is retained for internal code/tests only.

Public `sats agent` and `sats chat --agent` entrypoints are disabled.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from sats.agent.command_runner import AgentCommandRunner
from sats.agent.models import AgentExecutionPolicy, AgentObservation, AgentResult, AgentStep, TradeIntent
from sats.agent.recovery import classify_failure, failure_from_message
from sats.agent.source_repair import propose_source_repair_for_turn
from sats.agent.planner import build_agent_plan
from sats.agent.python_runtime import RestrictedPythonRuntime
from sats.agent.synthesis import collect_agent_sources, save_agent_report, should_write_agent_report, synthesize_agent_result
from sats.agent.tools import AgentToolContext, AgentToolRegistry, build_default_tool_registry
from sats.agent.trading import AgentTradingExecutor
from sats.chat_events import ChatEventSink, ChatTurnRecorder
from sats.config import Settings, load_settings
from sats.data.resolver import MarketDataResolver
from sats.llm import ChatLLM
from sats.memory import ChatMemoryStore
from sats.skills import default_skills_dir, load_skills
from sats.storage.duckdb import DuckDBStorage


_STEP_PLACEHOLDER_RE = re.compile(
    r"^\$\{(?P<step>[^.\[\]{}]+)\.(?P<path>[A-Za-z_][A-Za-z0-9_.]*)(?:\[(?P<start>\d*):(?P<end>\d*)\])?\}$"
)


def run_agent_once(
    message: str,
    *,
    settings: Settings | None = None,
    policy: AgentExecutionPolicy | None = None,
    session_id: str = "agent",
    event_sink: ChatEventSink | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
    cli_main: Callable[[list[str]], int] | None = None,
    reference_context: Any | None = None,
) -> AgentResult:
    settings = settings or load_settings()
    storage = DuckDBStorage(settings.db_path)
    store = ChatMemoryStore(settings.db_path)
    policy = policy or AgentExecutionPolicy()
    recorder = ChatTurnRecorder(session_id=session_id, request=message, store=store, event_sink=event_sink)
    started = time.monotonic()
    recorder.start(payload={"agent": True, "policy": policy.to_dict()})
    try:
        resolver = MarketDataResolver(settings, storage=storage)
        command_runner = AgentCommandRunner(policy=policy, cli_main=cli_main)
        python_runner = RestrictedPythonRuntime(resolver=resolver, timeout_seconds=policy.python_timeout)
        trader = AgentTradingExecutor(settings=settings, storage=storage, resolver=resolver, policy=policy)
        tool_registry = build_default_tool_registry()
        skills = tuple(load_skills(default_skills_dir(Path(getattr(settings, "project_root", ".")))))
        recorder.emit(
            "context_completed",
            item_type="skills",
            item_name="agent_skills",
            status="done",
            content=", ".join(skill.name for skill in skills[:8]),
            payload={"count": len(skills), "skills": [skill.name for skill in skills]},
        )
        if reference_context is not None:
            recorder.emit(
                "context_completed",
                item_type="context",
                item_name="reference_context",
                status="done",
                payload={
                    "data_name": str(getattr(reference_context, "data_name", "") or ""),
                    "symbols": list(getattr(reference_context, "symbols", ()) or ()),
                    "trade_date": str(getattr(reference_context, "trade_date", "") or ""),
                    "source": str(getattr(reference_context, "source", "") or ""),
                },
            )
        plan = build_agent_plan(
            message,
            settings=settings,
            policy=policy,
            llm_factory=llm_factory,
            tool_registry=tool_registry,
            reference_context=reference_context,
        )
        recorder.emit(
            "plan_ready",
            item_type="plan",
            item_name="agent",
            status="done",
            content=plan.objective,
            payload=plan.to_dict(),
        )
        tool_context = AgentToolContext(
            settings=settings,
            storage=storage,
            resolver=resolver,
            policy=policy,
            command_runner=command_runner,
            trader=trader,
            store=store,
            skills=skills,
            llm_factory=llm_factory,
            session_id=session_id,
            turn_id=recorder.turn_id,
            message=message,
            event_callback=lambda event_type, payload: recorder.emit(
                event_type,
                item_type="recovery",
                item_name=str(payload.get("tool") or ""),
                status="error" if event_type in {"failure_detected", "failure_exhausted", "repair_failed"} else "done",
                payload=payload,
            ),
        )
        observations: list[AgentObservation] = []
        steps = list(plan.steps)
        index = 0
        iteration = 0
        replanned_error_signatures: set[str] = set()
        catalog_replanned = False
        while index < len(steps) and iteration < max(1, policy.max_iterations):
            step = steps[index]
            index += 1
            iteration += 1
            item_id = recorder.start_item("agent_step", step.step_id, input_payload=step.to_dict())
            step_started = time.monotonic()
            recorder.emit("runtime_iteration_started", item_type="agent_step", item_name=step.step_id, status="running", payload=step.to_dict())
            observation = _execute_step(
                step,
                message=message,
                command_runner=command_runner,
                python_runner=python_runner,
                trader=trader,
                tool_registry=tool_registry,
                tool_context=tool_context.with_observations(tuple(observations)),
                observations=observations,
                trade_context={"source_step_id": step.step_id},
            )
            observation = _with_step_verification(step, observation)
            observation = _with_structured_failure(
                observation,
                settings=settings,
                recorder=recorder,
                store=store,
            )
            if observation.status == "error" and step.kind != "final":
                observation = _with_replan_decision(
                    observation,
                    _replan_decision(
                        observation,
                        previous_observations=observations,
                        replanned_error_signatures=replanned_error_signatures,
                        llm_factory=llm_factory,
                    ),
                )
            observations.append(observation)
            recorder.complete_item(
                item_id,
                status="done" if observation.status == "done" else "error",
                output_payload=observation.to_dict(),
                duration_seconds=max(0.0, time.monotonic() - step_started),
            )
            recorder.emit(
                "tool_completed",
                item_type="agent_step",
                item_name=step.step_id,
                status="done" if observation.status == "done" else "error",
                content=observation.content,
                payload=observation.payload,
                duration_seconds=max(0.0, time.monotonic() - step_started),
            )
            if step.kind == "final":
                break
            if observation.status == "error":
                decision = observation.payload.get("replan_decision") if isinstance(observation.payload, dict) else {}
                decision = decision if isinstance(decision, dict) else {}
                if decision.get("repeated"):
                    observations.append(
                        AgentObservation(
                            step_id="repeated_error",
                            kind="runtime",
                            status="error",
                            content=f"agent stopped after repeated error: {observation.content}",
                            payload={"replan_decision": decision},
                        )
                    )
                    break
                if not decision.get("should_replan"):
                    break
                signature = str(decision.get("error_signature") or "")
                if signature:
                    replanned_error_signatures.add(signature)
                replan = build_agent_plan(
                    _replan_request(message, observations, decision),
                    settings=settings,
                    policy=policy,
                    llm_factory=llm_factory,
                    tool_registry=tool_registry,
                    policy_message=message,
                    reference_context=reference_context,
                )
                replacement = [item for item in replan.steps if item.kind != "final"]
                if replacement:
                    recorder.emit(
                        "replan_ready",
                        item_type="plan",
                        item_name="agent_replan",
                        status="done",
                        content=replan.objective,
                        payload=replan.to_dict(),
                    )
                    steps = steps[:index] + replacement + [AgentStep(step_id="final", kind="final", title="总结结果")]
                    continue
                observations.append(
                    AgentObservation(
                        step_id="replan_unavailable",
                        kind="runtime",
                        status="error",
                        content="agent replan produced no replacement steps",
                        payload={"replan_decision": decision},
                    )
                )
                break
            if (
                observation.status == "done"
                and not catalog_replanned
                and llm_factory is not None
                and _should_replan_after_catalog(message, step, steps[index:])
            ):
                catalog_replanned = True
                replan = build_agent_plan(
                    _catalog_replan_request(message, observations),
                    settings=settings,
                    policy=policy,
                    llm_factory=llm_factory,
                    tool_registry=tool_registry,
                    policy_message=message,
                    reference_context=reference_context,
                )
                replacement = [item for item in replan.steps if item.kind != "final"]
                if replacement:
                    recorder.emit(
                        "replan_ready",
                        item_type="plan",
                        item_name="agent_catalog_replan",
                        status="done",
                        content=replan.objective,
                        payload=replan.to_dict(),
                    )
                    steps = steps[:index] + replacement + [AgentStep(step_id="final", kind="final", title="总结结果")]
        if iteration >= policy.max_iterations and (
            not observations or observations[-1].step_id not in {"max_iterations", "repeated_error"} and observations[-1].kind != "final"
        ):
            observations.append(
                AgentObservation(
                    step_id="max_iterations",
                    kind="runtime",
                    status="error",
                    content=f"agent reached max_iterations={policy.max_iterations}",
                )
            )
        recorder.emit("context_started", item_type="agent_synthesis", item_name="final_synthesis", status="running")
        synthesis_started = time.monotonic()
        synthesis = synthesize_agent_result(
            message=message,
            plan=plan,
            observations=tuple(observations),
            skills=skills,
            settings=settings,
            llm_factory=llm_factory,
        )
        content = synthesis.content
        recorder.emit(
            "context_completed",
            item_type="agent_synthesis",
            item_name="final_synthesis",
            status="done",
            content=content,
            payload={
                "used_llm": synthesis.used_llm,
                "skills": list(synthesis.skill_names),
                "phase": synthesis.phase,
                "model_policy": synthesis.model_policy,
                "model_profile": synthesis.model_profile,
                "model_name": synthesis.model_name,
            },
            duration_seconds=max(0.0, time.monotonic() - synthesis_started),
        )
        artifacts = list(_artifacts(observations))
        if str(getattr(settings, "self_repair_mode", "propose") or "propose").lower() == "propose":
            failures = store.list_agent_failures(turn_id=recorder.turn_id, limit=20)
            if any(
                item.get("category") == "local_code_defect"
                and item.get("status") == "exhausted"
                and item.get("frames")
                for item in failures
            ):
                try:
                    repair = propose_source_repair_for_turn(
                        recorder.turn_id,
                        settings=settings,
                        store=store,
                        llm_factory=llm_factory,
                    )
                except Exception as exc:
                    recorder.emit(
                        "repair_failed",
                        item_type="source_repair",
                        item_name=recorder.turn_id,
                        status="error",
                        content=str(exc),
                    )
                else:
                    recorder.emit(
                        "repair_proposed",
                        item_type="source_repair",
                        item_name=str(repair.get("repair_id") or ""),
                        status="done",
                        payload=repair,
                    )
                    artifacts.append(
                        {
                            "kind": "source_repair_patch",
                            "path": str(repair.get("patch_path") or ""),
                            "repair_id": str(repair.get("repair_id") or ""),
                            "pending_action_id": str(repair.get("pending_action_id") or ""),
                        }
                    )
        if should_write_agent_report(message, plan, tuple(observations)):
            report_started = time.monotonic()
            recorder.emit("context_started", item_type="artifact", item_name="agent_report", status="running")
            artifact = save_agent_report(
                content=content,
                message=message,
                settings=settings,
                store=store,
                session_id=session_id,
                turn_id=recorder.turn_id,
            )
            artifacts.append(artifact)
            recorder.emit(
                "artifact_created",
                item_type="artifact",
                item_name="agent_report",
                status="done",
                payload=artifact,
                duration_seconds=max(0.0, time.monotonic() - report_started),
            )
        data_names = list(_data_names(observations))
        if artifacts and "报告" not in data_names and any(str(item.get("kind") or "") == "markdown_report" for item in artifacts):
            data_names.append("报告")
        result = AgentResult(
            content=content,
            plan=plan,
            observations=tuple(observations),
            tool_call_count=sum(1 for item in observations if item.kind in {"tool", "command", "python", "trade"}),
            data_names=tuple(data_names),
            skill_names=synthesis.skill_names,
            artifacts=tuple(artifacts),
            sources=tuple(collect_agent_sources(tuple(observations))),
            turn_id=recorder.turn_id,
            session_id=session_id,
        )
        recorder.complete(
            content=content,
            intent="agent",
            data_names=result.data_names,
            model_name=synthesis.model_name or getattr(settings, "openai_model", ""),
            tool_call_count=result.tool_call_count,
            duration_seconds=max(0.0, time.monotonic() - started),
            meta={
                "agent_plan": plan.to_dict(),
                "observations": [item.to_dict() for item in observations],
                "synthesis": {
                    "used_llm": synthesis.used_llm,
                    "skills": list(synthesis.skill_names),
                    "messages": list(synthesis.messages),
                    "phase": synthesis.phase,
                    "model_policy": synthesis.model_policy,
                    "model_profile": synthesis.model_profile,
                    "model_name": synthesis.model_name,
                },
            },
        )
        return result
    except Exception as exc:
        recorder.fail(exc, duration_seconds=max(0.0, time.monotonic() - started))
        raise


def _execute_step(
    step: Any,
    *,
    message: str,
    command_runner: AgentCommandRunner,
    python_runner: RestrictedPythonRuntime,
    trader: AgentTradingExecutor,
    tool_registry: AgentToolRegistry,
    tool_context: AgentToolContext,
    observations: list[AgentObservation],
    trade_context: dict[str, Any],
) -> AgentObservation:
    gated = _gated_step_observation(step, tool_registry=tool_registry, policy=command_runner.policy)
    if gated is not None:
        return gated
    if step.kind == "tool":
        raw_arguments = dict(step.arguments or {})
        arguments, placeholder_error = _resolve_tool_arguments(raw_arguments, observations)
        argument_payload = _tool_argument_payload(arguments if not placeholder_error else raw_arguments, raw_arguments=raw_arguments)
        if placeholder_error:
            return AgentObservation(
                step_id=step.step_id,
                kind="tool",
                status="error",
                content=placeholder_error,
                payload={
                    "tool_name": step.tool_name,
                    **argument_payload,
                    "placeholder_error": placeholder_error,
                },
            )
        if step.tool_name == "research.write_report" and not str(arguments.get("content") or "").strip():
            return AgentObservation(
                step_id=step.step_id,
                kind="tool",
                status="done",
                content="报告将在最终分析生成后保存。",
                payload={
                    "tool_name": step.tool_name,
                    **argument_payload,
                    "result": {
                        "status": "done",
                        "content": "deferred agent report",
                        "payload": {"deferred_report": True},
                        "data_names": ["报告"],
                        "artifacts": [],
                    },
                    "data_names": ["报告"],
                    "artifacts": [],
                },
            )
        result = tool_registry.execute(step.tool_name, arguments, tool_context)
        return AgentObservation(
            step_id=step.step_id,
            kind="tool",
            status=result.status,
            content=result.content,
            payload={
                "tool_name": step.tool_name,
                **argument_payload,
                "result": result.to_dict(),
                "data_names": list(result.data_names),
                "artifacts": list(result.artifacts),
            },
        )
    if step.kind == "command":
        result = command_runner.run(step.command, timeout=command_runner.policy.command_timeout)
        return AgentObservation(
            step_id=step.step_id,
            kind="command",
            status="done" if result.returncode == 0 else "error",
            content=result.output,
            payload={"argv": list(result.argv), "returncode": result.returncode, "status": result.status},
        )
    if step.kind == "python":
        result = python_runner.run(
            step.code,
            context={"message": message, "observations": [item.to_dict() for item in observations]},
        )
        trade_audits = []
        for intent in result.trade_intents:
            audit = trader.execute(_with_source(intent, trade_context.get("source_step_id", step.step_id)))
            trade_audits.append(audit.to_dict())
        return AgentObservation(
            step_id=step.step_id,
            kind="python",
            status=result.status,
            content=result.error or _stringify(result.result),
            payload={"result": _jsonable(result.result), "trade_audits": trade_audits},
        )
    if step.kind == "trade":
        intent = _trade_intent(step.trade, source_step_id=step.step_id)
        audit = trader.execute(intent)
        return AgentObservation(
            step_id=step.step_id,
            kind="trade",
            status="done" if audit.status in {"submitted", "dry_run", "done"} else "error",
            content=audit.message,
            payload=audit.to_dict(),
        )
    return AgentObservation(step_id=step.step_id, kind=step.kind, status="done", content=step.title or "done")


def _resolve_tool_arguments(arguments: dict[str, Any], observations: list[AgentObservation]) -> tuple[dict[str, Any], str]:
    resolved, error = _resolve_argument_placeholders(arguments, observations)
    if error:
        return arguments, error
    return dict(resolved) if isinstance(resolved, dict) else arguments, ""


def _resolve_argument_placeholders(value: Any, observations: list[AgentObservation]) -> tuple[Any, str]:
    if isinstance(value, dict):
        resolved: dict[str, Any] = {}
        for key, item in value.items():
            child, error = _resolve_argument_placeholders(item, observations)
            if error:
                return value, error
            resolved[key] = child
        return resolved, ""
    if isinstance(value, list):
        resolved_items: list[Any] = []
        for item in value:
            child, error = _resolve_argument_placeholders(item, observations)
            if error:
                return value, error
            if isinstance(item, str) and _STEP_PLACEHOLDER_RE.match(item.strip()) and isinstance(child, list):
                resolved_items.extend(child)
            else:
                resolved_items.append(child)
        return resolved_items, ""
    if isinstance(value, tuple):
        resolved_list, error = _resolve_argument_placeholders(list(value), observations)
        return (tuple(resolved_list), "") if not error else (value, error)
    if isinstance(value, str):
        return _resolve_string_placeholder(value, observations)
    return value, ""


def _resolve_string_placeholder(value: str, observations: list[AgentObservation]) -> tuple[Any, str]:
    text = value.strip()
    match = _STEP_PLACEHOLDER_RE.match(text)
    if not match:
        return value, ""
    resolved, error = _placeholder_value(
        text,
        observations,
        step_id=str(match.group("step") or ""),
        path=str(match.group("path") or ""),
        start=str(match.group("start") or ""),
        end=str(match.group("end") or ""),
    )
    return (value, error) if error else (resolved, "")


def _placeholder_value(
    placeholder: str,
    observations: list[AgentObservation],
    *,
    step_id: str,
    path: str,
    start: str,
    end: str,
) -> tuple[Any, str]:
    observation = next((item for item in observations if item.step_id == step_id), None)
    if observation is None:
        return None, f"unresolved agent placeholder {placeholder}: observation {step_id} not found"
    payload = observation.payload if isinstance(observation.payload, dict) else {}
    if path == "symbols":
        value: Any = _collect_symbol_values(payload)
        if not value:
            return None, f"unresolved agent placeholder {placeholder}: no symbols found in observation {step_id}"
    else:
        found, value = _lookup_payload_path(payload, path.split("."))
        if not found:
            return None, f"unresolved agent placeholder {placeholder}: path {path} not found in observation {step_id}"
    if start or end:
        if not isinstance(value, list):
            return None, f"unresolved agent placeholder {placeholder}: slice requires a list value"
        value = value[int(start) if start else None : int(end) if end else None]
    return value, ""


def _lookup_payload_path(value: Any, path: list[str]) -> tuple[bool, Any]:
    current = value
    for part in path:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _collect_symbol_values(value: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                clean_key = str(key or "")
                if clean_key == "symbols":
                    if isinstance(item, (list, tuple, set)):
                        for symbol in item:
                            if isinstance(symbol, (dict, list, tuple, set)):
                                walk(symbol)
                            else:
                                add(symbol)
                    elif not isinstance(item, dict):
                        add(item)
                elif clean_key == "ts_code":
                    add(item)
                walk(item)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)

    walk(value)
    return result


def _tool_argument_payload(arguments: dict[str, Any], *, raw_arguments: dict[str, Any]) -> dict[str, Any]:
    payload = {"arguments": _jsonable(arguments)}
    if arguments != raw_arguments:
        payload["raw_arguments"] = _jsonable(raw_arguments)
    return payload


def _gated_step_observation(step: Any, *, tool_registry: AgentToolRegistry, policy: AgentExecutionPolicy) -> AgentObservation | None:
    side_effect = str(getattr(step, "side_effect", "") or "readonly")
    requires_confirmation = bool(getattr(step, "requires_confirmation", False))
    if getattr(step, "kind", "") == "tool":
        spec = tool_registry.get(str(getattr(step, "tool_name", "") or ""))
        if spec is not None:
            side_effect = side_effect or spec.side_effect
            requires_confirmation = requires_confirmation or bool(spec.requires_confirmation)
    if requires_confirmation:
        return AgentObservation(
            step_id=step.step_id,
            kind=step.kind,
            status="error",
            content=f"{step.title or step.step_id} requires confirmation before execution",
            payload={"requires_confirmation": True, "side_effect": side_effect},
        )
    if bool(getattr(policy, "dry_run", False)) and side_effect in {"write_artifact", "long_running", "live_trade"}:
        return AgentObservation(
            step_id=step.step_id,
            kind=step.kind,
            status="done",
            content=f"dry-run skipped {step.title or step.step_id}",
            payload={"dry_run_skipped": True, "side_effect": side_effect},
        )
    return None


def _with_step_verification(step: Any, observation: AgentObservation) -> AgentObservation:
    payload = dict(observation.payload or {})
    if payload.get("dry_run_skipped"):
        status = "skipped"
        message = "dry-run skipped side-effectful step"
    elif observation.status == "done":
        status = "passed"
        message = "step completed"
    else:
        status = "failed"
        message = observation.content or "step failed"
    payload["verification"] = {
        "name": f"{step.step_id}_completed",
        "status": status,
        "message": message,
        "success_criteria": str(getattr(step, "success_criteria", "") or ""),
    }
    return AgentObservation(
        step_id=observation.step_id,
        kind=observation.kind,
        status=observation.status,
        content=observation.content,
        payload=payload,
    )


def _with_replan_decision(observation: AgentObservation, decision: dict[str, Any]) -> AgentObservation:
    payload = dict(observation.payload or {})
    payload["replan_decision"] = decision
    return AgentObservation(
        step_id=observation.step_id,
        kind=observation.kind,
        status=observation.status,
        content=observation.content,
        payload=payload,
    )


def _trade_intent(payload: dict[str, Any], *, source_step_id: str) -> TradeIntent:
    return TradeIntent(
        ts_code=str(payload.get("ts_code") or payload.get("symbol") or ""),
        side=str(payload.get("side") or ""),
        quantity=int(payload["quantity"]) if payload.get("quantity") not in (None, "") else None,
        price_type=str(payload.get("price_type") or "latest"),
        price=float(payload["price"]) if payload.get("price") not in (None, "") else None,
        reason=str(payload.get("reason") or ""),
        source_step_id=str(payload.get("source_step_id") or source_step_id),
    )


def _with_source(intent: TradeIntent, source_step_id: str) -> TradeIntent:
    if intent.source_step_id:
        return intent
    return TradeIntent(
        ts_code=intent.ts_code,
        side=intent.side,
        quantity=intent.quantity,
        price_type=intent.price_type,
        price=intent.price,
        reason=intent.reason,
        source_step_id=source_step_id,
    )


def _replan_request(message: str, observations: list[AgentObservation], decision: dict[str, Any]) -> str:
    return (
        "这是一次 SATS Agent error recovery replan。请基于原目标、已完成 observations 和最新失败原因，"
        "输出新的严格 JSON plan，只包含后续替代步骤或 final。不要重复已经成功的步骤。"
        "不要规划会再次触发同一错误签名的步骤。不要绕过 confirmation、auto_trade、live_trading 或人工审批限制。"
        "只能使用 planner 上下文里真实存在的工具、SATS argv 命令和 provider/dataset；不要编造 provider 接口、tool 或 dataset。"
        f"\n原目标：{message}"
        f"\nerror_recovery_decision={json.dumps(decision, ensure_ascii=False, default=str)}"
        f"\nobservations={json.dumps([item.to_dict() for item in observations], ensure_ascii=False, default=str)}"
    )


def _catalog_replan_request(message: str, observations: list[AgentObservation]) -> str:
    return (
        "这是一次 SATS Agent catalog replan。你刚刚拿到了 SATS 数据目录 observation。"
        "如果 observation 来自 data.astock_catalog 且原目标需要真实数据，请从 astock_catalog.items 中选择最匹配的 operation，"
        "规划 data.astock_fetch；dataset 类 operation 使用 tushare.dataset.fetch 或 akshare.dataset.fetch，并把 dataset 放入 params.dataset。"
        "如果 observation 来自旧 AkShare 数据字典，请从 observation 中选择最匹配的 dataset，规划 data.describe_akshare_dataset 或 data.get_akshare_data；"
        "如果只是询问接口清单，则直接 final。不要编造未出现在 catalog 的 operation 或 dataset。"
        f"\n原目标：{message}\nobservations={json.dumps([item.to_dict() for item in observations], ensure_ascii=False, default=str)}"
    )


def _should_replan_after_catalog(message: str, step: AgentStep, remaining_steps: list[AgentStep]) -> bool:
    if step.kind != "tool" or step.tool_name not in {"data.astock_catalog", "data.list_akshare_datasets", "data.describe_akshare_dataset"}:
        return False
    if step.tool_name == "data.astock_catalog" and any(item.kind == "tool" and item.tool_name == "data.astock_fetch" for item in remaining_steps):
        return False
    if any(item.kind == "tool" and item.tool_name == "data.get_akshare_data" for item in remaining_steps):
        return False
    text = str(message or "")
    if any(term in text for term in ("有哪些", "列出", "清单", "目录")) and not any(term in text for term in ("获取", "取数", "查询数据", "分析")):
        return False
    lowered = text.lower()
    return any(term in lowered for term in ("akshare", "ak share")) or any(term in text for term in ("数据", "行情", "指标", "分析", "查询", "获取", "取数"))


def _replan_decision(
    observation: AgentObservation,
    *,
    previous_observations: list[AgentObservation],
    replanned_error_signatures: set[str],
    llm_factory: Callable[..., Any] | None,
) -> dict[str, Any]:
    signature = _error_signature(observation)
    category = _error_category(observation)
    repeated = bool(signature) and (
        signature in replanned_error_signatures
        or any(_error_signature(item) == signature for item in previous_observations if item.status == "error")
    )
    decision = {
        "error_category": category,
        "error_signature": signature,
        "should_replan": False,
        "reason": "",
        "repeated": repeated,
    }
    if repeated:
        decision["reason"] = "same error signature already failed; stop to avoid retry loop"
        return decision
    if category in {
        "requires_confirmation",
        "trade_permission",
        "live_trading_permission",
        "permission_required",
        "trade_permission",
        "trade_blocked",
        "dependency_error",
    }:
        decision["reason"] = f"{category} requires user authorization or direct action"
        return decision
    if llm_factory is None:
        decision["reason"] = "no LLM planner is available for error recovery"
        return decision
    decision["should_replan"] = True
    decision["reason"] = "recoverable runtime error; ask planner for alternative remaining steps"
    return decision


def _error_signature(observation: AgentObservation) -> str:
    payload = observation.payload if isinstance(observation.payload, dict) else {}
    content = _compact_error_text(_error_content(observation))
    if observation.kind == "command":
        argv = " ".join(str(item) for item in payload.get("argv") or [])
        return f"command:{argv}:returncode={payload.get('returncode')}:{content}"
    if observation.kind == "tool":
        tool_name = str(payload.get("tool_name") or "")
        return f"tool:{tool_name}:{content}"
    if observation.kind == "trade":
        side = str(payload.get("intent", {}).get("side") if isinstance(payload.get("intent"), dict) else "")
        return f"trade:{side}:{content}"
    return f"{observation.kind}:{content}" if content else observation.kind


def _error_category(observation: AgentObservation) -> str:
    payload = observation.payload if isinstance(observation.payload, dict) else {}
    result_payload = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    envelope = result_payload.get("payload") if isinstance(result_payload.get("payload"), dict) else {}
    failure = envelope.get("failure") if isinstance(envelope.get("failure"), dict) else {}
    if failure.get("category"):
        return str(failure["category"])
    content = _error_content(observation)
    if payload.get("requires_confirmation"):
        return "requires_confirmation"
    classified = classify_failure(
        content,
        exception_type="AgentObservationError",
        stage="runtime",
        tool=str(payload.get("tool_name") or ""),
    )
    if observation.kind == "trade" or str(payload.get("tool_name") or "").startswith("trade."):
        return classified if classified in {"trade_permission", "trade_blocked", "permission_required"} else "trade_blocked"
    if observation.kind == "command":
        return "command_failed"
    if observation.kind == "python":
        return "python_code_error"
    return classified


def _with_structured_failure(
    observation: AgentObservation,
    *,
    settings: Settings,
    recorder: ChatTurnRecorder,
    store: ChatMemoryStore,
) -> AgentObservation:
    if observation.status != "error":
        return observation
    payload = dict(observation.payload or {})
    result_payload = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    result_envelope = result_payload.get("payload") if isinstance(result_payload.get("payload"), dict) else {}
    if isinstance(result_envelope.get("failure"), dict) or isinstance(payload.get("failure"), dict):
        return observation
    category = _error_category(observation)
    failure = failure_from_message(
        _error_content(observation),
        project_root=getattr(settings, "project_root", "."),
        stage="runtime",
        tool=str(payload.get("tool_name") or observation.kind),
        category=category,
        exception_type="AgentObservationError",
    )
    payload["failure"] = failure.to_dict()
    try:
        store.add_agent_failure(
            failure,
            turn_id=recorder.turn_id,
            session_id=recorder.turn.session_id,
            status="exhausted",
        )
    except Exception:
        pass
    recorder.emit(
        "failure_detected",
        item_type="agent_step",
        item_name=observation.step_id,
        status="error",
        content=failure.message,
        payload=failure.to_dict(),
    )
    return AgentObservation(
        step_id=observation.step_id,
        kind=observation.kind,
        status=observation.status,
        content=observation.content,
        payload=payload,
    )


def _error_content(observation: AgentObservation) -> str:
    payload = observation.payload if isinstance(observation.payload, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    return str(result.get("content") or observation.content or "").strip()


def _compact_error_text(value: str, *, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _data_names(observations: list[AgentObservation]) -> tuple[str, ...]:
    names = ["Agent"]
    for obs in observations:
        for name in obs.payload.get("data_names") or []:
            if name and name not in names:
                names.append(str(name))
        result = obs.payload.get("result") if isinstance(obs.payload.get("result"), dict) else {}
        for name in result.get("data_names") or []:
            if name and name not in names:
                names.append(str(name))
    return tuple(names)


def _artifacts(observations: list[AgentObservation]) -> tuple[dict[str, Any], ...]:
    artifacts: list[dict[str, Any]] = []
    for obs in observations:
        for artifact in obs.payload.get("artifacts") or []:
            if isinstance(artifact, dict):
                artifacts.append(artifact)
        result = obs.payload.get("result") if isinstance(obs.payload.get("result"), dict) else {}
        for artifact in result.get("artifacts") or []:
            if isinstance(artifact, dict):
                artifacts.append(artifact)
    return tuple(artifacts)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_jsonable(value), ensure_ascii=False, default=str)
    return str(value)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if hasattr(value, "shape"):
        return {"type": type(value).__name__, "shape": list(getattr(value, "shape", ()))}
    if isinstance(value, Path):
        return str(value)
    return str(value)
