---
name: volume-breakout
description: DSA 放量突破策略，识别价格站上阻力位并由成交量确认的短中线突破。
category: strategy
source: daily_stock_analysis strategies adapted for SATS
triggers: volume_breakout, 放量突破, 突破, 突破阻力, 量价突破, 平台突破, 放量上攻
requires_tools: indicators, tickflow_provider
applies_to: stock_analysis, opportunity_discovery
evidence: indicators, analyze_signals, native_dsa, stock_context
auto_load: full
priority: 72
aliases: 放量突破, 突破策略, volume breakout
---

# volume-breakout

用于判断突破是否有量价确认。

## 判定要点

- 阻力识别：参考近期平台顶部、20 日高点、前高或 SATS 支撑压力字段。
- 价格确认：收盘站上阻力比盘中刺破更可靠。
- 量能确认：成交量显著高于近 5 日均量，量比越高越需同时检查高位兑现风险。
- 追高过滤：突破后若偏离 MA5 过高，应提示等待回踩突破位确认。
- 失效条件：跌回突破位、放量冲高回落、次日无法站稳。

热点或板块共振可提高权重，但不能替代真实量价确认。
价格、成交量、K 线、quote、因子和信号必须来自 SATS observations/provenance。
