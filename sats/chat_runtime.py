from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from sats.backtesting.service import format_backtest_report, run_strategy_backtest
from sats.backtesting.strategy_spec import StrategySpec, strategy_draft_python, strategy_spec_from_request, validate_strategy_spec
from sats.chat_artifacts import (
    ArtifactWriteResult,
    chat_artifact_dir,
    save_json_artifact,
    save_markdown_artifact,
)
from sats.config import Settings
from sats.memory import ChatMemoryStore
from sats.storage.duckdb import DuckDBStorage


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    session_id: str
    turn_id: str
    request: str
    project_root: Path
    symbols: tuple[str, ...] = ()
    trade_date: str = ""
    memory_enabled: bool = True


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    workflow_kind: str
    title: str
    symbols: tuple[str, ...] = ()
    data_requirements: tuple[str, ...] = ()
    tool_sequence_hint: tuple[str, ...] = ()
    requires_approval: bool = False
    action_type: str = ""
    risk_level: str = "low"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_kind": self.workflow_kind,
            "title": self.title,
            "symbols": list(self.symbols),
            "data_requirements": list(self.data_requirements),
            "tool_sequence_hint": list(self.tool_sequence_hint),
            "requires_approval": self.requires_approval,
            "action_type": self.action_type,
            "risk_level": self.risk_level,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RuntimeItem:
    item_id: str
    item_type: str
    item_name: str
    status: str = "running"


@dataclass(frozen=True, slots=True)
class RuntimeArtifact:
    kind: str
    title: str
    path: str
    mime_type: str
    summary: str = ""
    artifact_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "path": self.path,
            "mime_type": self.mime_type,
            "summary": self.summary,
            "artifact_id": self.artifact_id,
        }


@dataclass(frozen=True, slots=True)
class PendingRuntimeAction:
    action_id: str
    action_type: str
    title: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    content: str
    plan: ResearchPlan
    artifacts: tuple[RuntimeArtifact, ...] = ()
    pending_action: PendingRuntimeAction | None = None
    tool_call_count: int = 0
    data_names: tuple[str, ...] = ()
    status: str = "done"


RUNTIME_WORKFLOW_KINDS = {
    "report",
    "strategy_draft",
    "backtest",
    "code_artifact",
    "complex_research",
    "blocked",
}


