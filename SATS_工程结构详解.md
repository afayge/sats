# SATS 工程结构详解

> 历史结构快照：命令、工具、Skills 和数据接口请以 `sats catalog --json` 与 `docs/AGENT_CAPABILITIES.md` 为准。

> 生成日期：2026-05-31  
> 版本：v0.1.0  
> 工程路径：`/Users/elliotge/python/SATS`

---

## 1. 工程概述

SATS（Stock Automated Trading System）是一个 A 股自动交易系统的早期版本。当前版本已实现 A 股筛选规则、统一信号分析、LLM Provider 基础层、CLI 交互终端、FastAPI API、DuckDB 本地存储和完整的测试用例。真实交易执行通过远程 MiniQMT（国金证券）桥接。

### 1.1 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python ≥ 3.12 |
| 包管理 | setuptools + pyproject.toml |
| 数据库 | DuckDB ≥ 1.1 |
| LLM 框架 | LangChain + OpenAI-compatible |
| Web 框架 | FastAPI + uvicorn |
| 交互终端 | prompt_toolkit |
| 行情源 | TickFlow ≥ 0.1.21, Tushare ≥ 1.2.89 |
| 可选补充 | akshare ≥ 1.16 |
| PDF | pypdf ≥ 4.0, reportlab ≥ 4.0 |
| 科学计算 | pandas ≥ 2.2, numpy ≥ 1.26 |

### 1.2 命令行入口

- `sats` CLI → `sats.cli:main`（一次性命令）
- `python -m sats` → `sats.__main__`
- 交互 REPL → `sats` 进入后输入文本

---

## 2. 顶层目录结构

```
SATS/
├── pyproject.toml              # 项目配置、依赖声明、入口脚本
├── requirements.txt            # pip 依赖列表
├── AGENTS.md                   # LLM Agent 行为准则
├── CLAUDE.md                   # Claude 特定指令
├── README.md                   # 项目 README
├── architecture.txt            # 初始架构设计文档
├── .gitignore
├── docs/
│   └── SATS_ARCHITECTURE.md    # 架构文档
├── knowledge/                  # 知识库
│   └── chan/
│       └── rules/              # 缠论规则卡片 (JSON)
├── skills/                     # 47 个 Skills 模块 (各含 SKILL.md)
├── tests/                      # 31 个测试文件
├── reports/                    # 生成的 LLM 分析报告
├── data/
│   └── sats.duckdb             # DuckDB 数据库文件
├── tmp/                        # 临时文件
└── sats/                       # 主源代码包
```

---

## 3. 主源代码包 (`sats/`) 详解

### 3.1 核心入口与配置

| 文件 | 行数 | 功能 |
|------|------|------|
| `__init__.py` | 3 | 包声明，`__version__ = "0.1.0"` |
| `__main__.py` | - | `python -m sats` 入口 |
| `cli.py` | ~2300 | argparse 一次性 CLI，所有 `cmd_*` 函数 |
| `repl.py` | ~852 | 交互式 REPL（prompt_toolkit），斜杠命令转换 |
| `config.py` | ~177 | `.env` 加载、`Settings` dataclass、初始化模板 |

**CLI 命令清单**（来自 `repl.py` 的 `CLI_COMMANDS`）：

```
init screen results result-rules quote analyze analyze-dsa dsa
analyze-chan chan-kb discover chat model memory knowledge indicators
skills watchlist monitor monitor-display signal signal-defs schedule
trade broker order orders cancel-order trade-history asset positions
add-position init-env save symbol info stock-basic news dsa-compare
```

### 3.2 聊天系统

| 文件 | 行数 | 功能 |
|------|------|------|
| `chat.py` | ~1431 | 核心聊天会话，系统提示词，工具调用，DSA/opportunity discovery 集成 |
| `chat_preprocessor.py` | ~671 | 用户消息预处理：意图识别、股票代码提取、市场/大盘问题检测 |
| `chat_planner.py` | ~298 | 聊天计划构建：技能匹配、数据需求判定、风险评估 |
| `chat_reference.py` | - | 参考资料上下文构建 |
| `stock_question.py` | - | 股票问题解析（交易日期、盘中时间提取） |

