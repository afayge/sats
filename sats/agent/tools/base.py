from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from sats.data.provider_capabilities import planner_provider_capabilities
from sats.agent.date_policy import sanitize_agent_tool_arguments
from sats.agent.models import AgentExecutionPolicy


ToolExecutor = Callable[["AgentToolContext", dict[str, Any]], "AgentToolResult"]


@dataclass(frozen=True, slots=True)
class AgentToolResult:
    status: str = "done"
    content: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    data_names: tuple[str, ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "content": self.content,
            "payload": self.payload,
            "data_names": list(self.data_names),
            "artifacts": list(self.artifacts),
        }


@dataclass(frozen=True, slots=True)
class AgentToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}, "required": []})
    category: str = "general"
    side_effect: str = "readonly"
    requires_confirmation: bool = False
    requires_trade_permission: bool = False
    timeout: int = 30
    executor: ToolExecutor | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "category": self.category,
            "side_effect": self.side_effect,
            "requires_confirmation": self.requires_confirmation,
            "requires_trade_permission": self.requires_trade_permission,
            "timeout": self.timeout,
        }


@dataclass(frozen=True, slots=True)
class AgentToolContext:
    settings: Any
    storage: Any
    resolver: Any
    policy: AgentExecutionPolicy
    command_runner: Any
    trader: Any
    store: Any | None = None
    skills: tuple[Any, ...] = ()
    llm_factory: Callable[..., Any] | None = None
    session_id: str = "agent"
    turn_id: str = ""
    message: str = ""
    observations: tuple[Any, ...] = ()

    def with_observations(self, observations: tuple[Any, ...]) -> "AgentToolContext":
        return replace(self, observations=observations)


class AgentToolRegistry:
    def __init__(self, specs: list[AgentToolSpec] | None = None) -> None:
        self._specs: dict[str, AgentToolSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: AgentToolSpec) -> None:
        name = str(spec.name or "").strip()
        if not name:
            raise ValueError("agent tool name is required")
        self._specs[name] = spec

    def names(self) -> list[str]:
        return sorted(self._specs)

    def get(self, name: str) -> AgentToolSpec | None:
        return self._specs.get(str(name or "").strip())

    def summaries(self, *, max_description: int = 220) -> list[dict[str, Any]]:
        rows = []
        for name in self.names():
            item = self._specs[name].summary()
            description = str(item.get("description") or "")
            if len(description) > max_description:
                item["description"] = description[:max_description] + "..."
            rows.append(item)
        return rows

    def planner_context(self) -> str:
        return json.dumps(
            {
                "tools": self.summaries(),
                "data_capabilities": planner_provider_capabilities(),
            },
            ensure_ascii=False,
            default=str,
        )

    def execute(self, name: str, arguments: Mapping[str, Any] | None, context: AgentToolContext) -> AgentToolResult:
        tool_name = str(name or "").strip()
        spec = self.get(tool_name)
        if spec is None:
            return AgentToolResult(status="error", content=f"unknown agent tool: {tool_name}")
        args = dict(arguments or {})
        symbol_error = _normalize_symbol_arguments(args, context)
        if symbol_error:
            return AgentToolResult(status="error", content=symbol_error)
        sanitized = sanitize_agent_tool_arguments(tool_name, args, context.message)
        if sanitized.error:
            return AgentToolResult(status="error", content=sanitized.error, payload={"argument_policy": sanitized.metadata})
        args = sanitized.arguments
        error = validate_tool_arguments(spec, args)
        if error:
            return AgentToolResult(status="error", content=error, payload={"argument_policy": sanitized.metadata})
        if spec.requires_trade_permission and not context.policy.auto_trade:
            return AgentToolResult(status="error", content=f"{tool_name} requires explicit --auto-trade permission", payload={"argument_policy": sanitized.metadata})
        fabricated = find_fabricated_market_data(args, allow_trade_price=tool_name == "trade.submit_intent")
        if fabricated:
            return AgentToolResult(status="error", content=f"market data guard rejected fabricated field: {fabricated}", payload={"argument_policy": sanitized.metadata})
        if spec.executor is None:
            return AgentToolResult(status="error", content=f"agent tool has no executor: {tool_name}", payload={"argument_policy": sanitized.metadata})
        try:
            result = spec.executor(context, args)
        except Exception as exc:
            return AgentToolResult(status="error", content=str(exc))
        if sanitized.metadata:
            payload = dict(result.payload or {})
            payload["argument_policy"] = sanitized.metadata
            return AgentToolResult(
                status=result.status,
                content=result.content,
                payload=payload,
                data_names=result.data_names,
                artifacts=result.artifacts,
            )
        return result


