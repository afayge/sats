---
name: bull-trend
description: DSA 默认多头趋势策略，识别多头排列、趋势延续、回踩低吸和不追高风控。
category: strategy
source: daily_stock_analysis strategies adapted for SATS
triggers: bull_trend, 多头趋势, 趋势分析, 趋势向上, MA5, MA10, MA20, 回踩低吸, 不追高
requires_tools: indicators, tickflow_provider
---

# bull-trend

从 DSA `bull_trend` 适配而来，用作常规个股技术分析的默认趋势视角。

## 分析框架

- 趋势确认：优先看 MA5/MA10/MA20 是否多头排列，MA20 是否保持上行。
- 位置节奏：优先回踩不破后的低吸，不在明显偏离 MA5/MA10 的位置追涨。
- 量价验证：放量突破或回踩缩量更可信，缩量上涨和放量滞涨要降低置信度。
- 风险边界：跌破 MA20、跌破结构低点或大盘背景转弱时转为观望。

## SATS 边界

只使用 SATS 已注入的个股行情、指标和大盘上下文。没有真实结构化数据时，只能解释方法或建议运行 `/dsa --stocks ...`、`/chat 分析...`。

