---
name: serenity-stock-screen
description: SATS 原生 Serenity AI/科技供应链卡位筛选。用于 A 股 AI、半导体、光通信、先进封装、算力电力散热和具身智能主题的瓶颈层识别、候选排名、证据验证与反方压力测试。
category: strategy
source: SATS native; methodology adapted from muxuuu/serenity-skill and wbh604/UZI-Skill (MIT)
triggers: Serenity, 卡位, 卡脖子, 供应链卡点, 供应链瓶颈, 稀缺层, AI半导体选股, 光通信选股, 先进封装选股, 算力选股, 液冷选股, 人形机器人选股
requires_tools: research.serenity_screen, astock_provider
applies_to: opportunity_discovery, financial_analysis
evidence: serenity_screen, stock_context, tushare_data, opportunity_discovery
auto_load: full
priority: 96
aliases: Serenity筛选, 卡位筛选, 瓶颈筛选, bottleneck screen
license: MIT
---

# serenity-stock-screen

使用 `research.serenity_screen` 做 A 股 AI/科技供应链卡位筛选。

## 工作流

1. 先排供应链层级，再排公司。
2. 主题候选优先使用 SATS 的同花顺/申万成员和本地股票校验。
3. 对有限候选补充公告、财报、订单/认证、治理、估值和热点上下文。
4. 使用确定性 8 因子评分和 8 类风险罚分；LLM 只补充解释，不改分数。
5. 输出研究优先级、证据等级、反方理由、失效条件和下一步验证。

## 约束

- v1 只覆盖 A 股普通股票和 AI/科技卡位主题。
- 价格、财务、订单、客户、认证和产能必须来自 SATS observations/provenance。
- 缺失字段按 0 分处理并明确列出，不得补造数字。
- 普通短线推荐不应调用本 skill。
- 输出“研究优先级”，不得给保证收益或自动交易指令。

需要解释权重时读取 `references/scoring.md`；需要判断证据强弱时读取
`references/evidence-ladder.md`；需要解释供应链位置时读取
`references/chain-tiers.md`。
