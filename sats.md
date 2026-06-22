# SATS 项目结构与功能详细解析

## 一、项目概述

**SATS (Stock Automated Trading System)** 是一个 Python 3.12+ 的 A 股（中国股市）选股、分析与交易辅助系统。集成了 LLM 驱动的分析、DuckDB 持久化存储、多数据源提供商（TickFlow / Tushare / AkShare）、FastAPI HTTP 服务、CLI 命令行、REPL 交互终端、定时调度器、自主 Agent 框架、实时监控、因子研究、深度分析、交易委员会等能力。

**版本**: 0.1.0  
**入口**: `sats.cli:main`（控制台脚本 `sats`）

---

## 二、顶层目录结构

```
SATS/
├── sats/                          # 主包
│   ├── __init__.py                # 版本号 0.1.0
│   ├── __main__.py                # python -m sats 入口
│   ├── cli.py                     # CLI 命令行 (~3984行, 31个子命令)
│   ├── repl.py                    # REPL 交互终端
│   ├── chat.py                    # LLM Chat 管线 (~1747行)
│   ├── chat_components.py         # 路由构建、证据收集、综合响应
│   ├── chat_planner.py            # Chat 计划构建 (技能路由)
│   ├── chat_preprocessor.py       # 消息预处理 (意图分类、股票名解析)
│   ├── chat_reference.py          # 上下文引用 (从上次输出提取)
│   ├── chat_runtime.py            # 研究运行时 (多步研究工作流)
│   ├── chat_events.py             # 事件发射系统 (UI 流式更新)
│   ├── chat_artifacts.py          # 产物保存/验证 (MD + JSON)
│   ├── config.py                  # .env 配置加载 (Settings dataclass)
│   ├── symbols.py                 # 股票代码标准化
│   ├── progress.py                # 统一进度条系统
│   ├── memory.py                  # 聊天长期记忆
│   ├── skills.py                  # 技能系统 (YAML定义)
│   ├── skill_routing.py           # 技能路由上下文
│   ├── stock_question.py          # 股票问题意图识别
│   ├── stock_basic_lookup.py      # 股票基础数据查找
│   ├── natural_output.py          # 自然语言输出渲染
│   ├── natural_task.py            # 自然任务解析
│   ├── output_saver.py            # 输出持久化 (MD/PDF)
│   ├── output_names.py            # 股票/指数名称解析
│   ├── watchlist_editor.py        # 监控列表交互编辑器
│   ├── dependencies.py            # 可选依赖自愈管理
│   ├── data/                      # 市场数据层
│   ├── screening/                 # 选股规则引擎
│   ├── storage/                   # DuckDB 持久化
│   ├── llm/                       # LLM 供应商抽象
│   ├── analysis/                  # 后选股分析 & LLM Review
│   ├── agent/                     # 自主 Agent 框架
│   ├── api/                       # FastAPI HTTP API
│   ├── scheduler/                 # 定时任务调度
│   ├── indicators/                # 技术指标计算器
│   ├── signals/                   # 信号分析引擎 (40+ 复合信号)
│   ├── factors/                   # 因子研究系统
│   ├── chan/                       # 缠论引擎
│   ├── deep_analysis/             # 深度分析服务
│   ├── backtesting/               # 回测服务
│   ├── rag/                       # RAG 知识检索
│   ├── web/                       # 网络搜索 & 社交热榜 & Web RAG
│   ├── monitoring/                # 实时监控服务
│   └── trading/                   # 交易集成 (QMT)
├── skills/                        # 技能定义文件 (52个 SKILL.md)
├── knowledge/                     # 知识文档 (9个量价分析文档)
├── tests/                         # 测试 (~56个文件)
├── docs/                          # 文档
├── artifacts/                     # 聊天产物输出
├── reports/                       # 报告输出
├── pyproject.toml                 # 项目元数据 & 依赖
├── README.md                      # 项目说明
└── .env                           # 配置文件 (由 init 创建)
```

---

## 三、入口与调度机制

### 3.1 入口点

| 入口 | 文件 | 说明 |
|------|------|------|
| `python -m sats <cmd>` | `sats/__main__.py` | 调用 `sats.cli:main()` |
| `sats <cmd>` (console script) | `sats/cli.py:main()` | pip install -e . 注册 |
| 无参数启动 `sats` | `sats/cli.py:main()` -> `sats/repl.py:run_repl()` | 进入 REPL |

### 3.2 调度机制

`main(argv)` 的分发流程:
1. 无 argv → 启动 REPL (`run_repl()`)
2. 有 argv → `build_parser()` 构建 argparse → 解析参数 → `if/elif` 链分发到对应 `cmd_*` 处理函数

### 3.3 REPL 与 CLI 的统一

REPL 中 `/screen --trade-date 20260514` → 去掉 `/` → `shlex.split()` → 调用 `cli.main(["screen", "--trade-date", "20260514"])`，与一次性 CLI 完全共用同一入口。

---

## 四、所有命令详细解析 (31个)

> 所有命令均支持 `--db` 参数，用于指定 DuckDB 数据库路径，默认使用 `SATS_DB_PATH` 环境变量。

### 4.1 `init` — 初始化配置

在项目根目录创建 `.env` 配置模板。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--overwrite` | flag | False | 覆盖已有的 .env 文件 |

**示例：**
```bash
# 首次初始化
sats init

# 强制覆盖已有配置
sats init --overwrite
```

---

### 4.2 `screen` — 全市场选股

运行选股规则对全市场进行筛选，结果写入 DuckDB。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--rule` | str | `ma_volume_relative_strength` | 否 | 选股规则名称。可选: `price_volume_ma`, `chan_third_buy`, `chan_composite`, `chan_signals`, `monthly_base_breakout`, `turtle_trade`, `ma_volume`, `high_tight_flag`, `limit_up_shakeout`, `uptrend_limit_down`, `rps_breakout`, `ma_volume_relative_strength`, `signal_composite` |
| `--trade-date` | str | — | **是** | 交易日，格式 YYYYMMDD |
| `--select-watchlist` | flag | — | 否 | 筛选后提示导入到 watchlist (与 --no-select-watchlist 互斥) |
| `--no-select-watchlist` | flag | — | 否 | 不提示导入 watchlist |

**示例：**
```bash
# 使用默认规则筛选
sats screen --trade-date 20260612

# 使用缠论综合规则筛选
sats screen --rule chan_composite --trade-date 20260612

# 筛选并导入到 watchlist
sats screen --trade-date 20260612 --select-watchlist
```

---

### 4.3 `results` — 查询选股结果

从 DuckDB 查询已保存的选股结果。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--trade-date` | str | None | 按交易日筛选 |
| `--rule` | str | None | 按规则名称筛选 |
| `--passed` | flag | False | 仅显示通过的记录 |

**示例：**
```bash
sats results
sats results --trade-date 20260612 --passed
sats results --rule chan_composite --passed
```

---

### 4.4 `result-rules` — 列出选股规则名

列出 DuckDB 中已保存的选股规则名称。

```bash
sats result-rules
```

---

### 4.5 `quote` — 实时行情

显示实时行情 + 均线 (MA5, MA20, MA60, MA250)。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--stocks` | str | — | **是** | 逗号分隔的股票代码或中文名称 |

**示例：**
```bash
sats quote --stocks 000001
sats quote --stocks 000001,600519.SH,300750
sats quote --stocks 紫光股份,贵州茅台
```

---

### 4.6 `period-change` — 区间涨跌幅

计算股票或指数在最近 N 个自然日内的涨跌幅。`--stocks` 和 `--indices` 互斥且必选其一。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--stocks` | str | None | 条件必填 | 逗号分隔的股票代码或名称 (与 --indices 互斥) |
| `--indices` | str | None | 条件必填 | 逗号分隔的指数代码或名称 (与 --stocks 互斥)，如 `上证指数,沪深300` |
| `--days` | int | — | **是** | 从今天向前回溯的自然日天数 |

**示例：**
```bash
# 查看个股最近 20 天涨跌幅
sats period-change --stocks 000001,600519 --days 20

# 查看指数最近 60 天涨跌幅
sats period-change --indices 上证指数,沪深300,创业板指 --days 60

# 单只股票
sats period-change --stocks 宁德时代 --days 10
```

---

### 4.7 `analyze` — 统一信号分析

对指定股票运行信号分析，支持 Markdown 报告和可选 LLM Review。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `analyze_action` | positional | None | 否 | 子操作，可选 `"signals"` 用于打印信号策略定义 |
| `--stocks` | str | None | 否 | 逗号分隔的股票代码或名称 (与 --from-screened 互斥) |
| `--from-screened` | flag | False | 否 | 分析已保存的选股结果 (与 --stocks 互斥) |
| `--signals` | str | `"all"` | 否 | 逗号分隔的信号组或 ID |
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD，默认最近交易日 |
| `--rule` | str | `ma_volume_relative_strength` | 否 | 选股规则 (配合 --from-screened) |
| `--lookback-days` | int | 180 | 否 | 历史回溯交易天数 |
| `--category` | str | None | 否 | 信号分类 (配合 `analyze signals`) |
| `--json` | flag | False | 否 | 输出完整 JSON |
| `--noreport` | flag | False | 否 | 不生成 Markdown 报告 |
| `--llm-review` | flag | False | 否 | 使用 LLM 审查本地信号结果 |

**示例：**
```bash
sats analyze --stocks 000001,600519.SH
sats analyze --from-screened --trade-date 20260612 --rule chan_composite
sats analyze --stocks 300750 --signals technical
sats analyze signals
sats analyze --stocks 000001 --llm-review
```

---

### 4.8 `analyze-dsa` — 外部 DSA 分析桥接

调用外部 `daily_stock_analysis` 工具进行分析。`--stocks` 和 `--rule` 不能同时使用。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--trade-date` | str | None | 交易日 YYYYMMDD，默认最近交易日 |
| `--rule` | str | None | 选股规则名称 |
| `--stocks` | str | None | 逗号分隔的股票代码或名称 |