def _normalize_symbol_arguments(arguments: dict[str, Any], context: AgentToolContext) -> str:
    values = arguments.get("symbols")
    if not isinstance(values, list) or not values:
        return ""
    from sats.symbols import normalize_symbols

    if all(_looks_like_security_code(value) for value in values):
        arguments["symbols"] = normalize_symbols(values, required=True)
        return ""
    try:
        from sats.stock_basic_lookup import load_stock_basic_frame, resolve_symbol_or_name_values

        stock_basic = context.storage.get_stock_basic() if hasattr(context.storage, "get_stock_basic") else None
        if stock_basic is None or stock_basic.empty:
            stock_basic = load_stock_basic_frame(context.settings)
        arguments["symbols"] = resolve_symbol_or_name_values(values, stock_basic, required=True)
    except Exception as exc:
        return str(exc)
    return ""


def _looks_like_security_code(value: Any) -> bool:
    text = str(value or "").strip().upper()
    return (len(text) == 6 and text.isdigit()) or (
        len(text) == 9
        and text[:6].isdigit()
        and text[6] == "."
        and text[7:] in {"SH", "SZ", "BJ"}
    )


def validate_tool_arguments(spec: AgentToolSpec, arguments: Mapping[str, Any]) -> str:
    schema = spec.input_schema or {}
    if schema.get("type") not in (None, "object"):
        return f"{spec.name} schema must be object"
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    for key in required:
        if key not in arguments or arguments.get(key) in (None, ""):
            return f"{spec.name} missing required argument: {key}"
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for key, value in arguments.items():
        prop = properties.get(key)
        if isinstance(prop, dict):
            error = _validate_value_type(key, value, prop)
            if error:
                return error
    return ""


def find_fabricated_market_data(value: Any, *, allow_trade_price: bool = False, path: str = "") -> str:
    market_keys = {"open", "high", "low", "close", "volume", "vol", "amount", "kline", "k线", "quote", "quotes"}
    if not allow_trade_price:
        market_keys.add("price")
    if isinstance(value, Mapping):
        for key, item in value.items():
            clean = str(key or "").strip().lower()
            child_path = f"{path}.{clean}" if path else clean
            if clean in market_keys and _looks_like_market_literal(item):
                return child_path
            found = find_fabricated_market_data(item, allow_trade_price=allow_trade_price, path=child_path)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = find_fabricated_market_data(item, allow_trade_price=allow_trade_price, path=f"{path}[{index}]")
            if found:
                return found
    return ""


def _looks_like_market_literal(value: Any) -> bool:
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, (list, tuple, dict)):
        return True
    return False


def _validate_value_type(key: str, value: Any, prop: Mapping[str, Any]) -> str:
    expected = prop.get("type")
    if expected == "string" and not isinstance(value, str):
        return f"{key} must be string"
    if expected == "integer" and not isinstance(value, int):
        return f"{key} must be integer"
    if expected == "number" and not isinstance(value, (int, float)):
        return f"{key} must be number"
    if expected == "boolean" and not isinstance(value, bool):
        return f"{key} must be boolean"
    if expected == "array" and not isinstance(value, list):
        return f"{key} must be array"
    if expected == "object" and not isinstance(value, dict):
        return f"{key} must be object"
    enum = prop.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        return f"{key} must be one of: {', '.join(str(item) for item in enum)}"
    return ""


def object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def ok(content: str, *, payload: dict[str, Any] | None = None, data_names: tuple[str, ...] = (), artifacts: tuple[dict[str, Any], ...] = ()) -> AgentToolResult:
    return AgentToolResult(status="done", content=content, payload=payload or {}, data_names=data_names, artifacts=artifacts)


def json_content(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