def is_runtime_request(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    if _is_blocked_runtime_request(text):
        return True
    report_terms = ("生成报告", "研究报告", "输出报告", "保存报告", "导出报告", "写成报告")
    strategy_terms = ("策略", "回测", "backtest", "代码", "code", "strategy")
    artifact_terms = ("保存为文件", "生成文件", "产物", "artifact", "导出")
    complex_terms = ("分步骤", "多步", "调用工具", "工具编排")
    return any(term in text for term in (*report_terms, *strategy_terms, *artifact_terms, *complex_terms))


def build_research_plan(message: str, *, symbols: tuple[str, ...] = ()) -> ResearchPlan:
    text = str(message or "").strip()
    lower = text.lower()
    if _is_blocked_runtime_request(lower):
        return ResearchPlan(
            workflow_kind="blocked",
            title="受限请求",
            symbols=symbols,
            risk_level="high",
            reason="请求涉及 shell、危险代码执行或自动交易，SATS runtime v1 不支持。",
        )
    if "回测" in lower or "backtest" in lower:
        return ResearchPlan(
            workflow_kind="backtest",
            title="策略草稿与轻量回测",
            symbols=symbols,
            data_requirements=("A股日线", "策略spec"),
            tool_sequence_hint=("generate_strategy_spec", "run_light_backtest"),
            requires_approval=True,
            action_type="strategy_backtest",
            risk_level="medium",
            reason="回测会写入策略和报告产物，需用户确认。",
        )
    if "策略" in lower or "代码" in lower or "strategy" in lower or "code" in lower:
        return ResearchPlan(
            workflow_kind="strategy_draft",
            title="策略草稿",
            symbols=symbols,
            data_requirements=("策略spec",),
            tool_sequence_hint=("generate_strategy_spec",),
            requires_approval=True,
            action_type="strategy_draft",
            risk_level="medium",
            reason="策略草稿会写入文件产物，需用户确认。",
        )
    if "报告" in text or "保存" in text or "导出" in text:
        return ResearchPlan(
            workflow_kind="report",
            title="SATS 研究报告",
            symbols=symbols,
            data_requirements=("聊天上下文",),
            tool_sequence_hint=("write_markdown_report",),
            reason="用户要求生成或保存研究报告。",
        )
    return ResearchPlan(
        workflow_kind="complex_research",
        title="复杂研究任务",
        symbols=symbols,
        data_requirements=("按需工具",),
        tool_sequence_hint=("llm_tool_loop",),
        reason="用户请求需要多步工具编排。",
    )


class ChatResearchRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ChatMemoryStore | None,
        recorder: Any,
        llm: Any,
        tool_registry: Any,
        max_iterations: int = 4,
    ) -> None:
        self.settings = settings
        self.store = store
        self.recorder = recorder
        self.llm = llm
        self.tool_registry = tool_registry
        self.max_iterations = max(1, int(max_iterations or 1))

    def run(self, context: RuntimeContext, *, plan: ResearchPlan | None = None) -> RuntimeResult:
        plan = plan or build_research_plan(context.request, symbols=context.symbols)
        self.recorder.emit(
            "runtime_started",
            item_type="runtime",
            item_name=plan.workflow_kind,
            status="running",
            payload={"plan": plan.to_dict()},
        )
        item_id = self.recorder.start_item("runtime", plan.workflow_kind, input_payload={"request": context.request, "plan": plan.to_dict()})
        started = time.monotonic()
        try:
            if plan.workflow_kind == "blocked":
                content = (
                    "该请求涉及 SATS runtime v1 的安全边界，不能执行 shell、危险代码或自动交易。"
                    "可以改为生成只读研究报告、受限策略草稿或轻量回测计划。"
                )
                self.recorder.complete_item(item_id, output_payload={"content": content}, duration_seconds=_elapsed(started))
                self.recorder.emit("runtime_completed", item_type="runtime", item_name=plan.workflow_kind, status="done")
                return RuntimeResult(content=content, plan=plan, data_names=("Runtime",), status="done")
            if plan.requires_approval:
                pending = self._create_pending_action(context, plan)
                content = _pending_action_message(pending, plan)
                self.recorder.complete_item(
                    item_id,
                    output_payload={"pending_action_id": pending.action_id, "action_type": pending.action_type},
                    duration_seconds=_elapsed(started),
                )
                self.recorder.emit("runtime_completed", item_type="runtime", item_name=plan.workflow_kind, status="done")
                return RuntimeResult(
                    content=content,
                    plan=plan,
                    pending_action=pending,
                    data_names=("Runtime", "待确认动作"),
                    status="waiting_approval",
                )
            content, tool_count = self._run_llm_tool_loop(context, plan)
            artifacts: tuple[RuntimeArtifact, ...] = ()
            if plan.workflow_kind == "report":
                artifact = self._write_report_artifact(context, plan, content)
                artifacts = (artifact,)
                content = f"{content.rstrip()}\n\n报告: {artifact.path}"
            self.recorder.complete_item(
                item_id,
                output_payload={"content": content, "tool_call_count": tool_count},
                artifact_paths=[artifact.path for artifact in artifacts],
                duration_seconds=_elapsed(started),
            )
            self.recorder.emit("runtime_completed", item_type="runtime", item_name=plan.workflow_kind, status="done")
            return RuntimeResult(
                content=content or "Runtime 未生成有效响应。",
                plan=plan,
                artifacts=artifacts,
                tool_call_count=tool_count,
                data_names=("Runtime",) + (("产物",) if artifacts else ()),
            )
        except Exception as exc:
            self.recorder.fail_item(item_id, exc, duration_seconds=_elapsed(started))
            raise

    def _create_pending_action(self, context: RuntimeContext, plan: ResearchPlan) -> PendingRuntimeAction:
        spec = strategy_spec_from_request(context.request, symbols=list(context.symbols))
        payload = {
            "message": context.request,
            "plan": plan.to_dict(),
            "spec": spec.to_dict(),
            "session_id": context.session_id,
            "turn_id": context.turn_id,
        }
        action_id = self.recorder.require_approval(
            action_type=plan.action_type or plan.workflow_kind,
            title=plan.title,
            payload=payload,
        )
        return PendingRuntimeAction(
            action_id=action_id,
            action_type=plan.action_type or plan.workflow_kind,
            title=plan.title,
            payload=payload,
        )

    def _write_report_artifact(self, context: RuntimeContext, plan: ResearchPlan, content: str) -> RuntimeArtifact:
        write = save_markdown_artifact(
            project_root=context.project_root,
            session_id=context.session_id,
            turn_id=context.turn_id,
            title=plan.title,
            content=content,
            filename="research_report.md",
            report=True,
            meta={"workflow_kind": plan.workflow_kind},
        )
        artifact_id = self.recorder.create_artifact(
            kind=write.kind,
            title=write.title,
            path=str(write.path),
            mime_type=write.mime_type,
            summary=write.summary,
            meta=write.meta or {},
        )
        return _runtime_artifact(write, artifact_id=artifact_id)

    def _run_llm_tool_loop(self, context: RuntimeContext, plan: ResearchPlan) -> tuple[str, int]:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 SATS-native research runtime。你可以调用白名单只读研究工具，"
                    "但不能声称执行 shell、自动交易或运行未确认代码。输出应简洁、可操作，并说明不构成投资建议。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"request": context.request, "runtime_plan": plan.to_dict()},
                    ensure_ascii=False,
                ),
            },
        ]
        definitions = self.tool_registry.definitions() if self.tool_registry is not None else []
        tool_call_count = 0
        try:
            response = self.llm.chat(messages, tools=definitions)
        except TypeError:
            response = self.llm.chat(messages)
        for iteration in range(1, self.max_iterations + 1):
            tool_calls = list(getattr(response, "tool_calls", None) or [])
            self.recorder.emit(
                "llm_completed",
                item_type="llm",
                item_name=f"iteration_{iteration}",
                status="done",
                payload={
                    "tool_calls": [str(getattr(call, "name", "")) for call in tool_calls],
                    **_runtime_model_meta(self.llm, settings=self.settings),
                },
            )
            if not tool_calls:
                return str(getattr(response, "content", "") or "").strip(), tool_call_count
            self.recorder.emit(
                "runtime_iteration_started",
                item_type="runtime",
                item_name=f"iteration_{iteration}",
                status="running",
                payload={"tool_call_count": len(tool_calls)},
            )
            messages.append(_assistant_tool_call_message(response))
            batches = _tool_batches(tool_calls, self.tool_registry)
            for mode, batch in batches:
                batch_started = time.monotonic()
                self.recorder.emit(
                    "tool_batch_started",
                    item_type="tool_batch",
                    item_name=mode,
                    status="running",
                    payload={"tools": [str(getattr(call, "name", "")) for call in batch]},
                )
                results = self._execute_batch(batch, parallel=mode == "parallel")
                tool_call_count += len(results)
                for call, result in results:
                    messages.append(_tool_result_message(call, result))
                self.recorder.emit(
                    "tool_batch_completed",
                    item_type="tool_batch",
                    item_name=mode,
                    status="done",
                    payload={"tools": [str(getattr(call, "name", "")) for call, _ in results]},
                    duration_seconds=_elapsed(batch_started),
                )
            try:
                response = self.llm.chat(messages, tools=definitions)
            except TypeError:
                response = self.llm.chat(messages)
        content = str(getattr(response, "content", "") or "").strip()
        return content or "Runtime 工具调用已达到上限，请缩小问题范围后重试。", tool_call_count

    def _execute_batch(self, tool_calls: list[Any], *, parallel: bool) -> list[tuple[Any, str]]:
        if not parallel or len(tool_calls) <= 1:
            return [(call, self._execute_tool(call)) for call in tool_calls]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(tool_calls))) as pool:
            futures = [pool.submit(self._execute_tool, call) for call in tool_calls]
            return [(call, futures[index].result()) for index, call in enumerate(tool_calls)]

    def _execute_tool(self, call: Any) -> str:
        name = str(getattr(call, "name", "") or "")
        args = getattr(call, "arguments", {}) or {}
        metadata = self.tool_registry.metadata(name) if self.tool_registry is not None else {}
        item_id = self.recorder.start_item("tool", name, input_payload={"arguments": _tool_event_arguments(args), "metadata": metadata})
        started = time.monotonic()
        self.recorder.emit(
            "tool_started",
            item_type="tool",
            item_name=name,
            status="running",
            payload={"tool": name, "arguments": _tool_event_arguments(args), "metadata": metadata},
        )
        try:
            result = self.tool_registry.execute(name, args) if self.tool_registry is not None else "{}"
            compacted = _compact_tool_result(result, int(metadata.get("max_result_chars") or 6000), self.recorder)
            self.recorder.complete_item(
                item_id,
                output_payload={"result_size": len(str(result or "")), "compacted": compacted != result},
                duration_seconds=_elapsed(started),
            )
            self.recorder.emit(
                "tool_completed",
                item_type="tool",
                item_name=name,
                status=_tool_result_status(result),
                payload={"tool": name, "result_size": len(str(result or "")), "compacted": compacted != result},
                duration_seconds=_elapsed(started),
            )
            return compacted
        except Exception as exc:
            self.recorder.fail_item(item_id, exc, duration_seconds=_elapsed(started))
            self.recorder.emit("tool_completed", item_type="tool", item_name=name, status="error", payload={"error": str(exc)})
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


