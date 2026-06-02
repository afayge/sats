---
name: corporate-events
description: 公司事件驱动分析，覆盖公告、业绩预告、定增、回购、股东增减持、并购、解禁和 ST/退市事件。
category: flow
source: Vibe-Trading + daily_stock_analysis strategies adapted for SATS
triggers: 公司事件, 公告, 业绩预告, 定增, 回购, 减持, 增持, 并购, 解禁, 事件驱动, event_driven, 催化, 催化事件, 订单, 产品发布, 诉讼
requires_tools: tushare_provider, indicators
---

# corporate-events

用于解释公司事件对股票研究的潜在影响。

## 常见事件

- 利好：回购、增持、业绩超预期、订单或政策催化。
- 利空：减持、解禁压力、监管处罚、业绩预亏、退市风险。
- 中性待验证：定增、并购、重组、股权激励。
- DSA 适配：每个事件都要说明可信度、影响路径、兑现周期、价格已反映程度和失效条件。

没有公告数据时必须说明需要用户提供公告或后续接入数据源。
