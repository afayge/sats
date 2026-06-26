from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from sats.agent.tools.base import AgentToolSpec


BANNED_IMPORT_ROOTS = {"os", "sys", "subprocess", "socket", "requests", "urllib", "shutil", "pathlib"}
BANNED_CALLS = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint"}


def generated_tool_specs() -> list[AgentToolSpec]:
    return load_generated_tool_specs(Path(__file__).resolve().parent)


def load_generated_tool_specs(generated_dir: Path) -> list[AgentToolSpec]:
    specs: list[AgentToolSpec] = []
    directory = Path(generated_dir)
    if not directory.exists():
        return specs
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_") or path.name in {"__init__.py", "loader.py"}:
            continue
        try:
            _validate_generated_source(path)
            module = _load_generated_module(path)
            exported = getattr(module, "tool_specs", None)
            if not callable(exported):
                continue
            for spec in exported():
                if _is_readonly_generated_spec(spec):
                    specs.append(spec)
        except Exception:
            continue
    return specs


def _validate_generated_source(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            roots = _import_roots(node)
            banned = roots & BANNED_IMPORT_ROOTS
            if banned:
                raise ValueError(f"generated tool imports banned modules: {', '.join(sorted(banned))}")
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in BANNED_CALLS:
                raise ValueError(f"generated tool calls banned function: {name}")
            root = name.split(".", 1)[0]
            if root in BANNED_IMPORT_ROOTS:
                raise ValueError(f"generated tool calls banned module: {root}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("generated tool dunder attribute access is banned")


def _load_generated_module(path: Path) -> ModuleType:
    name = f"sats.agent.tools.generated.{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_readonly_generated_spec(value: Any) -> bool:
    if not isinstance(value, AgentToolSpec):
        return False
    if value.side_effect != "readonly" or value.requires_confirmation or value.requires_trade_permission:
        return False
    metadata = dict(value.metadata or {})
    return not bool(metadata.get("writes_db"))


def _import_roots(node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".", 1)[0] for alias in node.names}
    return {str(node.module or "").split(".", 1)[0]} if node.module else set()


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""
