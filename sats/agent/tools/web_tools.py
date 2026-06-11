from __future__ import annotations

from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, json_content, object_schema
from sats.web import hot_mentions, search, social_hot


def web_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="web.search",
            description="搜索公开网页标题和摘要；只作为公开网络证据，不替代 SATS 行情、K线、资金流或财务数据。",
            category="web",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "trusted_domains": {"type": "array", "items": {"type": "string"}},
                    "freshness": {"type": "string"},
                },
                ["query"],
            ),
            executor=_web_search,
        ),
        AgentToolSpec(
            name="web.social_hot",
            description="获取微博、知乎、百度、抖音、头条、B站、雪球热股/热点公共热榜；单平台失败会降级。",
            category="web",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "platforms": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                }
            ),
            executor=_social_hot,
        ),
        AgentToolSpec(
            name="web.hot_mentions",
            description="按股票名、公司名或主题词在社交热榜中查命中；用于舆情/热度，不作为行情数据。",
            category="web",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "keyword": {"type": "string"},
                    "platforms": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                    "extra_keywords": {"type": "array", "items": {"type": "string"}},
                },
                ["keyword"],
            ),
            executor=_hot_mentions,
        ),
    ]


def _web_search(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    payload = search(
        str(arguments.get("query") or ""),
        limit=int(arguments.get("limit") or 5),
        trusted_domains=arguments.get("trusted_domains") if isinstance(arguments.get("trusted_domains"), list) else None,
        freshness=str(arguments.get("freshness") or ""),
        settings=context.settings,
    )
    return AgentToolResult(
        status="done",
        content=json_content(payload),
        payload={"web_search": payload},
        data_names=("Web Search",),
    )


def _social_hot(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    payload = social_hot(
        platforms=arguments.get("platforms") if isinstance(arguments.get("platforms"), list) else None,
        limit=int(arguments.get("limit") or 20),
        settings=context.settings,
    )
    return AgentToolResult(
        status="done",
        content=json_content(payload),
        payload={"social_hot": payload},
        data_names=("社交热榜",),
    )


def _hot_mentions(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    payload = hot_mentions(
        str(arguments.get("keyword") or ""),
        platforms=arguments.get("platforms") if isinstance(arguments.get("platforms"), list) else None,
        limit=int(arguments.get("limit") or 50),
        extra_keywords=arguments.get("extra_keywords") if isinstance(arguments.get("extra_keywords"), list) else None,
        settings=context.settings,
    )
    return AgentToolResult(
        status="done",
        content=json_content(payload),
        payload={"hot_mentions": payload},
        data_names=("社交热榜",),
    )
