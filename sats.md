# SATS 项目结构与功能详细解析

## 一、项目概述

**SATS (Stock Automated Trading System)** 是一个 Python 3.12+ 的 A 股（中国股市）选股、分析与交易辅助系统。集成了 LLM 驱动的分析、DuckDB 持久化存储、多数据源提供商（TickFlow / Tushare / AkShare）、FastAPI HTTP 服务、CLI 命令行、REPL 交互终端、定时调度器、自主 Agent 框架、实时监控、因子研究等能力。

**版本**: 0.1.0  
**入口**: `sats.cli:main`（控制台脚本 `sats`）

---

## 二、顶层目录结构

```
SATS/
├── sats/                          # 主包
│   ├── __init__.py                # 版本号 0.1.0
│   ├── __main__.py                # python -m sats 入口
│   ├── cli.py                     # CLI 命令行 (~3300行, 27个子命令)
│   ├── repl.py                    # REPL 交互终端 (~1500行)
│   ├── chat.py                    # LLM Chat 管线 (~1729行)
│   ├── config.py                  # .env 配置加载 (Settings dataclass)
│   ├── symbols.py                 # 股票代码标准化
│   ├── progress.py                # 统一进度条系统
│   ├── memory.py                  # 聊天长期记忆
│   ├── skills.py                  # 技能系统 (YAML定义)
│   ├── stock_question.py          # 股票问题意图识别
│   ├── data/                      # 市场数据层
│   ├── screening/                 # 选股规则引擎
│   ├── storage/                   # DuckDB 持久化
│   ├── llm/                       # LLM 供应商抽象
│   ├── analysis/                  # 后选股分析 & LLM Review
│   ├── agent/                     # 自主 Agent 框架
│   ├── api/                       # FastAPI HTTP API
│   ├── scheduler/                 # 定时任务调度
│   ├── indicators/                # 技术指标计算器
│   ├── signals/                   # 信号分析引擎
│   ├── factors/                   # 因子研究系统
│   ├── chan/                       # 缠论引擎
│   ├── rag/                       # RAG 知识检索
│   ├── web/                       # 网络搜索 & 社交热榜
│   ├── monitoring/                # 实时监控服务
│   └── trading/                   # 交易集成 (QMT)
├── skills/                        # 技能定义文件 (YAML/SKILL.md)
├── tests/                         # 测试 (~50个文件)
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

## 四、所有命令详细解析 (27个)

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
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |

**示例：**
```bash
# 使用默认规则 (ma_volume_relative_strength) 筛选
sats screen --trade-date 20260612

# 使用缠论综合规则筛选
sats screen --rule chan_composite --trade-date 20260612

# 筛选并导入到 watchlist
sats screen --trade-date 20260612 --select-watchlist

# 指定自定义数据库路径
sats screen --trade-date 20260612 --db /tmp/test.duckdb
```

---

### 4.3 `results` — 查询选股结果

从 DuckDB 查询已保存的选股结果。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--trade-date` | str | None | 否 | 按交易日筛选 |
| `--rule` | str | None | 否 | 按规则名称筛选 |
| `--passed` | flag | False | 否 | 仅显示通过的记录 |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |

**示例：**
```bash
# 查看所有选股结果
sats results

# 查看特定交易日的结果
sats results --trade-date 20260612

# 仅查看通过的记录
sats results --trade-date 20260612 --passed

# 按规则筛选
sats results --rule chan_composite --passed
```

---

### 4.4 `result-rules` — 列出选股规则名

列出 DuckDB 中已保存的选股规则名称。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |

**示例：**
```bash
sats result-rules
```

---

### 4.5 `quote` — 实时行情

显示实时行情 + 均线 (MA5, MA20, MA60, MA250)。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--stocks` | str | — | **是** | 逗号分隔的股票代码或中文名称 |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |

**示例：**
```bash
# 单只股票
sats quote --stocks 000001

# 多只股票，混合代码格式
sats quote --stocks 000001,600519.SH,300750

# 使用中文名称
sats quote --stocks 紫光股份,贵州茅台

# 使用 .SZ 后缀
sats quote --stocks 000001.SZ
```

---

### 4.6 `analyze` — 统一信号分析

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
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |

**示例：**
```bash
# 分析指定股票
sats analyze --stocks 000001,600519.SH

# 分析选股结果中的股票
sats analyze --from-screened --trade-date 20260612 --rule chan_composite

# 仅分析技术类信号
sats analyze --stocks 300750 --signals technical

# 列出所有信号策略定义
sats analyze signals

# 带 LLM 审查
sats analyze --stocks 000001 --llm-review

# 不生成报告
sats analyze --stocks 000001 --noreport
```

---

### 4.7 `analyze-dsa` — 外部 DSA 分析桥接

调用外部 `daily_stock_analysis` 工具进行分析。`--stocks` 和 `--rule` 不能同时使用。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD，默认最近交易日 |
| `--rule` | str | None | 否 | 选股规则名称 |
| `--stocks` | str | None | 否 | 逗号分隔的股票代码或名称 |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |

**示例：**
```bash
# 分析指定股票
sats analyze-dsa --stocks 000001,600519

# 分析选股结果
sats analyze-dsa --rule chan_composite --trade-date 20260612
```

---

### 4.8 `dsa` — SATS 原生 DSA 分析

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
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |

**示例：**
```bash
# 分析指定股票
sats dsa --stocks 000001

# 分析选股结果
sats dsa --from-screened --trade-date 20260612 --rule chan_composite

