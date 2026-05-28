---
name: akshare
description: AkShare 可选补充数据源，用于 A 股、宏观、行业、主题和公开行情数据兜底；在 SATS 中只作为 TickFlow/Tushare 之后的补充。
category: data-source
source: Vibe-Trading adapted for SATS
triggers: AkShare, akshare, 东方财富, 新浪, 行业, 宏观, 公开数据, 兜底数据源
requires_tools: akshare_provider
---

# akshare

AkShare 在 SATS 中定位为可选补充 provider。优先级低于 TickFlow 和 Tushare。

## 适用场景

- 用户询问公开行业、主题、宏观、实时扩展字段，但 TickFlow/Tushare 暂无接口。
- Tushare token 权限不足，需要提示可用 AkShare 兜底。
- 研究性问答需要解释 AkShare 能提供什么，而不是直接承诺已取数。

## 限制

- SATS 若未安装或未实现 `akshare_provider`，只能建议后续接入，不可声称数据已获取。
- AkShare 接口来源分散，字段和频率可能变化；回答时说明数据口径需要校验。
