from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from sats import __version__
from sats.config import Settings
from sats.data.astock_operations import list_astock_capabilities
from sats.rag.knowledge import DEFAULT_COLLECTIONS
from sats.storage.duckdb import DuckDBStorage


CATALOG_SCHEMA_VERSION = "1.0"
CATALOG_SECTIONS = (
    "summary",
    "commands",
    "agent-tools",
    "skills",
    "knowledge",
    "providers",
    "screening-rules",
    "signals",
    "factors",
    "api",
    "all",
)


def build_capability_catalog(
    *,
    settings: Settings,
    section: str = "summary",
    provider: str | None = None,
    query: str | None = None,
    category: str | None = None,
    realtime: bool | None = None,
    writes_db: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    selected = str(section or "summary").strip().lower()
    if selected not in CATALOG_SECTIONS:
        raise ValueError(f"unknown catalog section: {section}")
    clean_limit = max(1, min(200, int(limit or 50)))
    clean_offset = max(0, int(offset or 0))
    filters = {
        "provider": str(provider or ""),
        "query": str(query or ""),
        "category": str(category or ""),
        "realtime": realtime,
        "writes_db": writes_db,
        "limit": clean_limit,
        "offset": clean_offset,
    }
    consistency = _command_consistency()
    builders: dict[str, Callable[[], dict[str, Any]]] = {
        "commands": lambda: _commands_section(query=query, limit=clean_limit, offset=clean_offset),
        "agent-tools": lambda: _agent_tools_section(query=query, category=category, limit=clean_limit, offset=clean_offset),
        "skills": lambda: _skills_section(settings=settings, query=query, category=category, limit=clean_limit, offset=clean_offset),
        "knowledge": lambda: _knowledge_section(settings=settings, query=query, limit=clean_limit, offset=clean_offset),
        "providers": lambda: _providers_section(
            provider=provider,
            query=query,
            category=category,
            realtime=realtime,
            writes_db=writes_db,
            limit=clean_limit,
            offset=clean_offset,
        ),
        "screening-rules": lambda: _screening_rules_section(query=query, limit=clean_limit, offset=clean_offset),
        "signals": lambda: _signals_section(query=query, category=category, limit=clean_limit, offset=clean_offset),
        "factors": lambda: _factors_section(query=query, category=category, limit=clean_limit, offset=clean_offset),
        "api": lambda: _api_section(settings=settings, query=query, limit=clean_limit, offset=clean_offset),
    }

    if selected == "summary":
        summaries = {name: builder() for name, builder in builders.items()}
        data = {
            "summary": {
                name: _section_summary(payload)
                for name, payload in summaries.items()
            }
        }
        counts = {name: int(payload.get("total") or 0) for name, payload in summaries.items()}
    elif selected == "all":
        data = {name: builder() for name, builder in builders.items()}
        counts = {name: int(payload.get("total") or 0) for name, payload in data.items()}
    else:
        payload = builders[selected]()
        data = {selected: payload}
        counts = {selected: int(payload.get("total") or 0)}

    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "project_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "section": selected,
        "filters": filters,
        "counts": counts,
        "data": data,
        "consistency": consistency,
    }