# 仅本地评分，不调用 LLM
sats dsa --stocks 000001 --no-llm

# 显示评分原因
sats dsa --stocks 000001 --explain-rating

# 设置 LLM 超时为 30 秒
sats dsa --stocks 000001 --llm-timeout 30
```

---

### 4.9 `analyze-chan` — 缠论 LLM 审查

缠论选股结果的 LLM 智能审查。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD，默认最近交易日 |
| `--rule` | str | None | 否 | 选股规则名称过滤 |
| `--chan-rule` | str | `chan_third_buy` | 否 | 缠论规则变体: `chan_third_buy`, `chan_composite`, `chan_signals` |
| `--top` | int | 20 | 否 | 最大审查候选数量 |
| `--stocks` | str | None | 否 | 逗号分隔的股票代码或名称 (临时缠论评估) |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |

**示例：**
```bash
# 审查选股结果中的缠论三买
sats analyze-chan --trade-date 20260612

# 使用缠论综合规则，审查前10名
sats analyze-chan --chan-rule chan_composite --top 10

# 对指定股票进行临时缠论评估
sats analyze-chan --stocks 000001,600519 --chan-rule chan_signals
```

---

### 4.10 `chan-kb` — 缠论知识库搜索

搜索本地缠论知识卡片 (RAG)。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `search` | `query` (位置参数, 剩余) | 搜索关键词 |

**示例：**
```bash
# 搜索三买相关知识
sats chan-kb search 三买定义

# 搜索中枢相关
sats chan-kb search 中枢震荡
```

---

### 4.11 `discover` — 短线机会发现

短线 A 股机会发现。有自然语言 query 时走 LLM 选股 Agent，无 query 时走纯本地信号排序。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD，默认最近交易日 |
| `--signals` | str | `DEFAULT_DISCOVERY_SIGNALS` | 否 | 分析信号组或 ID |
| `--limit` | int | None | 否 | 最终股票数量 |
| `--candidate-limit` | int | `DEFAULT_CANDIDATE_LIMIT` | 否 | 发送给 LLM 的本地候选数量 |
| `--lookback-days` | int | 180 | 否 | 历史回溯交易天数 |
| `--hot-sector-days` | int | 5 | 否 | 热门板块回溯天数，可选 3/4/5 |
| `--no-hot-sector` | flag | False | 否 | 禁用热门板块权重加成 |
| `--json` | flag | False | 否 | 输出完整 JSON |
| `--noreport` | flag | False | 否 | 不生成 Markdown 报告 |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |
| `query` | positional | None | 否 | 自然语言选股请求 (剩余参数) |

**示例：**
```bash
# 纯本地信号发现
sats discover --trade-date 20260612

# 限制输出 10 只
sats discover --limit 10

# 禁用热门板块权重
sats discover --no-hot-sector

# 使用自然语言 LLM 选股
sats discover 寻找半导体板块低位放量突破的股票

# 带热门板块 3 天回溯
sats discover --hot-sector-days 3 --limit 15
```

---

### 4.12 `chat` — LLM 对话

LLM 对话接口，默认走 Agent 路由。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--no-memory` | flag | False | 否 | 禁用本地聊天记忆 |
| `--knowledge` | str | None | 否 | 指定知识库名称/ID |
| `--no-agent` | flag | False | 否 | 禁用 Agent 路由，使用纯 LLM 聊天 |
| `--confirm` | str | None | 否 | 确认待处理的运行时动作 (action_id) |
| `--reject` | str | None | 否 | 拒绝待处理的运行时动作 (action_id) |
| `--trace` | str | None | 否 | 显示聊天轮次追踪 (turn_id) |
| `--auto-trade` | str | `""` | 否 | 启用的交易动作: `buy`, `sell` (逗号分隔) |
| `--broker` | choice | `noop` | 否 | 交易券商: `noop`, `qmt` |
| `--live-trading` | flag | False | 否 | 允许 QMT 实盘下单 |
| `--max-order-value` | float | 20000.0 | 否 | 最大买入金额 |
| `--max-position-pct` | float | 0.2 | 否 | 最大持仓占比 (占总资产) |
| `--sell-ratio` | float | 1.0 | 否 | 卖出信号的卖出比例 |
| `--max-iterations` | int | 6 | 否 | Agent 最大步骤数 |
| `--command-timeout` | int | 120 | 否 | 单个 SATS 命令超时秒数 |
| `--python-timeout` | int | 30 | 否 | 受限 Python 执行超时秒数 |
| `message` | positional | — | 否 | 发送给 LLM 的消息 (剩余参数) |

**示例：**
```bash
# 普通对话
sats chat 今天大盘走势怎么样

# 纯 LLM 聊天 (不走 Agent)
sats chat --no-agent 解释一下 MACD 指标

# 带知识库的对话
sats chat --knowledge chan-theory 三买的定义是什么

# 确认待处理的 Agent 动作
sats chat --confirm action_001

# 启用自动买入
sats chat --auto-trade buy --broker qmt 买入 100 股平安银行

# 查看聊天追踪
sats chat --trace turn_20260612_001
```

---

### 4.13 `agent` — 显式 Agent 任务

