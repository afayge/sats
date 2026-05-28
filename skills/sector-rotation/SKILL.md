---
name: sector-rotation
description: A 股行业轮动分析，结合行业动量、景气度、估值、资金流和主题催化解释板块强弱。
category: analysis
source: Vibe-Trading adapted for SATS
triggers: 行业轮动, 板块, 申万行业, 主题, 资金流, 景气度, 产业链, 热点
requires_tools: tushare_provider
---

# sector-rotation

用于解释 A 股行业/主题强弱。

- 动量：行业指数近期涨跌幅和相对强弱。
- 资金：板块资金流、北向或主力净流入。
- 基本面：景气度、盈利预期、估值位置。
- 催化：政策、产业事件、财报周期。

SATS 若未接入行业全量数据，应提示需要 Tushare/AkShare 补齐。