**聊天预处理意图分类** (`ChatPreprocessResult`)：
- `general_qa` — 一般问答
- `stock_analysis` — 需要股票上下文
- `market_analysis` — 需要大盘上下文
- `opportunity_discovery` — 需要机会发现
- `reference` — 需要参考资料

### 3.3 数据获取层 (`sats/data/`)

```
sats/data/
├── __init__.py
├── base.py                    # MarketDataProvider 基类
├── astock_provider.py         # ~756 行，A 股统一数据门面
├── tickflow_provider.py       # TickFlow 后端适配器
├── tushare_provider.py        # Tushare 后端适配器
├── tushare_stock_datasets.py  # Tushare 数据集工具
├── akshare_provider.py        # AkShare 可选补充适配器
└── limit_sentiment.py         # 涨跌停情绪指标
```

**数据源优先级**：`TickFlow → Tushare → AkShare`

| 数据源 | 负责内容 |
|--------|----------|
| TickFlow | 实时行情、日K、分钟K、实时quote、指数 |
| Tushare | 筛选输入、`daily_basic`（换手率/市值/PE/PB）、资金流、财务、同花顺行业/概念热点、涨跌停情绪统计 |
| AkShare | 全市场宽度、东财实时扩展、筹码、个股摘要（可选） |

### 3.4 股票筛选层 (`sats/screening/`)

```
sats/screening/
├── __init__.py
├── base.py                    # ScreeningRule ABC, ScreeningInput, ScreeningResult
├── registry.py                # 规则注册表 + 别名系统
├── service.py                 # 筛选评估服务
├── rule_composer.py           # AI 生成规则的编排器
├── generated_rule_runtime.py  # 生成规则的运行时
└── rules/
    ├── __init__.py
    ├── ma_volume_relative_strength.py  # 趋势确认 + 温和放量
    ├── price_volume_ma.py              # 量价换手均线
    ├── chan_signals.py                 # 缠论买卖点信号
    ├── chan_composite.py               # 缠论复合筛选
    ├── chan_third_buy.py               # 缠论三买
    ├── monthly_base_breakout.py        # 月线底部突破
    ├── signal_composite.py             # 信号交织筛选
    └── generated/                      # AI 自动生成的规则
```

**规则注册表别名**：

| 别名 | 映射规则 |
|------|----------|
| `chan-composite`, `chan-stock-select` | `ChanCompositeRule` |
| `chan-signals`, `chan-ai-select` | `ChanSignalsRule` |
| `chan-third-buy` | `ChanThirdBuyRule` |
| `ma-volume-relative-strength` | `MaVolumeRelativeStrengthRule` |
| `monthly-base-breakout` | `MonthlyBaseBreakoutRule` |
| `price-volume-ma` | `PriceVolumeMaRule` |
| `signal-composite`, `abu-signals` | `SignalCompositeRule` |

### 3.5 统一信号分析层 (`sats/signals/`)

```
sats/signals/
├── __init__.py
├── base.py            # SignalDefinition, SignalInput, SignalEvent, SignalAnalysisResult
├── registry.py        # 信号定义注册表（按 category 组织）
└── engine.py          # ~1050 行，信号分析引擎，复合信号规范
```

信号引擎整合了缠论、图形形态、均线系统、艾略特波浪等信号源，通过 `CompositeSpec` 定义复合信号的确认关系。主要信号类别：

- **short_up**: 中短期上涨信号（用于机会发现）
- **chan**: 缠论买卖点
- **graph**: 图形形态（三角形、楔形、旗形等）
- **wave**: 艾略特波浪
- **harmonic**: 和谐形态（蝙蝠、螃蟹、伽特利等）
- **trendline**: 趋势线
- **ma**: 均线系统（金叉、谷底、格兰维尔法则）

### 3.6 技术指标层 (`sats/indicators/`)

```
sats/indicators/
├── __init__.py
└── calculator.py     # ~521 行，IndicatorCalculator
```

`IndicatorResult` 包含：
- **technical**: MA(5,10,20,60,120), RSI(6,12,24), MACD, BOLL, ATR, KDJ
- **patterns**: 蜡烛图形态（十字星、锤头、吞没、晨星/暮星等）
- **volume**: 量能分析
- **support_resistance**: 支撑/阻力
- **elliott_wave**: 艾略特波浪分析
- **moneyflow**: 资金流快照
- **fundamentals**: 基本面快照