通过自主 Agent 执行自然语言目标。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--auto-trade` | str | `""` | 否 | 启用的交易动作: `buy`, `sell` (逗号分隔) |
| `--broker` | choice | `noop` | 否 | 交易券商: `noop`, `qmt` |
| `--live-trading` | flag | False | 否 | 允许 QMT 实盘下单 |
| `--max-order-value` | float | 20000.0 | 否 | 最大买入金额 |
| `--max-position-pct` | float | 0.2 | 否 | 最大持仓占比 |
| `--sell-ratio` | float | 1.0 | 否 | 卖出比例 |
| `--max-iterations` | int | 6 | 否 | Agent 最大步骤数 |
| `--command-timeout` | int | 120 | 否 | 单个命令超时秒数 |
| `--python-timeout` | int | 30 | 否 | Python 执行超时秒数 |
| `message` | positional | — | **是** | 自然语言 Agent 目标 (剩余参数) |

**示例：**
```bash
# 简单研究任务
sats agent 帮我分析贵州茅台最近的技术面

# 多步任务
sats agent 今天有哪些热点板块，选出每个板块的龙头股

# 限步任务
sats agent --max-iterations 3 筛选今天涨停板的股票
```

---

### 4.14 `web` — 网络搜索 & 社交热榜

公网搜索、社交平台热榜、关键词提及追踪。

#### 子命令 `web search`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--limit` | int | 5 | 否 | 最大结果数 |
| `--trusted-domains` | str | `""` | 否 | 逗号分隔的可信域名提示 |
| `--freshness` | choice | `""` | 否 | 时效过滤: `d`(天), `w`(周), `m`(月), `y`(年) |
| `--json` | flag | False | 否 | 输出 JSON |
| `query` | positional (≥1) | — | **是** | 搜索关键词 |

#### 子命令 `web hot`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--platforms` | str | `"all"` | 否 | 逗号分隔的平台或 `all`。支持: `weibo`, `zhihu`, `baidu`, `douyin`, `toutiao`, `bilibili`, `xueqiu_stock`, `xueqiu_spot` |
| `--limit` | int | 20 | 否 | 每个平台返回条数 |
| `--json` | flag | False | 否 | 输出 JSON |

#### 子命令 `web mentions`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--keyword` | str | — | **是** | 股票名称、公司名或话题关键词 |
| `--platforms` | str | `"all"` | 否 | 逗号分隔的平台或 `all` |
| `--limit` | int | 50 | 否 | 匹配前每个平台的最大条数 |
| `--json` | flag | False | 否 | 输出 JSON |

**示例：**
```bash
# 网页搜索
sats web search A股半导体行业分析

# 限定时效
sats web search --freshness w 新能源汽车政策

# 限定可信域名
sats web search --trusted-domains sse.com.cn,szse.cn 注册制改革

# 查看微博热搜
sats web hot --platforms weibo --limit 30

# 全平台热榜
sats web hot

# 关键词提及追踪
sats web mentions --keyword 贵州茅台

# 追踪特定平台
sats web mentions --keyword 宁德时代 --platforms xueqiu_stock,zhihu
```

---

### 4.15 `model` — 模型管理

查看和切换 LLM 模型配置。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `status` | — | 显示当前活跃的 main/light 模型 |
| `list` | — | 列出所有已配置的模型 Profile |
| `use <profile>` | `--target` (choice: `main`, `light`, `both`; 默认 `main`) | 切换默认模型 Profile |

**示例：**
```bash
# 查看当前模型
sats model status

# 列出所有配置
sats model list

# 切换默认模型为 DeepSeek
sats model use DEEPSEEK

# 同时切换 main 和 light 模型
sats model use XIAOMIMIMO --target both
```

---

### 4.16 `memory` — 聊天记忆管理

本地聊天长期记忆 CRUD，存储在 DuckDB。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | — | 列出活跃记忆 |
| `search <query>` | `query` (位置参数, 剩余) | 搜索记忆 |
| `forget <memory_id>` | `memory_id` (位置参数) | 归档指定记忆 |
| `clear` | `--yes` (flag) | 清除所有聊天记忆 |

**示例：**
```bash
# 列出所有记忆
sats memory list

# 搜索记忆
sats memory search 缠论三买

# 忘记指定记忆
sats memory forget mem_20260612_001

# 清除所有记忆
sats memory clear --yes
```

---

### 4.17 `history` — 交互历史

查询 REPL 交互历史记录，存储在 DuckDB。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | `--kind` (choice: `chat`, `command`), `--limit` (int, 默认 20, 上限 100), `--db` | 列出历史记录 |
| `search <query>` | `query` (位置参数, ≥1), `--kind`, `--limit`, `--db` | 搜索历史 |
| `show <history_id>` | `history_id` (位置参数), `--db` | 查看单条记录详情 |
| `delete <history_id>` | `history_id` (位置参数), `--db` | 软删除单条记录 |

**示例：**
```bash
# 列出最近 20 条
sats history list

# 仅列出命令类交互
sats history list --kind command --limit 50

# 搜索历史
sats history search DSA分析

# 查看详情
sats history show hist_20260612_001

# 删除记录
sats history delete hist_20260612_001
```

---

### 4.18 `knowledge` — RAG 知识库管理

管理本地 RAG 知识库。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | — | 列出知识库 |
| `add` | `--name` (必填), `--description` (默认 `""`), `--tags` (默认 `""`) | 新建/更新知识库 |
| `ingest` | `--knowledge` (必填), `--path` (Path, 必填), `--tags` (默认 `""`) | 导入文件或目录 |
| `search` | `--query` (必填), `--knowledge` (可选), `--limit` (int, 默认 6) | 搜索知识块 |
| `sync-stock-basic` | — | 同步 stock_basic 缓存到知识库 |