def confirm_pending_runtime_action(
    action_id: str,
    *,
    settings: Settings,
    store: ChatMemoryStore | None = None,
) -> RuntimeResult:
    store = store or ChatMemoryStore(settings.db_path)
    action = store.get_pending_action(action_id)
    if action is None:
        raise ValueError(f"未找到待确认动作 {action_id}")
    if action["status"] != "pending":
        raise ValueError(f"动作 {action_id} 当前状态为 {action['status']}，不能确认执行")
    payload = dict(action.get("payload") or {})
    action_type = str(action.get("action_type") or "")
    if action_type not in {"strategy_draft", "strategy_backtest"}:
        raise ValueError(f"不支持的 runtime action: {action_type}")
    result = _execute_strategy_action(payload, settings=settings, store=store, run_backtest=action_type == "strategy_backtest")
    store.update_pending_action(action_id, status="done", result=result.to_dict())
    plan = build_research_plan(str(payload.get("message") or ""), symbols=tuple(result.spec.symbols))
    artifacts = tuple(_artifact_from_dict(item) for item in result.to_dict().get("artifacts", []))
    content = result.message
    if artifacts:
        content = f"{content}\n" + "\n".join(f"产物: {artifact.path}" for artifact in artifacts)
    return RuntimeResult(
        content=content,
        plan=plan,
        artifacts=artifacts,
        data_names=("Runtime", "产物") + (("轻量回测",) if action_type == "strategy_backtest" else ()),
    )


