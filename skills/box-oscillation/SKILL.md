---
name: box-oscillation
description: DSA 箱体震荡策略，识别横盘区间，在支撑与阻力之间做波段研究。
category: strategy
source: daily_stock_analysis strategies adapted for SATS
triggers: box_oscillation, 箱体, 箱体震荡, 横盘震荡, 区间交易, 箱底, 箱顶
requires_tools: indicators
---

# box-oscillation

用于横盘震荡行情中的区间结构分析。

## 判定要点

- 箱体顶部：近期多次触碰但未有效突破的高点区域。
- 箱体底部：近期多次下探但未有效跌破的低点区域。
- 当前位置：箱底附近关注企稳，箱中少动，箱顶附近警惕追高。
- 有效突破：连续站上箱体边界且量能放大时，箱体策略切换为趋势策略。
- 失败信号：跌破箱底或突破后快速回落箱内。

输出时优先给出区间、当前所处位置、触发条件和失效条件；无结构化数据时不报具体箱体价位。

