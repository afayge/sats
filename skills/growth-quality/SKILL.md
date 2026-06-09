---
name: growth-quality
description: DSA 成长质量策略，结合收入利润增长、ROE、现金流和行业空间识别高质量成长或成长失速。
category: analysis
source: daily_stock_analysis strategies adapted for SATS
triggers: growth_quality, 成长质量, 成长股, 高质量成长, 成长失速, ROE, 现金流, 行业空间
requires_tools: tushare_provider, indicators
applies_to: financial_analysis, stock_analysis, opportunity_discovery
evidence: stock_context, factor_summary, tushare_data
auto_load: summary
priority: 64
aliases: 成长质量, 高质量成长
---

# growth-quality

用于中长期成长股研究，避免只看概念和短线涨幅。

## 分析框架

- 成长性：收入、利润、经营现金流是否同向改善。
- 盈利质量：ROE 稳定性、现金流与净利润匹配度、应收和存货风险。
- 估值承受力：高成长可以承受更高估值，但增长必须能覆盖估值。
- 行业空间：行业景气、竞争格局和公司份额变化是否支持成长。
- 技术确认：基本面改善若未被价格确认，应给观察条件而不是追买建议。

没有财报或基本面上下文时，只能给成长质量检查清单。
价格、成交量、K 线、quote、财务字段、因子和信号必须来自 SATS observations/provenance。