def reject_pending_runtime_action(
    action_id: str,
    *,
    settings: Settings,
    store: ChatMemoryStore | None = None,
) -> RuntimeResult:
    store = store or ChatMemoryStore(settings.db_path)
    action = store.get_pending_action(action_id)
    if action is None:
        raise ValueError(f"未找到待确认动作 {action_id}")
    store.update_pending_action(action_id, status="rejected", result={"message": "用户已取消"})
    plan = ResearchPlan(workflow_kind="complex_research", title=str(action.get("title") or "已取消"))
    return RuntimeResult(content=f"已取消待确认动作 {action_id}。", plan=plan, status="rejected")


def format_runtime_trace(store: ChatMemoryStore, *, turn_id: str = "", session_id: str = "") -> str:
    target_turn_id = str(turn_id or "").strip() or store.latest_chat_turn_id(session_id)
    if not target_turn_id:
        return "暂无 turn trace。"
    trace = store.get_chat_turn_trace(target_turn_id)
    if trace is None:
        return f"未找到 turn trace: {target_turn_id}"
    turn = trace["turn"]
    lines = [
        f"Turn: {turn['turn_id']}",
        f"Session: {turn['session_id']}",
        f"Status: {turn['status']}",
        f"Intent: {turn['intent'] or '-'}",
        f"Request: {turn['request']}",
    ]
    meta = turn.get("meta") if isinstance(turn.get("meta"), dict) else {}
    agent_plan = meta.get("agent_plan") if isinstance(meta.get("agent_plan"), dict) else {}
    if agent_plan:
        lines.extend(
            [
                f"Objective: {agent_plan.get('objective') or '-'}",
                f"Analysis Mode: {agent_plan.get('analysis_mode') or (agent_plan.get('natural_task') or {}).get('analysis_mode') or '-'}",
            ]
        )
        natural_task = agent_plan.get("natural_task") if isinstance(agent_plan.get("natural_task"), dict) else {}
        checks = natural_task.get("verification_checks") or agent_plan.get("verification_checks") or []
        if checks:
            lines.append("Verification:")
            for check in checks:
                if isinstance(check, dict):
                    lines.append(f"- {check.get('name')}: {check.get('status')}")
    if turn.get("duration_seconds"):
        lines.append(f"Duration: {turn['duration_seconds']:.2f}s")
    if trace["items"]:
        lines.append("")
        lines.append("Items:")
        for item in trace["items"]:
            label = f"{item['seq']}. {item['item_type']}:{item['item_name']} {item['status']}"
            if item.get("duration_seconds"):
                label += f" {item['duration_seconds']:.2f}s"
            lines.append(label)
    if trace["artifacts"]:
        lines.append("")
        lines.append("Artifacts:")
        for artifact in trace["artifacts"]:
            lines.append(f"- {artifact['kind']} {artifact['title']}: {artifact['path']}")
    if trace["events"]:
        lines.append("")
        lines.append("Events:")
        for event in trace["events"][-12:]:
            lines.append(f"- #{event['seq']} {event['event_type']} {event['item_type']}:{event['item_name']} {event['status']}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _ExecutedActionResult:
    spec: StrategySpec
    artifacts: tuple[RuntimeArtifact, ...]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "message": self.message,
        }


