---
name: report-generate
description: SATS 研究报告生成模板，规范摘要、技术面、资金流、基本面、风险、评级与后续观察清单。
category: tool
source: Vibe-Trading adapted for SATS
triggers: 研究报告, 报告, Markdown, 总结, 评级, 操作建议, 风险提示, 输出格式
requires_tools: dsa, indicators, results
---

# report-generate

用于生成结构化 Markdown 研究报告。

## 推荐结构

1. 结论摘要：评级、核心理由、主要风险。
2. 数据来源：TickFlow/Tushare/AkShare/本地 DuckDB。
3. 技术面：趋势、动能、波动、支撑压力。
4. 资金流与情绪：主力资金、北向、量能。
5. 基本面：估值、成长、质量、负债。
6. 风险因素：失效条件和需要继续观察的数据。

若数据缺失，应在报告中标注 unavailable。