**示例：**
```bash
# 列出知识库
sats knowledge list

# 新建知识库
sats knowledge add --name chan-theory --description "缠论知识库" --tags 缠论,技术分析

# 导入文件
sats knowledge ingest --knowledge chan-theory --path ./docs/chan.pdf

# 导入目录
sats knowledge ingest --knowledge chan-theory --path ./docs/ --tags 缠论

# 搜索
sats knowledge search --query 三买定义 --knowledge chan-theory --limit 10

# 同步股票基础数据
sats knowledge sync-stock-basic
```

---

### 4.19 `indicators` — 技术指标

计算指定股票的日线技术指标。

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--stocks` | str | — | **是** | 逗号分隔的股票代码或名称 |
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD，默认最近交易日 |
| `--lookback-days` | int | 180 | 否 | 历史回溯天数 |
| `--json` | flag | False | 否 | 输出 JSON |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 数据库路径 |

**示例：**
```bash
# 计算平安银行的技术指标
sats indicators --stocks 000001

# 多只股票
sats indicators --stocks 000001,600519.SH,300750

# 指定日期和回溯窗口
sats indicators --stocks 000001 --trade-date 20260612 --lookback-days 360
```

---

### 4.20 `factor` — 因子研究 & 选股

因子分析全流程：列出因子 → 查看元数据 → IC 分析 → 多因子选股 → 机器学习训练/预测。

#### 子命令 `factor list`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--zoo` | choice | None | 否 | 因子动物园: `alpha101`, `gtja191`, `barra_style` |
| `--theme` | str | None | 否 | 因子主题: `value`, `volume`, `momentum` 等 |
| `--universe` | str | None | 否 | 股票池: `equity_cn` 等 |
| `--json` | flag | False | 否 | 输出 JSON |

#### 子命令 `factor show`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `factor_id` | positional | None | 否 | 因子 ID |
| `--factor` | str | None | 否 | 因子 ID (与 positional 二选一) |
| `--json` | flag | False | 否 | 输出 JSON |

#### 子命令 `factor analyze`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--factor` | str | — | **是** | 因子 ID，如 `gtja191_001` |
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD |
| `--lookback-days` | int | 260 | 否 | 历史回溯天数 |
| `--horizon` | int | 1 | 否 | 前瞻收益周期 |
| `--groups` | int | 5 | 否 | 分位数组数 |
| `--stocks` | str | None | 否 | 可选的股票代码/名称 |
| `--json` | flag | False | 否 | 输出 JSON |
| `--noreport` | flag | False | 否 | 不生成报告 |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 路径 |

#### 子命令 `factor pick`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--factors` | str | None | 否 | 逗号分隔的因子 ID (覆盖 --profile) |
| `--trade-date` | str | None | 否 | 交易日 YYYYMMDD |
| `--lookback-days` | int | 260 | 否 | 历史回溯天数 |
| `--horizon` | int | 1 | 否 | 前瞻收益周期 |
| `--top` | int | 20 | 否 | 选股数量 |
| `--neutralize` | choice | `none` | 否 | 中性化: `none`, `industry` |
| `--weight` | choice | `equal` | 否 | 因子权重: `equal`, `ic` |
| `--groups` | int | 5 | 否 | 分位数组数 |
| `--stocks` | str | None | 否 | 可选的股票代码/名称 |
| `--profile` | choice | `DEFAULT_FACTOR_PROFILE` | 否 | 因子组合 Profile |
| `--screening-profile` | str | `"multi_factor"` | 否 | 写入选股结果时的规则后缀 |
| `--write-screening` | flag | False | 否 | 将 TopN 写入 screening_results |
| `--json` | flag | False | 否 | 输出 JSON |
| `--noreport` | flag | False | 否 | 不生成报告 |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 路径 |

#### 子命令 `factor ml` (嵌套子命令)

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `status` | `--json` | 检查 Qlib/ML 依赖可用性 |
| `setup` | `--json` | 安装缺失的 ML 依赖 |
| `train` | `--profile`, `--factors`, `--model` (lightgbm/xgboost), `--train-start`, `--train-end`, `--valid-end`, `--horizon`, `--lookback-days`, `--stocks`, `--json`, `--db` | 训练因子 ML 模型 |
| `evaluate` | `--model-run` (必填), `--trade-date`, `--json`, `--db` | 查看模型运行指标 |
| `predict` | `--model-run` (必填), `--trade-date` (必填), `--profile`, `--factors`, `--top`, `--lookback-days`, `--stocks`, `--write-screening`, `--json`, `--db` | 用训练好的模型预测 TopN |

**示例：**
```bash
# 列出所有 alpha101 因子
sats factor list --zoo alpha101

# 查看因子元数据
sats factor show gtja191_001

# IC 分析
sats factor analyze --factor gtja191_001 --trade-date 20260612

# 多因子选股
sats factor pick --factors gtja191_001,gtja191_002 --top 15

# 使用 profile 选股
sats factor pick --profile balanced --write-screening

# 检查 ML 依赖
sats factor ml status

# 训练 LightGBM 模型
sats factor ml train --model lightgbm --train-start 20240101 --train-end 20260501

# 评估模型
sats factor ml evaluate --model-run run_20260601

# 预测
sats factor ml predict --model-run run_20260601 --trade-date 20260612 --top 10
```

---

### 4.21 `skills` — 技能列表

列出本地 SATS 技能。

| 参数 | 无 |

