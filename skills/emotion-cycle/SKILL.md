---
name: emotion-cycle
description: DSA 情绪周期策略，结合换手率、成交量、涨跌停和新闻舆情识别冷淡、升温、过热与退潮阶段。
category: analysis
source: daily_stock_analysis strategies adapted for SATS
triggers: emotion_cycle, 情绪周期, 情绪底, 情绪顶, 恐慌底, 狂热顶, 换手率, 赚钱效应, 退潮
requires_tools: tushare_provider, indicators
applies_to: market_analysis, opportunity_discovery, stock_analysis
evidence: market_context, market_breadth, limit_sentiment, hot_sectors, native_dsa
auto_load: full
priority: 78
aliases: 情绪周期, emotion cycle, 赚钱效应
---

# emotion-cycle

用于把 A 股短线交易情绪分层，而不是单纯判断涨跌。

## 判定要点

- 冷淡底部：换手和成交量低迷，波动收缩，新闻与讨论热度较低。
- 升温阶段：成交量、换手率和涨停家数改善，热点开始扩散。
- 过热阶段：高换手、高量比、连续加速、利好刷屏或散户追捧。
- 退潮阶段：炸板增加、热点分化、放量滞涨或跌破关键支撑。
- 逆情绪：大众恐慌时寻找验证后的修复，大众狂热时优先保护收益。

没有真实情绪或市场宽度数据时，应降级为情绪周期解释。
价格、成交量、K 线、quote、情绪字段和信号必须来自 SATS observations/provenance。
