from __future__ import annotations

import json
import time
from typing import Any

from sats.agent.python_runtime import PythonRunResult, RestrictedPythonRuntime
from sats.agent.recovery import failure_from_message, redact_text
from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema
from sats.llm import build_standard_llm, extract_json_object


DEFAULT_ALLOWED_RESOLVER_METHODS = {
    "load_index_daily",
    "load_indicator_inputs",
    "load_realtime_quotes",
    "load_stock_daily",
    "load_stock_minute",
}


def analysis_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="analysis.python_program",
            description=(
                "执行只读受限 Python 分析程序；只能读取 SATS resolver 和前序 observations，"
                "用于没有合适现成工具时做有界数据整理、排序和计算。允许导入已安装模块，"
                "但禁止文件、进程、网络和动态执行等危险行为；"
                "程序必须写成 def run(context): ... 并 return 结构化结果；不要在顶层直接访问 context。"
                "读取前序工具结构化结果时优先使用 context['observations_by_step'][step_id]"
                "['payload']['result']['payload']，不要解析 content JSON 字符串。"
            ),
            category="analysis",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "task": {"type": "string"},
                    "code": {"type": "string"},
                    "expected_schema": {"type": "object"},
                    "observation_refs": {"type": "array", "items": {"type": "string"}},
                    "resolver_methods": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer"},
                    "row_limit": {"type": "integer"},
                },
                ["task", "code"],
            ),
            executor=_python_program,
            metadata={
                "domain": "analysis",
                "subject_grain": "generic",
                "metric_grain": "computed",
                "time_scope": "bounded",
                "output_shape": "json_table",
                "enumerates_universe": False,
                "requires_symbols": False,
                "writes_db": False,
            },
        )
    ]


