---
name: fundamental-filter
description: A 股基本面筛选框架，按 PE/PB/ROE/营收利润增长/负债率/现金流等字段构造价值、成长和质量筛选条件。
category: analysis
source: Vibe-Trading adapted for SATS
triggers: 基本面, 价值筛选, 成长筛选, 质量筛选, 高ROE低负债, 基本面筛选
requires_tools: tushare_provider, indicators
---

# fundamental-filter

帮助 SATS 聊天模型把自然语言基本面需求转成可解释的筛选逻辑。

## 常用筛选

- 价值：低 PE、低 PB、稳定分红、现金流为正。
- 成长：营收增长、利润增长、ROE 改善。
- 质量：低负债率、毛利率稳定、经营现金流覆盖利润。
- 风险排除：ST、高负债、利润连续下滑、审计异常。

## SATS 约束

- 优先使用 `TushareDataProvider` 已缓存或可取的 PIT 安全财务字段。
- 若某字段未接入，提示需要补齐 provider 字段，不要伪造数值。
