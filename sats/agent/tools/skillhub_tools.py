from __future__ import annotations

from pathlib import Path
from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema, ok
from sats.skillhub import (
    format_skillhub_records,
    format_skillhub_status,
    search_local_skillhub_records,
    skillhub_status,
)


def skillhub_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="skillhub.search",
            description=(
                "搜索已安装到 SATS 的同花顺问财 SkillHub skills。"
                "用于自然对话中发现研报、公告、新闻、行情、宏观、基金、期权、投研工作流等 SkillHub 能力；"
                "只读取本地生成的 skill 元数据，不调用外部问财 API。"
            ),
            category="skillhub",
            side_effect="readonly",
            timeout=10,
            input_schema=object_schema(
                {
                    "query": {"type": "string"},
                    "classify": {"type": "string", "enum": ["", "OFFICIAL", "THIRD_PARTY"]},
                    "limit": {"type": "integer"},
                }
            ),
            executor=_search,
            metadata={"output_shape": "skillhub_skill_list", "enumerates_universe": False},
        ),
        AgentToolSpec(
            name="skillhub.load",
            description=(
                "加载一个已安装 SkillHub skill 的本地 SATS wrapper 内容。"
                "该内容只能作为路由/方法论上下文，真实行情和财务数据仍必须通过 SATS 注册数据工具获取。"
            ),
            category="skillhub",
            side_effect="readonly",
            timeout=10,
            input_schema=object_schema({"name": {"type": "string"}}, ["name"]),
            executor=_load,
            metadata={"output_shape": "skillhub_skill_detail", "enumerates_universe": False},
        ),
        AgentToolSpec(
            name="skillhub.status",
            description="检查 SkillHub skills 是否已安装、本地数量、问财 API 环境变量是否存在，以及官方 CLI 是否在 PATH 中。",
            category="skillhub",
            side_effect="readonly",
            timeout=10,
            input_schema=object_schema(),
            executor=_status,
            metadata={"output_shape": "skillhub_status", "enumerates_universe": False},
        ),
    ]


def _search(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    rows = search_local_skillhub_records(
        Path(context.settings.project_root),
        query=str(arguments.get("query") or ""),
        classify=str(arguments.get("classify") or ""),
        limit=int(arguments.get("limit") or 20),
    )
    return ok(
        format_skillhub_records(rows),
        payload={"skillhub": {"records": rows, "count": len(rows)}},
        data_names=("SkillHub",),
    )


def _load(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    key = str(arguments.get("name") or "").strip().lower()
    rows = search_local_skillhub_records(Path(context.settings.project_root), limit=10000)
    for row in rows:
        candidates = {
            str(row.get("id") or "").lower(),
            str(row.get("name") or "").lower(),
            str(row.get("cn_name") or "").lower(),
            str(row.get("display_name") or "").lower(),
            str(row.get("skill_uuid") or "").lower(),
        }
        if key in candidates:
            skill_id = str(row.get("id") or "")
            path = Path(context.settings.project_root) / "skills" / skill_id / "SKILL.md"
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                return AgentToolResult(status="error", content=str(exc))
            return ok(
                content,
                payload={"skillhub": {"record": row, "content": content}},
                data_names=("SkillHub Skill",),
            )
    return AgentToolResult(status="error", content="unknown SkillHub skill", payload={"available": rows[:50]})


def _status(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    status = skillhub_status(
        Path(context.settings.project_root),
        api_key=str(getattr(context.settings, "iwencai_api_key", "") or ""),
        base_url=str(getattr(context.settings, "iwencai_base_url", "") or ""),
        cli_name=str(getattr(context.settings, "iwencai_skillhub_cli", "") or ""),
    )
    return ok(format_skillhub_status(status), payload={"skillhub": status}, data_names=("SkillHub",))
