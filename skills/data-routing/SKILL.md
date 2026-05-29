---
name: data-routing
description: SATS 数据源选择决策树：TickFlow 优先提供实时行情/分钟K/日线，Tushare 补资金流、财务、估值和宏观，AkShare 只作为可选兜底。
category: data-source
source: Vibe-Trading + finskills China-market adapted for SATS
triggers: 数据源, 数据路由, fallback, provider, provider选择, 数据兜底
requires_tools: tickflow_provider, tushare_provider, akshare_provider
---

# data-routing

本 skill 用于帮助 SATS 聊天模型选择正确的数据来源。它只提供路由指引，不代表已经执行取数。

## SATS v1 路由原则

- 实时行情、最新价格、分钟 K、日内分时、五档/盘口：优先 TickFlow。
- 日线 K、历史行情：优先 TickFlow；若缺字段，再使用 Tushare 缓存或接口补齐。
- 资金流、主力净流入、北向资金、龙虎榜、估值、财务报表、宏观：优先 Tushare。
- AkShare：仅作为可选兜底，用于 Tushare/TickFlow 没覆盖或 token/权限不足的研究场景。
- 所有 A 股用户输入代码都应使用 `sats.symbols` 规范化为 `000001.SZ` 形式。

## China-market 数据能力映射

`finskills/China-market/findata-toolkit-cn` 中提到的 AKShare 脚本不迁入 SATS，也不作为聊天工具直接执行。对应能力在 SATS 中按以下边界处理：

- A 股实时行情、历史行情、分钟 K：通过 `AStockDataProvider` 路由到 TickFlow/Tushare 缓存或补充源。
- 财务指标、利润表、资产负债表、现金流、估值、股本和股票列表：通过 `AStockDataProvider` 背后的 Tushare/TickFlow 能力。
- 北向资金、董监高增减持、龙虎榜、公告、宏观指标：优先使用 SATS 已封装的 Tushare 能力；若接口暂未封装，只能说明需要补接 provider。
- CPI/PPI、PMI、社融、M2、LPR、Shibor 等宏观公开数据：可作为 AkShare 补充方向，但回答不能声称已经自动拉取。

## 回答约束

- 如果 SATS 当前没有对应 provider 接口，明确说明“可以解释和建议命令，但不能自动拉取该数据”。
- 不要虚构实时数据、财报数值、资金流数值。
- 涉及投资判断时必须提示不构成投资建议。
