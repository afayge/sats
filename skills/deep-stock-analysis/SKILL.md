---
name: deep-stock-analysis
description: SATS 原生个股深研框架，覆盖 A 股普通股票的基础信息、财务质量、K线技术、市场环境、行业、估值、资金流、风险和投资人面板骨架。
category: analysis
source: SATS native workflow inspired by UZI-Skill methodology (MIT)
triggers: 深度分析, 全面分析, 个股深研, DCF, 估值, 投委会, 首次覆盖, 基本面拆解, 风险拆解
requires_tools: research.deep_stock_analysis, astock_provider
applies_to: financial_analysis, stock_analysis
evidence: deep_stock_analysis, stock_context, indicators
auto_load: full
priority: 95
aliases: 深研, 深度个股分析, 投委会备忘录, deep analysis
---

# deep-stock-analysis

用于 SATS 原生深度研究，不直接给交易指令。

## 使用边界

- v1 只覆盖 A 股普通股票。
- 数据必须来自 `AStockDataProvider`、DuckDB cache 或 SATS 已记录 observations。
- 港股、美股、ETF、LOF、可转债和 HTML 视觉报告暂不覆盖。
- 缺失字段必须列为数据缺口，不得由 LLM 补造价格、财务、资金流或行情。

## 分析顺序

1. 采集真实数据：基础信息、日线、估值、财务、资金流和市场环境。
2. 生成 12 个核心维度的质量标记和 0-10 分评分。
3. 生成 12 位旗舰投资人面板骨架，区分 bullish、neutral、bearish。
4. 综合成 Markdown/JSON artifact：总分、结论、多空理由、风险、估值观察区、数据缺口和来源。

## 输出要求

- 结论必须引用 SATS 深研结果里的字段。
- 明确区分“低分风险”和“数据缺失”。
- 普通走势分析不应默认调用深研工具；只有用户表达“深度、估值、投委会、首次覆盖、全面基本面”等意图时才调用。
