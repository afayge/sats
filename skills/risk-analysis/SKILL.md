---
name: risk-analysis
description: 风险分析与压力测试框架，覆盖回撤、波动、尾部风险、仓位、止损、事件风险和研究报告风险章节。
category: risk-analysis
source: Vibe-Trading adapted for SATS
triggers: 风险, 风控, 回撤, 止损, 仓位, 压力测试, 研究报告, 尾部风险, 风险因素
requires_tools: indicators, monitor
applies_to: stock_analysis, financial_analysis, opportunity_discovery, market_analysis
evidence: indicators, analyze_signals, factor_summary, market_context, monitor
auto_load: full
priority: 80
aliases: 风险提示, 风险控制, risk
---

# risk-analysis

用于 SATS 股票研究、DSA 报告、监控事件和聊天问答的风险部分。

## 输出要点

- 技术风险：跌破支撑、放量下跌、波动扩大。
- 基本面风险：利润下滑、负债高、现金流弱、估值过高。
- 市场风险：板块退潮、流动性下降、政策或事件冲击。
- 操作风险：仓位过重、追高、止损不清晰。

所有结论必须保持研究性质，不构成投资建议。
价格、成交量、K 线、quote、因子和信号必须来自 SATS observations/provenance，不能由 skill 或 LLM 补造。