### 3.7 缠论引擎 (`sats/chan/`)

```
sats/chan/
├── __init__.py
└── engine.py         # ~809 行，缠论信号评估
```

支持的缠论信号：

| 信号 ID | 中文标签 | 方向 |
|---------|----------|------|
| `chan_first_buy` | 一买 | 多 |
| `chan_second_buy` | 二买 | 多 |
| `chan_third_buy` | 三买 | 多 |
| `chan_first_sell` | 一卖 | 空 |
| `chan_second_sell` | 二卖 | 空 |
| `chan_third_sell` | 三卖 | 空 |
| `chan_center_oscillation_low` | 中枢震荡低 | 多 |
| `chan_center_oscillation_high` | 中枢震荡高 | 空 |
| `chan_bottom_fractal_confirm` | 底分型确认 | 多 |
| `chan_top_fractal_confirm` | 顶分型确认 | 空 |
| `chan_hold_by_level` | 持股待涨 | 多 |
| `chan_cash_by_level` | 持币观望 | 空 |

### 3.8 分析层 (`sats/analysis/`)

```
sats/analysis/
├── __init__.py
├── dsa_native.py                  # ~1247 行，原生 DSA 分析（指标 + LLM）
├── dsa_decision.py                # DSA 决策构建（Buy/Overweight/Hold/Underweight/Sell）
├── daily_stock_analysis.py        # 旧版 DSA 桥接
├── opportunity_discovery.py       # ~1166 行，机会发现引擎
├── chan_llm_review.py             # 缠论 LLM 复核
├── chan_chat_context.py           # 缠论聊天上下文
├── stock_llm_context.py           # 个股 LLM 上下文构建
├── stock_research_context.py      # 个股研究上下文
├── market_llm_context.py          # 大盘 LLM 上下文
└── quote_llm_context.py           # 行情 quote LLM 上下文
```

**机会发现 (`opportunity_discovery.py`) 工作流**：
1. 全市场信号扫描（使用 `short_up` 信号组）
2. 热点板块加权
3. LLM 排序 + 多样性惩罚
4. 输出 Top-N 候选

### 3.9 LLM 层 (`sats/llm/`)

```
sats/llm/
├── __init__.py
├── chat.py          # ~136 行，ChatLLM（支持 tool calls）
├── provider.py      # LLM 构建工厂
└── model_config.py  # ~189 行，模型配置管理
```

**内置模型配置组**：

| 配置组 (Profile) | Provider | 默认模型 |
|-------------------|----------|----------|
| `DEEPSEEK` | deepseek | `deepseek-chat` |
| `XIAOMIMIMO` | mimo | `MiMo-72B-A27B` |

**支持的 Provider**：`openai`, `openrouter`, `deepseek`, `gemini`, `groq`, `dashscope`, `qwen`, `zhipu`, `moonshot`, `minimax`, `mimo`, `zai`, `ollama`

**模型选择体系**：
- `DEFAULT_MODEL` — 主模型配置组
- `DEFAULT_LIGHT_MODEL` — 轻量任务模型（自然语言预处理、记忆抽取、滚动摘要）
- `ChatLLM(profile="default")` — 默认主模型
- `ChatLLM(profile="light")` — 轻量模型

### 3.10 存储层 (`sats/storage/`)

```
sats/storage/
├── __init__.py
├── schema.sql       # ~458 行，完整数据库 DDL
└── duckdb.py        # ~1424 行，DuckDBStorage CRUD
```

**数据库表清单**（共 20+ 张）：

