---
name: sentiment-analysis
description: 市场情绪分析，解释融资融券、北向资金、涨跌停、成交活跃度、新闻舆情和资金风险偏好。
category: analysis
source: Vibe-Trading adapted for SATS
triggers: 情绪, 市场情绪, 北向资金, 融资融券, 涨跌停, 舆情, 新闻, 恐慌, 贪婪
requires_tools: tushare_provider
---

# sentiment-analysis

用于回答市场情绪和风险偏好问题。

- A 股情绪：涨跌停家数、成交额、量能、融资融券。
- 资金情绪：北向资金、主力净流入、板块资金。
- 新闻舆情：若 SATS 未配置新闻源，只能解释框架，不能编造新闻。
- 输出建议：偏热 / 中性 / 偏冷，并说明依据与缺失项。