def format_capability_catalog(catalog: dict[str, Any]) -> str:
    section = str(catalog.get("section") or "summary")
    counts = catalog.get("counts") if isinstance(catalog.get("counts"), dict) else {}
    if section == "summary":
        lines = ["SATS 统一能力目录", ""]
        labels = {
            "commands": "命令路径",
            "agent-tools": "Agent tools",
            "skills": "Skills",
            "knowledge": "知识库",
            "providers": "数据接口",
            "screening-rules": "筛选规则",
            "signals": "信号",
            "factors": "因子",
            "api": "HTTP 路由",
        }
        for key, label in labels.items():
            lines.append(f"- {label}: {counts.get(key, 0)}")
        providers = (
            catalog.get("data", {})
            .get("summary", {})
            .get("providers", {})
            .get("by_provider", {})
        )
        if providers:
            lines.extend(["", "数据接口分布: " + ", ".join(f"{key}={value}" for key, value in providers.items())])
        warnings = catalog.get("consistency", {}).get("warnings", [])
        if warnings:
            lines.extend(["", *[f"警告: {item}" for item in warnings]])
        lines.extend(["", "使用 sats catalog --section <section> --json 查看机器可读详情。"])
        return "\n".join(lines)

    payload = catalog.get("data", {}).get(section, {})
    items = payload.get("items") if isinstance(payload, dict) else []
    lines = [f"SATS catalog: {section} ({payload.get('returned', 0)}/{payload.get('total', 0)})"]
    if section == "providers":
        for item in items or []:
            dataset = f" dataset={item.get('dataset')}" if item.get("dataset") else ""
            lines.append(
                f"- [{item.get('provider')}] {item.get('operation')}{dataset}: "
                f"{item.get('name') or item.get('description') or ''}"
            )
    elif section == "commands":
        for item in items or []:
            lines.append(f"- {item.get('path')}: {item.get('help') or ''}")
    elif section == "agent-tools":
        for item in items or []:
            lines.append(f"- {item.get('name')} [{item.get('side_effect')}]: {item.get('description')}")
    elif section == "skills":
        for item in items or []:
            lines.append(f"- {item.get('id')} [{item.get('category')}]: {item.get('description')}")
    elif section == "knowledge":
        for item in items or []:
            lines.append(
                f"- {item.get('name')} ({item.get('collection')}): "
                f"files={item.get('file_count', 0)} chunks={item.get('chunk_count', 0)}"
            )
    elif section == "screening-rules":
        for item in items or []:
            lines.append(f"- {item.get('name')}")
    elif section == "signals":
        for item in items or []:
            lines.append(f"- {item.get('id')} [{item.get('category')}/{item.get('side')}]: {item.get('label')}")
    elif section == "factors":
        for item in items or []:
            lines.append(f"- {item.get('id')} [{item.get('zoo')}]: {item.get('display_name') or ''}")
    elif section == "api":
        for item in items or []:
            lines.append(f"- {','.join(item.get('methods') or [])} {item.get('path')}")
    else:
        lines.append(json.dumps(payload, ensure_ascii=False, default=str))
    if payload.get("truncated"):
        lines.append(f"... 使用 --offset {int(payload.get('offset', 0)) + int(payload.get('returned', 0))} 查看下一页")
    return "\n".join(lines)


def _commands_section(*, query: str | None, limit: int, offset: int) -> dict[str, Any]:
    from sats.cli import build_parser

    rows: list[dict[str, Any]] = []

    def walk(parser: argparse.ArgumentParser, path: list[str]) -> None:
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            help_by_name = {
                str(item.dest): str(item.help or "")
                for item in getattr(action, "_choices_actions", [])
            }
            for name, child in action.choices.items():
                command_path = [*path, name]
                rows.append(
                    {
                        "path": " ".join(command_path),
                        "help": help_by_name.get(name, ""),
                        "options": [_argument_payload(item) for item in child._actions if item.dest != "help" and not isinstance(item, argparse._SubParsersAction)],
                    }
                )
                walk(child, command_path)

    walk(build_parser(), [])
    selected = _filter_items(rows, query)
    return _page(selected, limit=limit, offset=offset, extra={"top_level_count": len({item["path"].split()[0] for item in rows})})


def _agent_tools_section(*, query: str | None, category: str | None, limit: int, offset: int) -> dict[str, Any]:
    from sats.agent.tools import build_default_tool_registry

    rows = build_default_tool_registry().summaries(max_description=500)
    if category:
        category_key = str(category).lower()
        rows = [item for item in rows if category_key in str(item.get("category") or "").lower()]
    rows = _filter_items(rows, query)
    return _page(rows, limit=limit, offset=offset, extra={"by_category": dict(Counter(str(item.get("category") or "") for item in rows))})


