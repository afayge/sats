from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_ts_code


MONITOR_PLAN_SCHEMA_VERSION = 2
SUPPORTED_MONITOR_PLAN_SCHEMA_VERSIONS = {1, 2}
MONITOR_PLAN_ACTIONS = {"notify", "buy", "sell"}
MONITOR_PLAN_METRICS = {
    "latest_price",
    "change_points",
    "pct_change",
    "market_regime_score",
    "position_pnl_pct",
    "holding_trade_days",
    "peak_drawdown_pct",
}
MONITOR_PLAN_OPERATORS = {">=", ">", "<=", "<"}
MONITOR_PLAN_SIZING_MODES = {"default", "amount", "shares", "position_pct"}
SUPPORTED_MONITOR_INDEX_CODES = {
    "000001.SH",
    "399001.SZ",
    "399006.SZ",
    "000300.SH",
}

MONITOR_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "SATS Monitor Plan",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "name", "start_date", "end_date", "active_windows", "items"],
    "properties": {
        "schema_version": {"enum": sorted(SUPPORTED_MONITOR_PLAN_SCHEMA_VERSIONS)},
        "name": {"type": "string", "minLength": 1},
        "start_date": {"type": "string", "pattern": "^[0-9]{8}$"},
        "end_date": {"type": "string", "pattern": "^[0-9]{8}$"},
        "active_windows": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["start", "end"],
                "properties": {
                    "start": {"type": "string", "pattern": "^[0-9]{2}:[0-9]{2}$"},
                    "end": {"type": "string", "pattern": "^[0-9]{2}:[0-9]{2}$"},
                },
            },
        },
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/item"},
        },
    },
    "$defs": {
        "item": {
            "type": "object",
            "additionalProperties": False,
            "required": ["symbol", "trigger_groups"],
            "properties": {
                "symbol": {"type": "string", "minLength": 6},
                "name": {"type": "string"},
                "summary": {"type": "string"},
                "risk_note": {"type": "string"},
                "trigger_groups": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": "#/$defs/trigger_group"},
                },
            },
        },
        "trigger_group": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "conditions"],
            "properties": {
                "action": {"enum": sorted(MONITOR_PLAN_ACTIONS)},
                "message": {"type": "string"},
                "conditions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": "#/$defs/condition"},
                },
                "sizing": {"$ref": "#/$defs/sizing"},
            },
        },
        "condition": {
            "type": "object",
            "additionalProperties": False,
            "required": ["subject", "metric", "operator", "value"],
            "properties": {
                "subject": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type"],
                    "properties": {
                        "type": {"enum": ["stock", "index", "market", "position"]},
                        "symbol": {"type": "string"},
                    },
                },
                "metric": {"enum": sorted(MONITOR_PLAN_METRICS)},
                "operator": {"enum": sorted(MONITOR_PLAN_OPERATORS)},
                "value": {"type": "number"},
            },
        },
        "sizing": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"enum": sorted(MONITOR_PLAN_SIZING_MODES)},
                "value": {"type": "number", "exclusiveMinimum": 0},
            },
        },
    },
}


class MonitorPlanValidationError(ValueError):
    pass


