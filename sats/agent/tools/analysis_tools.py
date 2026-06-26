from __future__ import annotations

import json
from typing import Any

from sats.agent.python_runtime import RestrictedPythonRuntime
from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema


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
                "用于没有合适现成工具时做有界数据整理、排序和计算。"
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
    timeout = _positive_int(arguments.get("timeout_seconds"), default=30, maximum=120)
    row_limit = _positive_int(arguments.get("row_limit"), default=200, maximum=1000)
    runtime = RestrictedPythonRuntime(resolver=resolver, timeout_seconds=timeout)
    result = runtime.run(
        str(arguments.get("code") or ""),
        context={
            "task": str(arguments.get("task") or context.message or ""),
            "observations": [item.to_dict() if hasattr(item, "to_dict") else item for item in context.observations],
            "observation_refs": [str(item) for item in (arguments.get("observation_refs") or [])],
            "row_limit": row_limit,
        },
    )
    payload = _normalize_program_payload(result.result, row_limit=row_limit)
    payload["status"] = result.status
    payload["task"] = str(arguments.get("task") or context.message or "")
    payload["allowed_resolver_methods"] = sorted(allowed_methods)
    if result.error:
        payload["error"] = result.error
    content = json.dumps(payload, ensure_ascii=False, default=str)
    return AgentToolResult(
        status=result.status,
        content=content,
        payload={"python_program": payload},
        data_names=("python_program",),
    )


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