**示例：**
```bash
sats analyze-dsa --stocks 000001,600519
sats analyze-dsa --rule chan_composite --trade-date 20260612
```

---

### 4.9 `dsa` — SATS 原生 DSA 分析

本地评分系统 (0-100分) + 可选 LLM 审查。`--stocks` 和 `--from-screened` 必选其一。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--stocks` | str | None | 条件必填 | 逗号分隔的股票代码或名称 (与 --from-screened 互斥) |
| `--from-screened` | flag | False | 条件必填 | 分析已保存的选股结果 (与 --stocks 互斥) |
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD，默认今天 |
| `--rule` | str | None | 否 | 选股规则名称 (配合 --from-screened) |
| `--lookback-days` | int | 180 | 否 | 历史回溯窗口 |
| `--explain-rating` | flag | False | 否 | 展示评分调整原因 |
| `--llm-timeout` | int | 20 | 否 | LLM 超时秒数 |
| `--no-llm` | flag | False | 否 | 跳过 LLM 审查，仅使用本地规则 |

**示例：**
```bash
sats dsa --stocks 000001
sats dsa --from-screened --trade-date 20260612 --rule chan_composite
sats dsa --stocks 000001 --no-llm --explain-rating
```

---

### 4.10 `deep-analysis` — 深度分析

运行 SATS 原生深度股票分析，支持分阶段执行。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--stocks` | str | — | **是** | 逗号分隔的股票代码或名称 |
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD，默认最近交易日 |
| `--phase` | choice | `run` | 否 | 管道阶段: `run` (完整运行), `collect` (仅采集数据), `score` (仅评分), `panel` (仅面板), `report` (仅报告) |
| `--lookback-days` | int | 180 | 否 | 历史回溯窗口 |
| `--json` | flag | False | 否 | 输出 JSON |
| `--noreport` | flag | False | 否 | 不生成报告文件 |
| `--no-llm` | flag | False | 否 | 跳过可选 LLM 审查 |

**示例：**
```bash
# 完整深度分析
sats deep-analysis --stocks 000001

# 仅采集数据阶段
sats deep-analysis --stocks 000001 --phase collect

# 仅评分阶段
sats deep-analysis --stocks 000001 --phase score

# 不调用 LLM
sats deep-analysis --stocks 000001,600519 --no-llm
```

---

### 4.11 `serenity-screen` — Serenity 瓶颈筛选

运行 Serenity AI 瓶颈筛选，识别 AI/科技产业链中的关键瓶颈环节。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--theme` | str | `""` | 否 | AI/科技主题，默认 AI 供应链 |
| `--stocks` | str | None | 否 | 逗号分隔的股票代码或名称 |
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD |
| `--top` | int | 10 | 否 | 最大排名候选数 |
| `--candidate-limit` | int | 30 | 否 | 最大增强候选数 |
| `--lookback-days` | int | 180 | 否 | 历史回溯窗口 |
| `--json` | flag | False | 否 | 输出 JSON |
| `--noreport` | flag | False | 否 | 不生成报告文件 |
| `--no-llm` | flag | False | 否 | 跳过 LLM 审查和主题降级 |

**示例：**
```bash
# 默认 AI 供应链筛选
sats serenity-screen

# 指定主题
sats serenity-screen --theme "半导体芯片"

# 指定股票池
sats serenity-screen --stocks 000001,600519 --top 5
```

---

### 4.12 `trading-committee` — 交易委员会

多分析师辩论系统，模拟多空研究辩论 + 风险评估 + 交易员提案 + 最终决策。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--stocks` | str | — | **是** | 逗号分隔的股票代码或名称 |
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD |
| `--lookback-days` | int | 180 | 否 | 历史回溯窗口 |
| `--debate-rounds` | int | 1 | 否 | 多空研究辩论轮数 |
| `--risk-rounds` | int | 1 | 否 | 风险团队辩论轮数 |
| `--llm-timeout` | int | None | 否 | LLM 超时秒数 |
| `--json` | flag | False | 否 | 输出 JSON |
| `--noreport` | flag | False | 否 | 不生成报告文件 |
| `--no-llm` | flag | False | 否 | 跳过 LLM 团队调用，使用确定性摘要 |

**示例：**
```bash
# 单轮辩论
sats trading-committee --stocks 000001

# 3 轮多空辩论 + 2 轮风险评估
sats trading-committee --stocks 600519 --debate-rounds 3 --risk-rounds 2

# 仅本地确定性分析
sats trading-committee --stocks 300750 --no-llm
```

---

### 4.13 `analyze-chan` — 缠论 LLM 审查

缠论选股结果的 LLM 智能审查。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD |
| `--rule` | str | None | 否 | 选股规则名称过滤 |
| `--chan-rule` | str | `chan_third_buy` | 否 | 缠论规则: `chan_third_buy`, `chan_composite`, `chan_signals` |
| `--top` | int | 20 | 否 | 最大审查候选数 |
| `--stocks` | str | None | 否 | 逗号分隔的股票代码或名称 |

**示例：**
```bash
sats analyze-chan --trade-date 20260612
sats analyze-chan --chan-rule chan_composite --top 10
sats analyze-chan --stocks 000001,600519 --chan-rule chan_signals
```

---

### 4.14 `chan-kb` — 缠论知识库搜索

搜索本地缠论知识卡片 (RAG)。

```bash
sats chan-kb search 三买定义
sats chan-kb search 中枢震荡
```

---

### 4.15 `discover` — 短线机会发现

短线 A 股机会发现。有自然语言 query 时走 LLM 选股 Agent，无 query 时走纯本地信号排序。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD |
| `--signals` | str | `DEFAULT_DISCOVERY_SIGNALS` | 否 | 分析信号组或 ID |
| `--limit` | int | None | 否 | 最终股票数量 |
| `--candidate-limit` | int | `DEFAULT_CANDIDATE_LIMIT` | 否 | 发送给 LLM 的本地候选数 |
| `--lookback-days` | int | 180 | 否 | 历史回溯天数 |
| `--hot-sector-days` | int | 5 | 否 | 热门板块回溯天数 (3/4/5) |
| `--no-hot-sector` | flag | False | 否 | 禁用热门板块权重 |
| `--json` | flag | False | 否 | 输出 JSON |
| `--noreport` | flag | False | 否 | 不生成报告 |
| `query` | positional | None | 否 | 自然语言选股请求 (剩余参数) |

**示例：**
```bash
sats discover --trade-date 20260612
sats discover --limit 10 --no-hot-sector
sats discover 寻找半导体板块低位放量突破的股票
sats discover --hot-sector-days 3 --limit 15
```

---

### 4.16 `chat` — LLM 对话

LLM 对话接口，默认走 Agent 路由。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--no-memory` | flag | False | 否 | 禁用本地聊天记忆 |
| `--knowledge` | str | None | 否 | 指定知识库名称/ID |
| `--no-agent` | flag | False | 否 | 禁用 Agent 路由，使用纯 LLM 聊天 |
| `--confirm` | str | None | 否 | 确认待处理的运行时动作 |
| `--reject` | str | None | 否 | 拒绝待处理的运行时动作 |
| `--trace` | str | None | 否 | 显示聊天轮次追踪 |
| `--auto-trade` | str | `""` | 否 | 启用的交易动作: `buy`, `sell` |
| `--broker` | choice | `noop` | 否 | 交易券商: `noop`, `qmt` |
| `--live-trading` | flag | False | 否 | 允许 QMT 实盘下单 |
| `--max-order-value` | float | 20000.0 | 否 | 最大买入金额 |
| `--max-position-pct` | float | 0.2 | 否 | 最大持仓占比 |
| `--sell-ratio` | float | 1.0 | 否 | 卖出比例 |
| `--max-iterations` | int | 6 | 否 | Agent 最大步骤数 |
| `--command-timeout` | int | 120 | 否 | 单个命令超时秒数 |
| `--python-timeout` | int | 30 | 否 | Python 执行超时秒数 |
| `--plan-only` | flag | False | 否 | 仅构建并打印 Agent 计划 |
| `--dry-run` | flag | False | 否 | 跳过高风险副作用 |
| `message` | positional | — | 否 | 消息 (剩余参数) |

**示例：**
```bash
sats chat 今天大盘走势怎么样
sats chat --no-agent 解释一下 MACD 指标
sats chat --knowledge chan-theory 三买的定义是什么
sats chat --confirm action_001
sats chat --auto-trade buy --broker qmt 买入 100 股平安银行
sats chat --plan-only 帮我分析今天的机会
sats chat --trace turn_20260612_001
```

---

### 4.17 `agent` — 显式 Agent 任务

通过自主 Agent 执行自然语言目标。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--auto-trade` | str | `""` | 否 | 启用的交易动作: `buy`, `sell` |
| `--broker` | choice | `noop` | 否 | 交易券商: `noop`, `qmt` |
| `--live-trading` | flag | False | 否 | 允许 QMT 实盘下单 |
| `--max-order-value` | float | 20000.0 | 否 | 最大买入金额 |
| `--max-position-pct` | float | 0.2 | 否 | 最大持仓占比 |
| `--sell-ratio` | float | 1.0 | 否 | 卖出比例 |
| `--max-iterations` | int | 6 | 否 | Agent 最大步骤数 |
| `--command-timeout` | int | 120 | 否 | 单个命令超时秒数 |
| `--python-timeout` | int | 30 | 否 | Python 执行超时秒数 |
| `--plan-only` | flag | False | 否 | 仅构建并打印 Agent 计划 |
| `--dry-run` | flag | False | 否 | 跳过高风险副作用 |
| `message` | positional | — | **是** | 自然语言 Agent 目标 (剩余参数) |

