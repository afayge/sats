---
name: expectation-repricing
description: DSA 预期重估策略，分析业绩、政策、估值和产业预期变化带来的修复或兑现风险。
category: analysis
source: daily_stock_analysis strategies adapted for SATS
triggers: expectation_repricing, 预期重估, 预期差, 预期修复, 预期兑现, 估值重估, 业绩预期
requires_tools: tushare_provider, indicators
---

# expectation-repricing

用于判断价格变化背后是预期修复、预期兑现，还是预期落空。

## 分析框架

- 预期来源：财报、业绩预告、订单、政策、产品进展、行业景气或机构观点。
- 预期差方向：原先悲观后出现正向验证，或原先乐观后被证伪。
- 估值承接：估值提升必须匹配盈利质量、增长持续性和行业空间。
- 价格确认：放量突破说明资金确认，利好不涨或冲高回落提示兑现压力。
- 观察节点：下一份财报、订单兑现、政策落地、估值回落和技术确认。

回答要区分硬信息和软信息，不能把传闻当作已验证事实。

