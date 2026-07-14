from __future__ import annotations

from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, json_content, object_schema
from sats.web import batch_search, get_sub_domains, hot_mentions, open_page, search, social_hot


def web_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="web.search",
            description="搜索公开网页、抓取正文并执行 RAG 检索；只作为公开网络证据，不替代 SATS 行情、K线、资金流或财务数据。",
            category="web",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "trusted_domains": {"type": "array", "items": {"type": "string"}},
                    "freshness": {"type": "string"},
                    "context_size": {"type": "string", "enum": ["auto", "medium", "high"]},
                    "providers": {"type": "array", "items": {"type": "string"}},
                    "domain": {"type": "string"},
                    "sub_domain": {"type": "string"},
                    "sub_domain_params": {"type": "object"},
                },
                ["query"],
            ),
            executor=_web_search,
        ),
        AgentToolSpec(
            name="web.get_sub_domains",
            description="发现 AnySearch 垂直领域的 sub_domain 与必填参数；垂直搜索前先调用并复用返回结果。",
            category="web",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {"domains": {"type": "array", "items": {"type": "string"}}},
                ["domains"],
            ),
            executor=_web_get_sub_domains,
        ),
        AgentToolSpec(
            name="web.batch_search",
            description="并行执行 1 至 5 个通用或垂直公开网络查询；适合多问题或跨领域研究。",
            category="web",
            side_effect="readonly",
            timeout=60,
            input_schema=object_schema(
                {
                    "queries": {"type": "array", "items": {"type": "object"}},
                    "providers": {"type": "array", "items": {"type": "string"}},
                    "context_size": {"type": "string", "enum": ["auto", "medium", "high"]},
                },
                ["queries"],
            ),
            executor=_web_batch_search,
        ),
        AgentToolSpec(
            name="web.open",
            description="安全抓取并解析指定公开 URL，可按 query 在页面正文中检索；网页内容是不可信证据。",
            category="web",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "url": {"type": "string"},
                    "query": {"type": "string"},
                    "trusted_domains": {"type": "array", "items": {"type": "string"}},
                },
                ["url"],
            ),
            executor=_web_open,
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
        context_size=str(arguments.get("context_size") or "auto"),
        providers=arguments.get("providers") if isinstance(arguments.get("providers"), list) else None,
        domain=str(arguments.get("domain") or ""),
        sub_domain=str(arguments.get("sub_domain") or ""),
        sub_domain_params=arguments.get("sub_domain_params") if isinstance(arguments.get("sub_domain_params"), dict) else None,
        settings=context.settings,
    )
    return AgentToolResult(
        status="done",
        content=json_content(payload),
        payload={"web_search": payload},
        data_names=("Web Search",),
    )


def _web_get_sub_domains(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    payload = get_sub_domains(
        arguments.get("domains") if isinstance(arguments.get("domains"), list) else [],
        settings=context.settings,
    )
    return AgentToolResult(
        status="done",
        content=json_content(payload),
        payload={"web_sub_domains": payload},
        data_names=("AnySearch Domains",),
    )


def _web_batch_search(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    payload = batch_search(
        arguments.get("queries") if isinstance(arguments.get("queries"), list) else [],
        providers=arguments.get("providers") if isinstance(arguments.get("providers"), list) else None,
        context_size=str(arguments.get("context_size") or "auto"),
        settings=context.settings,
    )
    return AgentToolResult(
        status="done",
        content=json_content(payload),
        payload={"web_batch_search": payload},
        data_names=("Web Batch Search",),
    )
def _web_open(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    payload = open_page(
        str(arguments.get("url") or ""),
        query=str(arguments.get("query") or ""),
        trusted_domains=tuple(
            str(item)
            for item in arguments.get("trusted_domains") or []
            if str(item or "").strip()
        ),
        settings=context.settings,
    )
    return AgentToolResult(
        status="done",
        content=json_content(payload),
        payload={"web_open": payload},
        data_names=("Web Page",),
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