**示例：**
```bash
sats agent 帮我分析贵州茅台最近的技术面
sats agent --max-iterations 3 筛选今天涨停板的股票
sats agent --plan-only 分析半导体板块
sats agent --dry-run 买入 100 股平安银行
```

---

### 4.18 `web` — 网络搜索 & 社交热榜

公网搜索、网页抓取、社交平台热榜、关键词提及追踪。

#### 子命令 `web search`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--limit` | int | 5 | 否 | 最大结果数 |
| `--trusted-domains` | str | `""` | 否 | 逗号分隔的可信域名 |
| `--freshness` | choice | `""` | 否 | 时效: `d`(天), `w`(周), `m`(月), `y`(年) |
| `--context-size` | str | None | 否 | 上下文大小 |
| `--providers` | str | `""` | 否 | 搜索供应商: `ddgs`, `bing`, `tavily` (逗号分隔) |
| `--json` | flag | False | 否 | 输出 JSON |
| `query` | positional (≥1) | — | **是** | 搜索关键词 |

#### 子命令 `web open`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `url` | positional | — | **是** | 公网 HTTP/HTTPS URL |
| `--query` | str | `""` | 否 | 页内 RAG 检索查询 |
| `--trusted-domains` | str | `""` | 否 | 逗号分隔的允许域名 |
| `--json` | flag | False | 否 | 输出 JSON |

#### 子命令 `web cache clear`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--expired-only` | flag | False | 仅清除过期文档 |
| `--json` | flag | False | 输出 JSON |

#### 子命令 `web hot`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--platforms` | str | `"all"` | 逗号分隔平台或 `all`: `weibo`, `zhihu`, `baidu`, `douyin`, `toutiao`, `bilibili`, `xueqiu_stock`, `xueqiu_spot` |
| `--limit` | int | 20 | 每个平台返回条数 |
| `--json` | flag | False | 输出 JSON |

#### 子命令 `web mentions`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--keyword` | str | — | **是** | 股票名/公司名/话题关键词 |
| `--platforms` | str | `"all"` | 否 | 逗号分隔平台 |
| `--limit` | int | 50 | 否 | 匹配前每平台最大条数 |
| `--json` | flag | False | 否 | 输出 JSON |

**示例：**
```bash
sats web search A股半导体行业分析
sats web search --freshness w --providers ddgs,tavily 新能源汽车政策
sats web open https://www.sse.com.cn --query 注册制
sats web cache clear
sats web cache clear --expired-only
sats web hot --platforms weibo --limit 30
sats web mentions --keyword 贵州茅台
sats web mentions --keyword 宁德时代 --platforms xueqiu_stock,zhihu
```

---

### 4.19 `model` — 模型管理

查看和切换 LLM 模型配置。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `status` | — | 显示当前活跃的 main/light 模型 |
| `list` | — | 列出所有已配置的模型 Profile |
| `ping` | `--timeout` (int), `--json` | Ping 当前 LLM 供应商 |
| `use <profile>` | `--target` (choice: `main`, `light`, `both`; 默认 `main`) | 切换默认模型 Profile |

**示例：**
```bash
sats model status
sats model list
sats model ping
sats model ping --timeout 10 --json
sats model use DEEPSEEK
sats model use XIAOMIMIMO --target both
```

---

### 4.20 `memory` — 聊天记忆管理

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | — | 列出活跃记忆 |
| `search <query>` | `query` (位置参数, 剩余) | 搜索记忆 |
| `forget <memory_id>` | `memory_id` (位置参数) | 归档指定记忆 |
| `clear` | `--yes` (flag) | 清除所有聊天记忆 |

```bash
sats memory list
sats memory search 缠论三买
sats memory forget mem_20260612_001
sats memory clear --yes
```

---

### 4.21 `history` — 交互历史

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | `--kind` (chat/command), `--limit` (默认 20, 上限 100) | 列出历史 |
| `search <query>` | `query` (位置参数, ≥1), `--kind`, `--limit` | 搜索历史 |
| `show <history_id>` | `history_id` (位置参数) | 查看详情 |
| `delete <history_id>` | `history_id` (位置参数) | 软删除 |

```bash
sats history list --kind command --limit 50
sats history search DSA分析
sats history show hist_20260612_001
```

---

### 4.22 `knowledge` — RAG 知识库管理

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | — | 列出知识库 |
| `add` | `--name` (必填), `--description`, `--tags` | 新建/更新 |
| `ingest` | `--knowledge` (必填), `--path` (Path, 必填), `--tags` | 导入文件/目录 |
| `search` | `--query` (必填), `--knowledge` (可选), `--limit` (默认 6) | 搜索知识块 |
| `sync-stock-basic` | — | 同步 stock_basic 缓存 |

```bash
sats knowledge list
sats knowledge add --name chan-theory --description "缠论知识库"
sats knowledge ingest --knowledge chan-theory --path ./docs/chan.pdf
sats knowledge search --query 三买定义 --knowledge chan-theory
sats knowledge sync-stock-basic
```

---

### 4.23 `indicators` — 技术指标

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--stocks` | str | — | **是** | 逗号分隔的股票代码或名称 |
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD |
| `--lookback-days` | int | 180 | 否 | 历史回溯天数 |
| `--json` | flag | False | 否 | 输出 JSON |

```bash
sats indicators --stocks 000001
sats indicators --stocks 000001,600519.SH,300750 --lookback-days 360
```

---

### 4.24 `factor` — 因子研究 & 选股

#### `factor list`

| 参数 | 类型 | 说明 |
|------|------|------|
| `--zoo` | choice | 因子动物园: `alpha101`, `gtja191`, `barra_style` |
| `--theme` | str | 因子主题: `value`, `volume`, `momentum` 等 |
| `--universe` | str | 股票池: `equity_cn` 等 |
| `--json` | flag | 输出 JSON |

#### `factor show`

| 参数 | 类型 | 说明 |
|------|------|------|
| `factor_id` / `--factor` | str | 因子 ID |
| `--json` | flag | 输出 JSON |

#### `factor analyze`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--factor` | str | — | **是** | 因子 ID |
| `--trade-date` | str | None | 否 | 交易日 |
| `--lookback-days` | int | 260 | 否 | 历史回溯天数 |
| `--horizon` | int | 1 | 否 | 前瞻收益周期 |
| `--groups` | int | 5 | 否 | 分位数组数 |
| `--stocks` | str | None | 否 | 可选股票代码 |
| `--json` | flag | False | 否 | 输出 JSON |
| `--noreport` | flag | False | 否 | 不生成报告 |

#### `factor pick`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--factors` | str | None | 逗号分隔因子 ID |
| `--trade-date` | str | None | 交易日 |
| `--lookback-days` | int | 260 | 历史回溯天数 |
| `--horizon` | int | 1 | 前瞻收益周期 |
| `--top` | int | 20 | 选股数量 |
| `--neutralize` | choice | `none` | 中性化: `none`, `industry` |
| `--weight` | choice | `equal` | 因子权重: `equal`, `ic` |
| `--profile` | choice | 默认 profile | 因子组合 Profile |
| `--screening-profile` | str | `multi_factor` | 写入选股结果时的规则后缀 |
| `--write-screening` | flag | False | 将 TopN 写入 screening_results |
| `--json` | flag | False | 输出 JSON |
| `--noreport` | flag | False | 不生成报告 |

#### `factor ml`

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `status` | `--json` | 检查 ML 依赖 |
| `setup` | `--json` | 安装缺失依赖 |
| `train` | `--profile`, `--factors`, `--model` (lightgbm/xgboost), `--train-start`, `--train-end`, `--valid-end`, `--horizon`, `--lookback-days`, `--stocks`, `--json`, `--db` | 训练模型 |
| `evaluate` | `--model-run` (必填), `--trade-date`, `--json`, `--db` | 评估模型 |
| `predict` | `--model-run` (必填), `--trade-date` (必填), `--profile`, `--factors`, `--top`, `--lookback-days`, `--stocks`, `--write-screening`, `--json`, `--db` | 预测 TopN |

```bash
sats factor list --zoo alpha101
sats factor show gtja191_001
sats factor analyze --factor gtja191_001 --trade-date 20260612
sats factor pick --factors gtja191_001,gtja191_002 --top 15
sats factor ml status
sats factor ml train --model lightgbm --train-start 20240101 --train-end 20260501
sats factor ml predict --model-run run_20260601 --trade-date 20260612 --top 10
```

---

### 4.25 `skills` — 技能列表

```bash
sats skills
```

---

### 4.26 `watchlist` — 监控列表管理

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | — | 列出监控列表 |
| `add` | `--stocks` (必填), `--name`, `--note` | 添加股票 |
| `remove` | `--stocks` (必填) | 移除股票 |
| `clear` | — | 清空 |
| `select-delete` | — | 交互式选择删除 |
| `import-screened` | `--trade-date` (必填), `--rule` | 从选股结果导入 |

```bash
sats watchlist list
sats watchlist add --stocks 000001,600519 --name "核心持仓"
sats watchlist import-screened --trade-date 20260612 --rule chan_composite
```

---

### 4.27 `monitor` — 实时监控

#### `monitor positions`

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `add` | `--symbol` (必填), `--buy-price` (float, 必填), `--quantity` (float, 必填), `--buy-date`, `--note` | 添加持仓 |
| `list` | — | 列出持仓 |
| `remove` | `--symbol` (必填) | 移除持仓 |

