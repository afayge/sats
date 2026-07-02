from __future__ import annotations

import ast
import builtins
import concurrent.futures
from dataclasses import dataclass, field
from typing import Any

from sats.agent.models import TradeIntent
from sats.data.resolver import MarketDataResolver


BANNED_CALLS = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint"}
BANNED_ATTR_ROOTS = {"os", "sys", "subprocess", "socket", "pathlib", "requests", "urllib", "shutil", "importlib"}
MARKET_LITERAL_KEYS = {"open", "high", "low", "close", "price", "volume", "vol", "amount", "kline", "quote"}


@dataclass(slots=True)
class PythonRunResult:
    status: str
    result: Any = None
    trade_intents: list[TradeIntent] = field(default_factory=list)
    error: str = ""


class RestrictedPythonRuntime:
    def __init__(self, *, resolver: MarketDataResolver, timeout_seconds: int = 30) -> None:
        self.resolver = resolver
        self.timeout_seconds = max(1, int(timeout_seconds or 30))

    def run(self, code: str, *, context: dict[str, Any] | None = None) -> PythonRunResult:
        try:
            tree = ast.parse(str(code or ""), mode="exec")
            _validate_tree(tree)
        except Exception as exc:
            return PythonRunResult(status="error", error=str(exc))
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self._execute, compile(tree, "<sats-agent-python>", "exec"), context or {})
        try:
            return future.result(timeout=self.timeout_seconds)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return PythonRunResult(status="timeout", error=f"python step timed out after {self.timeout_seconds}s")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _execute(self, compiled: Any, context: dict[str, Any]) -> PythonRunResult:
        intents: list[TradeIntent] = []

        def safe_import(name: str, globals: Any = None, locals: Any = None, fromlist: Any = (), level: int = 0) -> Any:
            return builtins.__import__(name, globals, locals, fromlist, level)

        def emit_trade_intent(**kwargs: Any) -> dict[str, Any]:
            intent = TradeIntent(
                ts_code=str(kwargs.get("ts_code") or kwargs.get("symbol") or ""),
                side=str(kwargs.get("side") or ""),
                quantity=int(kwargs["quantity"]) if kwargs.get("quantity") not in (None, "") else None,
                price_type=str(kwargs.get("price_type") or "latest"),
                price=float(kwargs["price"]) if kwargs.get("price") not in (None, "") else None,
                reason=str(kwargs.get("reason") or ""),
                source_step_id=str(kwargs.get("source_step_id") or "python"),
            )
            intents.append(intent)
            return intent.to_dict()

        safe_globals = {
            "__builtins__": {
                "abs": abs,
                "all": all,
                "any": any,
                "bool": bool,
                "dict": dict,
                "enumerate": enumerate,
                "float": float,
                "int": int,
                "len": len,
                "list": list,
                "max": max,
                "min": min,
                "range": range,
                "round": round,
                "sorted": sorted,
                "str": str,
                "sum": sum,
                "tuple": tuple,
                "__import__": safe_import,
            },
            "resolver": self.resolver,
            "emit_trade_intent": emit_trade_intent,
        }
        env: dict[str, Any] = dict(safe_globals)
        try:
            exec(compiled, env, env)
            if callable(env.get("run")):
                result = env["run"]({"resolver": self.resolver, **context})
            else:
                result = env.get("RESULT")
            return PythonRunResult(status="done", result=result, trade_intents=intents)
        except Exception as exc:
            return PythonRunResult(status="error", error=str(exc), trade_intents=intents)


def _validate_tree(tree: ast.AST) -> None:
    dangerous_import_aliases = _dangerous_import_aliases(tree)
    if "*" in dangerous_import_aliases:
        raise ValueError(f"restricted Python forbids {dangerous_import_aliases['*']} access")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in BANNED_CALLS:
                raise ValueError(f"restricted Python forbids {name}()")
            root = name.split(".", 1)[0]
            if root in BANNED_ATTR_ROOTS:
                raise ValueError(f"restricted Python forbids {root} access")
            if root in dangerous_import_aliases:
                raise ValueError(f"restricted Python forbids {dangerous_import_aliases[root]} access")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("restricted Python forbids dunder attribute access")
        if isinstance(node, ast.Dict):
            _validate_market_literal_dict(node)


def _dangerous_import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = str(alias.name or "").split(".", 1)[0]
                if root in BANNED_ATTR_ROOTS:
                    aliases[str(alias.asname or root)] = root
        elif isinstance(node, ast.ImportFrom):
            root = str(node.module or "").split(".", 1)[0]
            if root in BANNED_ATTR_ROOTS:
                for alias in node.names:
                    if alias.name == "*":
                        aliases["*"] = root
                    else:
                        aliases[str(alias.asname or alias.name)] = root
    return aliases


def _validate_market_literal_dict(node: ast.Dict) -> None:
    for key, value in zip(node.keys, node.values):
        if not isinstance(key, ast.Constant) or str(key.value).lower() not in MARKET_LITERAL_KEYS:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
            raise ValueError("market data literals are forbidden; use SATS resolver")
        if isinstance(value, (ast.List, ast.Tuple)) and any(isinstance(item, ast.Constant) and isinstance(item.value, (int, float)) for item in value.elts):
            raise ValueError("market data literals are forbidden; use SATS resolver")


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""
