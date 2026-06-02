---
name: bottom-volume
description: DSA 底部放量策略，识别长期下跌后放量企稳的高风险反转信号。
category: strategy
source: daily_stock_analysis strategies adapted for SATS
triggers: bottom_volume, 底部放量, 地量见底, 放量企稳, 超跌反弹, 底部反转
requires_tools: indicators, tickflow_provider
---

# bottom-volume

用于识别下跌后潜在反转，但默认风险高于趋势跟随。

## 判定要点

- 背景：此前已有较长下跌或明显超跌，而不是高位第一次放量。
- 量能：当前成交量显著高于近期均量，最好出现在前期持续缩量之后。
- 价格：收阳、长下影或守住近期低点更有意义。
- 催化：若有公告、业绩、政策或板块修复，需要区分硬催化和情绪反抽。
- 风控：仓位应轻，止损放在近期低点下方；趋势未扭转前不做确定性表述。

回答必须明确“反转观察”与“趋势确认”不是一回事。

