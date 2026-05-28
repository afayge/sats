---
name: workflow-templates
description: SATS 研究工作流模板，改写自 Vibe-Trading swarm presets；用于单助手模拟多角色研究流程，不代表已实现真正多 agent swarm。
category: workflow
source: Vibe-Trading presets adapted for SATS
triggers: 工作流, 研究流程, 投委会, 多角色, equity_research_team, investment_committee, quant_strategy_desk, risk_committee, portfolio_review_board, sector_rotation_team, technical_analysis_panel
requires_tools: list_skills, load_skill
---

# workflow-templates

本 skill 将 Vibe-Trading 的高价值 swarm presets 改写为 SATS 单助手可执行的研究模板。SATS v1 不启动真正多 agent，只按角色顺序组织分析。

## equity_research_team

宏观环境 -> 行业景气 -> 个股技术/基本面 -> 研究报告编辑。

## investment_committee

多头理由 -> 空头风险 -> 风控复核 -> 最终观察/持有/回避建议。

## quant_strategy_desk

筛选结果 -> 因子解释 -> 指标/回测可行性 -> 风险审计。

## risk_committee

回撤风险 -> 尾部风险 -> 市场状态 -> 风控结论。

## portfolio_review_board

持仓表现 -> 风险暴露 -> 交易质量 -> 再平衡建议。

## sector_rotation_team

经济周期 -> 行业景气 -> 资金流 -> 轮动结论。

## factor_research_committee

因子假设 -> 因子有效性 -> 因子组合 -> 回测审查。

## fundamental_research_team

财务质量 -> 估值 -> 公司事件 -> 基本面结论。

## technical_analysis_panel

趋势 -> 动能 -> 波动/量能 -> 支撑压力 -> 技术结论。

输出时应明确数据来源和缺失数据，不构成投资建议。
