from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sats.data.akshare_datasets import list_akshare_datasets
from sats.data.tushare_stock_datasets import TUSHARE_STOCK_DATASETS, list_tushare_datasets


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    capability_id: str
    name: str
    provider: str
    category: str
    use_cases: tuple[str, ...]
    input_fields: tuple[str, ...]
    output_fields: tuple[str, ...]
    realtime: bool
    writes_db: bool
    recommended_tool: str
    chat_tool: str = ""

    def to_dict(self, *, compact: bool = False) -> dict[str, Any]:
        output_fields = self.output_fields[:8] if compact else self.output_fields
        use_cases = self.use_cases[:2] if compact else self.use_cases
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "provider": self.provider,
            "category": self.category,
            "use_cases": list(use_cases),
            "input_fields": list(self.input_fields),
            "output_fields": list(output_fields),
            "realtime": self.realtime,
            "writes_db": self.writes_db,
            "recommended_tool": self.recommended_tool,
            "chat_tool": self.chat_tool,
        }


def list_provider_capabilities(
    *,
    provider: str | None = None,
    category: str | None = None,
    realtime: bool | None = None,
    compact: bool = False,
) -> list[dict[str, Any]]:
    capabilities = [*_tushare_capabilities(), *_tickflow_capabilities(), *_akshare_capabilities()]
    provider_key = str(provider or "").strip().lower()
    category_key = str(category or "").strip().lower()
    rows: list[ProviderCapability] = []
    for item in capabilities:
        if provider_key and item.provider.lower() != provider_key:
            continue
        if category_key and category_key not in item.category.lower():
            continue
        if realtime is not None and item.realtime is not bool(realtime):
            continue
        rows.append(item)
    return [item.to_dict(compact=compact) for item in rows]


def planner_provider_capabilities() -> list[dict[str, Any]]:
    from sats.data.astock_operations import planner_astock_capabilities

    return planner_astock_capabilities()


def _tushare_capabilities() -> list[ProviderCapability]:
    stock_dataset_names = set(TUSHARE_STOCK_DATASETS)
    rows: list[ProviderCapability] = []
    for dataset in list_tushare_datasets(include_deprecated=True):
        name = str(dataset.get("title") or dataset.get("dataset") or "")
        dataset_id = str(dataset.get("dataset") or "").strip()
        domain = str(dataset.get("domain") or "")
        category = str(dataset.get("category") or "")
        is_stock_dataset = dataset_id in stock_dataset_names
        rows.append(
            ProviderCapability(
                capability_id=f"tushare.{dataset_id}",
                name=name,
                provider="tushare",
                category="/".join(part for part in (domain, category) if part),
                use_cases=tuple(
                    item
                    for item in (
                        name,
                        domain,
                        category,
                    )
                    if item
                ),
                input_fields=tuple(str(item) for item in dataset.get("input_fields") or ()),
                output_fields=tuple(str(item) for item in dataset.get("output_fields") or ()),
                realtime=_is_realtime_tushare_dataset(dataset_id),
                writes_db=False,
                recommended_tool="data.get_tushare_stock_data" if is_stock_dataset else "data.get_tushare_data",
                chat_tool="get_tushare_stock_data" if is_stock_dataset else "get_tushare_data",
            )
        )
    return rows


