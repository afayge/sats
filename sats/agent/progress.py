from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Callable


def agent_progress_event_sink(progress: Any) -> Callable[[Any], None] | None:
    if progress is None or not bool(getattr(progress, "enabled", False)):
        return None
    active: dict[str, Any] = {}

    def sink(event: Any) -> None:
        key = _event_key(event)
        if not key:
            return
        label = _event_label(event)
        detail = _event_detail(event)
        status = str(getattr(event, "status", "") or "")
        event_type = str(getattr(event, "event_type", "") or "")
        if status == "running" or event_type.endswith("_started"):
            active[key] = progress.step(label)
            active[key].update(message=detail)
            return
        step = active.pop(key, None) or progress.step(label)
        if status == "error":
            step.fail(message=detail or "error")
        else:
            step.complete(message=detail or "done")

    return sink


def _event_key(event: Any) -> str:
    event_type = str(getattr(event, "event_type", "") or "")
    item_name = str(getattr(event, "item_name", "") or "")
    item_type = str(getattr(event, "item_type", "") or "")
    if event_type in {"runtime_iteration_started", "tool_completed"} and item_name:
        return f"step:{item_name}"
    if item_name in {"final_synthesis", "agent_report"}:
        return item_name
    if event_type == "plan_ready":
        return "plan"
    if item_type == "skills":
        return "skills"
    return ""


def _event_label(event: Any) -> str:
    payload = getattr(event, "payload", {}) or {}
    item_name = str(getattr(event, "item_name", "") or "")
    item_type = str(getattr(event, "item_type", "") or "")
    tool_name = str(payload.get("tool_name") or "")
    if not tool_name and isinstance(payload.get("tool_name"), str):
        tool_name = payload["tool_name"]
    if not tool_name and isinstance(payload, dict):
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
    if item_name == "final_synthesis":
        return "生成分析"
    if item_name == "agent_report" or tool_name == "research.write_report":
        return "报告生成"
    if item_type == "skills":
        return "加载 skill"
    if tool_name.startswith("factor.") or tool_name == "research.internal_analysis":
        return "因子与信号"
    if tool_name.startswith("data.") or tool_name in {"research.market_context", "research.stock_context"}:
        return "获取真实数据"
    if tool_name.startswith("chat."):
        return "调用 chat/skill"
    if str(getattr(event, "event_type", "") or "") == "plan_ready":
        return "理解目标"
    title = str(payload.get("title") or "")
    return title or item_name or "Agent"


def _event_detail(event: Any) -> str:
    payload = _as_mapping(getattr(event, "payload", {}) or {})
    item_name = str(getattr(event, "item_name", "") or "")
    content = str(getattr(event, "content", "") or "").strip()
    status = str(getattr(event, "status", "") or "")
    event_type = str(getattr(event, "event_type", "") or "")
    item_type = str(getattr(event, "item_type", "") or "")

    if item_name == "final_synthesis":
        if status == "running" or event_type.endswith("_started"):
            return "正在生成最终分析"
        used_llm = payload.get("used_llm")
        skills = payload.get("skills") if isinstance(payload.get("skills"), list) else []
        parts = ["最终分析完成"]
        if used_llm is not None:
            parts.append("LLM" if used_llm else "本地总结")
        if skills:
            parts.append(f"skills={len(skills)}")
        return _short_text(", ".join(parts))
    if item_name == "agent_report":
        return _artifact_detail(payload, content) or "报告产物已生成"
    if item_type == "skills":
        return _skills_detail(payload, content)
    if event_type == "plan_ready":
        return _plan_detail(payload, content)

    detail = _agent_step_detail(payload, content, status=status)
    if detail:
        return detail
    fallback = str(payload.get("tool_name") or payload.get("tool") or payload.get("title") or item_name or content)
    return _short_text(fallback)


def _agent_step_detail(payload: Mapping[str, Any], content: str, *, status: str) -> str:
    tool_name = _tool_name(payload)
    result = _as_mapping(payload.get("result"))
    if result:
        if status == "error" or _result_status(result) not in {"", "done", "ok"}:
            reason = _result_error(result, content)
            args = _arguments_summary(_as_mapping(payload.get("arguments")))
            subject = " ".join(part for part in (tool_name or "step", args) if part)
            return _short_text(f"{subject} error: {reason}" if reason else f"{subject} error")
        summary = _result_summary(result, payload, content)
        if summary and tool_name:
            return _short_text(f"{tool_name}: {summary}")
        return _short_text(summary or tool_name or content)

    arguments = _as_mapping(payload.get("arguments"))
    if tool_name:
        args = _arguments_summary(arguments)
        return _short_text(f"{tool_name} {args}".strip())
    command = payload.get("command")
    if isinstance(command, list) and command:
        return _short_text("command " + " ".join(str(item) for item in command))
    kind = str(payload.get("kind") or "")
    title = str(payload.get("title") or "")
    if kind or title:
        return _short_text(" ".join(part for part in (kind, title) if part))
    return ""


def _tool_name(payload: Mapping[str, Any]) -> str:
    tool_name = str(payload.get("tool_name") or "")
    if tool_name:
        return tool_name
    result = _as_mapping(payload.get("result"))
    if result:
        return str(payload.get("tool") or result.get("tool_name") or "")
    return str(payload.get("tool") or "")


