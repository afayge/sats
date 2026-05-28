---
name: minute-analysis
description: 分钟级行情分析，结合 TickFlow 分钟 K、日内趋势、量能变化和关键价位解释短线结构。
category: strategy
source: Vibe-Trading adapted for SATS
triggers: 分钟K, 分钟 K, 1m, 5m, 15m, 30m, 日内, 分时, 短线, 实时行情
requires_tools: tickflow_provider, minute_k
---

# minute-analysis

用于 SATS `/minute-k` 和 TickFlow 实时场景。

- 先确认周期：1m/5m/15m/30m/60m。
- 结合日线趋势，避免只看分钟线。
- 重点解释：日内高低点、量能峰值、突破回踩、VWAP/均线附近反应。
- v1 不自动下单，只给研究性解释和可观察价位。
