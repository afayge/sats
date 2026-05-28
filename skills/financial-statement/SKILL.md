---
name: financial-statement
description: 财务报表解读 skill，覆盖利润表、资产负债表、现金流量表、ROE、负债率、营收利润趋势和财务质量。
category: analysis
source: Vibe-Trading adapted for SATS
triggers: 财报, 财务报表, 利润表, 资产负债表, 现金流量表, ROE, 营收, 利润, 负债率, 财务质量
requires_tools: tushare_provider
---

# financial-statement

用于解释上市公司财务趋势和质量。

## 分析顺序

1. 收入与利润：看同比、环比和扣非利润。
2. 盈利能力：ROE、毛利率、净利率是否改善。
3. 资产负债：资产负债率、短债压力、商誉或应收异常。
4. 现金流：经营现金流是否支持利润。
5. 风险：一次性收益、财务费用、减值、审计意见。

若 SATS 没有取到对应字段，应明确列出缺失项。
