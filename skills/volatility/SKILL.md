---
name: volatility
description: 波动率分析与风险提示，使用 ATR、历史波动、布林带宽度和量价异常判断波动状态。
category: strategy
source: Vibe-Trading adapted for SATS
triggers: 波动率, ATR, 布林带宽度, 放量, 缩量, 风险, 止损, 波动
requires_tools: indicators
---

# volatility

用于解释波动扩大、收敛和风险控制。

- ATR 上升：波动扩大，仓位和止损距离需要调整。
- 布林带收窄：可能进入蓄势阶段，等待方向确认。
- 放量长阴或长阳：结合支撑压力判断突破或诱多诱空。
- 缩量上涨：趋势可能延续但买盘确认不足。
