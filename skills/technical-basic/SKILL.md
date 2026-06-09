---
name: technical-basic
description: 技术指标基础分析，覆盖 MA/SMA/EMA、MACD、RSI、BOLL、ATR、KDJ、支撑压力和成交量。
category: strategy
source: Vibe-Trading adapted for SATS
triggers: 技术指标, MA, 均线, MACD, RSI, BOLL, 布林带, ATR, KDJ, 支撑, 压力, 成交量
requires_tools: indicators
applies_to: stock_analysis, opportunity_discovery
evidence: indicators, analyze_signals, stock_context
auto_load: full
priority: 90
aliases: 技术面, 技术分析, technical analysis
---

# technical-basic

用于解释 SATS 指标系统输出。

## 分析框架

- 趋势：MA5/10/20/60/120 与价格位置。
- 动能：MACD 金叉/死叉、柱体变化。
- 超买超卖：RSI 6/12/24 与 KDJ。
- 波动：BOLL 位置和 ATR。
- 结构：支撑/压力、量价配合、蜡烛图确认。

回答应说明指标滞后性，不构成投资建议。
价格、成交量、K 线、quote、因子和信号必须来自 SATS observations/provenance，不能由 skill 或 LLM 补造。
