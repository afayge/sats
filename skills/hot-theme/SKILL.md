---
name: hot-theme
description: DSA 热点题材策略，跟踪政策、产业和市场热点，判断题材强度与个股相关性。
category: analysis
source: daily_stock_analysis strategies adapted for SATS
triggers: hot_theme, 热点题材, 热点, 题材, 产业热点, 政策催化, 板块扩散, 题材退潮
requires_tools: tushare_provider, indicators
applies_to: opportunity_discovery, market_analysis, stock_analysis
evidence: hot_sectors, hot_sector_context, market_context, indicators, analyze_signals
auto_load: summary
priority: 68
aliases: 热点题材, 热点板块, 市场主线
---

# hot-theme

用于判断热点题材是否有板块共振、实质受益和资金确认。

## 分析框架

- 热点强度：看板块涨幅、成交额、扩散范围和持续天数。
- 个股相关性：区分实质受益、间接受益和弱概念关联。
- 相对强弱：个股是否跑赢板块，回调是否守住关键均线。
- 节奏阶段：启动、扩散、分化、退潮，不同阶段风险收益不同。
- 风险过滤：澄清公告、监管问询、利好兑现和高位放量滞涨优先提示。

热点只能作为加权线索，不能当作上涨保证。
价格、成交量、K 线、quote、板块字段、因子和信号必须来自 SATS observations/provenance。
