---
name: quant-factor-screener
description: A 股多因子选股与因子暴露分析框架，覆盖价值、动量、质量、低波动、规模、成长、行业中性和因子拥挤度。
category: strategy
source: finskills China-market adapted for SATS; original Apache-2.0
triggers: 多因子, 因子投资, 因子选股, 量化选股, Smart Beta, 价值因子, 动量因子, 质量因子, 低波动, 因子打分, 因子择时
requires_tools: tushare_provider, indicators, discover
applies_to: factor_analysis, opportunity_discovery, stock_analysis
evidence: factor, factor_summary, factor.pick, factor.analyze, opportunity_discovery
auto_load: full
priority: 80
aliases: 因子选股, 多因子, factor screener
---

# quant-factor-screener

用于把多因子 A 股筛选请求转成 SATS 可解释的研究框架。不要直接运行外部脚本；需要真实行情、估值、财务或指数数据时，先通过 SATS 的 `AStockDataProvider`、`indicators`、`discover` 或已封装内部分析能力取得结构化上下文。

## 工作流程

1. 确认股票池：沪深300、中证500、中证1000、全 A、已筛选结果或用户给出的代码/名称列表。
2. 确认因子：默认价值、动量、质量、低波动、规模、成长六类等权；如用户指定权重，按用户权重解释。
3. 计算或解释因子得分：价值看 PE/PB/股息率/自由现金流收益率；动量看 12-1 月收益、近期相对强弱和盈利预期变化；质量看 ROE、毛利率、现金流、低杠杆；低波动看 ATR/历史波动/Beta；规模看流通市值；成长看营收和利润增速。
4. 控制行业偏差：默认行业中性；如果没有行业全量数据，明确说明只能做候选内相对比较。
5. 评估因子环境：经济复苏偏规模/动量，扩张偏动量/成长，扩张末期偏质量/价值，下行偏低波动/质量，触底偏价值/规模/动量。
6. 检查因子拥挤：估值价差收窄、因子收益相关性升高、相关 ETF/基金流入过热、媒体关注过高时降低置信度。
7. 输出候选排序：给出综合分、各因子强弱、行业分布、因子暴露、主要风险和需要补充的数据字段。

## 输出结构

- 宏观/市场环境与因子择时观点。
- 因子权重和可用数据说明。
- 候选股票表：代码、名称、行业、综合分、价值/动量/质量/低波动/规模/成长分。
- 行业分布和因子暴露汇总。
- 个股简述：为什么入选、主要催化、关键反证。
- 风险提示：因子失效、拥挤交易、行业集中、换手成本、A 股涨跌停和流动性约束。

## SATS 边界

- 因子结果只能基于已取得的真实数据或明确标注为方法论示例。
- 不把 RAG 方法论当作实时因子数据。
- 不给确定性买卖建议；结论应表述为研究观察或候选优先级。
- 价格、成交量、K 线、quote、因子输入和信号必须来自 SATS observations/provenance。