def _tickflow_capabilities() -> list[ProviderCapability]:
    return [
        ProviderCapability(
            capability_id="tickflow.universe_symbols",
            name="A 股股票池 universe",
            provider="tickflow",
            category="基础数据",
            use_cases=("获取 CN_Equity_A 股票池", "构建 A 股全市场候选范围"),
            input_fields=("universe_id",),
            output_fields=("ts_code",),
            realtime=False,
            writes_db=False,
            recommended_tool="data.stock_basic",
            chat_tool="get_stock_research_context",
        ),
        ProviderCapability(
            capability_id="tickflow.stock_basic",
            name="Instrument / stock_basic",
            provider="tickflow",
            category="基础数据",
            use_cases=("获取股票名称、行业、市场、上市状态", "同步本地 stock_basic"),
            input_fields=("symbols", "universe_id"),
            output_fields=("ts_code", "symbol", "name", "industry", "market", "exchange", "list_date"),
            realtime=False,
            writes_db=True,
            recommended_tool="data.stock_basic",
            chat_tool="get_stock_research_context",
        ),
        ProviderCapability(
            capability_id="tickflow.realtime_quotes",
            name="当日日 K 实时报价",
            provider="tickflow",
            category="实时行情",
            use_cases=("从 TickFlow 当日日 K 提取实时价格、成交量/额", "交易前 quote 校验"),
            input_fields=("symbols", "universe_id"),
            output_fields=("ts_code", "price", "open", "high", "low", "volume", "amount", "pct_chg", "fetched_at"),
            realtime=True,
            writes_db=True,
            recommended_tool="data.realtime_quotes",
            chat_tool="get_stock_research_context",
        ),
        ProviderCapability(
            capability_id="tickflow.historical_klines",
            name="历史 K 线",
            provider="tickflow",
            category="行情数据",
            use_cases=("历史日/周/月/季/年 K", "技术指标和趋势分析"),
            input_fields=("symbols", "period", "start_time", "end_time", "count", "adjust"),
            output_fields=("ts_code", "trade_date", "trade_time", "open", "high", "low", "close", "vol", "amount"),
            realtime=False,
            writes_db=True,
            recommended_tool="data.stock_daily",
            chat_tool="get_stock_research_context",
        ),
        ProviderCapability(
            capability_id="tickflow.realtime_minute_klines",
            name="当日分钟 K",
            provider="tickflow",
            category="分钟K",
            use_cases=("盘中 1m/5m/15m/30m/60m K 线，支持 15min/15分钟 别名和 10min 等派生周期", "缠论和日内走势确认"),
            input_fields=("symbols", "period", "count"),
            output_fields=("ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount"),
            realtime=True,
            writes_db=True,
            recommended_tool="data.stock_minute",
            chat_tool="get_stock_research_context",
        ),
        ProviderCapability(
            capability_id="tickflow.current_daily_kline",
            name="当日日 K",
            provider="tickflow",
            category="行情数据",
            use_cases=("交易时段筛选的当日日线覆盖", "盘中累计 OHLCV"),
            input_fields=("symbols", "trade_date"),
            output_fields=("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"),
            realtime=True,
            writes_db=False,
            recommended_tool="data.stock_daily",
            chat_tool="get_stock_research_context",
        ),
        ProviderCapability(
            capability_id="tickflow.historical_minute_klines",
            name="历史分钟 K",
            provider="tickflow",
            category="分钟K",
            use_cases=("历史 1m/5m/15m/30m/60m K 线，支持 15min/15分钟 别名和 10min 等派生周期", "回看日内结构"),
            input_fields=("symbols", "period", "start_time", "end_time", "count"),
            output_fields=("ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount"),
            realtime=False,
            writes_db=True,
            recommended_tool="data.stock_minute",
            chat_tool="get_stock_research_context",
        ),
        ProviderCapability(
            capability_id="tickflow.intraday_timeshare",
            name="日内分时别名",
            provider="tickflow",
            category="分时数据",
            use_cases=("日内分时走势", "实时分钟 K 的轻量别名"),
            input_fields=("symbols", "period", "count"),
            output_fields=("ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount"),
            realtime=True,
            writes_db=False,
            recommended_tool="data.stock_minute",
            chat_tool="get_stock_research_context",
        ),
        ProviderCapability(
            capability_id="tickflow.market_depth",
            name="盘口 depth",
            provider="tickflow",
            category="实时盘口",
            use_cases=("盘口买卖档位", "微观结构观察"),
            input_fields=("symbols",),
            output_fields=("ts_code", "bid_price", "bid_volume", "ask_price", "ask_volume", "fetched_at"),
            realtime=True,
            writes_db=False,
            recommended_tool="data.list_provider_capabilities",
            chat_tool="list_provider_capabilities",
        ),
        ProviderCapability(
            capability_id="tickflow.ex_factors",
            name="复权因子",
            provider="tickflow",
            category="复权数据",
            use_cases=("复权价格计算", "历史 K 线调整"),
            input_fields=("symbols", "start_time", "end_time"),
            output_fields=("ts_code", "timestamp", "factor", "data_source"),
            realtime=False,
            writes_db=False,
            recommended_tool="data.list_provider_capabilities",
            chat_tool="list_provider_capabilities",
        ),
        ProviderCapability(
            capability_id="tickflow.realtime_daily_basic_like",
            name="实时 daily_basic-like",
            provider="tickflow",
            category="衍生指标",
            use_cases=("盘中用 quote 和股本推导市值/换手等近似字段", "Tushare daily_basic 盘中缺失时兜底"),
            input_fields=("symbols", "trade_date", "daily_frame", "share_frame"),
            output_fields=("ts_code", "trade_date", "close", "turnover_rate", "total_mv", "circ_mv", "volume_ratio"),
            realtime=True,
            writes_db=False,
            recommended_tool="data.indicator_inputs",
            chat_tool="get_stock_research_context",
        ),
    ]


