from __future__ import annotations

from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, json_content, object_schema, ok
from sats.data.astock_provider import AStockDataProvider
from sats.data.resolver import require_market_data_provenance
from sats.symbols import normalize_symbols


def data_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="data.list_provider_capabilities",
            description="列出 SATS 已接入的 Tushare/TickFlow 数据能力目录，供计划阶段选择真实数据工具。",
            category="data_catalog",
            side_effect="readonly",
            timeout=20,
            input_schema=object_schema(
                {
                    "provider": {"type": "string"},
                    "category": {"type": "string"},
                    "realtime": {"type": "boolean"},
                    "compact": {"type": "boolean"},
                }
            ),
            executor=_list_provider_capabilities,
        ),
        AgentToolSpec(
            name="data.stock_basic",
            description="通过 AStockDataProvider 获取 A 股股票基础信息；优先 TickFlow universe/instruments，回退 Tushare，并写回 DuckDB。",
            category="data",
            side_effect="write_db",
            timeout=60,
            input_schema=object_schema(),
            executor=_stock_basic,
        ),
        AgentToolSpec(
            name="data.stock_daily",
            description="DuckDB-first 获取 A 股日 K；缺口由 AStockDataProvider 补齐并写回 DuckDB。",
            category="data",
            side_effect="write_db",
            timeout=60,
            input_schema=object_schema(
                {
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                },
                ["symbols", "start_date", "end_date"],
            ),
            executor=_stock_daily,
        ),
        AgentToolSpec(
            name="data.index_daily",
            description="DuckDB-first 获取 A 股指数日 K；缺口由 AStockDataProvider 补齐并写回 DuckDB。",
            category="data",
            side_effect="write_db",
            timeout=60,
            input_schema=object_schema(
                {
                    "index_codes": {"type": "array", "items": {"type": "string"}},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                },
                ["index_codes", "start_date", "end_date"],
            ),
            executor=_index_daily,
        ),
        AgentToolSpec(
            name="data.stock_minute",
            description="DuckDB-first 获取 A 股分钟 K；按 ts_code、period、datetime 覆盖补齐。",
            category="data",
            side_effect="write_db",
            timeout=60,
            input_schema=object_schema(
                {
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "period": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "count": {"type": "integer"},
                },
                ["symbols"],
            ),
            executor=_stock_minute,
        ),
        AgentToolSpec(
            name="data.realtime_quotes",
            description="DuckDB-first 获取 A 股实时 quote；分析 TTL 60 秒，交易 TTL 30 秒。",
            category="data",
            side_effect="write_db",
            timeout=30,
            input_schema=object_schema(
                {
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "for_trading": {"type": "boolean"},
                },
                ["symbols"],
            ),
            executor=_realtime_quotes,
        ),
        AgentToolSpec(
            name="data.indicator_inputs",
            description="通过 resolver 获取指标计算输入，返回带 provenance 的指标输入摘要。",
            category="data",
            side_effect="write_db",
            timeout=60,
            input_schema=object_schema(
                {
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "trade_date": {"type": "string"},
                    "lookback_days": {"type": "integer"},
                },
                ["symbols", "trade_date"],
            ),
            executor=_indicator_inputs,
        ),
        AgentToolSpec(
            name="data.list_tushare_datasets",
            description="列出 SATS 白名单 Tushare 数据集。",
            category="data_catalog",
            side_effect="readonly",
            timeout=20,
            input_schema=object_schema(
                {
                    "domain": {"type": "string"},
                    "category": {"type": "string"},
                    "include_deprecated": {"type": "boolean"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                }
            ),
            executor=_list_tushare_datasets,
        ),
        AgentToolSpec(
            name="data.get_tushare_data",
            description="获取 SATS 白名单 Tushare 数据集行数据；只读，不写 DuckDB。",
            category="data",
            side_effect="readonly",
            timeout=60,
            input_schema=object_schema(
                {
                    "dataset": {"type": "string"},
                    "params": {"type": "object"},
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
                ["dataset"],
            ),
            executor=_get_tushare_data,
        ),
        AgentToolSpec(
            name="data.list_tushare_stock_datasets",
            description="列出 SATS 白名单 Tushare 股票数据集。",
            category="data_catalog",
            side_effect="readonly",
            timeout=20,
            input_schema=object_schema(
                {
                    "category": {"type": "string"},
                    "include_deprecated": {"type": "boolean"},
                }
            ),
            executor=_list_tushare_stock_datasets,
        ),
        AgentToolSpec(
            name="data.get_tushare_stock_data",
            description="获取 SATS 白名单 Tushare 股票数据集行数据；只读，不写 DuckDB。",
            category="data",
            side_effect="readonly",
            timeout=60,
            input_schema=object_schema(
                {
                    "dataset": {"type": "string"},
                    "params": {"type": "object"},
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
                ["dataset"],
            ),
            executor=_get_tushare_stock_data,
        ),
        AgentToolSpec(
            name="data.list_akshare_datasets",
            description="列出 SATS 白名单 AkShare 全量数据字典接口；只读，不写 DuckDB。",
            category="data_catalog",
            side_effect="readonly",
            timeout=20,
            input_schema=object_schema(
                {
                    "domain": {"type": "string"},
                    "category": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "query": {"type": "string"},
                    "realtime": {"type": "boolean"},
                    "compact": {"type": "boolean"},
                }
            ),
            executor=_list_akshare_datasets,
        ),
        AgentToolSpec(
            name="data.describe_akshare_dataset",
            description="查看一个 AkShare 白名单 dataset 的入参、分类和取数元信息；只读。",
            category="data_catalog",
            side_effect="readonly",
            timeout=20,
            input_schema=object_schema(
                {
                    "dataset": {"type": "string"},
                },
                ["dataset"],
            ),
            executor=_describe_akshare_dataset,
        ),
        AgentToolSpec(
            name="data.get_akshare_data",
            description="按 AkShare 白名单 dataset 取数；只读、不写库，参数必须为 JSON 安全值。",
            category="data",
            side_effect="readonly",
            timeout=60,
            input_schema=object_schema(
                {
                    "dataset": {"type": "string"},
                    "params": {"type": "object"},
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
                ["dataset"],
            ),
            executor=_get_akshare_data,
        ),
    ]


def _list_provider_capabilities(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    provider = AStockDataProvider(context.settings)
    capabilities = provider.load_provider_capabilities(
        provider=str(arguments.get("provider") or "").strip() or None,
        category=str(arguments.get("category") or "").strip() or None,
        realtime=arguments.get("realtime") if isinstance(arguments.get("realtime"), bool) else None,
        compact=bool(arguments.get("compact", False)),
    )
    return ok(
        f"listed {len(capabilities)} provider capabilities",
        payload={"capabilities": capabilities},
        data_names=("Provider capabilities",),
    )


def _stock_basic(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    provider = AStockDataProvider(context.settings)
    frame = provider.load_stock_basic(storage=context.storage)
    return _frame_result("stock_basic", frame)


def _stock_daily(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
    frame = context.resolver.load_stock_daily(symbols, start_date=str(arguments.get("start_date") or ""), end_date=str(arguments.get("end_date") or ""))
    require_market_data_provenance(frame, dataset="stock_daily")
    return _frame_result("stock_daily", frame)


def _index_daily(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    frame = context.resolver.load_index_daily(
        [str(item) for item in arguments.get("index_codes") or []],
        start_date=str(arguments.get("start_date") or ""),
        end_date=str(arguments.get("end_date") or ""),
    )
    require_market_data_provenance(frame, dataset="index_daily")
    return _frame_result(
        "index_daily",
        frame,
        include_rows=True,
        sample_limit=80,
        group_tail_by="index_code",
        group_tail=10,
    )


def _stock_minute(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
    frame = context.resolver.load_stock_minute(
        symbols,
        period=str(arguments.get("period") or "1m"),
        start_time=str(arguments.get("start_time") or "") or None,
        end_time=str(arguments.get("end_time") or "") or None,
        count=arguments.get("count"),
    )
    require_market_data_provenance(frame, dataset="stock_minute")
    return _frame_result("stock_minute", frame)


def _realtime_quotes(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
    frame = context.resolver.load_realtime_quotes(symbols, for_trading=bool(arguments.get("for_trading", False)))
    require_market_data_provenance(frame, dataset="realtime_quote")
    return _frame_result("realtime_quote", frame, include_rows=True)


def _indicator_inputs(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
    inputs = context.resolver.load_indicator_inputs(
        symbols,
        str(arguments.get("trade_date") or ""),
        lookback_days=int(arguments.get("lookback_days") or 180),
    )
    payload = {
        "inputs": [
            {
                "ts_code": item.ts_code,
                "trade_date": item.trade_date,
                "daily_rows": int(len(item.daily)),
                "data_sources": dict(item.data_sources or {}),
            }
            for item in inputs
        ]
    }
    return ok(f"loaded indicator inputs for {len(inputs)} symbols", payload=payload, data_names=("指标输入",))


def _list_tushare_datasets(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    provider = AStockDataProvider(context.settings)
    datasets = provider.list_tushare_datasets(
        domain=str(arguments.get("domain") or "").strip() or None,
        category=str(arguments.get("category") or "").strip() or None,
        include_deprecated=bool(arguments.get("include_deprecated", True)),
        tags=arguments.get("tags") if isinstance(arguments.get("tags"), list) else None,
    )
    return ok(f"listed {len(datasets)} Tushare datasets", payload={"datasets": datasets}, data_names=("Tushare",))


def _get_tushare_data(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    provider = AStockDataProvider(context.settings)
    payload = provider.fetch_tushare_dataset(
        str(arguments.get("dataset") or "").strip(),
        arguments.get("params") if isinstance(arguments.get("params"), dict) else {},
        fields=arguments.get("fields") if isinstance(arguments.get("fields"), list) else None,
        limit=int(arguments.get("limit") or 200),
    )
    return ok("loaded Tushare dataset", payload={"tushare_data": payload}, data_names=("Tushare",))


def _list_tushare_stock_datasets(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    provider = AStockDataProvider(context.settings)
    datasets = provider.list_tushare_stock_datasets(
        category=str(arguments.get("category") or "").strip() or None,
        include_deprecated=bool(arguments.get("include_deprecated", True)),
    )
    return ok(f"listed {len(datasets)} Tushare stock datasets", payload={"datasets": datasets}, data_names=("Tushare 股票数据",))


def _get_tushare_stock_data(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    provider = AStockDataProvider(context.settings)
    payload = provider.fetch_tushare_stock_dataset(
        str(arguments.get("dataset") or "").strip(),
        arguments.get("params") if isinstance(arguments.get("params"), dict) else {},
        fields=arguments.get("fields") if isinstance(arguments.get("fields"), list) else None,
        limit=int(arguments.get("limit") or 200),
    )
    return ok("loaded Tushare stock dataset", payload={"tushare_stock_data": payload}, data_names=("Tushare 股票数据",))


def _list_akshare_datasets(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    provider = AStockDataProvider(context.settings)
    datasets = provider.list_akshare_datasets(
        domain=str(arguments.get("domain") or "").strip() or None,
        category=str(arguments.get("category") or "").strip() or None,
        tags=arguments.get("tags") if isinstance(arguments.get("tags"), list) else None,
        query=str(arguments.get("query") or "").strip() or None,
        realtime=arguments.get("realtime") if isinstance(arguments.get("realtime"), bool) else None,
        compact=bool(arguments.get("compact", True)),
    )
    return ok(f"listed {len(datasets)} AkShare datasets", payload={"datasets": datasets}, data_names=("AkShare 数据字典",))


def _describe_akshare_dataset(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    provider = AStockDataProvider(context.settings)
    payload = provider.describe_akshare_dataset(str(arguments.get("dataset") or "").strip())
    return ok("described AkShare dataset", payload={"dataset": payload}, data_names=("AkShare 数据字典",))


def _get_akshare_data(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    provider = AStockDataProvider(context.settings)
    payload = provider.fetch_akshare_dataset(
        str(arguments.get("dataset") or "").strip(),
        arguments.get("params") if isinstance(arguments.get("params"), dict) else {},
        fields=arguments.get("fields") if isinstance(arguments.get("fields"), list) else None,
        limit=int(arguments.get("limit") or 200),
    )
    return ok("loaded AkShare dataset", payload={"akshare_data": payload}, data_names=("AkShare 数据",))


def _frame_result(
    name: str,
    frame: Any,
    *,
    include_rows: bool = False,
    sample_limit: int = 20,
    group_tail_by: str = "",
    group_tail: int = 0,
) -> AgentToolResult:
    provenance = frame.attrs.get("market_data_provenance") or []
    payload: dict[str, Any] = {"rows": int(len(frame)), "columns": list(frame.columns), "provenance": provenance}
    if include_rows:
        sample = frame
        if group_tail_by and group_tail_by in frame.columns and group_tail > 0:
            sample = frame.groupby(group_tail_by, group_keys=False).tail(group_tail)
        payload["sample"] = sample.head(max(1, int(sample_limit))).to_dict(orient="records")
    return ok(f"{name}: {len(frame)} rows\n{json_content(provenance)}", payload=payload, data_names=(name,))