def _execute_strategy_action(
    payload: Mapping[str, Any],
    *,
    settings: Settings,
    store: ChatMemoryStore,
    run_backtest: bool,
) -> _ExecutedActionResult:
    spec = validate_strategy_spec(dict(payload.get("spec") or {}))
    session_id = str(payload.get("session_id") or "default")
    turn_id = str(payload.get("turn_id") or "runtime")
    project_root = Path(getattr(settings, "project_root", "."))
    spec_artifact = save_json_artifact(
        project_root=project_root,
        session_id=session_id,
        turn_id=turn_id,
        title="strategy_spec",
        payload=spec.to_dict(),
        filename="strategy_spec.json",
        summary="受限策略 spec",
    )
    draft_artifact = save_markdown_artifact(
        project_root=project_root,
        session_id=session_id,
        turn_id=turn_id,
        title="strategy_draft.py",
        content=strategy_draft_python(spec),
        filename="strategy_draft.py.md",
        report=False,
        summary="可读策略草稿，不由 runtime v1 执行",
    )
    artifacts = [
        _persist_artifact(store, session_id=session_id, turn_id=turn_id, write=spec_artifact),
        _persist_artifact(store, session_id=session_id, turn_id=turn_id, write=draft_artifact),
    ]
    store.add_chat_turn_item(
        turn_id=turn_id,
        item_type="runtime_action",
        item_name="strategy_draft",
        status="done",
        input_payload={"spec": spec.to_dict()},
        output_payload={"artifacts": [artifact.path for artifact in artifacts]},
        artifact_paths=[artifact.path for artifact in artifacts],
    )
    message = "策略草稿已生成。"
    if run_backtest:
        backtest = run_strategy_backtest(spec, settings=settings, storage=DuckDBStorage(settings.db_path))
        report = save_markdown_artifact(
            project_root=project_root,
            session_id=session_id,
            turn_id=turn_id,
            title="backtest_report",
            content=format_backtest_report(backtest),
            filename="backtest_report.md",
            report=True,
            summary="轻量回测报告",
        )
        metrics = save_json_artifact(
            project_root=project_root,
            session_id=session_id,
            turn_id=turn_id,
            title="backtest_metrics",
            payload=backtest.to_dict(),
            filename="backtest_metrics.json",
            summary="轻量回测指标",
        )
        artifacts.extend(
            [
                _persist_artifact(store, session_id=session_id, turn_id=turn_id, write=report),
                _persist_artifact(store, session_id=session_id, turn_id=turn_id, write=metrics),
            ]
        )
        store.add_chat_turn_item(
            turn_id=turn_id,
            item_type="backtest",
            item_name="lightweight_daily",
            status="done",
            input_payload={"spec": spec.to_dict()},
            output_payload={"metrics": backtest.metrics},
            artifact_paths=[artifact.path for artifact in artifacts],
        )
        message = "策略草稿与轻量回测已完成。"
    return _ExecutedActionResult(spec=spec, artifacts=tuple(artifacts), message=message)


def _persist_artifact(
    store: ChatMemoryStore,
    *,
    session_id: str,
    turn_id: str,
    write: ArtifactWriteResult,
) -> RuntimeArtifact:
    artifact_id = store.add_chat_artifact(
        session_id=session_id,
        turn_id=turn_id,
        kind=write.kind,
        title=write.title,
        path=str(write.path),
        mime_type=write.mime_type,
        summary=write.summary,
        meta=write.meta or {},
    )
    return _runtime_artifact(write, artifact_id=artifact_id)