| 表名 | 用途 |
|------|------|
| `stock_daily` | 个股日 K 线 |
| `stock_daily_basic` | 个股日线基础指标（换手率、市值、PE/PB/PS） |
| `stock_basic` | 个股基本信息（名称、行业、市场） |
| `industry_daily` | 行业日线 |
| `sector_basic` / `sector_daily` / `sector_members` | 板块/概念信息 |
| `stock_moneyflow` | 个股资金流 |
| `stock_fundamentals` | 个股财务数据 |
| `screening_results` | 筛选结果 |
| `chat_sessions` / `chat_messages` | 聊天会话/消息 |
| `chat_memories` | 长期记忆 |
| `knowledge_bases` / `knowledge_files` / `knowledge_chunks` | RAG 知识库 |
| `monitor_positions` | 持仓列表 |
| `monitor_watchlist` | 关注列表 |
| `monitor_buy_candidates` | 待买入列表 |
| `monitor_events` | 监控事件（缠论买卖点等） |
| `monitor_trade_events` | 交易事件 |
| `monitor_runtime` | 监控运行时状态 |
| `broker_accounts` / `broker_positions` | 券商账户/持仓 |
| `broker_orders` / `broker_trades` / `broker_order_events` | 订单/成交/事件 |
| `scheduled_tasks` / `scheduled_task_runs` | 定时任务 |

### 3.11 实时监控层 (`sats/monitoring/`)

```
sats/monitoring/
├── __init__.py
├── service.py       # ~384 行，MonitorService
└── display.py       # ~412 行，curses 终端显示 (MonitorDisplay)
```

**监控服务核心特性**：
- 默认规则：`chan_signals`
- 默认监控列表：`positions` + `watchlist`
- 支持 30m 分钟 K 线
- 可选的 LLM 复核
- 自动交易集成（`auto_trade` 配置）
- 最大单笔委托金额、最大持仓比例、卖出比例配置

**终端显示面板布局**（curses）：
- 顶部一行：后台服务运行状态
- 左侧：持仓列表（代码、名称、买入价、实时价、盈亏%，红盈绿亏，滚动）
- 右侧：成交情况（买入/卖出记录）
- 底部 1/3：监控事件推送（滚动）

### 3.12 交易层 (`sats/trading/`)

```
sats/trading/
├── __init__.py
├── broker.py            # ~35 行，BrokerClient Protocol
├── models.py            # 交易数据模型 (OrderRequest, BrokerAsset, BrokerPosition, etc.)
├── qmt_bridge.py        # ~176 行，QMT/MiniQMT 桥接 (XtQuant)
├── miniqmt_client.py    # MiniQMT 客户端
├── sync.py              # 持仓同步
└── monitor_provider.py  # 监控交易提供者 (AutoTradeConfig, QmtTradingProvider)
```

**BrokerClient 协议方法**：
- `status()` → 状态
- `asset()` → 资产
- `positions()` → 持仓
- `orders(open_only)` → 订单
- `trades(limit)` → 成交
- `place_order(request)` → 下单
- `cancel_order(order_id)` → 撤单

### 3.13 定时任务层 (`sats/scheduler/`)

```
sats/scheduler/
├── __init__.py
└── service.py          # ~377 行，ScheduledTaskRunner + SchedulerService
```

支持：
- 按工作日 + 时间调度
- 循环执行间隔控制
- 禁止递归调度（不能 schedule 内调用 schedule）
- 禁止长时间运行命令（monitor start/run）
- 运行记录持久化到 `scheduled_task_runs`

### 3.14 RAG 知识库 (`sats/rag/`)

```
sats/rag/
├── __init__.py
├── knowledge.py        # 通用知识库存储和检索
└── chan_knowledge.py   # ~288 行，缠论知识 RAG
```

缠论知识库从 `knowledge/chan/rules/*.json` 加载规则卡片，每个卡片包含：
- `rule_id`, `label`, `side`（方向）, `level`（级别）
- `definition`（定义）, `hard_conditions`（硬条件）
- `risk_notes`（风险提示）, `source_pages`（原著页码）
- `keywords`（关键词）

### 3.15 Web API (`sats/api/`)

```
sats/api/
├── __init__.py
├── app.py             # ~150 行，FastAPI 应用
└── routes/            # 预留路由目录（当前为空）
```

**现有端点**：
- `GET /` — HTML 首页（列出可用规则）
- `POST /screen` — 执行筛选
- `GET /results` — 查询历史筛选结果
- `GET /results/{ts_code}` — 按股票代码查询

### 3.16 辅助模块

| 文件 | 行数 | 功能 |
|------|------|------|
| `symbols.py` | ~45 | A 股代码规范化（6 位数字 → `000001.SZ` 格式） |
| `stock_basic_lookup.py` | - | 股票名称/代码查找 |
| `skills.py` | - | Skills 加载和管理 |
| `memory.py` | - | 聊天记忆存储/检索 |
| `progress.py` | ~410 | 进度条（TTY / JSON / silent 三模式） |
| `output_saver.py` | - | 输出保存 |
| `watchlist_editor.py` | - | 关注列表编辑 |