#### `monitor watchlist`

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `add` | `--symbol` (必填), `--name`, `--note` | 添加监控标的 |
| `list` | — | 列出 |
| `remove` | `--symbol` (必填) | 移除 |

#### `monitor buy-candidates`

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | — | 列出买入候选 |
| `remove` | `--symbol` (必填) | 移除 |

#### `monitor plans` — 可执行监控计划

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `validate` | `--file` (Path, 必填) | 验证计划 JSON 文件 |
| `import` | `--file` (Path, 必填) | 导入已验证计划为草稿 |
| `list` | — | 列出计划 |
| `show` | `--plan-id` (必填) | 查看计划详情 |
| `activate` | `--plan-id` (必填) | 激活计划 |
| `disable` | `--plan-id` (必填) | 禁用计划 |
| `remove` | `--plan-id` (必填) | 删除计划 |
| `disable-item` | `--item-id` (必填) | 禁用单个触发项 |
| `disable-group` | `--group-id` (必填) | 禁用触发组 |

#### `monitor start` / `monitor run`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--rules` | str | `chan_signals` | 监控规则 |
| `--lists` | str | `positions,watchlist` | 监控列表 |
| `--interval` | int | 60 | 轮询间隔秒数 |
| `--llm-review` | flag | False | 启用 LLM 审查 |
| `--broker` | choice | `noop` | 交易券商 |
| `--auto-trade` | str | `""` | 启用交易动作 |
| `--max-order-value` | float | 20000.0 | 最大买入金额 |
| `--max-position-pct` | float | 0.2 | 最大持仓占比 |
| `--sell-ratio` | float | 1.0 | 卖出比例 |
| `--once` | flag | False | 仅运行一轮 (仅 run) |

```bash
sats monitor positions add --symbol 000001 --buy-price 12.5 --quantity 500
sats monitor positions list
sats monitor watchlist add --symbol 600519 --name 贵州茅台
sats monitor plans import --file plan.json
sats monitor plans list
sats monitor plans activate --plan-id plan_001
sats monitor run --rules chan_signals --interval 30
sats monitor start --rules chan_signals
sats monitor status
sats monitor stop
```

---

### 4.28 `monitor-display` — 监控面板

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `start` | `--refresh` (int, 默认 3), `--new-terminal` (flag, macOS) | 启动面板 |
| `run` | `--refresh` (int, 默认 3), `--plain` (flag) | 前台运行 |
| `stop` | — | 停止面板 |

```bash
sats monitor-display run
sats monitor-display run --plain
sats monitor-display start --new-terminal
sats monitor-display stop
```

---

### 4.29 `schedule` — 定时任务

仅可调度 SATS CLI 命令或聊天消息。时区为上海。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `add` | `--name` (必填), `--type` (cli/chat, 必填), `--text` (必填), `--daily`/`--weekly` (互斥), `--days`, `--time` (HH:MM, 必填) | 添加任务 |
| `list` | — | 列出任务 |
| `runs` | `--limit` (默认 20), `--name` | 运行记录 |
| `enable <name>` | — | 启用 |
| `disable <name>` | — | 禁用 |
| `remove <name>` | — | 删除 |
| `run <name>` | — | 立即运行 |
| `start` | `--interval` (默认 30) | 后台启动 |
| `run-loop` | `--interval` (默认 30), `--once` | 前台运行 |
| `stop` | — | 停止 |
| `status` | — | 查看状态 |

```bash
sats schedule add --name morning-screen --type cli --text "screen --trade-date today --rule chan_composite" --daily --time 09:30
sats schedule add --name afternoon-dsa --type cli --text "dsa --from-screened --trade-date today" --weekly --days mon,wed,fri --time 15:30
sats schedule list
sats schedule start
sats schedule runs --limit 10
```

---

### 4.30 `qmt` — QMT 券商交易

#### `qmt bridge run`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--host` | str | `127.0.0.1` | Bridge 地址 |
| `--port` | int | 8765 | Bridge 端口 |
| `--qmt-path` | str | `""` | QMT 安装路径 |
| `--account-id` | str | `""` | 资金账号 |
| `--account-type` | str | `STOCK` | 账户类型 |
| `--session-id` | int | 0 | 会话 ID |
| `--token` | str | `""` | 认证 Token |

#### `qmt buy` / `qmt sell`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--symbol` | str | — | **是** | 股票代码或名称 |
| `--quantity` | int | — | **是** | 数量 (100 整数倍) |
| `--price-type` | choice | `latest` | 否 | `latest` (最新价), `limit` (限价) |
| `--price` | float | None | 否 | 限价价格 |
| `--dry-run` | flag | False | 否 | 模拟模式 |

#### 其他子命令

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `status` | — | 查看状态 |
| `asset` | — | 查看资产 |
| `positions` | — | 查看持仓 |
| `sync positions` | `--prune-missing` | 同步 QMT 持仓到监控 |
| `orders` | `--open` | 查看订单 |
| `trades` | `--limit` (默认 50) | 查看成交 |
| `cancel` | `--order-id` (必填) | 取消订单 |

```bash
sats qmt bridge run --host 127.0.0.1 --port 8765 --account-id 123456
sats qmt status
sats qmt asset
sats qmt positions
sats qmt sync positions --prune-missing
sats qmt buy --symbol 000001 --quantity 100 --dry-run
sats qmt buy --symbol 000001 --quantity 300 --price-type limit --price 12.50
sats qmt sell --symbol 000001 --quantity 200
sats qmt cancel --order-id ord_20260612_001
```

---