**示例：**
```bash
sats skills
```

---

### 4.22 `watchlist` — 监控列表管理

编辑监控列表 (被监控股票)。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | `--db` | 列出监控列表 |
| `add` | `--stocks` (必填), `--name` (默认 `""`), `--note` (默认 `""`), `--db` | 添加股票到监控列表 |
| `remove` | `--stocks` (必填), `--db` | 从监控列表移除 |
| `clear` | `--db` | 清空监控列表 |
| `select-delete` | `--db` | 交互式选择删除 |
| `import-screened` | `--trade-date` (必填), `--rule` (默认 `ma_volume_relative_strength`), `--db` | 从选股结果导入 |

**示例：**
```bash
# 列出监控列表
sats watchlist list

# 添加股票
sats watchlist add --stocks 000001,600519 --name "核心持仓"

# 移除股票
sats watchlist remove --stocks 000001

# 从选股结果导入
sats watchlist import-screened --trade-date 20260612 --rule chan_composite

# 清空
sats watchlist clear
```

---

### 4.23 `monitor` — 实时监控

实时监控股票，基于规则信号评估。

#### 子命令 `monitor positions`

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `add` | `--symbol` (必填), `--name` (默认 `""`), `--buy-price` (float, 必填), `--quantity` (float, 必填), `--buy-date` (默认 `""`), `--note` (默认 `""`), `--db` | 添加/更新持仓 |
| `list` | `--db` | 列出持仓 |
| `remove` | `--symbol` (必填), `--db` | 移除持仓 |

#### 子命令 `monitor watchlist`

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `add` | `--symbol` (必填), `--name` (默认 `""`), `--note` (默认 `""`), `--db` | 添加监控标的 |
| `list` | `--db` | 列出监控标的 |
| `remove` | `--symbol` (必填), `--db` | 移除监控标的 |

#### 子命令 `monitor buy-candidates`

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `list` | `--db` | 列出买入候选 |
| `remove` | `--symbol` (必填), `--db` | 移除买入候选 |

#### 子命令 `monitor start` / `monitor run`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--rules` | str | `chan_signals` | 监控规则 |
| `--lists` | str | `positions,watchlist` | 监控列表 (逗号分隔) |
| `--interval` | int | 60 | 轮询间隔秒数 |
| `--llm-review` | flag | False | 启用 LLM 审查 |
| `--broker` | choice | `noop` | 交易券商: `noop`, `qmt` |
| `--auto-trade` | str | `""` | 启用的交易动作: `buy`, `sell` |
| `--max-order-value` | float | 20000.0 | 最大买入金额 |
| `--max-position-pct` | float | 0.2 | 最大持仓占比 |
| `--sell-ratio` | float | 1.0 | 卖出比例 |
| `--once` | flag | False | 仅运行一轮 (仅 run) |
| `--db` | Path | SATS_DB_PATH | DuckDB 路径 |

#### 子命令 `monitor stop` / `monitor status`

| 参数 | 说明 |
|------|------|
| `--db` | DuckDB 路径 |

**示例：**
```bash
# 添加持仓
sats monitor positions add --symbol 000001 --buy-price 12.5 --quantity 500

# 列出持仓
sats monitor positions list

# 添加监控标的
sats monitor watchlist add --symbol 600519 --name 贵州茅台

# 前台运行监控
sats monitor run --rules chan_signals --interval 30

# 后台启动监控
sats monitor start --rules chan_signals

# 查看状态
sats monitor status

# 停止监控
sats monitor stop
```

---

### 4.24 `monitor-display` — 监控面板

终端监控面板 (curses 或纯文本)。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `start` | `--refresh` (int, 默认 3), `--new-terminal` (flag, macOS), `--db` | 启动面板 |
| `run` | `--refresh` (int, 默认 3), `--plain` (flag), `--db` | 前台运行面板 |
| `stop` | `--db` | 停止面板 |

**示例：**
```bash
# 在当前终端运行面板
sats monitor-display run

# 纯文本快照 (非 curses)
sats monitor-display run --plain

# macOS 在新终端窗口打开
sats monitor-display start --new-terminal

# 自定义刷新频率 (每 5 秒)
sats monitor-display run --refresh 5

# 停止
sats monitor-display stop
```

---

### 4.25 `schedule` — 定时任务

类 Cron 定时任务管理。仅可调度 SATS CLI 命令或聊天消息，不能执行任意 shell。时区为上海。

| 子命令 | 参数 | 说明 |
|--------|------|------|
| `add` | `--name` (必填), `--type` (choice: `cli`/`chat`, 必填), `--text` (必填), `--daily`/`--weekly` (互斥), `--days` (默认 `""`), `--time` (HH:MM, 必填), `--db` | 添加定时任务 |
| `list` | `--db` | 列出任务 |
| `runs` | `--limit` (int, 默认 20), `--name` (可选), `--db` | 查看运行记录 |
| `enable <name>` | `--db` | 启用任务 |
| `disable <name>` | `--db` | 禁用任务 |
| `remove <name>` | `--db` | 删除任务 |
| `run <name>` | `--db` | 立即运行任务 |
| `start` | `--interval` (int, 默认 30), `--db` | 后台启动调度器 |
| `run-loop` | `--interval` (int, 默认 30), `--once` (flag), `--db` | 前台运行调度器 |
| `stop` | `--db` | 停止调度器 |
| `status` | `--db` | 查看调度器状态 |

