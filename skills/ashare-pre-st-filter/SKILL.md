---
name: ashare-pre-st-filter
description: A 股 ST/*ST 与退市风险预警框架，结合营收、利润、净资产、审计意见、监管处罚和交易临界状态做风险分层。
category: risk-analysis
source: Vibe-Trading adapted for SATS
triggers: ST风险, ST, 退市, 风险警示, 净资产, 审计意见, 监管处罚, 财务风险
requires_tools: tushare_provider
---

# ashare-pre-st-filter

用于解释或辅助构建 A 股 ST/*ST 风险筛查。SATS 当前可通过 Tushare 财务、公告和监管相关字段逐步补齐，不要在缺数据时编造结论。

## 分析维度

- 营收与扣非净利润是否触及风险警示线。
- 净资产是否为负，经营现金流是否异常。
- 审计意见、内部控制、重大违法或监管处罚记录。
- 交易层面是否长期低价、低市值或流动性恶化。

## 输出建议

- 给出风险等级：低 / 中 / 高 / 极高。
- 列出触发依据和缺失数据。
- 明确“不预测财务造假，仅基于可得公开字段做风险提示”。
