---
name: dragon-head
description: DSA 龙头策略，在板块轮动或题材启动时识别相对强度领先的 A 股候选。
category: strategy
source: daily_stock_analysis strategies adapted for SATS
triggers: dragon_head, 龙头, 龙头战法, 板块龙头, 领涨股, 相对强度, 板块启动
requires_tools: tickflow_provider, tushare_provider, indicators
applies_to: opportunity_discovery, market_analysis, stock_analysis
evidence: hot_sectors, hot_sector_context, market_context, indicators, analyze_signals, native_dsa
auto_load: summary
priority: 62
aliases: 龙头战法, 板块龙头, 领涨股
---

# dragon-head

用于板块或题材行情中的龙头候选分析。

## 判定要点

- 板块地位：所在行业或主题近期涨幅、成交额、人气处于前列。
- 个股强度：个股涨幅、成交额、换手和回撤韧性强于板块平均。
- 启动顺序：率先突破、率先涨停或率先修复的股票更像龙头。
- 催化验证：政策、业绩、订单或产业事件需要有可核验来源。
- 追高过滤：连续加速和高乖离位置只能给观察条件，不能直接建议追买。

SATS 若未注入板块或热点上下文，应说明限制并只给分析框架。
价格、成交量、K 线、quote、板块字段、因子和信号必须来自 SATS observations/provenance。