---

## 4. Skills 模块 (`skills/`) — 共 47 个

### 4.1 DSA 策略类（日线分析信号）

| Skill | 策略 |
|-------|------|
| `bull-trend` | 默认多头趋势策略 |
| `shrink-pullback` | 缩量回踩 MA5/MA10 低吸 |
| `volume-breakout` | 放量突破策略 |
| `ma-golden-cross` | 均线金叉策略 |
| `bottom-volume` | 底部放量反转信号 |
| `box-oscillation` | 箱体震荡策略 |
| `one-yang-three-yin` | 一阳夹三阴 K 线形态 |
| `dragon-head` | 龙头股策略 |
| `expectation-repricing` | 预期重估策略 |
| `growth-quality` | 成长质量策略 |
| `emotion-cycle` | 情绪周期策略 |
| `hot-theme` | 热点题材策略 |

### 4.2 分析框架类

| Skill | 内容 |
|-------|------|
| `chan-theory` | 缠中说禅理论 |
| `elliott-wave` | 艾略特波浪 |
| `candlestick` | 蜡烛图形态识别 |
| `technical-basic` | 技术指标基础（MA/MACD/RSI/BOLL/ATR/KDJ） |
| `minute-analysis` | 分钟级行情分析 |
| `market-microstructure` | 市场微观结构 |
| `volatility` | 波动率分析 |
| `sentiment-analysis` | 市场情绪分析 |
| `sector-rotation` | 行业轮动分析 |

### 4.3 基本面/估值类

| Skill | 内容 |
|-------|------|
| `financial-statement` | 财务报表解读 |
| `fundamental-filter` | 基本面筛选 |
| `valuation-model` | 估值分析 |
| `undervalued-stock-screener` | 低估值筛选 |
| `high-dividend-strategy` | 高股息策略 |
| `esg-screener` | ESG 筛选 |
| `quant-factor-screener` | 多因子选股 |

### 4.4 风险/合规/事件类

| Skill | 内容 |
|-------|------|
| `risk-analysis` | 风险分析 |
| `portfolio-health-check` | 投资组合诊断 |
| `risk-adjusted-return-optimizer` | 风险调整收益优化 |
| `ashare-pre-st-filter` | ST/退市风险预警 |
| `regulatory-knowledge` | 金融监管知识 |
| `corporate-events` | 公司事件驱动 |
| `event-driven-detector` | 事件驱动机会 |
| `insider-trading-analyzer` | 内部人交易分析 |
| `tech-hype-vs-fundamentals` | 科技主题估值偏离 |
| `sentiment-reality-gap` | 情绪-基本面偏差 |

### 4.5 数据/基础设施类

| Skill | 内容 |
|-------|------|
| `tickflow` | TickFlow SDK 数据获取 |
| `tushare-data` | Tushare 数据服务 |
| `akshare` | AkShare 补充数据 |
| `data-routing` | 数据源选择决策树 |
| `report-generate` | 研究报告生成模板 |
| `suitability-report-generator` | 适当性报告模板 |
| `workflow-templates` | 研究工作流模板 |
| `sats-market-assistant` | 市场助手 |
| `small-cap-growth-identifier` | 小盘成长股发现 |

---

## 5. 测试层 (`tests/`) — 共 31 个测试文件

