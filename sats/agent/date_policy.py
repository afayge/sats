from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DATE_FIELD_NAMES = {"trade_date", "start_date", "end_date", "ann_date", "cal_date"}
TIME_FIELD_NAMES = {"start_time", "end_time"}
DATE_RE = re.compile(r"^\s*(20\d{2})([-/]?)(\d{2})\2(\d{2})\s*$")
DATE_FIND_RE = re.compile(r"(?<!\d)(20\d{2})([-/]?)(\d{2})\2(\d{2})(?!\d)")
INTRADAY_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?!\d)")
SUPPORTED_HORIZONS = ("today", "tomorrow", "day_after_tomorrow", "next_week")


@dataclass(frozen=True, slots=True)
class AgentTimeContext:
    today: str
    explicit_dates: tuple[str, ...] = ()
    horizons: tuple[str, ...] = ()
    is_forecast: bool = False
    requires_intraday: bool = False


@dataclass(frozen=True, slots=True)
class SanitizedToolArguments:
    arguments: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def agent_today() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")


def normalize_agent_date(value: Any) -> str:
    raw = str(value or "").strip()
    match = DATE_RE.match(raw)
    if not match:
        raise ValueError(f"日期格式无效: {raw}")
    normalized = "".join([match.group(1), match.group(3), match.group(4)])
    try:
        datetime.strptime(normalized, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"日期格式无效: {raw}") from exc
    return normalized


def resolve_agent_time_context(message: str, *, today: str | None = None, arguments: Mapping[str, Any] | None = None) -> AgentTimeContext:
    text = str(message or "")
    horizons = _forecast_horizons(text)
    explicit_dates = _explicit_dates(text)
    requires_intraday = _requires_intraday_text(text) or _requires_intraday_arguments(arguments or {})
    return AgentTimeContext(
        today=today or agent_today(),
        explicit_dates=explicit_dates,
        horizons=horizons,
        is_forecast=bool(horizons),
        requires_intraday=requires_intraday,
    )


def sanitize_agent_tool_arguments(
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    message: str,
    *,
    today: str | None = None,
) -> SanitizedToolArguments:
    args = _copy_jsonable(dict(arguments or {}))
    time_context = resolve_agent_time_context(message, today=today, arguments=args)
    changes: list[str] = []

    if time_context.is_forecast and not time_context.explicit_dates:
        _apply_forecast_policy(str(tool_name or ""), args, time_context, changes)

    try:
        args = _normalize_date_fields(args, changes)
    except ValueError as exc:
        return SanitizedToolArguments(arguments=args, error=str(exc))

    metadata: dict[str, Any] = {}
    if changes:
        metadata["changes"] = changes
        metadata["time_context"] = {
            "today": time_context.today,
            "explicit_dates": list(time_context.explicit_dates),
            "horizons": list(time_context.horizons),
            "is_forecast": time_context.is_forecast,
            "requires_intraday": time_context.requires_intraday,
        }
    return SanitizedToolArguments(arguments=args, metadata=metadata)


def is_forecast_without_intraday(message: str, arguments: Mapping[str, Any] | None = None) -> bool:
    context = resolve_agent_time_context(message, arguments=arguments)
    return context.is_forecast and not context.requires_intraday


def _apply_forecast_policy(tool_name: str, args: dict[str, Any], context: AgentTimeContext, changes: list[str]) -> None:
    if tool_name == "research.market_context":
        if args.pop("trade_date", None) not in (None, ""):
            changes.append("removed generated trade_date for forecast market context")
        if context.horizons:
            args["horizons"] = list(context.horizons)
            args.pop("horizon", None)
            changes.append("set forecast horizons from user message")
        return
    if tool_name in {
        "research.stock_context",
        "research.internal_analysis",
        "research.deep_stock_analysis",
        "research.serenity_screen",
        "data.indicator_inputs",
        "factor.analyze",
        "factor.pick",
    }:
        previous = str(args.get("trade_date") or "").strip()
        if previous != context.today:
            args["trade_date"] = context.today
            changes.append("replaced generated trade_date with current as-of date for forecast")
        if context.horizons and tool_name.startswith("research."):
            args["horizons"] = list(context.horizons)
    if tool_name == "research.discover_opportunities" and args.get("trade_date") not in (None, ""):
        args.pop("trade_date", None)
        changes.append("removed generated trade_date for forecast discovery")


def _normalize_date_fields(value: Any, changes: list[str], *, key: str = "") -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            child_key = str(raw_key or "")
            result[child_key] = _normalize_date_fields(item, changes, key=child_key)
        return result
    if isinstance(value, list):
        return [_normalize_date_fields(item, changes, key=key) for item in value]
    if key in DATE_FIELD_NAMES and value not in (None, ""):
        normalized = normalize_agent_date(value)
        if normalized != str(value).strip():
            changes.append(f"normalized {key} to YYYYMMDD")
        return normalized
    if key in TIME_FIELD_NAMES and value not in (None, "") and DATE_RE.match(str(value or "")):
        normalized = normalize_agent_date(value)
        if normalized != str(value).strip():
            changes.append(f"normalized {key} date to YYYYMMDD")
        return normalized
    return value


def _explicit_dates(text: str) -> tuple[str, ...]:
    dates: list[str] = []
    seen: set[str] = set()
    for match in DATE_FIND_RE.finditer(str(text or "")):
        try:
            normalized = normalize_agent_date(match.group(0))
        except ValueError:
            continue
        if normalized not in seen:
            seen.add(normalized)
            dates.append(normalized)
    return tuple(dates)


def _forecast_horizons(text: str) -> tuple[str, ...]:
    horizons: list[str] = []
    if any(term in text for term in ("明后天", "明后", "明天后天", "未来两天", "未来几天", "未来数天", "未来三天")):
        horizons.extend(["tomorrow", "day_after_tomorrow"])
    else:
        if "明天" in text or "次日" in text:
            horizons.append("tomorrow")
        if "后天" in text:
            horizons.append("day_after_tomorrow")
    if "下周" in text or "未来一周" in text:
        horizons.append("next_week")
    return tuple(_dedupe([item for item in horizons if item in SUPPORTED_HORIZONS]))


def _requires_intraday_text(text: str) -> bool:
    lowered = str(text or "").lower()
    if INTRADAY_RE.search(lowered):
        return True
    return any(term in lowered for term in ("15m", "30m", "60m", "1m", "5m", "分钟k", "分钟 k", "minute", "intraday", "盘中", "分时"))


def _requires_intraday_arguments(arguments: Mapping[str, Any]) -> bool:
    period = str(arguments.get("period") or "").lower()
    if period.endswith("m"):
        return True
    periods = arguments.get("periods")
    if isinstance(periods, (list, tuple)) and any(str(item).lower().endswith("m") for item in periods):
        return True
    return False


def _copy_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_copy_jsonable(item) for item in value]
    return value


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
