from __future__ import annotations

from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema, ok
from sats.catalog import CATALOG_SECTIONS, build_capability_catalog


def catalog_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="catalog.capabilities",
            description="读取 SATS 统一能力目录；可查询命令、Agent tools、Skills、知识库、数据接口、规则、信号、因子和 API。",
            category="catalog",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "section": {"type": "string", "enum": list(CATALOG_SECTIONS)},
                    "provider": {"type": "string"},
                    "query": {"type": "string"},
                    "category": {"type": "string"},
                    "realtime": {"type": "boolean"},
                    "writes_db": {"type": "boolean"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                }
            ),
            executor=_capabilities,
        )
    ]


def _capabilities(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    catalog = build_capability_catalog(
        settings=context.settings,
        section=str(arguments.get("section") or "summary"),
        provider=str(arguments.get("provider") or "").strip() or None,
        query=str(arguments.get("query") or "").strip() or None,
        category=str(arguments.get("category") or "").strip() or None,
        realtime=arguments.get("realtime") if isinstance(arguments.get("realtime"), bool) else None,
        writes_db=arguments.get("writes_db") if isinstance(arguments.get("writes_db"), bool) else None,
        limit=int(arguments.get("limit") or 50),
        offset=int(arguments.get("offset") or 0),
    )
    return ok(
        f"catalog section {catalog['section']}",
        payload={"catalog": catalog},
        data_names=("SATS capabilities",),
    )
