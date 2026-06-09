---
name: ma-golden-cross
description: DSA 均线金叉策略，检测 MA5 上穿 MA10、MA10 上穿 MA20 及 MACD/量能确认。
category: strategy
source: daily_stock_analysis strategies adapted for SATS
triggers: ma_golden_cross, 均线金叉, 金叉, MA5上穿MA10, MA10上穿MA20, MACD金叉
requires_tools: indicators
applies_to: stock_analysis, opportunity_discovery
evidence: indicators, analyze_signals, native_dsa, stock_context
auto_load: full
priority: 72
aliases: 均线金叉, 金叉策略, ma golden cross
---

# ma-golden-cross

用于解释均线金叉是否代表趋势反转或趋势延续。

## 判定要点

- 主信号：MA5 在近期上穿 MA10；更稳健信号是 MA10 上穿 MA20。
- 动能确认：MACD 金叉、MACD 位于零轴附近或零轴上方时可信度更高。
- 量能确认：金叉日或确认日成交量高于近 5 日均量更好。
- 背景过滤：盘整后金叉强于下跌途中的弱反弹金叉。
- 追高过滤：金叉后若价格已明显偏离短均线，应等待回踩确认。

没有 SATS 指标上下文时，只输出方法论，不编造交叉日期或价位。
价格、成交量、K 线、quote、因子和信号必须来自 SATS observations/provenance。
