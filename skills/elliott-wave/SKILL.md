---
name: elliott-wave
description: 艾略特波浪启发式分析，基于峰谷、ZigZag 和斐波那契比例解释潜在 5 浪/3 浪结构。
category: strategy
source: Vibe-Trading + daily_stock_analysis strategies adapted for SATS
triggers: 艾略特, 波浪, 波浪理论, wave_theory, Elliott, ZigZag, 五浪, 三浪, 推动浪, 调整浪, 斐波那契, 峰谷
requires_tools: indicators
applies_to: stock_analysis, opportunity_discovery
evidence: indicators, analyze_signals, native_dsa, stock_context
auto_load: summary
priority: 48
aliases: 艾略特波浪, 波浪理论, wave theory
---

# elliott-wave

SATS 第一版艾略特波浪是启发式辅助信号，不是精确自动数浪。

## 回答原则

- 用“潜在浪型”“可能处于”这类谨慎表述。
- 输出关键峰谷、候选浪段、置信度和失效位。
- 与趋势、MACD、成交量和支撑压力交叉验证。
- 不把波浪分析作为单独交易依据。

## DSA 适配要点

- 推动浪关注 1/3/5 浪结构，第 3 浪通常需要更强量能与动能确认。
- 调整浪关注 A/B/C 结构，B 浪反弹需要警惕诱多。
- 常用位置包括 0.382、0.618 回撤和 1.618 延伸，但必须来自 SATS 已注入的真实价格序列。
- 若第 4 浪侵入第 1 浪关键区域，应说明原计数失效，需要重新归数。
- 价格、成交量、K 线、quote、因子和信号必须来自 SATS observations/provenance。