### 4.31 `serve` — HTTP API 服务

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--host` | str | `127.0.0.1` | 监听地址 |
| `--port` | int | 8000 | 监听端口 |

```bash
sats serve
sats serve --port 9000
sats serve --host 0.0.0.0 --port 8000
```

---

## 五、REPL 内置命令 (11个)

| 命令 | 参数 | 功能 |
|------|------|------|
| `/help` | — | 渲染帮助面板 |
| `/exit`, `/quit` | — | 退出 REPL |
| `/clear` | — | 清屏 |
| `/save` | `--format` (md/pdf), `--path`, `--source` (output/report) | 保存上次输出 |
| `/new` | `[title]` | 创建新聊天会话 |
| `/goal` | `[text]` / `status` / `cancel` / `clear` | 设置/查看/清除 Agent 目标 |
| `/plan` | `[text]` | 构建 Agent 计划 (等同 `agent --plan-only`) |
| `/confirm` | `ACTION_ID` | 确认待处理动作 |
| `/reject` | `ACTION_ID` | 拒绝待处理动作 |
| `/trace` | `[turn_id]` | 显示聊天追踪 |

```bash
sats> /help
sats> /goal 帮我每天收盘后分析缠论信号
sats> /plan 分析半导体板块机会
sats> /save --format pdf
sats> /confirm action_001
sats> /exit
```

---

## 六、完整技能列表 (49个)

> 技能定义位于 `skills/<skill_id>/SKILL.md`，通过 YAML 前置元数据 + Markdown 正文描述方法论。Agent/Chat 系统根据用户意图自动匹配最相关的技能。

### 6.1 策略类 (strategy, 16个)

| # | 技能 ID | 名称 | 优先级 | 功能说明 |
|---|---------|------|--------|----------|
| 1 | `serenity-stock-screen` | Serenity 卡位筛选 | 96 | SATS 原生 AI/科技供应链瓶颈筛选，覆盖半导体/光通信/先进封装/算力电力散热/具身智能，8因子评分+8类风险罚分 |
| 2 | `technical-basic` | 技术面分析 | 90 | MA/SMA/EMA、MACD、RSI、BOLL、ATR、KDJ、支撑压力和成交量基础分析 |
| 3 | `quant-factor-screener` | 多因子选股 | 80 | 6因子等权打分+行业偏差控制+因子择时+因子拥挤度检查，覆盖价值/动量/质量/低波动/规模/成长 |
| 4 | `bull-trend` | 多头趋势 | 76 | MA5/MA10/MA20 多头排列确认+量价验证+风险边界(MA20跌破) |
| 5 | `shrink-pullback` | 缩量回踩 | 74 | 上升趋势中回踩 MA5/MA10 且量能收缩的低吸节奏 |
| 6 | `volume-breakout` | 放量突破 | 72 | 价格站上阻力位+成交量确认的短中线突破信号 |
| 7 | `ma-golden-cross` | 均线金叉 | 72 | MA5/MA10/MA20 金叉检测+MACD动能+量能确认+追高过滤 |
| 8 | `one-yang-three-yin` | 一阳夹三阴 | 50 | 5日形态：阳线→3缩量小阴→阳线突破+趋势过滤 |
| 9 | `elliott-wave` | 艾略特波浪 | 48 | 启发式5浪/3浪结构分析，基于峰谷/ZigZag/斐波那契比例 |
| 10 | `bottom-volume` | 底部放量 | 55 | 长期下跌后放量企稳的高风险反转信号识别 |
| 11 | `box-oscillation` | 箱体震荡 | 55 | 横盘区间支撑/阻力波段研究+突破/失败信号 |
| 12 | `dragon-head` | 龙头策略 | 62 | 板块轮动中识别相对强度领先的龙头候选 |
| 13 | `chan-theory` | 缠论助手 | — | 缠论1/2/3买卖点+背驰+中枢+区间套解释助手 |
| 14 | `small-cap-growth-identifier` | 小盘成长识别 | — | 低市值/高增长/资产负债表质量/管理层/机构覆盖不足筛选 |
| 15 | `undervalued-stock-screener` | 低估值筛选 | — | PE/PB/ROE/现金流/行业分位识别低估稳健公司 |
| 16 | `high-dividend-strategy` | 高股息策略 | — | 股息率/分红连续性/现金流覆盖/分红税分析(持有>1年免税) |

### 6.2 分析类 (analysis, 15个)

| # | 技能 ID | 名称 | 优先级 | 功能说明 |
|---|---------|------|--------|----------|
| 1 | `deep-stock-analysis` | 深度个股分析 | 95 | 12维度质量标记+0-10分评分，12位投资人面板(bullish/neutral/bearish) |
| 2 | `sentiment-analysis` | 市场情绪面 | 88 | 涨跌停家数/成交量/融资融券/北向资金评估情绪偏热/中性/偏冷 |
| 3 | `sector-rotation` | 行业轮动 | 86 | 宏观周期定位+行业强弱，输出超配/中性/低配+拥挤度检查 |
| 4 | `financial-statement` | 财务报表解读 | 85 | 利润表/资产负债表/现金流量表+杜邦分解+Altman Z/Piotroski F/Beneish M |
| 5 | `market-microstructure` | 市场微观结构 | 84 | 盘口价差/成交冲击/日内分时/集合竞价/流动性风险 |
| 6 | `valuation-model` | 估值分析 | 82 | PE/PB/PS/ROE/成长性/行业对比判断估值及安全边际 |
| 7 | `emotion-cycle` | 情绪周期 | 78 | 4阶段分层：冷淡底部/升温/过热/退潮+逆情绪策略 |
| 8 | `fundamental-filter` | 基本面筛选 | 70 | 价值/成长/质量/风险排除4类PE/PB/ROE/营收筛选逻辑 |
| 9 | `hot-theme` | 热点题材 | 68 | 政策/产业/市场热点强度评估+个股相关性分级+阶段判断 |
| 10 | `growth-quality` | 成长质量 | 64 | 收入利润增长/ROE/现金流/行业空间/技术确认5维分析 |
| 11 | `expectation-repricing` | 预期重估 | 58 | 业绩/政策/估值/产业预期变化的修复或兑现风险分析 |
| 12 | `sentiment-reality-gap` | 情绪与基本面偏差 | — | 寻找被过度看空/错杀但基本面仍稳健的候选 |
| 13 | `insider-trading-analyzer` | 内部人交易分析 | — | 董监高增减持分析，6维信号评估+信号强度分级 |
| 14 | `tech-hype-vs-fundamentals` | 科技估值vs基本面 | — | A股科技主题估值偏离分析，识别概念炒作/高估/低估 |
| 15 | `esg-screener` | ESG筛选 | — | ESG三维度+争议事件+ESG动量+财务整合评分 |

### 6.3 数据源类 (data-source, 4个)

| # | 技能 ID | 名称 | 功能说明 |
|---|---------|------|----------|
| 1 | `tushare-data` | Tushare 数据研究 | 最详细的数据技能：9大意图分类+实体解析+10个工作流模板，覆盖行情/财务/估值/资金流/板块/宏观 |
| 2 | `tickflow` | TickFlow 实时行情 | SDK 使用指南：实时行情、日K/分钟K、财务数据、多市场(A股/港股/美股/期货) |
| 3 | `akshare` | AkShare 补充数据 | 最低优先级补充 provider，仅当 TickFlow/Tushare 不可用时兜底 |
| 4 | `data-routing` | 数据源路由 | 数据源选择决策树：TickFlow→Tushare→AkShare 层级路由 |

### 6.4 风险分析类 (risk-analysis, 4个)

| # | 技能 ID | 名称 | 功能说明 |
|---|---------|------|----------|
| 1 | `risk-analysis` | 风险分析 | 技术/基本面/市场/操作风险，输出保持研究性质不构成投资建议 |
| 2 | `risk-adjusted-return-optimizer` | 风险调整收益优化 | 核心卫星配置+仓位上限+再平衡+下行保护，含A股税费约束 |
| 3 | `portfolio-health-check` | 组合健康诊断 | 0-100健康评分，含6大历史压力测试(2015股灾/2018贸易摩擦等) |
| 4 | `ashare-pre-st-filter` | ST/退市风险预警 | 4维度风险筛查(营收/利润/净资产/审计)+低/中/高/极高风险等级 |

### 6.5 工具类 (tool, 4个)

| # | 技能 ID | 名称 | 功能说明 |
|---|---------|------|----------|
| 1 | `sats-market-assistant` | SATS 市场助手 | 项目主助手(优先级100)，解释 CLI/REPL 用法，基于注入数据回答大盘/板块/个股问题 |
| 2 | `suitability-report-generator` | 适当性报告 | 投资适当性和风险披露报告模板(7节：摘要→客户画像→投资理由→风险→适当性→假设→复评) |
| 3 | `report-generate` | 研究报告生成 | 6节 Markdown 研究报告结构模板(摘要/技术面/资金流/基本面/风险/评级) |
| 4 | `regulatory-knowledge` | 监管知识库 | 涨跌停/ST退市/T+1/融资融券/北交所差异/交易合规提醒 |

### 6.6 流程类 (flow, 2个)

| # | 技能 ID | 名称 | 功能说明 |
|---|---------|------|----------|
| 1 | `event-driven-detector` | 事件驱动识别 | 7类事件(并购重组/资产注入/回购增持/国企改革/指数调整/分拆/重大公告)+情景分析 |
| 2 | `corporate-events` | 公司事件分析 | 公告/业绩预告/定增/回购/增减持/并购/解禁/ST退市，利好/利空/中性分类+影响路径 |

### 6.7 工作流类 (workflow, 1个)

| # | 技能 ID | 名称 | 功能说明 |
|---|---------|------|----------|
| 1 | `workflow-templates` | 研究工作流模板 | 改写自 Vibe-Trading swarm presets，9个工作流：投研团队/投委会/量化策略/风控/组合审查/行业轮动/因子研究/基本面/技术面板 |

### 6.8 策略类补充 (strategy, 追加)

| # | 技能 ID | 名称 | 功能说明 |
|---|---------|------|----------|
| 1 | `minute-analysis` | 分钟级分析 | TickFlow 分钟K(1m/5m/15m/30m/60m)+日内趋势+VWAP+均线反应 |
| 2 | `volatility` | 波动率分析 | ATR/历史波动/布林带宽度/量价异常判断波动状态(扩大/蓄势/放量) |
| 3 | `candlestick` | 蜡烛图形态 | 十字星/锤头/射击之星/吞没/早晨星/黄昏星识别，需结合趋势位置+量能 |

### 6.9 技能架构要点

- **自动加载**: 高优先级技能(≥70)随 Agent 启动自动加载摘要到上下文
- **触发匹配**: 用户意图通过 triggers/name/aliases 匹配最相关技能
- **数据政策**: 所有技能强制要求价格/量能/K线数据来自 SATS 观测，禁止 LLM 编造
- **研究性质**: 所有技能输出"研究候选"而非"交易建议"，不直接发出买卖指令
- **分层路由**: 数据源技能形成 TickFlow→Tushare→AkShare 层级路由

---

## 七、知识库详细内容

> 知识库通过 `sats.rag.knowledge` 管理，支持文件导入、分块、检索。默认 9 个知识集合 + 用户自定义知识库。

### 7.1 默认知识集合 (9个)

| # | 集合名 | 标签 | 描述 | 包含路径 |
|---|--------|------|------|----------|
| 1 | `chan` | chan, 缠论 | 缠论规则、买卖点、中枢、背驰和风险控制 | `knowledge/chan/rules` |
| 2 | `technical` | technical, 技术指标 | 技术指标、K线、成交量、波动率和短线技术分析 | 12个技术类 SKILL.md |
| 3 | `price-action` | price-action, 短线纪律 | 阴线买入、开盘溢价率、量价关系、均线信号、520均线、RSI极值、趋势执行纪律、左右倍量、主力洗盘形态 | `knowledge/price_action` (9个文档) |
| 4 | `signals` | signals, 信号分析 | SATS 信号分析、筛选规则、缠论信号和机会发现 | 12个策略类 SKILL.md + signals + screening/rules |
| 5 | `sentiment` | sentiment, A股情绪 | A股市场情绪、热点板块、市场微结构和资金行为 | 8个情绪/分析类 SKILL.md |
| 6 | `market` | market, 大盘 | A股大盘、数据路由、行情数据源和市场助手 | 8个市场/数据类 SKILL.md |
| 7 | `fundamental` | fundamental, 基本面 | 基本面、财报、估值、财务筛选和公司事件 | 12个基本面类 SKILL.md |
| 8 | `risk` | risk, 风险 | 风险分析、监管知识和A股ST/退市/合规约束 | 6个风险类 SKILL.md |
| 9 | `stock-basic` | stock-basic, 股票列表 | Tushare/TickFlow stock_basic A股股票名称、代码、行业和交易所映射 | (动态生成) |

---

### 7.2 缠论知识库 (`knowledge/chan/rules/seed.json`)

15 种缠论规则卡片，来源于缠中说禅原著 PDF 提取。

| # | 规则 ID | 标签 | 方向 | 级别 | 定义 | 硬条件 | 风险提示 |
|---|---------|------|------|------|------|--------|----------|
| 1 | `chan_first_buy` | 一买 | buy | 操作级别 | 下跌趋势末端的底背驰买点，结构为下跌+盘整+下跌 | 同级别两段下跌比较、第二段创新低、MACD绿柱面积弱化 | 背驰后不必然V反，可能先扩展为盘整 |
| 2 | `chan_second_buy` | 二买 | buy | 操作/次级别 | 一买后次级别上涨结束，首次回抽结束的再确认买点 | 已有一买低点、一买后上行、首次回抽不破坏前低 | 弱二买可跌破一买低点并演化为大级别盘整 |
| 3 | `chan_third_buy` | 三买 | buy | 操作/次级别 | 次级别向上离开中枢后回试不跌回中枢上沿的买点 | 先形成中枢、次级别向上离开、回试低点不跌破ZG | 三买后可能形成更大级别中枢 |
| 4 | `chan_first_sell` | 一卖 | sell | 操作级别 | 上涨趋势末端的顶背驰卖点，一买的反向结构 | 同级别两段上涨比较、第二段创新高、MACD红柱面积弱化 | 宁愿卖早不要卖晚 |
| 5 | `chan_second_sell` | 二卖 | sell | 操作/次级别 | 一卖后下跌，首次次级别反抽结束的卖点 | 已有一卖高点、一卖后下行、首次反抽不突破前高 | 反抽若重新走强需等待更清晰卖点 |
| 6 | `chan_third_sell` | 三卖 | sell | 操作/次级别 | 次级别向下离开中枢后回抽不升回中枢下沿的卖点 | 先形成中枢、次级别向下离开、回抽高点不升破ZD | 中枢内震荡不等于三卖 |
| 7 | `chan_second_third_overlap` | 二三买重合 | buy | 操作/次级别 | 二买回抽确认同时构成原中枢的三买，较强走势 | 二买回抽成立、回抽不回中枢、位置重合或接近 | 强势结构也要设置前低和中枢上沿失效位 |
| 8 | `chan_center_oscillation_low` | 中枢低吸 | buy | 操作级别中枢 | 中枢震荡成立时下沿附近下探失败并收回的买点 | 中枢未被三卖破坏、下沿下探失败、力度弱于前一次 | 一旦出现三卖不再按中枢震荡回补 |
| 9 | `chan_center_oscillation_high` | 中枢高抛 | sell | 操作级别中枢 | 中枢震荡成立时上沿附近上攻失败并回落的卖点 | 中枢未被三买破坏、上沿上攻失败、力度弱于前一次 | 若形成有效三买不应按中枢高抛处理 |
| 10 | `chan_bottom_fractal_confirm` | 底分型确认 | buy | 分型代理 | 底分型区间不被跌破后站回上沿，底部构造成功代理 | 形成底分型区间、不跌破最低点、站住上沿 | 分型法比走势类型粗糙，应结合级别和区间套 |
| 11 | `chan_top_fractal_confirm` | 顶分型确认 | sell | 分型代理 | 顶分型区间不被突破后跌回下沿，顶部构造成立代理 | 形成顶分型区间、不突破最高点、跌回下沿 | 大级别卖点优先于小级别噪声 |
| 12 | `chan_interval_nesting` | 区间套定位 | hold | 多级别定位 | 从大级别背驰段向低级别递归定位精确买卖点 | 大级别处于背驰段、逐级下降寻找次级别一买/一卖 | 不要用太小级别信号对抗大级别走势 |

---

### 7.3 价量行为知识库 (`knowledge/price_action/`)

9 篇实战经验文档，来源于用户股票经验截图整理。

| # | 文件 | 标题 | 核心内容 |
|---|------|------|----------|
| 1 | `volume_price_relationship_patterns.md` | 量价关系七种情形 | 放量横盘/缩量横盘/放量上涨/缩量上涨/放量下跌/缩量下跌/量价背离，核心原则：同时看价格方向+成交量方向 |
| 2 | `moving_average_520_discipline.md` | 520均线战法纪律 | 5日+20日均线系统，20日线向上时金叉回踩信号才有效，20日线向下时所有短线信号默认忽略，含止损止盈和仓位管理 |
| 3 | `moving_average_signal_patterns.md` | 8种均线信号 | 5/10/20/60日均线含义，金叉/死叉/粘合/多头发散/缩量阴线/放量阳线等8种信号识别 |
| 4 | `rsi_extreme_reversal_discipline.md` | RSI极值反转与执行纪律 | RSI低于20/高于80的极端情绪识别，超买超卖+趋势过滤+量能验证+顶底背离+反人性执行纪律 |
| 5 | `trend_execution_five_disciplines.md` | 趋势执行五条纪律 | 趋势确认、主升浪首次分歧、强势板块龙头、缩量回踩、量比过滤、RPS强度、破位离场 |
| 6 | `left_right_double_volume_discipline.md` | 左倍量与右倍量纪律 | 左倍量抄底(底部放量吸筹)、右倍量逃顶(高位放量出货)，量在价先，识别主力资金进出痕迹 |
| 7 | `main_force_washout_patterns.md` | 主力洗盘形态识别 | 回踩均线洗盘、假跌破支撑洗盘、大阴线洗盘、平台整理洗盘、量价背离洗盘、缩量洗盘等10种形态，先看趋势再看量能最后看是否收回关键位 |
| 8 | `opening_premium_retention.md` | 开盘溢价率去留判断 | 涨停后持仓去留框架：开盘溢价率=(开盘价-昨收)/昨收×100%，≥5%强、0~5%正常、<0%弱，结合盘中走势和成交量确认 |
| 9 | `retail_candlestick_discipline.md` | 阴线买入与交易纪律 | 阴线买入四个铁门槛(趋势+量能+空间+支撑)，仓位控制、止损止盈和交易禁区 |

---

### 7.4 知识库管理命令

```bash
# 列出知识库
sats knowledge list