def _result_status(result: Mapping[str, Any]) -> str:
    status = str(result.get("status") or "").lower()
    payload = _as_mapping(result.get("payload"))
    return status or str(payload.get("status") or "").lower()


def _result_error(result: Mapping[str, Any], content: str) -> str:
    payload = _as_mapping(result.get("payload"))
    for value in (
        payload.get("error"),
        payload.get("message"),
        result.get("error"),
        result.get("content"),
        content,
    ):
        parsed = _json_mapping(value)
        if parsed:
            nested = _result_error({"payload": parsed}, "")
            if nested:
                return nested
        text = _clean_text(value)
        if text:
            return text
    return ""


def _result_summary(result: Mapping[str, Any], payload: Mapping[str, Any], content: str) -> str:
    result_payload = _as_mapping(result.get("payload"))
    payload_summary = _payload_summary(result_payload)
    if payload_summary:
        return payload_summary
    parsed = _json_mapping(result.get("content") or content)
    if parsed:
        parsed_summary = _payload_summary(parsed)
        if parsed_summary:
            return parsed_summary
    data_names = payload.get("data_names") or result.get("data_names") or []
    artifacts = payload.get("artifacts") or result.get("artifacts") or []
    parts: list[str] = []
    if isinstance(data_names, list) and data_names:
        parts.append("data=" + ",".join(str(item) for item in data_names[:3]))
    if isinstance(artifacts, list) and artifacts:
        parts.append(f"artifacts={len(artifacts)}")
    if parts:
        return " ".join(parts)
    text = _clean_text(result.get("content") or content)
    if text and not text.startswith("{"):
        return text
    return "done"


def _payload_summary(payload: Mapping[str, Any]) -> str:
    if not payload:
        return ""
    status = str(payload.get("status") or "")
    if status and status not in {"ok", "done"}:
        return _clean_text(payload.get("error") or payload.get("message") or status)
    if payload.get("error"):
        return _clean_text(payload.get("error"))
    stock_agent = _as_mapping(payload.get("stock_picking_agent"))
    if stock_agent:
        return _stock_picking_summary(stock_agent)
    opportunity = _as_mapping(payload.get("opportunity_discovery"))
    if opportunity:
        return _opportunity_summary(opportunity)
    for key in ("market_context", "stock_context", "analysis"):
        if key in payload:
            return f"{key} ready"
    if payload.get("message"):
        return _clean_text(payload.get("message"))
    return _opportunity_summary(payload)


def _stock_picking_summary(payload: Mapping[str, Any]) -> str:
    opportunity = _as_mapping(payload.get("opportunity_discovery"))
    summary = _opportunity_summary(opportunity)
    if summary:
        return summary
    theme = _as_mapping(payload.get("theme_universe")).get("theme")
    if theme:
        return f"theme={theme}"
    return ""


def _opportunity_summary(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key, label in (
        ("candidate_count", "candidates"),
        ("scanned_count", "scanned"),
        ("llm_pool_count", "llm_pool"),
    ):
        value = payload.get(key)
        if value not in (None, ""):
            parts.append(f"{label}={value}")
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates and not any(part.startswith("candidates=") for part in parts):
        parts.append(f"candidates={len(candidates)}")
    report_path = payload.get("report_path")
    if report_path:
        parts.append("report=ready")
    message = _clean_text(payload.get("message"))
    if message:
        parts.append(message)
    return " ".join(parts)


def _arguments_summary(arguments: Mapping[str, Any]) -> str:
    if not arguments:
        return ""
    preferred = ("query", "symbols", "trade_date", "signals", "limit", "candidate_limit", "horizon", "horizons", "kind")
    keys = [key for key in preferred if key in arguments]
    keys.extend(key for key in arguments if key not in keys)
    parts = []
    for key in keys[:4]:
        value = _value_summary(arguments.get(key))
        if value:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _value_summary(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value[:3]]
        suffix = "..." if len(value) > 3 else ""
        return "[" + ",".join(items) + suffix + "]"
    if isinstance(value, Mapping):
        return "{" + ",".join(str(key) for key in list(value.keys())[:3]) + "}"
    return _clean_text(value)


def _skills_detail(payload: Mapping[str, Any], content: str) -> str:
    count = payload.get("count")
    skills = payload.get("skills") if isinstance(payload.get("skills"), list) else []
    if count not in (None, ""):
        return _short_text(f"{count} skills")
    if skills:
        return _short_text(f"{len(skills)} skills")
    return _short_text(content or "skills loaded")


def _plan_detail(payload: Mapping[str, Any], content: str) -> str:
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    objective = _clean_text(payload.get("objective") or content)
    prefix = f"{len(steps)} steps" if steps else "plan ready"
    return _short_text(f"{prefix}: {objective}" if objective else prefix)


def _artifact_detail(payload: Mapping[str, Any], content: str) -> str:
    path = _clean_text(payload.get("path") or content)
    title = _clean_text(payload.get("title"))
    if path and title:
        return _short_text(f"{title}: {path}")
    return _short_text(path or title)


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _short_text(value: Any, limit: int = 120) -> str:
    text = _clean_text(value)
    return text if len(text) <= limit else text[:limit] + "..."