**示例：**
```bash
# 每天 9:30 运行选股
sats schedule add --name morning-screen --type cli --text "screen --trade-date today --rule chan_composite" --daily --time 09:30

# 每周一三五收盘后分析
sats schedule add --name afternoon-dsa --type cli --text "dsa --from-screened --trade-date today --rule chan_composite" --weekly --days mon,wed,fri --time 15:30

# 定时聊天
sats schedule add --name daily-report --type chat --text "帮我总结今天的市场表现" --daily --time 16:00

# 列出任务
sats schedule list

# 启动调度器
sats schedule start

# 查看运行记录
sats schedule runs --limit 10

# 立即运行
sats schedule run morning-screen
```

---

### 4.26 `qmt` — QMT 券商交易

MiniQMT/QMT 券商连接与交易。

#### 子命令 `qmt bridge run`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--host` | str | `127.0.0.1` | Bridge 监听地址 |
| `--port` | int | 8765 | Bridge 监听端口 |
| `--qmt-path` | str | `""` | QMT 安装路径 |
| `--account-id` | str | `""` | 资金账号 |
| `--account-type` | str | `STOCK` | 账户类型 |
| `--session-id` | int | 0 | 会话 ID |
| `--token` | str | `""` | 认证 Token |
| `--db` | Path | SATS_DB_PATH | DuckDB 路径 |

#### 子命令 `qmt status` / `qmt asset` / `qmt positions`

| 参数 | 说明 |
|------|------|
| `--db` | DuckDB 路径 |

#### 子命令 `qmt sync positions`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--prune-missing` | flag | False | 同步时删除不在 QMT 中的持仓 |
| `--db` | Path | SATS_DB_PATH | DuckDB 路径 |

#### 子命令 `qmt orders`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--open` | flag | False | 仅查看未完成订单 |
| `--db` | Path | SATS_DB_PATH | DuckDB 路径 |

#### 子命令 `qmt trades`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--limit` | int | 50 | 最大返回条数 |
| `--db` | Path | SATS_DB_PATH | DuckDB 路径 |

#### 子命令 `qmt buy` / `qmt sell`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--symbol` | str | — | **是** | 股票代码或名称 |
| `--quantity` | int | — | **是** | 数量 (必须为 100 的整数倍) |
| `--price-type` | choice | `latest` | 否 | 价格类型: `latest` (最新价), `limit` (限价) |
| `--price` | float | None | 否 | 限价价格 (price-type=limit 时需要) |
| `--dry-run` | flag | False | 否 | 模拟模式，不实际下单 |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 路径 |

#### 子命令 `qmt cancel`

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--order-id` | str | — | **是** | 订单 ID |
| `--db` | Path | SATS_DB_PATH | 否 | DuckDB 路径 |

**示例：**
```bash
# 启动 Bridge
sats qmt bridge run --host 127.0.0.1 --port 8765 --account-id 123456

# 查看状态
sats qmt status

# 查看资产
sats qmt asset

# 查看持仓
sats qmt positions

# 同步 QMT 持仓到监控
sats qmt sync positions --prune-missing

# 查看未完成订单
sats qmt orders --open

# 模拟买入
sats qmt buy --symbol 000001 --quantity 100 --price-type latest --dry-run

# 限价买入
sats qmt buy --symbol 000001 --quantity 300 --price-type limit --price 12.50

# 卖出
sats qmt sell --symbol 000001 --quantity 200 --price-type latest

# 取消订单
sats qmt cancel --order-id ord_20260612_001
```

---

### 4.27 `serve` — HTTP API 服务

启动 FastAPI HTTP 服务。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--host` | str | `127.0.0.1` | 监听地址 |
| `--port` | int | 8000 | 监听端口 |

**示例：**
```bash
# 默认启动
sats serve

# 自定义端口
sats serve --port 9000

# 允许外部访问
sats serve --host 0.0.0.0 --port 8000
```

---

## 五、REPL 内置命令 (10个)

| 命令 | 参数 | 功能 |
|------|------|------|
| `/help` | — | 渲染帮助面板 (所有命令、快捷键、示例) |
| `/exit`, `/quit` | — | 打印 "bye" 并退出 REPL |
| `/clear` | — | 清屏 (ANSI escape) |
| `/save` | `--format` (md/pdf, 默认 md), `--path` (可选路径), `--source` (output/report, 默认 output) | 保存上次输出为 MD 或 PDF |
| `/new` | `[title]` (可选标题) | 创建新聊天会话 |
| `/goal` | `[text]` (设置目标) / `status` (查看) / `cancel` (取消) / `clear` (清除) | 设置/查看/清除 Agent 目标 |
| `/confirm` | `ACTION_ID` | 确认待处理的运行时动作 |
| `/reject` | `ACTION_ID` | 拒绝待处理的运行时动作 |
| `/trace` | `[turn_id]` (可选) | 显示聊天轮次追踪 |

**示例：**
```bash
# REPL 中使用
sats> /help
sats> /save --format pdf
sats> /save --format md --path ~/reports/today.md --source report
sats> /new "缠论研究"
sats> /goal 帮我每天收盘后分析缠论信号
sats> /goal status
sats> /confirm action_001
sats> /trace turn_20260612_001
sats> /exit
```

---

## 六、核心模块详解

### 6.1 数据层 (`sats/data/`)

#### 架构：Facade + Provider 模式

```
AStockDataProvider (统一门面)
    ├── TickFlowDataProvider (实时行情主力)
    ├── TushareDataProvider (基本面/日线主力)
    └── AkShareDataProvider (可选补充)
```

