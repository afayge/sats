---
name: one-yang-three-yin
description: DSA 一阳夹三阴 K 线整理形态，用于识别趋势延续中的缩量整理后再突破。
category: strategy
source: daily_stock_analysis strategies adapted for SATS
triggers: one_yang_three_yin, 一阳夹三阴, 一阳穿三阴, K线整理, 缩量整理, 趋势延续形态
requires_tools: indicators
---

# one-yang-three-yin

用于解释“一根阳线后三根小阴或小 K，再次阳线突破”的形态。

## 判定要点

- 第 1 日：实体较强的阳线，代表资金主动上攻。
- 第 2-4 日：连续小阴或小 K，最好缩量且不跌破第 1 日关键区间。
- 第 5 日：再次阳线突破第 1 日收盘附近，趋势延续概率才提高。
- 趋势过滤：MA5/MA10/MA20 多头或至少不空头时更可信。
- 失效条件：整理期间放量下跌、跌破第 1 日起点或突破失败。

该形态必须结合趋势、量能和位置解释，不单独作为买入依据。

