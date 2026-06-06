from __future__ import annotations

from pathlib import Path
from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema, ok
from sats.rag.knowledge import KnowledgeStore
from sats.skills import default_skills_dir, find_skill, load_skills, skill_summaries


def chat_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="chat.answer",
            description="普通问答、解释、总结、命令帮助。只读；调用现有 ChatSession，但禁止再次进入 Agent。",
            category="chat",
            side_effect="readonly",
            timeout=60,
            input_schema=object_schema(
                {
                    "message": {"type": "string"},
                    "knowledge": {"type": "string"},
                    "no_memory": {"type": "boolean"},
                },
                ["message"],
            ),
            executor=_answer,
        ),
        AgentToolSpec(
            name="chat.list_skills",
            description="列出 SATS 本地 skills 分类摘要。",
            category="skill",
            side_effect="readonly",
            timeout=10,
            input_schema=object_schema(),
            executor=_list_skills,
        ),
        AgentToolSpec(
            name="chat.load_skill",
            description="按 skill 名称或 id 加载完整 SKILL.md 内容。",
            category="skill",
            side_effect="readonly",
            timeout=10,
            input_schema=object_schema({"name": {"type": "string"}}, ["name"]),
            executor=_load_skill,
        ),
        AgentToolSpec(
            name="chat.knowledge_search",
            description="搜索 SATS 本地 RAG 知识库。",
            category="knowledge",
            side_effect="readonly",
            timeout=20,
            input_schema=object_schema(
                {
                    "query": {"type": "string"},
                    "knowledge": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                ["query"],
            ),
            executor=_knowledge_search,
        ),
    ]


def _answer(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    from sats.chat import run_chat_once

    result = run_chat_once(
        str(arguments.get("message") or context.message or ""),
        settings=context.settings,
        skills=list(context.skills) or None,
        llm_factory=context.llm_factory,
        memory_enabled=not bool(arguments.get("no_memory", False)),
        knowledge=str(arguments.get("knowledge") or "").strip() or None,
        tools_enabled=False,
        preprocess_enabled=False,
    )
    payload = {
        "skill_names": list(result.skill_names),
        "data_names": list(result.data_names),
        "turn_id": result.turn_id or "",
        "session_id": result.session_id or "",
    }
    return ok(result.content, payload=payload, data_names=tuple(result.data_names or ("Chat",)), artifacts=tuple(result.artifacts or ()))


def _list_skills(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    skills = _skills(context)
    return ok(skill_summaries(skills), payload={"skills": [item.name for item in skills]}, data_names=("Skills",))


def _load_skill(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    skills = _skills(context)
    skill = find_skill(skills, str(arguments.get("name") or ""))
    if skill is None:
        return AgentToolResult(status="error", content="unknown skill", payload={"available": [item.name for item in skills]})
    payload = {
        "name": skill.name,
        "id": skill.id,
        "category": skill.category,
        "description": skill.description,
        "content": skill.content,
    }
    return ok(skill.content, payload=payload, data_names=("Skill",))


def _knowledge_search(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    store = KnowledgeStore(context.settings.db_path)
    rows = store.search(
        str(arguments.get("query") or ""),
        knowledge=str(arguments.get("knowledge") or "").strip() or None,
        limit=max(1, int(arguments.get("limit") or 6)),
    )
    payload = {
        "results": [
            {
                "chunk_id": row.chunk_id,
                "knowledge_id": row.knowledge_id,
                "knowledge_name": row.knowledge_name,
                "collection_name": row.collection_name,
                "source_path": row.source_path,
                "title": row.title,
                "content": row.content,
                "score": row.score,
                "tags": list(row.tags),
            }
            for row in rows
        ]
    }
    lines = []
    for index, row in enumerate(payload["results"], start=1):
        lines.append(f"{index}. {row.get('knowledge_name') or row.get('knowledge') or ''}: {row.get('text') or row.get('content') or ''}")
    return ok("\n".join(lines) or "无搜索结果", payload=payload, data_names=("Knowledge",))


def _skills(context: AgentToolContext) -> list[Any]:
    if context.skills:
        return list(context.skills)
    project_root = Path(getattr(context.settings, "project_root", "."))
    return load_skills(default_skills_dir(project_root))