| 文件 | 行数 | 职责 |
|------|------|------|
| `base.py` | 55 | `MarketDataProvider` ABC 定义 |
| `astock_provider.py` | 860 | 统一门面，懒加载 + 失败缓存 + 降级级联 |
| `tickflow_provider.py` | 1275 | TickFlow SDK 适配器，含限流器 |
| `tushare_provider.py` | 2400 | Tushare 适配器，数据缓存写入 DuckDB |
| `akshare_provider.py` | 490 | AkShare 适配器，安全参数过滤 |
| `akshare_datasets.py` | - | AkShare 数据集目录 (数百个端点) |
| `tushare_stock_datasets.py` | 314 | Tushare 数据集目录 (~125个) |
| `resolver.py` | 277 | DuckDB 优先数据解析器 |
| `provider_capabilities.py` | 301 | 供应商能力目录 |
| `limit_sentiment.py` | 70 | 涨跌停情绪计算器 |

**关键设计**:
- **懒加载 + 失败缓存**: Provider 首次失败后永久禁用，避免反复尝试
- **降级级联**: TickFlow → Tushare → AkShare → DuckDB 缓存
- **规则特化数据**: Tushare Provider 根据 `rule_name` 预取该规则所需数据 (缠论取30分钟K线，月线突破取月K线等)
- **数据溯源**: 每个 DataFrame 携带 `frame.attrs["market_data_provenance"]`

### 6.2 选股规则引擎 (`sats/screening/`)

#### 架构：Strategy + Registry 模式

| 文件 | 职责 |
|------|------|
| `base.py` | `ScreeningInput`, `ScreeningResult`, `ScreeningRule` ABC |
| `registry.py` | 规则注册表 (13 内置 + AI 生成 + ~25 别名) |
| `service.py` | `evaluate_inputs()`, `evaluate_and_store()` 编排 |
| `rule_composer.py` | AI 规则生成 (自然语言 → Python 代码) |
| `generated_rule_runtime.py` | 生成规则的通用运行时 |

#### 13 个内置选股规则

| 规则 ID | 中文名 | 核心逻辑 |
|---------|--------|----------|
| `price_volume_ma` | 价量均线 | 非ST, pct_chg 3-5%, 量比>1, 换手5-10%, MA多头排列 |
| `chan_third_buy` | 缠论三买 | 日线整理箱体突破 + 30分钟确认 |
| `chan_composite` | 缠论综合 | 一买+二买+三买+中枢低吸 组合评估 |
| `chan_signals` | 缠论信号 | 委托缠论引擎评估买/卖信号 |
| `monthly_base_breakout` | 月线底部突破 | 24-96月长期底部形态，颈线突破 |
| `turtle_trade` | 海龟交易 | 20日新高突破 + 成交量确认 |
| `ma_volume` | 均线放量 | MA5 金叉 MA20 + 成交量 > 1.5x |
| `high_tight_flag` | 高位紧凑旗形 | 40日动量>60%, 10日盘整<15% |
| `limit_up_shakeout` | 涨停洗盘 | 昨涨停+今日放量阴线 |
| `uptrend_limit_down` | 上升趋势跌停 | MA20>MA60 趋势中跌停 + 量>2x |
| `rps_breakout` | 相对强度突破 | RPS >= 90 + 价格 >= 90% of 120日高 |
| `ma_volume_relative_strength` | 均量相对强度 | 3日连阳+MA多头+量比1.2-2.0+平台突破检测 |
| `signal_composite` | 信号综合 | 桥接信号分析框架 |

### 6.3 存储层 (`sats/storage/`)

#### DuckDB 数据库

| 文件 | 行数 | 职责 |
|------|------|------|
| `schema.sql` | 710 | 定义 27 张表的 SQL Schema |
| `duckdb.py` | 1721 | 完整 CRUD 访问层 |

#### 27 张表分类

| 域 | 表名 | 说明 |
|----|------|------|
| 市场数据 | `stock_daily`, `stock_daily_basic`, `stock_basic`, `industry_daily`, `stock_minute_cache`, `realtime_quote_cache` | 日线 OHLCV、基本面、股票元数据 |
| 板块 | `sector_basic`, `sector_daily`, `sector_members` | 行业/概念板块 |
| 基本面 | `stock_moneyflow`, `stock_fundamentals` | 资金流、财务数据 |
| 选股 | `screening_results` | 选股结果 (含 metrics JSON) |
| 因子 | `factor_runs`, `factor_candidates` | 因子分析记录 |
| 聊天 | `chat_sessions`, `chat_messages`, `chat_turns`, `chat_turn_events`, `chat_turn_items`, `chat_artifacts`, `chat_pending_actions` | 对话全链路 |
| 知识库 | `chat_memories`, `knowledge_bases`, `knowledge_files`, `knowledge_file_links`, `knowledge_chunks` | RAG 知识 |
| 监控 | `monitor_positions`, `monitor_watchlist`, `monitor_buy_candidates`, `monitor_events`, `monitor_trade_events`, `monitor_runtime` | 实时监控状态 |
| 交易 | `broker_accounts`, `broker_positions`, `broker_orders`, `broker_trades`, `broker_order_events` | QMT 交易记录 |
| 调度 | `scheduled_tasks`, `scheduled_task_runs` | 定时任务 |
| 历史 | `interaction_history` | REPL 交互记录 |

**Schema 迁移**: 使用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 实现安全升级。

