from __future__ import annotations

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
    payload = getattr(event, "payload", {}) or {}
    item_name = str(getattr(event, "item_name", "") or "")
    tool_name = ""
    if isinstance(payload, dict):
        tool_name = str(payload.get("tool_name") or "")
        if not tool_name and isinstance(payload.get("steps"), list):
            return f"{len(payload.get('steps') or [])} steps"
        if not tool_name:
            tool_name = str(payload.get("tool") or payload.get("title") or "")
    content = str(getattr(event, "content", "") or "").strip()
    detail = tool_name or item_name or content
    detail = detail.replace("\n", " ")
    return detail if len(detail) <= 80 else detail[:80] + "..."