def _python_program(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    allowed_methods = _allowed_resolver_methods(arguments.get("resolver_methods"))
    resolver = _ResolverProxy(context.resolver, allowed_methods)
    timeout = _positive_int(arguments.get("timeout_seconds"), default=30, maximum=30)
    deadline = time.monotonic() + timeout
    row_limit = _positive_int(arguments.get("row_limit"), default=200, maximum=1000)
    code = str(arguments.get("code") or "")
    preflight_error = _python_program_preflight_error(code)
    runtime = RestrictedPythonRuntime(
        resolver=resolver,
        timeout_seconds=timeout,
        project_root=getattr(context.settings, "project_root", "."),
    )
    observation_rows = _observation_dicts(context.observations)
    program_context = {
        "task": str(arguments.get("task") or context.message or ""),
        "observations": observation_rows,
        "observations_by_step": _observations_by_step(observation_rows),
        "observation_refs": [str(item) for item in (arguments.get("observation_refs") or [])],
        "row_limit": row_limit,
    }
    if preflight_error:
        failure = failure_from_message(
            preflight_error,
            project_root=getattr(context.settings, "project_root", "."),
            stage="python_validation",
            tool="analysis.python_program",
            category="python_code_error",
        )
        result = PythonRunResult(status="error", error=failure.message, failure=failure.to_dict())
    else:
        result = runtime.run(code, context=program_context)
    recovery_attempts: list[dict[str, Any]] = []
    mode = str(getattr(context.settings, "self_repair_mode", "propose") or "propose").lower()
    max_attempts = min(1, max(0, int(getattr(context.settings, "self_repair_max_attempts", 2) or 0)))
    if result.status != "done" and mode in {"runtime", "propose"} and context.recovery_depth == 0:
        revised_code = code
        for attempt in range(1, max_attempts + 1):
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                recovery_attempts.append({"attempt": attempt, "strategy": "revise_python_program", "status": "skipped", "detail": "tool budget exhausted"})
                break
            revised = _revise_python_program(
                context,
                task=str(arguments.get("task") or context.message or ""),
                code=revised_code,
                failure=result.failure or {"message": result.error},
                observations=observation_rows,
                allowed_methods=allowed_methods,
                timeout_seconds=remaining,
            )
            if not revised or revised == revised_code:
                recovery_attempts.append({"attempt": attempt, "strategy": "revise_python_program", "status": "rejected"})
                break
            preflight_error = _python_program_preflight_error(revised)
            if preflight_error:
                recovery_attempts.append(
                    {"attempt": attempt, "strategy": "revise_python_program", "status": "rejected", "detail": preflight_error}
                )
                failure = failure_from_message(
                    preflight_error,
                    project_root=getattr(context.settings, "project_root", "."),
                    stage="python_validation",
                    tool="analysis.python_program",
                    attempt=attempt,
                    category="python_code_error",
                )
                result = PythonRunResult(status="error", error=failure.message, failure=failure.to_dict())
                revised_code = revised
                continue
            revised_code = revised
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                recovery_attempts.append({"attempt": attempt, "strategy": "revise_python_program", "status": "skipped", "detail": "tool budget exhausted"})
                break
            revised_runtime = RestrictedPythonRuntime(
                resolver=resolver,
                timeout_seconds=remaining,
                project_root=getattr(context.settings, "project_root", "."),
            )
            result = revised_runtime.run(revised_code, context=program_context)
            recovery_attempts.append(
                {
                    "attempt": attempt,
                    "strategy": "revise_python_program",
                    "status": "done" if result.status == "done" else "error",
                    "failure": result.failure,
                }
            )
            if result.status == "done":
                code = revised_code
                break
    payload = _normalize_program_payload(result.result, row_limit=row_limit)
    payload["status"] = result.status
    payload["task"] = str(arguments.get("task") or context.message or "")
    payload["allowed_resolver_methods"] = sorted(allowed_methods)
    if result.error:
        payload["error"] = result.error
    payload["code"] = code
    payload["failure"] = result.failure
    payload["recovery_attempts"] = recovery_attempts
    content = json.dumps(payload, ensure_ascii=False, default=str)
    return AgentToolResult(
        status=result.status,
        content=content,
        payload={"python_program": payload, "failure": result.failure},
        data_names=("python_program",),
    )


def _revise_python_program(
    context: AgentToolContext,
    *,
    task: str,
    code: str,
    failure: dict[str, Any],
    observations: list[dict[str, Any]],
    allowed_methods: set[str],
    timeout_seconds: int,
) -> str:
    if context.llm_factory is None:
        return ""
    prompt = [
        {
            "role": "system",
            "content": (
                "你负责修订 SATS 的只读受限 Python 分析程序。只输出严格 JSON："
                '{"code":"完整的 def run(context): ...","reason":"修订原因"}。'
                "不得读写文件、启动进程、访问网络、动态执行代码、绕过 resolver 或发起交易。"
                "必须保留 def run(context) 并返回 JSON 可序列化结果；行情只能来自已有 observations 或允许的 resolver 方法。"
            ),
        },
        {
            "role": "user",
            "content": redact_text(json.dumps(
                {
                    "task": task,
                    "code": code,
                    "failure": failure,
                    "allowed_resolver_methods": sorted(allowed_methods),
                    "observations": observations[-8:],
                },
                ensure_ascii=False,
                default=str,
            )[:24000], project_root=getattr(context.settings, "project_root", None)),
        },
    ]
    try:
        llm = build_standard_llm(
            context.llm_factory,
            model_name=str(getattr(context.settings, "openai_model", "") or ""),
            timeout_seconds=timeout_seconds,
        )
        try:
            response = llm.chat(prompt, timeout=timeout_seconds)
        except TypeError:
            response = llm.chat(prompt)
        parsed = extract_json_object(str(getattr(response, "content", "") or ""))
    except Exception:
        return ""
    return str(parsed.get("code") or "").strip() if isinstance(parsed, dict) else ""


def _python_program_preflight_error(code: str) -> str:
    clean = str(code or "")
    if "context" in clean and "def run(" not in clean:
        return "analysis.python_program code must define def run(context): and access context inside that function, not at top level"
    return ""


def _observation_dicts(observations: tuple[Any, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in observations:
        if hasattr(item, "to_dict"):
            value = item.to_dict()
        elif isinstance(item, dict):
            value = dict(item)
        else:
            value = {"value": item}
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _observations_by_step(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("step_id")): item for item in observations if str(item.get("step_id") or "").strip()}


class _ResolverProxy:
    def __init__(self, resolver: Any, allowed_methods: set[str]) -> None:
        self._resolver = resolver
        self._allowed_methods = allowed_methods

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._allowed_methods:
            raise AttributeError(f"resolver method not allowed: {name}")
        value = getattr(self._resolver, name)
        if not callable(value):
            raise AttributeError(name)
        return value


def _allowed_resolver_methods(raw: Any) -> set[str]:
    if isinstance(raw, list) and raw:
        selected = {str(item).strip() for item in raw if str(item).strip()}
        return {item for item in selected if item in DEFAULT_ALLOWED_RESOLVER_METHODS}
    return set(DEFAULT_ALLOWED_RESOLVER_METHODS)


def _normalize_program_payload(value: Any, *, row_limit: int) -> dict[str, Any]:
    if isinstance(value, dict):
        payload = dict(value)
    else:
        payload = {"result": value}
    for key in ("rows", "data"):
        if isinstance(payload.get(key), list) and len(payload[key]) > row_limit:
            payload[key] = payload[key][:row_limit]
            payload["truncated"] = True
            payload["row_limit"] = row_limit
    payload.setdefault("provenance", [])
    payload.setdefault("data_sources", {})
    payload.setdefault("missing_fields", [])
    return payload


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed <= 0:
        parsed = default
    return min(parsed, maximum)