### 6.4 LLM 层 (`sats/llm/`)

#### 支持 13 个 LLM 供应商

`openai`, `openrouter`, `deepseek`, `gemini`, `groq`, `dashscope`/`qwen`, `zhipu`, `moonshot`, `minimax`, `mimo` (小米 MiMo), `zai`, `ollama`

| 文件 | 职责 |
|------|------|
| `provider.py` | 供应商注册表 + `build_llm()` 工厂 + `ChatOpenAIWithReasoning` (保留推理内容) |
| `model_config.py` | 模型 Profile 发现/解析/持久化 |
| `chat.py` | `ChatLLM` (sync/async/stream) + `LightFallbackChatLLM` (廉价模型优先降级) |

**关键设计**:
- 所有供应商通过环境变量操纵归一化为 OpenAI 兼容 API
- `ChatOpenAIWithReasoning` 保留推理模型的 `reasoning_content`
- `LightFallbackChatLLM`: 先尝试轻量模型，失败回退重型模型

### 6.5 分析层 (`sats/analysis/`)

| 文件 | 行数 | 职责 |
|------|------|------|
| `dsa_native.py` | 1247 | 原生 DSA 分析管线 |
| `dsa_decision.py` | 552 | 本地规则决策引擎 (0-100分) |
| `opportunity_discovery.py` | 1500+ | 短线机会发现管线 |
| `stock_picking_agent.py` | 1300+ | 自然语言选股票 Agent |
| `chan_llm_review.py` | - | 缠论 LLM 审查 (RAG 增强) |
| `market_llm_context.py` | - | 市场级 LLM 上下文构建 |
| `stock_llm_context.py` | - | 个股级 LLM 上下文构建 |

**关键设计**:
- **数据政策**: 所有上下文构建器注入 system message 告知 LLM 已获取数据，禁止编造
- **本地降级**: LLM 失败时回退到本地规则决策
- **missing_fields 追踪**: 全链路透明标记缺失数据

### 6.6 其他模块

| 模块 | 职责 |
|------|------|
| `sats/agent/` | 自主 Agent 框架 (规划 → 执行 → 综合) |
| `sats/api/` | FastAPI HTTP (5 个端点: /, /health, /api/screen, /api/screen/results, /api/market/minute-k) |
| `sats/scheduler/` | 定时任务 (仅 CLI/Chat 任务，禁止 shell 执行) |
| `sats/progress.py` | 统一进度面板 (TTY 有 UI, 非 TTY 静默) |
| `sats/symbols.py` | 股票代码标准化 (6xx→SH, 0xx/3xx→SZ, 4xx/8xx/9xx→BJ) |
| `sats/memory.py` | 聊天长期记忆 (DuckDB) |
| `sats/skills.py` | 技能系统 (YAML 定义) |
| `sats/indicators/` | 技术指标计算 (MA, MACD, KDJ, RSI, BOLL, ATR 等) |
| `sats/signals/` | 信号分析引擎 |
| `sats/factors/` | 因子研究 (alpha101/gtja191/barra + ML) |
| `sats/chan/` | 缠论引擎 |
| `sats/rag/` | RAG 知识检索 |
| `sats/web/` | 网络搜索 & 社交热榜 |
| `sats/monitoring/` | 实时监控 (后台进程 + 心跳) |
| `sats/trading/` | QMT 券商交易 |

---

## 七、关键架构模式

| 模式 | 说明 |
|------|------|
| **单一入口** | CLI/REPL/Chat 共用 `cli.main(argv)` |
| **Agent-first 路由** | 默认对话走 Agent 循环，`--no-agent` 降级 |
| **DuckDB 万能存储** | 选股/记忆/历史/监控/调度/交易/因子全部写入同一 DuckDB |
| **数据门面** | `AStockDataProvider` 统一入口，业务模块不直接导入后端 |
| **输入边界标准化** | 所有股票代码在入口处通过 `sats.symbols` 标准化 |
| **LLM 数据政策** | 上下文构建器显式告知 LLM 已有数据，禁止编造 |
| **优雅降级** | LLM 失败→本地规则；Provider 失败→降级级联 |
| **进度系统** | TTY 有 UI 面板，非 TTY/JSON/测试静默 |
| **AI 规则生成** | 自然语言→结构化计划→AST 验证→合成测试→确认生成 |
| **安全边界** | 交易需显式 `--auto-trade`；调度禁止 shell；QMT 支持 dry-run |

---

## 八、测试覆盖

`tests/` 目录约 50 个测试文件，覆盖:

| 类别 | 文件数 | 重点 |
|------|--------|------|
| LLM | 1 | Provider 工厂、ChatLLM、降级、JSON 提取 |
| 分析/DSA | 6 | 原生 DSA、市场/个股上下文、机会发现、选股 Agent |
| 缠论 | 5 | 所有缠论规则、RAG 知识、LLM 审查 |
| API/存储 | 1 | DuckDB + FastAPI TestClient + CLI |
| Chat/REPL | 10 | Chat 管线、REPL 命令、事件、审批、回测 |
| 选股 | 2 | 规则评估、价量均线规则 |
| 数据源 | 3 | AStock/Tushare/TickFlow Provider |
| 其他 | ~22 | 进度、调度、因子、信号、符号、配置、监控、Web 等 |

所有测试使用 `unittest.TestCase` + `unittest.mock.patch`，DuckDB 测试使用 `tempfile.TemporaryDirectory` 隔离。
