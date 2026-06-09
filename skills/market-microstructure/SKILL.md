---
name: market-microstructure
description: 市场微观结构分析，解释盘口、价差、成交冲击、日内分时、集合竞价和流动性风险。
category: analysis
source: Vibe-Trading adapted for SATS
triggers: 微观结构, 盘口, 五档, bid, ask, spread, 分时, 集合竞价, 流动性, 冲击成本, VPIN
requires_tools: tickflow_provider
applies_to: market_analysis, stock_analysis
evidence: market_context, realtime_quote, stock_minute, minute_k, quote
auto_load: full
priority: 84
aliases: market microstructure, 微观交易结构, 盘口流动性
---

# market-microstructure

用于 TickFlow 实时行情和分钟 K 场景。

## SATS v1 可解释内容

- 最新价、成交量、盘口价差、日内分时强弱。
- 分钟 K 的放量突破、回踩、急拉急跌。
- 流动性不足导致的滑点和冲击成本。

如果 SATS 暂未提供逐笔或完整 order book，不要声称已经计算 VPIN/Kyle lambda，只能解释方法。
价格、成交量、K 线、quote、盘口和分钟数据必须来自 SATS observations/provenance。