# 新建知识库
sats knowledge add --name my-research --description "我的研究" --tags 研究

# 导入文件
sats knowledge ingest --knowledge chan --path ./docs/缠论.pdf

# 导入目录
sats knowledge ingest --knowledge price-action --path ./knowledge/price_action/

# 搜索
sats knowledge search --query 三买定义 --knowledge chan --limit 10

# 同步股票基础数据
sats knowledge sync-stock-basic
```

---

## 八、核心模块详解

### 8.1 数据层 (`sats/data/`)

#### 架构：Facade + Provider 模式

```
AStockDataProvider (统一门面)
    ├── TickFlowDataProvider (实时行情主力)
    ├── TushareDataProvider (基本面/日线主力)
    └── AkShareDataProvider (可选补充)
```

| 文件 | 行数 | 职责 |
|------|------|------|
| `base.py` | 58 | `MarketDataProvider` ABC 定义 |
| `astock_provider.py` | 1097 | 统一门面，懒加载 + 失败缓存 + 降级级联 |
| `tickflow_provider.py` | ~1400 | TickFlow SDK 适配器，含限流器 |
| `tushare_provider.py` | ~1600 | Tushare 适配器，数据缓存写入 DuckDB |
| `akshare_provider.py` | 490 | AkShare 适配器，安全参数过滤 |
| `akshare_datasets.py` | ~1500+ | AkShare 数据集目录 (数百个端点) |
| `tushare_stock_datasets.py` | 314 | Tushare 数据集目录 (~125个) |
| `resolver.py` | 347 | DuckDB 优先数据解析器 |
| `provider_capabilities.py` | 301 | 供应商能力目录 |
| `limit_sentiment.py` | 70 | 涨跌停情绪计算器 |

**关键设计**:
- **懒加载 + 失败缓存**: Provider 首次失败后永久禁用
- **降级级联**: TickFlow → Tushare → AkShare → DuckDB 缓存
- **数据溯源**: `frame.attrs["market_data_provenance"]`
- **扩展上下文**: `load_statement_context()`, `load_company_news_context()`, `load_holder_activity_context()`, `load_social_sentiment_context()`

### 8.2 选股规则引擎 (`sats/screening/`)

13 个内置规则 + AI 生成规则目录 (`rules/generated/`)。

#### 规则总览

| # | 规则 ID | 中文名 | 核心逻辑 | 数据窗口 |
|---|---------|--------|----------|----------|
| 1 | `price_volume_ma` | 价量均线 | 非ST+涨幅3-5%+量比>1+换手5-10%+MA多头 | 60日 |
| 2 | `ma_volume_relative_strength` | 均量相对强度 | 3日连阳+MA多头+量比1.2-2.0+平台突破 | 60日 |
| 3 | `chan_third_buy` | 缠论三买 | 日线箱体突破+30分钟MACD确认 | 37日+30m |
| 4 | `chan_composite` | 缠论综合 | 一买+二买+三买+中枢低吸 组合评估 | 85日+30m |
| 5 | `chan_signals` | 缠论信号 | 委托缠论引擎评估15种买卖信号 | 60日+30m |
| 6 | `monthly_base_breakout` | 月线底部突破 | 24-96月长期底部形态+颈线突破 | 60月 |
| 7 | `turtle_trade` | 海龟交易 | 20日新高突破+成交额≥1亿+阳线 | 21日 |
| 8 | `ma_volume` | 均线放量 | MA5金叉MA20+成交量>1.5倍MA20 | 20日 |
| 9 | `high_tight_flag` | 高位紧凑旗形 | 40日动量>60%+10日盘整<15%+缩量 | 40日 |
| 10 | `limit_up_shakeout` | 涨停洗盘 | 昨涨停+今日放量阴线+支撑在昨收 | 3日 |
| 11 | `uptrend_limit_down` | 上升趋势跌停 | MA20>MA60趋势中跌停+量>2倍 | 60日 |
| 12 | `rps_breakout` | 相对强度突破 | RPS≥90+价格≥90% of 120日高 | 121日 |
| 13 | `signal_composite` | 信号综合 | 桥接40+信号分析框架 | 可变 |

---

#### 规则 1: `price_volume_ma` — 价量均线

**文件**: `sats/screening/rules/price_volume_ma.py` (295行)

**策略逻辑**: 寻找价格温和上涨、成交量适中、均线多头排列的健康趋势股。

**筛选条件** (全部满足才通过):

| 条件 | 阈值 | 说明 |
|------|------|------|
| `not_st` | — | 排除 ST 股票 (名称含 "ST") |
| `not_bse` | — | 排除北交所 (代码 43/81/82/83/87/88/92 开头或 .BJ 后缀) |
| `data_window_60` | ≥60日 | 至少60个交易日以计算 MA60 |
| `daily_trade_date_current` | — | 最新日线数据为请求交易日 |
| `pct_chg_3_to_5` | 3%~5% | 当日涨幅在 3% 到 5% 之间 |
| `volume_ratio_gt_1` | >1.0 | 量比 (当日成交量/前5日均量) 大于 1.0 |
| `turnover_rate_5_to_10` | 5%~10% | 换手率在 5% 到 10% 之间 |
| `circ_mv_50_to_200_yi` | 50亿~200亿 | 流通市值在 50 万万 (50亿) 到 200 万万 (200亿) 之间 |
| `ma_bull_stack_5_10_20_60` | MA5>MA10>MA20>MA60 | 均线多头排列 |

**评分**: 基础分 = min(70, 匹条件数×10) + 涨幅加分(≤10) + 量比加分(≤10) + 换手率加分(5) - 失败扣分(≤30)。满分 100。

**特殊**: 支持预计算模式 — Tushare Provider 可在数据加载阶段预筛选，跳过重复计算。

---

#### 规则 2: `ma_volume_relative_strength` — 均量相对强度

**文件**: `sats/screening/rules/ma_volume_relative_strength.py` (278行)

**策略逻辑**: 寻找连续上涨、均线多头、量比适中、收盘价在日内高位的相对强势股。

**筛选条件**:

| 条件 | 阈值 | 说明 |
|------|------|------|
| `close_above_ma5_3d` | 连续3日 | 最近3日收盘价均在 MA5 之上 |
| `bullish_days_3_of_4` | ≥3/4 | 最近4日中至少3日收阳 (close>open) |
| `three_day_gain_lte_9pct` | ≤9% | 3日累计涨幅不超过 9% (避免追高) |
| `ma5_bias_lte_4pct` | 0~4% | 收盘价偏离 MA5 不超过 4% |
| `latest_close_upper_half` | ≥0.5 | 收盘价在日内振幅的上半部分 |
| `volume_ratio_1p2_to_2_or_breakout` | 1.2~2.0 (普通) 或 1.2~2.5 (突破) | 量比适中；平台突破时允许更高量比 |
| `positive_day` | >0% | 当日收涨 |
| `ten_day_gain_lte_18pct` | ≤18% | 10日累计涨幅不超过 18% |
| `ma_bull_stack_5_10_20_60` | MA5>MA10>MA20>MA60 | 均线多头排列 |

**平台突破检测**: 价格突破20日最高价，且前20日振幅≤18%。

**评分**: 基础分 = min(70, 匹条件数×6) + 收盘位置加分(8) + 量比分级加分(5~8) + 平台突破加分(5) - 失败扣分(≤30)。

---

#### 规则 3: `chan_third_buy` — 缠论三买

**文件**: `sats/screening/rules/chan_third_buy.py` (378行)

**策略逻辑**: 缠论三买 = 整理箱体突破后回抽不跌回箱体，日线+30分钟双确认。

**日线条件**:

| 条件 | 阈值 | 说明 |
|------|------|------|
| `box_amplitude_lte_20pct` | ≤20% | 箱体振幅不超过 20% |
| `breakout_volume_ratio_gte_1p2` | ≥1.2 | 突破日量比不低于 1.2 |
| `pullback_days_gte_2` | ≥2日 | 回抽至少 2 个交易日 |
| `pullback_holds_box` | ≥箱顶×0.99 | 回抽最低价不跌回箱体 (允许1%容差) |
| `latest_close_near_or_above_box` | ≥箱顶×0.99 | 最新收盘价在箱顶附近或之上 |
| `ten_day_gain_lte_25pct` | ≤25% | 10日涨幅不超过 25% |
| `ma20_bias_lte_12pct` | ≤12% | 收盘价偏离 MA20 不超过 12% |

**30分钟确认条件**:

| 条件 | 说明 |
|------|------|
| `minute_pullback_holds_box` | 30分钟回抽低点不跌回箱顶 |
| `minute_close_above_ma5` | 最新30分钟收盘价在 MA5 之上 |
| `minute_macd_hist_improving` | MACD 柱状线从低点回升 |

**评分**: 基础分 = min(70, 匹条件数×5) + 全通过加分(10) + 量比分级加分(3~6) + MA20偏离适中加分(6) + MACD改善加分(≤8) - 失败扣分(≤35)。

---

#### 规则 4: `chan_composite` — 缠论综合

**文件**: `sats/screening/rules/chan_composite.py` (590行)

**策略逻辑**: 组合缠论5种买点结构的综合评估。

**5个子规则**:

| 子规则 | 中文 | 核心逻辑 |
|--------|------|----------|
| `chan_first_buy` | 一买 | A-B-C下跌结构 + MACD底背离 + 最新底部修复 |
| `chan_second_buy` | 二买 | 一买后反弹 + 回抽不破前低 + MACD改善 |
| `chan_third_buy` | 三买 | 委托 ChanThirdBuyRule (见规则3) |
| `chan_center_oscillation_low` | 中枢低吸 | 价格探底箱体下沿后收回 + MACD改善 |
| `chan_second_third_overlap` | 二三买重合 | 二买和三买同时通过时的特殊加分 |

**评分**: 取最高匹配子规则分数 + 多信号加分 + 重合加分。

---

#### 规则 5: `chan_signals` — 缠论信号

**文件**: `sats/screening/rules/chan_signals.py` (87行)

**策略逻辑**: 委托 `sats.chan.engine` 评估 15 种缠论信号。

**15种信号**: 一买/二买/三买/二三买重合/中枢低吸/一卖/二卖/三卖/中枢高抛/底分型确认/顶分型确认/持多级别/持空级别。

**输出**: `matched_chan_rules` (通过的信号标签)、`risk_flags` (风险标记)、`watch_levels` (观察价位)、`evidence_refs` (证据引用)。

**评分**: 最高信号分数 + 多信号加分(×3) - 买卖冲突扣分(12)。

---

#### 规则 6: `monthly_base_breakout` — 月线底部突破

**文件**: `sats/screening/rules/monthly_base_breakout.py` (397行)

**策略逻辑**: 识别月线级别的长期底部形态 (24-96个月) 并在突破颈线时入选。

**筛选条件**:

| 条件 | 说明 |
|------|------|
| `monthly_window_gte_60` | 至少60根月K线 |
| `neckline_touches_gte_2` | 颈线至少被触及2次 |
| `alternating_pivots_gte_5` | 至少5个高低交替的枢轴点 |
| `pullback_lows_gte_2` | 至少2个回撤低点 |
| `base_mostly_below_neckline` | ≥70% 的K线在颈线之下 |
| `early_or_confirmed_stage` | 处于"早期突破"(3-35%溢价) 或 "确认主升"(≥35%溢价+MA多头) |

---

#### 规则 7: `turtle_trade` — 海龟交易

**文件**: `sats/screening/rules/sequoia_x.py` (TurtleTradeRule)

**策略逻辑**: 经典海龟交易法则 — 20日新高突破。

| 条件 | 说明 |
|------|------|
| `close_breaks_prior_20d_high` | 收盘价突破前20日最高价 |
| `amount_gte_100m` | 成交额≥1亿元 (单位:千元) |
| `bullish_body` | 阳线 (close>open) |
| `close_gt_previous_close` | 收盘价高于前日收盘 |

**评分**: 全部通过时 score = 100 + 流通市值/100万 (市值越大分越高)。

---

#### 规则 8: `ma_volume` — 均线放量

**文件**: `sats/screening/rules/sequoia_x.py` (MaVolumeRule)

**策略逻辑**: MA5 金叉 MA20 + 成交量显著放大。

| 条件 | 说明 |
|------|------|
| `ma5_crosses_above_ma20` | 前日 MA5<MA20 且今日 MA5>MA20 (金叉) |
| `volume_gt_1p5x_ma20` | 成交量 > 1.5 × 20日均量 |

---

#### 规则 9: `high_tight_flag` — 高位紧凑旗形

**文件**: `sats/screening/rules/sequoia_x.py` (HighTightFlagRule)

**策略逻辑**: 强势上涨后高位窄幅盘整 + 缩量，蓄势待发。

| 条件 | 说明 |
|------|------|
| `momentum_40d_gt_60pct` | 40日动量 > 60% (high40/low40 > 1.6) |
| `consolidation_10d_lt_15pct` | 10日盘振 < 15% (high10/low10 < 1.15) |
| `high_level_low10_gte_80pct_high40` | 10日最低价 ≥ 40日最高价的 80% (高位紧凑) |
| `volume_shrink_lt_0p6x_prior20` | 成交量 < 前20日均量的 60% (缩量) |

---

#### 规则 10: `limit_up_shakeout` — 涨停洗盘

**文件**: `sats/screening/rules/sequoia_x.py` (LimitUpShakeoutRule)

**策略逻辑**: 昨日涨停后今日放量阴线洗盘，但支撑位守住昨收。

| 条件 | 说明 |
|------|------|
| `yesterday_limit_up` | 昨日收盘涨幅 ≥ 9.5% (涨停) |
| `bearish_today` | 今日收阴 (close<open) |
| `volume_gt_2x_yesterday` | 今日成交量 > 2× 昨日 |
| `support_low_gte_yesterday_close` | 今日最低价 ≥ 昨日收盘价 (支撑有效) |

---

#### 规则 11: `uptrend_limit_down` — 上升趋势跌停

**文件**: `sats/screening/rules/sequoia_x.py` (UptrendLimitDownRule)

**策略逻辑**: MA20>MA60 上升趋势中的跌停洗盘机会。

| 条件 | 说明 |
|------|------|
| `previous_ma20_gt_ma60` | 前日 MA20 > MA60 (上升趋势) |
| `close_lte_90p5pct_previous_close` | 今日收盘 ≤ 前日收盘×0.905 (跌幅≥9.5%，跌停) |
| `volume_gt_2x_ma20` | 成交量 > 2× 20日均量 (恐慌放量) |

---

#### 规则 12: `rps_breakout` — 相对强度突破

**文件**: `sats/screening/rules/sequoia_x.py` (RpsBreakoutRule)

**策略逻辑**: 相对价格强度 (RPS) 排名前10% 且价格接近120日高点。

| 条件 | 说明 |
|------|------|
| `rps_gte_90` | 120日 RPS ≥ 90 (全市场排名前10%) |
| `close_gte_90pct_120d_high` | 收盘价 ≥ 120日最高价的 90% |

**特殊**: 需要 `prepare_inputs()` 做全市场 RPS 排名计算 (120日涨幅百分位)。`required_trade_days = 121`。

**评分**: 直接使用 RPS 值 (0-100)。

---

#### 规则 13: `signal_composite` — 信号综合

**文件**: `sats/screening/rules/signal_composite.py` (20行)

**策略逻辑**: 桥接 `sats.signals` 信号分析框架，评估 40+ 复合信号。

调用 `screening_result_from_signal_input()` 将信号分析结果转换为选股结果。

---

#### AI 生成规则 (`rules/generated/`)

**文件**: `sats/screening/rule_composer.py` (591行) + `sats/screening/generated_rule_runtime.py` (274行)

**流程**: 自然语言描述 → `RuleGenerationPlan` → Python 代码生成 → AST 验证 → 合成测试 → 用户确认 → 写入 `rules/generated/`

**支持的条件类型**:

| 条件类型 | 说明 |
|----------|------|
| `exclude_st` | 排除 ST |
| `exclude_bse` | 排除北交所 |
| `pct_chg_between` | 涨跌幅区间 |
| `volume_ratio_gte/lte` | 量比上下限 |
| `close_above_ma` | 收盘价在均线之上 |
| `ma_stack` | 均线多头排列 |
| `turnover_between` | 换手率区间 |
| `circ_mv_between` | 流通市值区间 |
| `daily_basic_max/min` | 基本面指标上下限 |
| `relative_strength_gte` | 相对强度下限 |
| `breakout_high` | 突破N日高点 |
| `range_position_lte` | 日内收盘位置 |
| `min_daily_rows` | 最少日线数量 |

**安全限制**: 禁止 news/sentiment/chips/minute/depth/moneyflow 作为硬性条件 (不在 ScreeningInput 中)。

### 8.3 信号分析引擎 (`sats/signals/`)

40+ 复合信号，涵盖缠论、均线、K线形态、谐波形态、趋势形态。

| 文件 | 职责 |
|------|------|
| `engine.py` (1050行) | 信号分析引擎，`analyze_signal_input/inputs()` |
| `base.py` | `SignalDefinition`, `SignalEvent`, `SignalInput`, `SignalAnalysisResult` |
| `registry.py` | 信号定义注册表，`SIGNAL_DEFINITIONS`, `COMPOSITE_DEFINITIONS`, `GROUP_ALIASES` |

### 8.4 存储层 (`sats/storage/`)

**30+ 张 DuckDB 表**:

| 域 | 表名 |
|----|------|
| 市场数据 | `stock_daily`, `stock_daily_basic`, `stock_basic`, `industry_daily`, `stock_minute_cache`, `realtime_quote_cache` |
| 板块 | `sector_basic`, `sector_daily`, `sector_members` |
| 基本面 | `stock_moneyflow`, `stock_fundamentals` |
| 选股 | `screening_results` |
| 因子 | `factor_runs`, `factor_candidates` |
| 聊天 | `chat_sessions`, `chat_messages`, `chat_turns`, `chat_turn_events`, `chat_turn_items`, `chat_artifacts`, `chat_pending_actions` |
| 知识库 | `chat_memories`, `knowledge_bases`, `knowledge_files`, `knowledge_file_links`, `knowledge_chunks` |
| Web 缓存 | `web_documents`, `web_chunks`, `web_chunk_embeddings` |
| 监控 | `monitor_positions`, `monitor_watchlist`, `monitor_buy_candidates`, `monitor_events`, `monitor_trade_events`, `monitor_runtime` |
| 监控计划 | `monitor_plans`, `monitor_plan_items`, `monitor_plan_trigger_groups`, `monitor_plan_trigger_state` |
| 交易 | `broker_accounts`, `broker_positions`, `broker_orders`, `broker_trades`, `broker_order_events` |
| 调度 | `scheduled_tasks`, `scheduled_task_runs` |
| 历史 | `interaction_history` |

### 8.5 LLM 层 (`sats/llm/`)

13 个 LLM 供应商: `openai`, `openrouter`, `deepseek`, `gemini`, `groq`, `dashscope`/`qwen`, `zhipu`, `moonshot`, `minimax`, `mimo` (小米 MiMo), `zai`, `ollama`

| 文件 | 职责 |
|------|------|
| `provider.py` | 供应商注册表 + `build_llm()` 工厂 + `ChatOpenAIWithReasoning` |
| `model_config.py` | 模型 Profile 发现/解析/持久化 |
| `chat.py` | `ChatLLM` (sync/async/stream) + `LightFallbackChatLLM` |

### 8.6 分析层 (`sats/analysis/`)

| 文件 | 行数 | 职责 |
|------|------|------|
| `dsa_native.py` | 1251 | 原生 DSA 分析管线 |
| `opportunity_discovery.py` | 1955 | 短线机会发现管线 |
| `stock_picking_agent.py` | 1500 | 自然语言选股 Agent |
| `trading_committee.py` | 1112 | 多分析师辩论系统 |
| `market_llm_context.py` | — | 市场级 LLM 上下文 |
| `stock_llm_context.py` | — | 个股级 LLM 上下文 |
| `chan_llm_review.py` | — | 缠论 LLM 审查 (RAG) |

### 8.7 Agent 框架 (`sats/agent/`)

| 文件 | 行数 | 职责 |
|------|------|------|
| `runtime.py` | 554 | Agent 执行循环 |
| `planner.py` | 1499 | LLM 规划器 |
| `synthesis.py` | 1497 | 结果综合 |
| `trading.py` | 177 | 交易执行器 |
| `models.py` | 132 | Agent 类型定义 |
| `date_policy.py` | — | 日期策略 |
| `progress.py` | — | Agent 进度 |
| `python_runtime.py` | — | 受限 Python 执行 |

**10 个工具模块**: `chat_tools`, `command_tools`, `data_tools`, `factor_tools`, `research_tools`, `trade_tools`, `web_tools`, `workflow_tools`

### 8.8 其他模块

| 模块 | 职责 |
|------|------|
| `sats/backtesting/` | 回测服务 (策略规格验证、轻量回测) |
| `sats/deep_analysis/` | 深度分析 (分阶段管道: collect→score→panel→report) |
| `sats/api/` | FastAPI HTTP (5 端点) |
| `sats/scheduler/` | 定时任务 (仅 CLI/Chat，禁止 shell) |
| `sats/progress.py` | 统一进度面板 |
| `sats/symbols.py` | 股票代码标准化 |
| `sats/memory.py` | 聊天长期记忆 |
| `sats/skills.py` | 技能系统 (52个 YAML) |
| `sats/indicators/` | 技术指标计算 |
| `sats/chan/` | 缠论引擎 (15种买卖信号) |
| `sats/rag/` | RAG 知识检索 (5个默认知识库) |
| `sats/web/` | 网络搜索 + 社交热榜 + Web RAG |
| `sats/monitoring/` | 实时监控 + 计划系统 |
| `sats/trading/` | QMT 券商交易 + 仓位同步 |

---

## 九、关键架构模式

| 模式 | 说明 |
|------|------|
| **单一入口** | CLI/REPL/Chat 共用 `cli.main(argv)` |
| **Agent-first 路由** | 默认对话走 Agent 循环，`--no-agent` 降级 |
| **DuckDB 万能存储** | 30+ 张表覆盖所有数据域 |
| **数据门面** | `AStockDataProvider` 统一入口 |
| **输入边界标准化** | `sats.symbols` 标准化股票代码 |
| **LLM 数据政策** | 禁止 LLM 编造数据 |
| **优雅降级** | LLM→本地规则；Provider→降级级联 |
| **进度系统** | TTY 有 UI，非 TTY 静默 |
| **AI 规则生成** | 自然语言→AST 验证→合成测试→确认 |
| **多阶段管道** | deep-analysis 支持 collect/score/panel/report 分阶段 |
| **多分析师辩论** | trading-committee 模拟多空+风险+交易决策 |
| **安全边界** | 交易需 `--auto-trade`；调度禁止 shell；QMT 支持 dry-run |

---

## 十、测试覆盖

`tests/` 目录约 56 个测试文件:

| 类别 | 重点 |
|------|------|
| Agent | 规划器、运行时、综合、工具 |
| Chat | 运行时、审批、事件、产物、预处理、引用、组件 |
| 分析 | DSA、机会发现、选股 Agent、交易委员会、深度分析 |
| 缠论 | 所有规则、RAG 知识、LLM 审查 |
| 选股 | 规则评估、价量均线 |
| 数据源 | AStock/Tushare/TickFlow Provider |
| LLM | Provider 工厂、降级、模型路由 |
| 存储/API | DuckDB + FastAPI |
| 其他 | 回测、Web RAG、进度、调度、因子、信号、符号、监控、QMT 等 |