def _akshare_capabilities() -> list[ProviderCapability]:
    datasets = list_akshare_datasets(compact=True)
    domains = sorted({str(item.get("domain") or "AkShare") for item in datasets})
    rows = [
        ProviderCapability(
            capability_id="akshare.dataset_catalog",
            name="AkShare 全量数据字典",
            provider="akshare",
            category="数据字典",
            use_cases=("按领域、分类、标签或关键词发现 AkShare 数据接口", "先查目录/描述，再按白名单 dataset 取数"),
            input_fields=("domain", "category", "tags", "query", "realtime", "compact"),
            output_fields=("dataset", "function_name", "domain", "category", "tags", "input_fields", "realtime"),
            realtime=False,
            writes_db=False,
            recommended_tool="data.list_akshare_datasets",
            chat_tool="list_akshare_datasets",
        ),
        ProviderCapability(
            capability_id="akshare.dataset_fetch",
            name="AkShare 白名单数据取数",
            provider="akshare",
            category="数据接口",
            use_cases=("补充 TickFlow/Tushare 未覆盖的宏观、行业、期货、期权、债券、基金等数据", "用户明确要求 AkShare 数据时按 dataset 取数"),
            input_fields=("dataset", "params", "fields", "limit"),
            output_fields=("rows", "columns", "head", "tail", "latest", "market_data_provenance", "missing_fields"),
            realtime=True,
            writes_db=False,
            recommended_tool="data.get_akshare_data",
            chat_tool="get_akshare_data",
        ),
    ]
    for domain in domains:
        domain_rows = [item for item in datasets if item.get("domain") == domain]
        rows.append(
            ProviderCapability(
                capability_id=f"akshare.domain.{_capability_key(domain)}",
                name=f"AkShare {domain} 数据接口",
                provider="akshare",
                category=domain,
                use_cases=(f"发现 {domain} 相关 AkShare 接口", f"当前目录约 {len(domain_rows)} 个接口"),
                input_fields=("query", "category", "tags", "realtime"),
                output_fields=("dataset", "title", "input_fields", "realtime"),
                realtime=any(bool(item.get("realtime")) for item in domain_rows),
                writes_db=False,
                recommended_tool="data.list_akshare_datasets",
                chat_tool="list_akshare_datasets",
            )
        )
    return rows


def _capability_key(value: str) -> str:
    result = []
    for char in str(value or "").lower():
        if char.isalnum():
            result.append(char)
        elif char in {" ", "-", "_", "/"}:
            result.append("_")
    key = "".join(result).strip("_")
    return key or "general"


def _is_realtime_tushare_dataset(dataset: str) -> bool:
    return str(dataset or "").strip().lower() in {"rt_k", "realtime_quote"}
