---
name: valuation-model
description: 估值分析框架，使用 PE/PB/PS/市值、ROE、成长性和行业对比判断估值高低及安全边际。
category: analysis
source: Vibe-Trading adapted for SATS
triggers: 估值, 安全边际, 低估, 高估, 估值模型, 同业估值, 历史估值
requires_tools: tushare_provider
---

# valuation-model

用于生成估值解释，不直接给交易指令。

## SATS 估值框架

- PE：适合盈利稳定公司；亏损或周期股需谨慎。
- PB：适合银行、地产、公用事业和资产重行业。
- PS：适合利润波动但收入稳定的成长公司。
- ROE 与成长性：高 ROE 且增长稳定可承受更高估值。
- 行业对比：估值必须放在同业和历史区间中解释。

输出时给出“估值偏低/合理/偏高/不可判断”和依据。
