---
name: sector-rotation
description: A 股行业轮动分析，结合宏观周期、行业动量、景气度、估值、资金流和主题催化解释板块强弱。
category: analysis
source: Vibe-Trading + finskills China-market adapted for SATS
triggers: 行业轮动, 板块, 申万行业, 主题, 资金流, 景气度, 产业链, 热点, 经济周期, 超配, 低配, 宏观驱动
requires_tools: tushare_provider
applies_to: market_analysis, opportunity_discovery, stock_analysis
evidence: market_context, hot_sectors, hot_sector_context
auto_load: full
priority: 86
aliases: 板块轮动, 热点板块, 行业强弱
---

# sector-rotation

用于解释 A 股行业/主题强弱。

- 动量：行业指数近期涨跌幅和相对强弱。
- 资金：板块资金流、北向或主力净流入。
- 基本面：景气度、盈利预期、估值位置。
- 催化：政策、产业事件、财报周期。

SATS 若未接入行业全量数据，应提示需要 Tushare/AkShare 补齐。
价格、成交量、K 线、quote、板块字段、因子和信号必须来自 SATS observations/provenance，不能补造题材归属。

## 宏观行业轮动增强

- 周期定位：复苏早期偏顺周期和可选消费，扩张中期偏成长和制造，通胀上行偏资源和能源，下行期偏防御、高股息和质量。
- 宏观变量：利率、信用、PMI、CPI/PPI、社融、地产链、出口和汇率变化分别影响不同行业利润弹性。
- 轮动证据：行业相对强弱、成交占比、资金流、盈利预期、估值分位、政策催化和拥挤度。
- 输出应区分“超配观察”“中性持有”“低配回避”，并列出反证条件。