def load_monitor_plan_file(path: Path | str) -> dict[str, Any]:
    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MonitorPlanValidationError(f"无法读取计划文件: {file_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MonitorPlanValidationError(f"计划文件不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MonitorPlanValidationError("计划 JSON 顶层必须是对象")
    return payload


def validate_monitor_plan(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MonitorPlanValidationError("计划 JSON 顶层必须是对象")
    _reject_unknown(payload, {"schema_version", "name", "start_date", "end_date", "active_windows", "items"}, "计划")
    _require_keys(payload, ("schema_version", "name", "start_date", "end_date", "active_windows", "items"), "计划")
    schema_version = int(payload.get("schema_version") or 0)
    if schema_version not in SUPPORTED_MONITOR_PLAN_SCHEMA_VERSIONS:
        raise MonitorPlanValidationError(
            f"schema_version 必须为 {min(SUPPORTED_MONITOR_PLAN_SCHEMA_VERSIONS)} "
            f"或 {max(SUPPORTED_MONITOR_PLAN_SCHEMA_VERSIONS)}"
        )

    name = _required_text(payload.get("name"), "name")
    start_date = _date(payload.get("start_date"), "start_date")
    end_date = _date(payload.get("end_date"), "end_date")
    if start_date > end_date:
        raise MonitorPlanValidationError("start_date 不能晚于 end_date")
    windows = _active_windows(payload.get("active_windows"))

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise MonitorPlanValidationError("items 必须是非空数组")
    items = [_item(item, index=index) for index, item in enumerate(raw_items, start=1)]
    symbols = [item["symbol"] for item in items]
    if len(set(symbols)) != len(symbols):
        raise MonitorPlanValidationError("items 中股票代码不能重复")

    return {
        "schema_version": schema_version,
        "name": name,
        "start_date": start_date,
        "end_date": end_date,
        "active_windows": windows,
        "items": items,
    }


def import_monitor_plan(storage: DuckDBStorage, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_monitor_plan(payload)
    plan_id = _id("plan")
    bundle = {
        **normalized,
        "plan_id": plan_id,
        "status": "draft",
        "items": [],
    }
    for item_index, item in enumerate(normalized["items"], start=1):
        item_id = _id("item")
        stored_item = {
            **item,
            "item_id": item_id,
            "plan_id": plan_id,
            "enabled": True,
            "sort_order": item_index,
            "trigger_groups": [],
        }
        for group_index, group in enumerate(item["trigger_groups"], start=1):
            stored_item["trigger_groups"].append(
                {
                    **group,
                    "group_id": _id("group"),
                    "item_id": item_id,
                    "plan_id": plan_id,
                    "enabled": True,
                    "sort_order": group_index,
                }
            )
        bundle["items"].append(stored_item)
    storage.insert_monitor_plan_bundle(bundle)
    return storage.get_monitor_plan(plan_id)


def _item(raw: Any, *, index: int) -> dict[str, Any]:
    path = f"items[{index}]"
    if not isinstance(raw, dict):
        raise MonitorPlanValidationError(f"{path} 必须是对象")
    _reject_unknown(raw, {"symbol", "name", "summary", "risk_note", "trigger_groups"}, path)
    _require_keys(raw, ("symbol", "trigger_groups"), path)
    symbol = normalize_ts_code(raw.get("symbol"))
    if not _is_a_share_symbol(symbol):
        raise MonitorPlanValidationError(f"{path}.symbol 不是合法 A 股代码")
    raw_groups = raw.get("trigger_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise MonitorPlanValidationError(f"{path}.trigger_groups 必须是非空数组")
    groups = [_trigger_group(group, item_symbol=symbol, path=f"{path}.trigger_groups[{group_index}]") for group_index, group in enumerate(raw_groups, start=1)]
    return {
        "symbol": symbol,
        "name": _optional_text(raw.get("name"), f"{path}.name"),
        "summary": _optional_text(raw.get("summary"), f"{path}.summary"),
        "risk_note": _optional_text(raw.get("risk_note"), f"{path}.risk_note"),
        "trigger_groups": groups,
    }


def _trigger_group(raw: Any, *, item_symbol: str, path: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MonitorPlanValidationError(f"{path} 必须是对象")
    _reject_unknown(raw, {"action", "message", "conditions", "sizing"}, path)
    _require_keys(raw, ("action", "conditions"), path)
    action = str(raw.get("action") or "").strip().lower()
    if action not in MONITOR_PLAN_ACTIONS:
        raise MonitorPlanValidationError(f"{path}.action 必须是 notify、buy 或 sell")
    raw_conditions = raw.get("conditions")
    if not isinstance(raw_conditions, list) or not raw_conditions:
        raise MonitorPlanValidationError(f"{path}.conditions 必须是非空数组")
    conditions = [
        _condition(condition, item_symbol=item_symbol, path=f"{path}.conditions[{condition_index}]")
        for condition_index, condition in enumerate(raw_conditions, start=1)
    ]
    sizing = _sizing(raw.get("sizing"), action=action, path=f"{path}.sizing")
    return {
        "action": action,
        "message": _optional_text(raw.get("message"), f"{path}.message"),
        "conditions": conditions,
        "sizing": sizing,
    }


def _condition(raw: Any, *, item_symbol: str, path: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MonitorPlanValidationError(f"{path} 必须是对象")
    _reject_unknown(raw, {"subject", "metric", "operator", "value"}, path)
    _require_keys(raw, ("subject", "metric", "operator", "value"), path)
    subject = raw.get("subject")
    if not isinstance(subject, dict):
        raise MonitorPlanValidationError(f"{path}.subject 必须是对象")
    _reject_unknown(subject, {"type", "symbol"}, f"{path}.subject")
    subject_type = str(subject.get("type") or "").strip().lower()
    if subject_type == "stock":
        subject_symbol = normalize_ts_code(subject.get("symbol") or item_symbol)
        if subject_symbol != item_symbol:
            raise MonitorPlanValidationError(f"{path}.subject.symbol 必须与计划股票一致")
    elif subject_type == "index":
        subject_symbol = normalize_ts_code(subject.get("symbol"))
        if subject_symbol not in SUPPORTED_MONITOR_INDEX_CODES:
            raise MonitorPlanValidationError(f"{path}.subject.symbol 不是首版支持的指数代码")
    elif subject_type == "position":
        subject_symbol = normalize_ts_code(subject.get("symbol") or item_symbol)
        if subject_symbol != item_symbol:
            raise MonitorPlanValidationError(f"{path}.subject.symbol 必须与计划股票一致")
    elif subject_type == "market":
        subject_symbol = ""
    else:
        raise MonitorPlanValidationError(f"{path}.subject.type 必须是 stock、index、market 或 position")
    metric = str(raw.get("metric") or "").strip()
    if metric not in MONITOR_PLAN_METRICS:
        raise MonitorPlanValidationError(f"{path}.metric 不受支持")
    if metric == "market_regime_score" and subject_type != "market":
        raise MonitorPlanValidationError(f"{path}.metric market_regime_score 仅支持 market 对象")
    if metric in {"position_pnl_pct", "holding_trade_days", "peak_drawdown_pct"} and subject_type != "position":
        raise MonitorPlanValidationError(f"{path}.metric {metric} 仅支持 position 对象")
    operator = str(raw.get("operator") or "").strip()
    if operator not in MONITOR_PLAN_OPERATORS:
        raise MonitorPlanValidationError(f"{path}.operator 不受支持")
    value = _finite_number(raw.get("value"), f"{path}.value")
    return {
        "subject": {"type": subject_type, "symbol": subject_symbol},
        "metric": metric,
        "operator": operator,
        "value": value,
    }


def _sizing(raw: Any, *, action: str, path: str) -> dict[str, Any]:
    if raw is None:
        return {"mode": "default"}
    if not isinstance(raw, dict):
        raise MonitorPlanValidationError(f"{path} 必须是对象")
    _reject_unknown(raw, {"mode", "value"}, path)
    mode = str(raw.get("mode") or "default").strip().lower()
    if mode not in MONITOR_PLAN_SIZING_MODES:
        raise MonitorPlanValidationError(f"{path}.mode 不受支持")
    if action == "notify" and mode != "default":
        raise MonitorPlanValidationError(f"{path}：notify 动作不能配置交易规模")
    if mode == "default":
        if "value" in raw and raw.get("value") is not None:
            raise MonitorPlanValidationError(f"{path}.value 在 default 模式下必须省略")
        return {"mode": "default"}
    value = _finite_number(raw.get("value"), f"{path}.value")
    if value <= 0:
        raise MonitorPlanValidationError(f"{path}.value 必须大于 0")
    if mode == "shares":
        if not float(value).is_integer():
            raise MonitorPlanValidationError(f"{path}.value 在 shares 模式下必须是整数")
        value = int(value)
        if action == "buy" and value % 100 != 0:
            raise MonitorPlanValidationError(f"{path}.value：A 股买入股数必须是 100 的整数倍")
    if mode == "position_pct" and value > 1:
        raise MonitorPlanValidationError(f"{path}.value 在 position_pct 模式下必须不大于 1")
    return {"mode": mode, "value": value}


def _active_windows(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise MonitorPlanValidationError("active_windows 必须是非空数组")
    result = []
    for index, item in enumerate(raw, start=1):
        path = f"active_windows[{index}]"
        if not isinstance(item, dict):
            raise MonitorPlanValidationError(f"{path} 必须是对象")
        _reject_unknown(item, {"start", "end"}, path)
        _require_keys(item, ("start", "end"), path)
        start = _time(item.get("start"), f"{path}.start")
        end = _time(item.get("end"), f"{path}.end")
        if start >= end:
            raise MonitorPlanValidationError(f"{path}.start 必须早于 end")
        result.append({"start": start, "end": end})
    result.sort(key=lambda item: item["start"])
    for previous, current in zip(result, result[1:]):
        if current["start"] < previous["end"]:
            raise MonitorPlanValidationError("active_windows 不能重叠")
    return result


def _date(value: Any, field: str) -> str:
    text = str(value or "").strip()
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise MonitorPlanValidationError(f"{field} 必须是有效的 YYYYMMDD 日期") from exc
    return text


def _time(value: Any, field: str) -> str:
    text = str(value or "").strip()
    try:
        datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise MonitorPlanValidationError(f"{field} 必须是有效的 HH:MM 时间") from exc
    return text


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise MonitorPlanValidationError(f"{field} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MonitorPlanValidationError(f"{field} 必须是数字") from exc
    if not math.isfinite(number):
        raise MonitorPlanValidationError(f"{field} 必须是有限数字")
    return number


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MonitorPlanValidationError(f"{field} 不能为空")
    return text


def _optional_text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MonitorPlanValidationError(f"{field} 必须是字符串")
    return value.strip()


def _is_a_share_symbol(symbol: str) -> bool:
    if len(symbol) != 9 or not symbol[:6].isdigit():
        return False
    code = symbol[:6]
    exchange = symbol[6:]
    if exchange == ".SH":
        return code.startswith("6")
    if exchange == ".SZ":
        return code.startswith(("0", "3"))
    if exchange == ".BJ":
        return code.startswith(("4", "8", "9"))
    return False


def _require_keys(payload: dict[str, Any], keys: tuple[str, ...], path: str) -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise MonitorPlanValidationError(f"{path} 缺少字段: {', '.join(missing)}")


def _reject_unknown(payload: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise MonitorPlanValidationError(f"{path} 包含未知字段: {', '.join(unknown)}")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"
