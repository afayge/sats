---
name: shrink-pullback
description: DSA 缩量回踩策略，寻找上升趋势中回踩 MA5/MA10 且量能收缩的低吸节奏。
category: strategy
source: daily_stock_analysis strategies adapted for SATS
triggers: shrink_pullback, 缩量回踩, 回踩, 回踩MA5, 回踩MA10, 缩量调整, 低吸
requires_tools: indicators, tickflow_provider
applies_to: stock_analysis, opportunity_discovery
evidence: indicators, analyze_signals, native_dsa, stock_context
auto_load: full
priority: 74
aliases: 缩量回踩, 回踩低吸, pullback
---

# shrink-pullback

用于识别趋势延续中的回踩买点。

## 判定要点

- 前提：个股处于上升趋势，MA5 > MA10 > MA20 更优。
- 回踩：价格回到 MA5 附近或 MA10 附近，且不有效跌破关键均线。
- 缩量：回调阶段成交量低于近期均量，说明抛压暂未放大。
- 确认：反弹日重新放量或收复短均线，优于单纯盘中触碰支撑。
- 风控：止损放在 MA20、结构低点或回踩失败位置下方。

回答时要说明“回踩成立条件”和“失败条件”，不能把回踩假定成必然反弹。
价格、成交量、K 线、quote、因子和信号必须来自 SATS observations/provenance。