```
tests/
├── __init__.py
├── fixtures.py                            # 测试 fixtures
├── test_config.py                         # 配置加载
├── test_symbols.py                        # 代码规范化
├── test_astock_provider.py                # A 股数据门面
├── test_tickflow_provider.py              # TickFlow 适配器
├── test_tushare_provider.py               # Tushare 适配器
├── test_stock_basic_lookup.py             # 股票查找
├── test_storage_and_api.py                # 存储 + API
├── test_screening_rule.py                 # 筛选规则基类
├── test_price_volume_ma_rule.py           # 量价均线规则
├── test_chan_signals_rule.py              # 缠论信号规则
├── test_chan_composite_rule.py            # 缠论复合规则
├── test_chan_third_buy_rule.py            # 缠论三买规则
├── test_signals.py                        # 统一信号
├── test_indicators.py                     # 技术指标
├── test_llm_provider.py                   # LLM Provider
├── test_dsa_native.py                     # DSA 原生分析
├── test_daily_stock_analysis_bridge.py    # DSA 桥接
├── test_opportunity_discovery.py          # 机会发现
├── test_chan_llm_review.py                # 缠论 LLM 复核
├── test_market_llm_context.py             # 大盘上下文
├── test_stock_llm_context.py              # 个股上下文
├── test_chat_cli.py                       # 聊天 CLI
├── test_chat_preprocessor.py              # 聊天预处理
├── test_chat_reference.py                 # 聊天参考
├── test_memory.py                         # 记忆存储
├── test_repl_cli.py                       # REPL 交互
├── test_scheduler.py                      # 定时调度
├── test_monitoring.py                     # 实时监控
├── test_output_saver.py                   # 输出保存
├── test_progress.py                       # 进度条
├── test_skills_and_chat.py               # Skills + 聊天集成
└── test_knowledge_rag.py / test_chan_knowledge_rag.py  # RAG 知识库
```

---

## 6. 知识库 (`knowledge/`)

```
knowledge/
└── chan/
    └── rules/          # 缠论规则 JSON 卡片
```

缠论规则以 JSON 文件存储，通过 `sats.rag.chan_knowledge` 模块加载，用于：
1. 在监控服务中增强缠论信号判断
2. 在 LLM 复核时注入缠论知识上下文
3. 在聊天上下文中提供缠论参考

---

## 7. 数据流架构

```
数据源 (TickFlow / Tushare / AkShare)
    │
    ▼
AStockDataProvider (统一门面)
    │
    ├──► screening/     → DuckDB (screening_results)
    ├──► indicators/    → 技术指标快照
    ├──► chan/          → 缠论买卖点
    ├──► signals/       → 统一信号事件
    └──► analysis/      → DSA / 机会发现 / LLM 复核
                              │
                              ▼
                       DuckDB (reports, analysis)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         monitoring/      trading/         api/
       (实时监控推送)    (QMT 桥接)     (FastAPI)
              │
              ▼
       DuckDB (monitor_events, monitor_trade_events)
              │
              ▼
       MonitorDisplay (curses 终端)
```

---

## 8. 关键设计决策

1. **数据门面模式**：业务模块只依赖 `AStockDataProvider`，不直接接入具体数据源。后端优先级 `TickFlow → Tushare → AkShare`，每个结果标注 `data_source`。

2. **筛选规则可插拔**：`ScreeningRule` ABC → `registry.py` 注册 → `generated/` 目录支持 AI 生成规则，需用户确认后才写入。

3. **统一信号层**：`sats/signals/` 整合缠论、图形、波浪、均线等多种信号源，通过 `CompositeSpec` 定义复合信号确认关系，避免各分析模块重复实现。

4. **双途径 LLM 使用**：
   - 主模型（如 DeepSeek）：分析、排序、复核
   - 轻量模型（如 MiMo）：预处理、记忆抽取、滚动摘要

5. **全命令面同步**：任何新 CLI 功能必须同步更新 `cli.py`（argparse）、`repl.py`（斜杠命令）、`README.md` 和对应测试。

6. **监控四列表架构**：`monitor_positions`（持仓）、`monitor_watchlist`（关注）、`monitor_buy_candidates`（待买）、`monitor_events`（信号事件），支持多规则同时监控。

7. **交易兜底设计**：`BrokerClient` 是 Protocol，当前实现包括 `NoopTradingProvider`（纸面）、QMT 桥接（真实交易），可扩展其他券商。

---

## 9. 文件统计

| 类别 | 数量 |
|------|------|
| Python 源文件（sats/） | ~55 个 |
| Skills（skills/） | 47 个 |
| 测试文件（tests/） | 31 个 |
| SQL 定义（表） | 20+ 张 |
| CLI 命令 | 30+ 个 |
| 最大源文件 | `cli.py` (~2300 行) |
| 第二大源文件 | `duckdb.py` (~1424 行) |
| 总 Python 代码量 | 约 20,000+ 行（估算） |