def _skills_section(
    *,
    settings: Settings,
    query: str | None,
    category: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    from sats.skills import default_skills_dir, load_skills

    skills = load_skills(default_skills_dir(Path(settings.project_root)))
    rows = [
        {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "category": item.category,
            "source": item.source,
            "triggers": list(item.triggers),
            "aliases": list(item.aliases),
            "requires_tools": list(item.requires_tools),
            "applies_to": list(item.applies_to),
            "evidence": list(item.evidence),
            "auto_load": item.auto_load,
            "priority": item.priority,
            "path": str(item.path),
        }
        for item in skills
    ]
    if category:
        rows = [item for item in rows if str(item.get("category") or "") == str(category)]
    rows = _filter_items(rows, query)
    return _page(rows, limit=limit, offset=offset, extra={"by_category": dict(Counter(str(item.get("category") or "") for item in rows))})


def _knowledge_section(*, settings: Settings, query: str | None, limit: int, offset: int) -> dict[str, Any]:
    rows = [
        {
            "name": item["name"],
            "collection": item["name"],
            "description": item["description"],
            "tags": list(item["tags"]),
            "paths": list(item["paths"]),
            "file_count": 0,
            "chunk_count": 0,
            "source": "default_registry",
        }
        for item in DEFAULT_COLLECTIONS.values()
    ]
    db_path = Path(settings.db_path)
    if db_path.exists():
        try:
            storage = DuckDBStorage(db_path, read_only=True)
            with storage.connect() as con:
                actual = con.execute(
                    """
                    SELECT kb.name, kb.collection_name, kb.description, kb.tags_json,
                           COUNT(DISTINCT kfl.file_id), COUNT(DISTINCT kc.chunk_id)
                    FROM knowledge_bases kb
                    LEFT JOIN knowledge_file_links kfl ON kb.knowledge_id = kfl.knowledge_id
                    LEFT JOIN knowledge_chunks kc ON kb.knowledge_id = kc.knowledge_id
                    WHERE kb.archived = FALSE
                    GROUP BY kb.name, kb.collection_name, kb.description, kb.tags_json
                    ORDER BY kb.name
                    """
                ).fetchall()
            rows = [
                {
                    "name": str(item[0]),
                    "collection": str(item[1]),
                    "description": str(item[2] or ""),
                    "tags": _parse_json_list(item[3]),
                    "paths": [],
                    "file_count": int(item[4] or 0),
                    "chunk_count": int(item[5] or 0),
                    "source": "duckdb_metadata",
                }
                for item in actual
            ]
        except Exception:
            pass
    rows = _filter_items(rows, query)
    return _page(rows, limit=limit, offset=offset)


def _providers_section(
    *,
    provider: str | None,
    query: str | None,
    category: str | None,
    realtime: bool | None,
    writes_db: bool | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    return list_astock_capabilities(
        provider=provider,
        query=query,
        category=category,
        realtime=realtime,
        writes_db=writes_db,
        limit=limit,
        offset=offset,
        compact=False,
    )


def _screening_rules_section(*, query: str | None, limit: int, offset: int) -> dict[str, Any]:
    from sats.screening.registry import get_rule, list_rules, rule_metadata

    rows = []
    for name in list_rules():
        rule = get_rule(name)
        metadata = rule_metadata(name)
        rows.append(
            {
                "name": name,
                "class": type(rule).__name__,
                "required_trade_days": getattr(rule, "required_trade_days", None),
                "description": metadata.description,
                "semantic_tags": list(metadata.semantic_tags),
                "condition_summary": metadata.condition_summary,
                "data_dependencies": list(metadata.data_dependencies),
            }
        )
    rows = _filter_items(rows, query)
    return _page(rows, limit=limit, offset=offset)


def _signals_section(*, query: str | None, category: str | None, limit: int, offset: int) -> dict[str, Any]:
    from sats.signals.registry import list_signal_definitions

    rows = [
        {
            "id": item.signal_id,
            "label": item.label,
            "category": item.category,
            "side": item.side,
            "description": item.description,
        }
        for item in list_signal_definitions(category=category)
    ]
    rows = _filter_items(rows, query)
    return _page(rows, limit=limit, offset=offset, extra={"by_category": dict(Counter(str(item.get("category") or "") for item in rows))})


def _factors_section(*, query: str | None, category: str | None, limit: int, offset: int) -> dict[str, Any]:
    from sats.factors.registry import Registry

    registry = Registry()
    factor_ids = registry.list(theme=category)
    rows = []
    for factor_id in factor_ids:
        factor = registry.get(factor_id)
        meta = dict(getattr(factor, "meta", {}) or {})
        rows.append(
            {
                "id": factor_id,
                "zoo": getattr(factor, "zoo", ""),
                "display_name": meta.get("display_name") or meta.get("nickname") or "",
                "theme": list(meta.get("theme") or []),
                "universe": list(meta.get("universe") or []),
                "columns_required": list(meta.get("columns_required") or []),
                "direction": meta.get("direction", ""),
            }
        )
    rows = _filter_items(rows, query)
    return _page(rows, limit=limit, offset=offset, extra={"health": registry.health()})


def _api_section(*, settings: Settings, query: str | None, limit: int, offset: int) -> dict[str, Any]:
    from sats.api.app import create_app

    app = create_app(settings=settings, storage=DuckDBStorage(settings.db_path, read_only=True))
    ignored = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    rows = [
        {
            "path": str(getattr(route, "path", "") or ""),
            "methods": sorted(getattr(route, "methods", []) or []),
            "name": str(getattr(route, "name", "") or ""),
        }
        for route in app.routes
        if str(getattr(route, "path", "") or "") not in ignored
    ]
    rows = _filter_items(rows, query)
    return _page(rows, limit=limit, offset=offset)


def _command_consistency() -> dict[str, Any]:
    import argparse

    from sats.agent.tools.command_tools import SATS_COMMANDS
    from sats.cli import build_parser
    from sats.repl import CLI_COMMANDS

    cli_commands: list[str] = []
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            cli_commands = list(action.choices)
            break
    expected_agent_exclusions = {"chat"}
    expected_repl_exclusions: set[str] = set()
    warnings = []
    unexpected_repl = sorted((set(cli_commands) - set(CLI_COMMANDS)) - expected_repl_exclusions)
    unexpected_agent = sorted((set(cli_commands) - set(SATS_COMMANDS)) - expected_agent_exclusions)
    if unexpected_repl:
        warnings.append(f"CLI 未同步到 REPL: {', '.join(unexpected_repl)}")
    if unexpected_agent:
        warnings.append(f"CLI 未同步到 Agent argv runner: {', '.join(unexpected_agent)}")
    return {
        "cli_top_level": cli_commands,
        "repl_cli": list(CLI_COMMANDS),
        "agent_argv": list(SATS_COMMANDS),
        "expected_repl_exclusions": sorted(expected_repl_exclusions),
        "expected_agent_exclusions": sorted(expected_agent_exclusions),
        "unexpected_cli_not_repl": unexpected_repl,
        "unexpected_cli_not_agent": unexpected_agent,
        "warnings": warnings,
    }


def _argument_payload(action: argparse.Action) -> dict[str, Any]:
    default = action.default
    if default is argparse.SUPPRESS:
        default = None
    return {
        "dest": action.dest,
        "flags": list(action.option_strings),
        "required": bool(getattr(action, "required", False)),
        "nargs": action.nargs,
        "default": _json_safe(default),
        "choices": [_json_safe(item) for item in action.choices] if action.choices is not None else None,
        "help": str(action.help or ""),
    }


def _filter_items(rows: list[dict[str, Any]], query: str | None) -> list[dict[str, Any]]:
    key = str(query or "").strip().lower()
    if not key:
        return rows
    return [item for item in rows if key in json.dumps(item, ensure_ascii=False, default=str).lower()]


def _page(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    offset: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    page = rows[offset : offset + limit]
    payload = {
        "total": len(rows),
        "returned": len(page),
        "offset": offset,
        "limit": limit,
        "truncated": offset + len(page) < len(rows),
        "items": page,
    }
    payload.update(extra or {})
    return payload


def _section_summary(payload: dict[str, Any]) -> dict[str, Any]:
    result = {"total": int(payload.get("total") or 0)}
    for key in ("by_provider", "by_category", "top_level_count", "health"):
        if key in payload:
            result[key] = payload[key]
    return result


def _parse_json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)