def _runtime_artifact(write: ArtifactWriteResult, *, artifact_id: str = "") -> RuntimeArtifact:
    return RuntimeArtifact(
        kind=write.kind,
        title=write.title,
        path=str(write.path),
        mime_type=write.mime_type,
        summary=write.summary,
        artifact_id=artifact_id,
    )


def _artifact_from_dict(payload: Mapping[str, Any]) -> RuntimeArtifact:
    return RuntimeArtifact(
        kind=str(payload.get("kind") or ""),
        title=str(payload.get("title") or ""),
        path=str(payload.get("path") or ""),
        mime_type=str(payload.get("mime_type") or ""),
        summary=str(payload.get("summary") or ""),
        artifact_id=str(payload.get("artifact_id") or ""),
    )


def _pending_action_message(pending: PendingRuntimeAction, plan: ResearchPlan) -> str:
    return (
        f"已生成 {plan.title} 的待确认动作。\n"
        f"- action_id: {pending.action_id}\n"
        f"- 类型: {pending.action_type}\n"
        "确认后才会写入策略/回测产物；不会执行 shell、不会自动交易。\n"
        f"确认: /confirm {pending.action_id}\n"
        f"取消: /reject {pending.action_id}"
    )


def _tool_batches(tool_calls: list[Any], registry: Any) -> list[tuple[str, list[Any]]]:
    batches: list[tuple[str, list[Any]]] = []
    current_readonly: list[Any] = []
    for call in tool_calls:
        metadata = registry.metadata(str(getattr(call, "name", "") or "")) if registry is not None else {}
        if bool(metadata.get("readonly", True)):
            current_readonly.append(call)
            continue
        if current_readonly:
            batches.append(("parallel", current_readonly))
            current_readonly = []
        batches.append(("serial", [call]))
    if current_readonly:
        batches.append(("parallel", current_readonly))
    return batches


def _assistant_tool_call_message(response: Any) -> dict[str, Any]:
    calls = []
    for call in getattr(response, "tool_calls", None) or []:
        calls.append(
            {
                "id": str(getattr(call, "id", "") or ""),
                "type": "function",
                "function": {
                    "name": str(getattr(call, "name", "") or ""),
                    "arguments": json.dumps(getattr(call, "arguments", {}) or {}, ensure_ascii=False, default=str),
                },
            }
        )
    return {"role": "assistant", "content": str(getattr(response, "content", "") or ""), "tool_calls": calls}


def _tool_result_message(call: Any, result: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": str(getattr(call, "id", "") or ""),
        "name": str(getattr(call, "name", "") or ""),
        "content": str(result or ""),
    }


def _compact_tool_result(result: str, limit: int, recorder: Any) -> str:
    text = str(result or "")
    if len(text) <= max(1000, limit):
        return text
    recorder.emit("compact_started", item_type="tool_result", status="running", payload={"chars_before": len(text)})
    compacted = json.dumps(
        {"status": "ok", "compacted": True, "chars_before": len(text), "preview": text[: max(1000, limit)]},
        ensure_ascii=False,
    )
    recorder.emit("compact_completed", item_type="tool_result", status="done", payload={"chars_after": len(compacted)})
    return compacted


def _tool_result_status(result: str) -> str:
    try:
        payload = json.loads(str(result or ""))
    except Exception:
        return "done"
    return "error" if isinstance(payload, dict) and str(payload.get("status") or "").lower() == "error" else "done"


def _tool_event_arguments(arguments: Any) -> Any:
    if isinstance(arguments, (dict, list, tuple)):
        text = json.dumps(arguments, ensure_ascii=False, default=str)
        return arguments if len(text) <= 2000 else {"truncated_json": text[:2000]}
    text = str(arguments or "")
    return text if len(text) <= 2000 else text[:2000]


def _is_blocked_runtime_request(text: str) -> bool:
    blocked_terms = ("shell", "bash", "subprocess", "系统命令", "自动交易", "自动下单", "直接买入", "直接卖出")
    return any(term in text for term in blocked_terms)


def _runtime_model_meta(llm: Any, *, settings: Settings) -> dict[str, str]:
    return {
        "phase": "runtime",
        "model_policy": "standard",
        "model_profile": str(getattr(llm, "profile", "") or "default"),
        "model_name": str(getattr(llm, "model_name", "") or getattr(settings, "openai_model", "") or "LLM"),
    }


def _elapsed(started: float) -> float:
    return max(0.0, time.monotonic() - started)
