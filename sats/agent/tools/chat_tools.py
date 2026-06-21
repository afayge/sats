from __future__ import annotations

from pathlib import Path
from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema, ok
from sats.chat_components import build_plain_chat_answer
from sats.rag.knowledge import KnowledgeStore
from sats.skills import default_skills_dir, find_skill, load_skills, skill_summaries


def chat_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="chat.answer",
            description="单步骤普通问答、解释和命令帮助。不读取 Agent 前序 observations，也不负责研究结果汇总。",
            category="chat",
            side_effect="readonly",
            timeout=60,
            input_schema=object_schema(
                {
                    "message": {"type": "string"},
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
    result = build_plain_chat_answer(
        str(arguments.get("message") or context.message or ""),
        settings=context.settings,
        skills=list(context.skills) or None,
        llm_factory=context.llm_factory,
    )
    payload = {
        "skill_names": [item.name for item in context.skills],
        "data_names": ["Chat"],
    }
    return ok(result.content, payload=payload, data_names=("Chat",))


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
