# SATS 详细说明书

> **版本**：0.1.0  
> **最后更新**：2026-06-07  
> **适用环境**：Python 3.12+ / macOS / Linux

---

## 目录

1. [系统概览](#1-系统概览)
2. [安装与配置](#2-安装与配置)
3. [架构总览](#3-架构总览)
4. [三种入口](#4-三种入口)
5. [CLI 命令详解](#5-cli-命令详解)
6. [数据体系](#6-数据体系)
7. [信号分析系统](#7-信号分析系统)
8. [筛选规则系统](#8-筛选规则系统)
9. [因子系统](#9-因子系统)
10. [LLM 与聊天系统](#10-llm-与聊天系统)
11. [Skills 技能系统](#11-skills-技能系统)
12. [机会发现 (discover)](#12-机会发现-discover)
13. [监控与定时任务](#13-监控与定时任务)
14. [交易接入 (MiniQMT)](#14-交易接入-miniqmt)
15. [回测系统](#15-回测系统)
16. [知识库 (RAG)](#16-知识库-rag)
17. [Agent 系统](#17-agent-系统)
18. [API 服务](#18-api-服务)
19. [项目结构](#19-项目结构)

---

## 1. 系统概览

SATS（Stock Automated Trading System）是一个**面向 A 股研究的本地分析系统**，核心定位是：

- **研究辅助，非自动交易**：提供筛选、分析、信号发现等能力，真实交易需显式命令触发。
- **数据真实，不编造**：所有行情和财务数据来自 TickFlow / Tushare / AkShare 三个后端，缺失字段不伪造。
- **LLM 增强，非替代**：LLM 用于意图理解、排名、报告生成，不做行情预测，不保证上涨。
- **本地优先**：DuckDB 本地存储，不依赖云数据库。

核心能力矩阵：

| 能力域 | 描述 |
|--------|------|
| 股票筛选 | 基于规则的 A 股全市场筛选，支持多种策略规则 |
| 信号分析 | 统一 Analyze 信号引擎，覆盖缠论、均线、K 线形态、艾略特波浪等 |
| 机会发现 | 自然语言选股，基于信号 + 热点板块 + LLM 排名的短线机会发现 |
| DSA 分析 | 原生每日股票分析，含技术指标、风险评级、决策建议 |
| 因子选股 | 多因子量化选股，支持价值/动量/质量/低波动等因子画像 |
| 聊天助手 | LLM 驱动的交互式行情助手，自动注入真实数据上下文 |
| 持仓监控 | 关注列表和持仓的终端仪表盘，支持定时刷新 |
| 定时任务 | 定期执行筛选、分析、报告等任务 |
| QMT 交易 | MiniQMT broker 接入，支持查询和委托（需显式确认） |
| 回测 | 策略回测框架，支持 K 线策略回测 |
| 知识库 | 本地 RAG，支持缠论、技术指标、信号知识检索 |

---

## 2. 安装与配置

### 2.1 环境要求

- Python 3.12+
- pip / uv 包管理器

### 2.2 安装

```bash
git clone <repo-url>
cd SATS
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

可选依赖：

```bash
pip install -e ".[akshare]"    # AkShare 数据补充
pip install -e ".[ml]"         # 机器学习因子（LightGBM/XGBoost）
pip install -e ".[deep]"       # PyTorch 深度学习
pip install -e ".[web-rag]"    # 可选本地 FastEmbed 网页向量检索
```

### 2.3 初始化配置

```bash
sats init          # 生成 .env 模板
sats init --overwrite  # 覆盖已有配置
```

### 2.4 配置项说明

`.env` 文件位于项目根目录，核心配置项：

#### 数据源

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SATS_DB_PATH` | DuckDB 数据库路径 | `data/sats.duckdb` |
| `TUSHARE_TOKEN` | Tushare API Token | — |
| `TUSHARE_TIMEOUT_SECONDS` | Tushare 请求超时 | `30` |
| `TUSHARE_MAX_RETRIES` | Tushare 重试次数 | `2` |
| `TICKFLOW_API_KEY` | TickFlow API Key | — |
| `TICKFLOW_BASE_URL` | TickFlow 服务地址 | `https://api.tickflow.org` |
| `TICKFLOW_TIMEOUT_SECONDS` | TickFlow 请求超时 | `30` |
| `TICKFLOW_MAX_RETRIES` | TickFlow 重试次数 | `3` |

#### 网络 RAG

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `WEB_SEARCH_BACKEND` | `auto/rag` 使用原生 RAG；`responses` 为显式兼容路径；`ddgs` 为纯摘要模式 | `auto` |
| `WEB_SEARCH_PROVIDERS` | 原生搜索提供方列表，可选 `ddgs,bing,tavily,bocha,querit` | `ddgs,bing` |
| `WEB_PAGE_CACHE_TTL_SECONDS` | 网页正文与分块索引有效期 | `86400` |
| `WEB_EMBEDDING_PROVIDER` | `auto/openai/fastembed/none` | `auto` |
| `WEB_EMBEDDING_BASE_URL` | OpenAI-compatible embeddings 端点 | — |
| `WEB_EMBEDDING_MODEL` | embedding 模型名 | — |

`auto` 不会自动调用 Responses API，也不会自动下载本地 embedding 模型。未配置远程 embeddings 时使用关键词检索并显式标记降级。

#### LLM 模型

配置采用 **"模型配置组"** 模式。每个配置组以大写前缀命名：

```
<PROFILE>_PROVIDER     → adapter 类型（deepseek/openrouter/qwen/mimo/ollama...）
<PROFILE>_BASE_URL     → OpenAI-compatible endpoint
<PROFILE>_API_KEY      → API 密钥
<PROFILE>_MODEL        → 主模型名称
<PROFILE>_LIGHT_MODEL  → 轻量模型名称（可选）
```

默认内置两个配置组 `DEEPSEEK` 和 `XIAOMIMIMO`。

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DEFAULT_MODEL` | 主模型配置组名称 | `DEEPSEEK` |
| `DEFAULT_LIGHT_MODEL` | 轻量模型配置组名称 | `XIAOMIMIMO` |
| `LLM_TEMPERATURE` | 生成温度 | `0.0` |
| `LLM_TIMEOUT_SECONDS` | 调用超时 | `120` |
| `LLM_MAX_RETRIES` | 重试次数 | `2` |

轻量模型用于：自然语言预处理、普通聊天回答、工具调用总结、选股/机会排序、长期记忆抽取、会话摘要和监控信号摘要。DSA 原生复核、缠论候选复核、信号 AI 生成/解释和规则生成/确认等高风险路径使用主模型。

#### 交易

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TRADING_MODE` | 交易模式 `paper` / `live` | `paper` |
| `REQUIRE_TRADE_CONFIRMATION` | 是否需要交易确认 | `true` |
| `SATS_BROKER_PROVIDER` | Broker 提供者 | — |
| `SATS_QMT_BRIDGE_URL` | QMT 网关注解地址 | — |
| `SATS_QMT_TOKEN` | QMT 网关 Token | — |
| `SATS_QMT_ACCOUNT_ID` | QMT 账户 ID | — |
| `SATS_QMT_ACCOUNT_TYPE` | 账户类型 | `STOCK` |

### 2.5 模型管理

```bash
sats model status         # 查看当前模型
sats model list           # 列出所有模型配置组
sats model use DEEPSEEK --target main   # 切换主模型
sats model use XIAOMIMIMO --target light  # 切换轻量模型
```

---

## 3. 架构总览

```
用户 → CLI（一次性命令）/ REPL（交互式）/ API（FastAPI）
         │
         ├── chat.py（ChatSession：LLM 聊天编排）
         │     ├── chat_preprocessor.py（自然语言预处理）
         │     ├── chat_planner.py（意图规划：选股/个股/大盘/缠论/规则生成）
         │     ├── chat_reference.py（引用上下文管理）
         │     └── skills.py（技能加载与匹配）
         │
         ├── data/astock_provider.py（AStockDataProvider 统一数据门面）
         │     ├── TickFlow（优先：实时行情/分钟K/日线）
         │     ├── Tushare（补充：资金流/财务/估值/行业热点）
         │     └── AkShare（兜底：全市场宽度/东财扩展/筹码分布）
         │
         ├── storage/duckdb.py（DuckDB 本地存储）
         │     └── schema.sql（表结构定义）
         │
         ├── screening/（规则筛选）
         ├── signals/（统一信号分析）
         ├── analysis/（DSA/大盘/个股/discover/缠论）
         ├── indicators/（技术指标计算）
         ├── factors/（量化因子选股）
         ├── monitoring/（持仓监控仪表盘）
         ├── scheduler/（定时任务）
         ├── trading/（MiniQMT broker 接入）
         ├── backtesting/（策略回测）
         ├── rag/（知识库检索）
         └── agent/（Agent 运行时）
```

---

## 4. 三种入口

### 4.1 CLI 一次性命令

```bash
sats <command> [options]
# 或
python -m sats <command> [options]
```

适合脚本调用、定时任务、CI/CD 集成。

### 4.2 REPL 交互式

```bash
sats
```

进入交互式会话，提示符 `sats>`。支持：

- **斜杠命令**：`/screen`、`/analyze`、`/discover`、`/chat`、`/dsa` 等
- **自然语言**：直接输入选股问题、股票分析请求，系统自动预处理并注入数据上下文
- **会话管理**：`/new` 新会话、`/history` 历史、`/memory` 记忆
- **输出保存**：`/save <文件名>` 保存上一轮输出

### 4.3 FastAPI HTTP 服务

```bash
sats serve [--host 127.0.0.1] [--port 8000]
```

提供 REST API：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 管理页面，列出可用规则 |
| `/health` | GET | 健康检查 |
| `/api/screen` | POST | 执行筛选 |
| `/api/screen/results` | GET | 查询筛选结果 |
| `/api/market/minute-k` | GET | 获取分钟 K 线 |

---

## 5. CLI 命令详解

### 5.1 `init` — 初始化配置

```bash
sats init [--overwrite]
```

在项目根目录生成 `.env` 配置模板。

### 5.2 `screen` — 规则筛选

```bash
sats screen --rule <规则名> --trade-date YYYYMMDD [--db <路径>]
```

运行指定规则的全 A 股筛选，结果写入 DuckDB。

### 5.3 `results` — 查询筛选结果

```bash
sats results --trade-date YYYYMMDD [--rule <规则名>] [--db <路径>]
```

### 5.4 `quote` — 实时行情

```bash
sats quote --stocks 000001,600519.SH,紫光股份 [--db <路径>]
```

显示实时行情和均线数据。支持股票代码、ts_code、股票名称。

### 5.5 `analyze` — 统一信号分析

```bash
sats analyze signals                     # 列出可用信号策略
sats analyze --stocks 000001,600519      # 分析指定股票信号
```

### 5.6 `dsa` — 原生 DSA 分析

```bash
sats dsa --stocks 000001.SZ,600519.SH [--trade-date YYYYMMDD] [--no-llm]
```

原生每日股票分析，输出技术指标、趋势判断、操作建议、风险评估。`--no-llm` 关闭 LLM 复核。

### 5.7 `analyze-dsa` — 外部 DSA 分析桥接

```bash
sats analyze-dsa --trade-date YYYYMMDD [--rule <规则名>] [--stocks ...]
```

### 5.8 `analyze-chan` — 缠论分析

```bash
sats analyze-chan --stocks 000001.SZ,600519.SH [--trade-date YYYYMMDD]
```

### 5.9 `chan-kb` — 缠论知识库检索

```bash
sats chan-kb <查询>
```

### 5.10 `discover` — 机会发现

```bash
# 纯参数模式
sats discover [--signals short_up] [--limit 5] [--trade-date YYYYMMDD]

# 自然语言模式
sats discover 推荐几只短线可能上涨的股票
sats discover 新能源板块有哪些机会
```

详见 [第 12 节](#12-机会发现-discover)。

### 5.11 `chat` — LLM 聊天

```bash
sats chat <消息>                # 单次聊天
sats chat --no-agent <消息>     # 不启用 Agent 计划
sats chat --trace <turn_id>     # 查看历史 turn 详情
sats chat --confirm <action_id> # 确认待执行动作
sats chat --reject <action_id>  # 拒绝待执行动作
```

### 5.12 `agent` — Agent 执行

```bash
sats agent <任务描述>
```

进入 Agent 计划-执行循环，支持多步骤任务分解和工具调用。

### 5.13 `model` — 模型管理

```bash
sats model status              # 当前模型状态
sats model list                # 列出配置组
sats model use <名称> --target main|light  # 切换模型
```

### 5.14 `memory` — 聊天记忆

```bash
sats memory [--limit 20]       # 查看记忆
```

### 5.15 `history` — 聊天历史

```bash
sats history [--limit 20]      # 查看会话历史
```

### 5.16 `knowledge` — 知识库

```bash
sats knowledge                 # 列出知识库文集
sats knowledge <查询>          # 搜索知识库
```

### 5.17 `indicators` — 技术指标

```bash
sats indicators --stocks 000001.SZ [--trade-date YYYYMMDD]
```

### 5.18 `factor` — 因子选股

```bash
sats factor pick --profile <因子画像> [--limit 20] [--trade-date YYYYMMDD]
sats factor analyze --stocks 000001.SZ,600519.SH
sats factor panel --profile <因子画像> --trade-date YYYYMMDD
sats factor compose --profile <因子画像> --trade-date YYYYMMDD
sats factor train-ml --profile <因子画像> [--trade-date YYYYMMDD]
sats factor predict-ml --run-id <运行ID>
sats factor profiles          # 列出可用因子画像
```

### 5.19 `skills` — 技能列表

```bash
sats skills                    # 列出所有已安装 skills
```

### 5.20 `watchlist` — 关注列表

```bash
sats watchlist                 # 查看关注列表
sats watchlist add <股票> [<股票> ...]
sats watchlist remove <股票> [<股票> ...]
```

### 5.21 `monitor` — 持仓监控

```bash
sats monitor start [--auto-trade] [--interval 60]
sats monitor stop
sats monitor status
```

### 5.22 `monitor-display` — 监控仪表盘

```bash
sats monitor-display
```

终端仪表盘，展示持仓/关注列表的实时行情和盈亏。

### 5.23 `schedule` — 定时任务

```bash
sats schedule list                            # 列出任务
sats schedule add --command "screen ..." --time "09:00" --days mon-fri
sats schedule remove <任务ID>
sats schedule run <任务ID>
sats schedule start                           # 启动调度器
sats schedule stop                            # 停止调度器
```

### 5.24 `qmt` — QMT 交易

```bash
sats qmt status                               # 连接状态
sats qmt positions                            # 查询并自动同步持仓
sats qmt orders [--open]                      # 查询委托
sats qmt trades [--limit 50]                  # 查询成交
sats qmt buy --symbol 000001.SZ --quantity 100 [--dry-run]
sats qmt sell --symbol 000001.SZ --quantity 100 [--dry-run]
sats qmt cancel --order-id <委托ID>
```

### 5.25 `serve` — API 服务

```bash
sats serve [--host 127.0.0.1] [--port 8000]
```

---

## 6. 数据体系

### 6.1 数据门面

所有业务模块通过 `AStockDataProvider` 获取 A 股数据，不直接导入 TickFlow/Tushare/AkShare。

```python
from sats.data.astock_provider import AStockDataProvider
from sats.config import load_settings

settings = load_settings()
provider = AStockDataProvider(settings)
```

### 6.2 数据源优先级

```
TickFlow（优先）→ Tushare（补充）→ AkShare（兜底）
```

| 数据项 | TickFlow | Tushare | AkShare |
|--------|----------|---------|---------|
| 实时行情 | ✓ | — | — |
| 日 K 线 | ✓ | ✓ | — |
| 分钟 K 线 | ✓ | — | — |
| 实时 quote | ✓ | — | ✓（补充） |
| 指数数据 | ✓ | ✓ | — |
| 资金流 | — | ✓ | — |
| daily_basic（PE/PB/PS/换手率） | — | ✓ | — |
| 财务数据 | — | ✓ | — |
| 同花顺行业/概念热点 | — | ✓ | — |
| 涨跌停/炸板情绪 | — | ✓ | — |
| 全市场宽度 | — | — | ✓ |
| 东财实时扩展 | — | — | ✓ |
| 筹码分布 | — | — | ✓ |
| 个股摘要 | — | — | ✓ |

每个数据结果标注 `data_source` / `data_sources` / `missing_fields`，缺失字段明确暴露。

### 6.3 股票代码规范

所有 A 股代码通过 `sats.symbols` 模块统一规范化：

```python
from sats.symbols import normalize_ts_code, normalize_symbols

normalize_ts_code("000001")   # → "000001.SZ"
normalize_ts_code("600519")   # → "600519.SH"
normalize_ts_code("300750")   # → "300750.SZ"
normalize_ts_code("688981")   # → "688981.SH"
normalize_ts_code("832982")   # → "832982.BJ"

normalize_symbols(["000001", "600519", "紫光股份"])  # 批量规范化
```

规则：
- `6xxxxx` → `.SH`（沪市主板）
- `0xxxxx`、`3xxxxx` → `.SZ`（深市主板/创业板）
- `4/8/9xxxxx` → `.BJ`（北交所）

### 6.4 DuckDB 存储

本地 DuckDB 文件（默认 `data/sats.duckdb`），表结构由 `sats/storage/schema.sql` 定义。

核心表：

| 表名 | 内容 |
|------|------|
| `stock_daily` | 日 K 线（OHLCV + 涨跌幅） |
| `stock_daily_basic` | 每日基本面（换手率/流通市值/PE/PB/PS） |
| `stock_moneyflow` | 资金流向 |
| `stock_fundamentals` | 财务数据（季报/年报） |
| `screening_results` | 筛选结果 |
| `stock_basic` | 股票基本信息 |
| `screening_candidates` | 筛选候选 |
| `monitor_positions` | 监控持仓 |
| `qmt_orders` | QMT 委托记录 |
| `qmt_trades` | QMT 成交记录 |
| `chat_memory` | 聊天记忆 |
| `interaction_history` | 交互历史 |

支持并发读取和只读模式：

```python
from sats.storage.duckdb import DuckDBStorage

storage = DuckDBStorage("data/sats.duckdb")
readonly = storage.readonly()  # 只读实例（并发安全）
```

---

## 7. 信号分析系统

`ScreeningInput`（筛选输入）持有一只股票的日 K 线、`daily_basic`、`stock_basic` 等原始数据，`SignalEngine` 将其转换为统一的 `SignalAnalysisResult`，包含买卖信号列表、趋势判定、置信度打分和风险标注。

### 7.1 信号类型

SATS 内置 100+ 种信号，覆盖六大类别：

| 类别 | 示例 | 说明 |
|------|------|------|
| **graph** | `triangle_up_break`, `trend_breakthrough_risk`, `wedge_down_break` | K 线图形信号 |
| **ma** | `ma_golden_spider`, `ma_warplane`, `ma_granville_b3` | 均线信号 |
| **kc** | `kc_up_pioneer`, `kc_tower_bottom`, `kc_bullish_harami` | K 线组合信号 |
| **chan** | `chan_first_buy`, `chan_second_buy`, `chan_third_buy` | 缠论买卖点 |
| **wave** | `elliott_c_reversal`, `elliott_b_pullback` | 艾略特波浪 |
| **harmonic** | `gartley_bullish`, `bat_bullish`, `cypher_bullish` | 谐波形态 |

### 7.2 信号组

常用信号组（通过 `--signals` 参数使用）：

| 组名 | 包含内容 |
|------|----------|
| `short_up` | 中短期上涨信号（默认用于 discover） |
| `all` | 所有信号 |
| `chan` | 缠论买卖点信号 |
| `ma` | 均线系统信号 |
| `kc` | K 线组合信号 |

### 7.3 复合信号

系统自动将相关信号交织为复合信号（`signal_composite_*`），例如：
- `chan_third_buy` + graph/wave/harmonic/trendline 确认 → `graph_chan_third_buy`
- `elliott_c_reversal` + chan 确认 → `graph_elliott_c_chan`
- `ma_golden_valley` + `ma_silver_valley` + chan 确认 → `ma_golden_silver_valley_chan`

### 7.4 信号分析结果

```python
@dataclass
class SignalAnalysisResult:
    ts_code: str          # 股票代码
    name: str             # 股票名称
    trade_date: str       # 交易日
    score: float          # 综合得分
    decision: str         # 决策建议（操作/持有/观望/回避）
    trend: str            # 趋势（看多/看空/震荡）
    close: float          # 收盘价
    events: list[SignalEvent]  # 信号事件列表
    key_levels: dict      # 关键价位（支撑/压力）
```

每个 `SignalEvent` 包含：
- `signal_id`：信号标识
- `label`：信号名称
- `side`：方向（buy/sell）
- `category`：类别
- `confidence`：置信度 0-1
- `score`：信号得分
- `reason`：触发理由
- `risk_flags`：风险标注

### 7.5 用法

```bash
# CLI
sats analyze --stocks 000001.SZ,600519.SH

# REPL
/analyze 000001.SZ,600519.SH

# Python
from sats.signals import analyze_signal_input, SignalInput

result = analyze_signal_input(input_data, selected_signals="short_up")
print(result.decision, result.trend, result.score)
```

---

## 8. 筛选规则系统

### 8.1 规则接口

```python
class ScreeningRule(ABC):
    name: str

    @abstractmethod
    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        ...
```

`ScreeningInput` 包含日 K 线、`daily_basic`、`stock_basic` 等原始数据，规则基于此产生 `ScreeningResult`：

```python
@dataclass
class ScreeningResult:
    trade_date: str
    ts_code: str
    rule_name: str
    passed: bool              # 是否通过
    score: float              # 得分
    matched_conditions: list[str]   # 满足的条件
    failed_conditions: list[str]    # 未满足的条件
    metrics: dict             # 指标明细
```

### 8.2 内置规则

规则位于 `sats/screening/rules/`，包括：

- `price_volume_ma`：量价换手均线规则
- `ma_volume_relative_strength`：均线量能相对强度（默认规则）
- `chan_composite`：缠论复合规则（一买/二买/三买/中枢低吸/二三买重合）
- `chan_third_buy`：缠论三买规则
- `chan_signals`：缠论信号交叉规则
- `trend_volume_confirmation`：趋势确认+温和放量
- `signal_composite`：信号交织规则

```bash
# 列出规则
sats screen --help    # 显示可用规则列表

# 运行筛选
sats screen --rule price_volume_ma --trade-date 20260606

# API
curl -X POST http://127.0.0.1:8000/api/screen \
  -H "Content-Type: application/json" \
  -d '{"trade_date": "20260606", "rule": "price_volume_ma"}'
```

### 8.3 AI 规则生成

SATS 支持通过 LLM 自动生成筛选规则：

1. 用户在聊天中描述规则需求
2. 系统生成 `RuleGenerationPlan`（规则名称、逻辑、参数）
3. 展示确认口令，用户回复 `确认生成规则 <rule_name>`
4. 代码写入 `sats/screening/rules/generated/` 并注册

```
用户：帮我生成一个筛选最近5天持续放量的规则
SATS：[展示规则计划] 确认生成规则 volume_5d_increase
用户：确认生成规则 volume_5d_increase
SATS：[规则已生成]
```

---

## 9. 因子系统

因子系统提供多因子量化选股能力。核心概念：

- **Factor**：单个因子（如 PE、ROE、动量）
- **FactorProfile**：因子组合画像（如 `value_quality`）
- **FactorRegistry**：因子注册中心

### 9.1 内置因子画像

| 画像 | 说明 |
|------|------|
| `value_quality` | 价值+质量因子组合 |
| `momentum_quality` | 动量+质量因子组合 |
| `small_growth` | 小盘成长因子组合 |
| `low_volatility` | 低波动因子组合 |

### 9.2 一级模块

```
sats/factors/
├── base.py          → Factor 基类
├── registry.py      → 因子注册中心（FactorRegistry）
├── profiles.py      → 因子画像定义（FactorProfile）
├── service.py       → 因子快照计算（compute_factor_snapshot）
├── composite.py     → 因子得分合成和 Top-K 选股（pick_top）
├── analysis.py      → 因子分析报告
├── panel.py         → 因子面板构建
├── reporting.py     → 因子选股报告生成
├── ml.py            → 机器学习因子（LightGBM/XGBoost）
└── factor_analysis_core.py → 因子分析核心
```

### 9.3 Python 用法

```python
from sats.factors import pick_with_factor_profile, compute_factor_snapshot
from sats.config import load_settings
from sats.data.astock_provider import AStockDataProvider

settings = load_settings()
provider = AStockDataProvider(settings)

# 按画像选股
result = pick_with_factor_profile(
    profile="value_quality",
    trade_date="20260606",
    limit=20,
    provider=provider,
    settings=settings
)
```

### 9.4 CLI 用法

```bash
sats factor profiles                               # 列出画像
sats factor pick --profile value_quality --limit 20 # 选股
sats factor analyze --stocks 000001.SZ,600519.SH    # 因子暴露分析
sats factor panel --profile momentum_quality --trade-date 20260606
sats factor compose --profile value_quality --trade-date 20260606
```

---

## 10. LLM 与聊天系统

### 10.1 ChatLLM

```python
from sats.llm import ChatLLM, extract_json_object

llm = ChatLLM()  # 自动从 .env 加载配置
response = llm.chat([
    {"role": "system", "content": "你是A股研究助手。"},
    {"role": "user", "content": "分析000001.SZ"}
])

data = extract_json_object(response.content or "")
```

- 支持所有 OpenAI-compatible providers（DeepSeek、OpenRouter、Qwen、Zhipu、Moonshot、MiniMax、MiMo、Ollama 等）
- 轻量模型自动用于常规任务，主模型用户高风险复核路径
- 超时和重试由 `LLM_TIMEOUT_SECONDS` 和 `LLM_MAX_RETRIES` 控制

### 10.2 聊天流程

```
用户输入
  │
  ├── chat_preprocessor（预处理：提取股票代码、交易日期、意图分类）
  │
  ├── chat_planner（规划：判断需要哪些数据、skills、Analysis）
  │     ├── 选股问题 → opportunity_discovery
  │     ├── 个股问题 → stock_context + indicators
  │     ├── 大盘问题 → market_context
  │     ├── 缠论问题 → chan_context
  │     └── 规则生成 → rule_generation
  │
  ├── 数据注入（真实行情/信号/指标/知识库）
  │
  ├── LLM 调用（轻量模型 + 主模型 fallback）
  │
  └── 输出（文本 + 工具调用 + 报告）
```

### 10.3 聊天 Python API

```python
from sats.chat import run_chat_once, ChatSession

result = run_chat_once("分析一下今天的大盘走势")
print(result.content)
print(result.tool_calls)
```

### 10.4 聊天记忆

系统自动提取重要交互到长期记忆：

```bash
sats memory           # 查看记忆
```

---

## 11. Skills 技能系统

Skills 是存储在 `skills/` 目录中的本地知识文件，每个 skill 包含 `SKILL.md` 和可选的配套文件。

### 11.1 加载方式

- **显式调用**：用户提及 skill 名称，系统通过 `load_skill` 加载
- **自动匹配**：聊天预处理阶段，根据用户意图自动匹配相关 skills
- **命令参数**：某些命令通过 `--skills` 参数指定

### 11.2 可用 Skills（48 个）

详见上一轮对话中的 skills 列表，按功能分为：

- **DSA 策略**（12 个）：bull-trend、bottom-volume、box-oscillation、shrink-pullback、volume-breakout、ma-golden-cross、dragon-head、one-yang-three-yin、emotion-cycle、hot-theme、expectation-repricing、growth-quality
- **技术分析**（7 个）：technical-basic、candlestick、chan-theory、elliott-wave、volatility、minute-analysis、market-microstructure
- **基本面与估值**（8 个）：financial-statement、valuation-model、fundamental-filter、undervalued-stock-screener、high-dividend-strategy、sentiment-reality-gap、tech-hype-vs-fundamentals、small-cap-growth-identifier
- **事件与情绪**（5 个）：sentiment-analysis、corporate-events、event-driven-detector、insider-trading-analyzer
- **风险与合规**（7 个）：risk-analysis、risk-adjusted-return-optimizer、ashare-pre-st-filter、regulatory-knowledge、esg-screener、portfolio-health-check、suitability-report-generator
- **行业与主题**（2 个）：sector-rotation
- **量化因子**（1 个）：quant-factor-screener
- **数据与工具**（6 个）：tushare-data、tickflow、akshare、data-routing、report-generate、workflow-templates、sats-market-assistant

自然对话 Agent 会按策略词自动调用内建能力：均线金叉触发 `analyze_signals(ma)`，多头趋势/回踩低吸/放量突破触发 `analyze_signals(ma,trendline,kline)`，缠论触发 `chan_context + analyze_signals(chan)`，波浪理论触发 `analyze_signals(wave)`，这些技术策略都会补充 `native_dsa`。热点题材、龙头和情绪周期会额外获取 `market_context(hot_sectors)`；事件驱动、公告、题材发酵和预期重估必须依赖公开 web/社媒证据，缺失时只能报告证据缺口；成长品质、ROE 和利润质量会补充 `factor_summary(growth_quality)`。

### 11.3 编程接口

```python
from sats.skills import load_skills, match_skills, find_skill, default_skills_dir

# 加载所有
skills = load_skills(default_skills_dir())

# 按名称查找
skill = find_skill("bull-trend", skills)

# 按意图匹配
matched = match_skills("分析均线金叉", skills)
```

---

## 12. 机会发现 (discover)

`discover` 是 SATS 的核心选股工作流，详见 [上一轮对话中的详细说明](discover 命令工作流程)。

### 12.1 两种模式

**纯参数模式**：
```bash
sats discover --signals short_up --limit 5 --trade-date 20260606
```

**自然语言模式**：
```bash
sats discover 推荐几只新能源板块短线可能上涨的股票
```

### 12.2 工作流（纯参数模式）

1. **加载全市场数据**：通过 `AStockDataProvider.load_all_screening_inputs()` 获取所有 A 股 K 线数据
2. **信号筛选**：`analyze_signal_input()` 对每只股票做信号分析
   - 只保留 buy 侧信号
   - score < 58.0 或 trend="看空" 淘汰
   - sell_score >= buy_score * 0.6 淘汰
3. **热点板块加权**：`_safe_hot_sector_context()` 获取当前热点，属于热点的股票加分
4. **分散化选择**：`_pick_diversified_signal_results()` 按综合得分贪婪选择，同时施加同板块/同行业/同热点惩罚
5. **候选增强**：加载技术指标（MACD/RSI/BOLL 等）、计算缠论得分（一买+3/二买+4/三买+6/二三买重合+8）、组装 `OpportunityCandidate`
6. **LLM 排名**：`_rank_with_llm()` 将候选上下文发送给 LLM 排序，LLM 生成排名理由、入场触发条件、失效条件和风险提示
7. **分散化再平衡**：确保结果不过度集中在同一板块/行业/热点
8. **生成报告**：Markdown 报告输出到 `reports/`

### 12.3 工作流（自然语言模式）

增加 Agent 规划层：

1. **意图解析**：LLM 解析用户自然语言，提取主题、匹配 skills、确定策略
2. **主题股票池**：从同花顺行业/概念解析出相关 A 股股池
3. **研究上下文**：加载 RAG 知识库
4. **本地机会发现**（LLM 关闭）
5. **因子叠加**（可选）
6. **Agent LLM 排名**：带 RAG 知识 + 策略约束的增强排名

---

## 13. 监控与定时任务

### 13.1 持仓监控

```bash
# 启动监控服务
sats monitor start [--auto-trade] [--interval 60]

# 查看状态
sats monitor status

# 停止
sats monitor stop
```

`--auto-trade` 启用自动交易（需配合 broker 配置）。默认 60 秒刷新一次。

### 13.2 终端仪表盘

```bash
sats monitor-display
```

实时展示：关注列表 + 持仓、最新价、涨跌幅、盈亏、信号状态。

### 13.3 定时任务

```bash
# 列出任务
sats schedule list

# 添加任务
sats schedule add --command "screen --rule price_volume_ma --trade-date 20260606" \
                  --time "09:00" --days mon-fri

# 管理
sats schedule remove <任务ID>
sats schedule run <任务ID>        # 手动执行
sats schedule start               # 启动调度器
sats schedule stop                # 停止调度器
```

调度器以独立进程运行，通过 `SATS_DB_PATH` 共享配置。任务只执行 SATS CLI 命令，不执行任意 shell。

---

## 14. 交易接入 (MiniQMT)

### 14.1 连接

```bash
sats qmt status
```

配置 `.env`：

```
SATS_QMT_BRIDGE_URL=http://<Windows IP>:端口
SATS_QMT_TOKEN=<Token>
SATS_QMT_ACCOUNT_ID=<账户ID>
SATS_QMT_ACCOUNT_TYPE=STOCK
```

### 14.2 查询

```bash
sats qmt positions             # 查询并自动同步持仓到本地
sats qmt orders [--open]       # 查询委托
sats qmt trades --limit 50     # 查询成交
```

### 14.3 交易

所有实盘委托需要显式确认（`REQUIRE_TRADE_CONFIRMATION=true`）：

```bash
sats qmt buy --symbol 000001.SZ --quantity 100 [--dry-run]
sats qmt sell --symbol 000001.SZ --quantity 100 [--dry-run]
sats qmt cancel --order-id <委托ID>
```

`--dry-run` 仅验证和审计，不发出实际委托。

### 14.4 安全约束

- 定时任务、聊天和 LLM 工具不能自主执行真实交易
- 实盘交易只能通过 broker 白名单接口 + 显式命令进入
- `paper` 模式下所有交易为模拟

---

## 15. 回测系统

### 15.1 StrategySpec

```python
from sats.backtesting.strategy_spec import StrategySpec

spec = StrategySpec(
    name="MA金叉策略",
    symbols=("000001.SZ", "600519.SH"),
    start_date="20240101",
    end_date="20241231",
    ma_short=5,
    ma_long=20,
)
```

### 15.2 运行回测

```python
from sats.backtesting.service import run_strategy_backtest

result = run_strategy_backtest(spec, settings=settings)
print(result.metrics)        # 回测指标
print(result.equity_curve)   # 资金曲线
print(result.trades)         # 交易记录
```

### 15.3 当前实现

当前为 **均线策略轻量级回测**。回测结果包含：
- 总收益率、年化收益、最大回撤、夏普比率
- 资金曲线（每交易日净值）
- 交易记录（买卖时点、价格、数量）

---

## 16. 知识库 (RAG)

### 16.1 知识库结构

知识库位于 `knowledge/` 和 `skills/` 目录，按主题组织为 collections：

| Collection | 内容 | 来源 |
|------------|------|------|
| `chan` | 缠论规则、买卖点、中枢、背驰 | `knowledge/chan/rules/` |
| `technical` | 技术指标、K 线、成交量、波动率 | `skills/technical-basic/` 等 |
| `signals` | SATS 信号分析、筛选规则 | `skills/sats-market-assistant/`、`sats/signals/` |
| `market` | 大盘分析、市场宽度、情绪 | `skills/sentiment-analysis/` 等 |
| `sentiment` | 市场情绪、资金流 | 对应 skills |
| `fundamental` | 基本面、估值、财务 | `skills/financial-statement/`、`skills/valuation-model/` 等 |
| `risk` | 风险分析、合规 | `skills/risk-analysis/` 等 |
| `data` | 数据源规范 | `skills/data-routing/`、`skills/tickflow/` |
| `stock-basic` | A 股股票基本信息 | DuckDB stock_basic 表 |

### 16.2 检索

```bash
sats knowledge                    # 列出 collections
sats knowledge 均线金叉策略       # 搜索
sats chan-kb 二买条件             # 缠论专用检索
```

### 16.3 编程接口

```python
from sats.rag.knowledge import KnowledgeStore, search_knowledge

store = KnowledgeStore(project_root)
results = store.search("均线金叉", collections=["technical", "signals"])
```

---

## 17. Agent 系统

### 17.1 概念

Agent 系统提供"计划-执行-观察"循环，允许 LLM 拆解复杂任务为多个步骤并依次执行。

```
用户任务 → 规划（AgentPlan） → 执行步骤 → 观察结果 → 下一个步骤 → 合成输出
```

### 17.2 核心类型

```python
from sats.agent import (
    AgentPlan, AgentStep, AgentObservation,
    AgentResult, AgentExecutionPolicy,
    TradeDecisionAudit, TradeIntent
)
```

- **AgentPlan**：任务分解计划（步骤列表）
- **AgentStep**：单个执行步骤（工具名 + 参数）
- **AgentObservation**：步骤执行结果
- **AgentResult**：最终结果（包含所有步骤和合成输出）
- **AgentExecutionPolicy**：执行策略（允许/禁止写入、shell、交易等）
- **TradeDecisionAudit**：交易决策审计
- **TradeIntent**：交易意图

### 17.3 工具

Agent 可用的工具定义在 `sats/agent/tools/`，包括：
- 数据获取工具（行情、K 线、指标）
- 信号分析工具
- 报告生成工具
- 知识检索工具
- 筛选工具

### 17.4 用法

```bash
sats agent 分析最近三天涨幅超过5%的股票
```

```python
from sats.agent import run_agent_once

result = run_agent_once(
    "筛选PE低于15且ROE大于15%的股票",
    settings=settings,
)
```

---

## 18. API 服务

启动：

```bash
sats serve [--host 0.0.0.0] [--port 8080]
```

### 18.1 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | HTML 管理页 |
| GET | `/health` | 健康检查 |
| POST | `/api/screen` | 执行筛选 |
| GET | `/api/screen/results` | 查询筛选结果 |
| GET | `/api/market/minute-k` | 分钟 K 线 |

### 18.2 示例

```bash
# 执行筛选
curl -X POST http://127.0.0.1:8000/api/screen \
  -H "Content-Type: application/json" \
  -d '{"trade_date": "20260606", "rule": "price_volume_ma"}'

# 查询结果
curl "http://127.0.0.1:8000/api/screen/results?trade_date=20260606&rule=price_volume_ma"

# 分钟K线
curl "http://127.0.0.1:8000/api/market/minute-k?symbols=000001.SZ&freq=15min&trade_date=20260606"
```

---

## 19. 项目结构

```
SATS/
├── sats/                        # 主包
│   ├── __init__.py             # 版本号
│   ├── __main__.py             # python -m sats 入口
│   ├── cli.py                  # 一次性 CLI（3162 行）
│   ├── repl.py                 # 交互式 REPL（1310 行）
│   ├── chat.py                 # LLM 聊天编排（2104 行）
│   ├── chat_planner.py         # 聊天意图规划
│   ├── chat_preprocessor.py    # 自然语言预处理
│   ├── chat_reference.py       # 引用上下文管理
│   ├── chat_events.py          # 聊天事件记录
│   ├── chat_artifacts.py       # 聊天产物管理
│   ├── chat_runtime.py         # 运行时工具调度
│   ├── config.py               # 配置加载（Settings）
│   ├── symbols.py              # 股票代码规范化
│   ├── skills.py               # Skills 加载与管理
│   ├── skill_routing.py        # Skills → collections 映射
│   ├── memory.py               # 聊天长期记忆
│   ├── history.py              # 交互历史
│   ├── progress.py             # 进度条（TTY 感知）
│   ├── output_saver.py         # 输出保存
│   ├── stock_basic_lookup.py   # 股票基本信息查询
│   ├── stock_question.py       # 股票问题解析
│   ├── watchlist_editor.py     # 关注列表编辑
│   ├── dependencies.py         # 可选依赖检查
│   │
│   ├── data/                   # 数据层
│   │   ├── astock_provider.py  # A 股数据统一门面
│   │   ├── tickflow_provider.py
│   │   ├── tushare_provider.py
│   │   ├── akshare_provider.py
│   │   ├── base.py
│   │   ├── resolver.py
│   │   ├── limit_sentiment.py  # 涨跌停/炸板情绪
│   │   └── tushare_stock_datasets.py
│   │
│   ├── storage/                # 存储层
│   │   ├── duckdb.py           # DuckDB 存储（1721 行）
│   │   └── schema.sql          # 数据库建表脚本
│   │
│   ├── screening/              # 筛选规则
│   │   ├── base.py             # 基类
│   │   ├── registry.py         # 规则注册
│   │   ├── service.py          # 筛选执行
│   │   ├── rule_composer.py    # AI 规则生成
│   │   └── rules/              # 规则实现
│   │       ├── price_volume_ma.py
│   │       ├── chan_composite.py
│   │       ├── chan_third_buy.py
│   │       ├── chan_signals.py
│   │       ├── trend_volume_confirmation.py
│   │       ├── signal_composite.py
│   │       └── generated/      # AI 生成的规则
│   │
│   ├── signals/                # 信号分析
│   │   ├── base.py             # 类型定义
│   │   ├── engine.py           # 信号引擎（1050 行）
│   │   └── registry.py         # 信号注册
│   │
│   ├── analysis/               # 分析模块
│   │   ├── dsa_native.py       # 原生 DSA 分析（1247 行）
│   │   ├── dsa_decision.py     # DSA 决策生成
│   │   ├── daily_stock_analysis.py
│   │   ├── opportunity_discovery.py  # 机会发现（1955 行）
│   │   ├── stock_picking_agent.py    # 选股 Agent（1358 行）
│   │   ├── stock_llm_context.py      # 个股 LLM 上下文构建
│   │   ├── market_llm_context.py     # 大盘 LLM 上下文构建
│   │   ├── quote_llm_context.py      # 行情 LLM 上下文构建
│   │   ├── stock_research_context.py  # 研究上下文
│   │   ├── chan_llm_review.py        # 缠论 LLM 复核
│   │   └── chan_chat_context.py      # 缠论聊天上下文
│   │
│   ├── indicators/             # 技术指标
│   │   └── calculator.py       # 指标计算器
│   │
│   ├── chan/                   # 缠论
│   │   └── engine.py           # 缠论引擎（809 行）
│   │
│   ├── factors/                # 因子选股
│   │   ├── base.py             # Factor 基类
│   │   ├── registry.py         # 因子注册
│   │   ├── profiles.py         # 因子画像
│   │   ├── service.py          # 因子服务
│   │   ├── composite.py        # 因子合成
│   │   ├── analysis.py         # 因子分析
│   │   ├── panel.py            # 因子面板
│   │   ├── reporting.py        # 报告生成
│   │   ├── ml.py               # 机器学习因子
│   │   └── factor_analysis_core.py
│   │
│   ├── llm/                    # LLM 层
│   │   ├── __init__.py
│   │   ├── chat.py             # ChatLLM
│   │   ├── provider.py         # Provider 构建
│   │   └── model_config.py     # 模型配置管理
│   │
│   ├── agent/                  # Agent 运行时
│   │   ├── models.py           # 类型定义
│   │   ├── runtime.py          # 运行时
│   │   ├── planner.py          # 规划器
│   │   ├── command_runner.py   # 命令执行器
│   │   ├── trading.py          # 交易审计
│   │   ├── synthesis.py        # 结果合成
│   │   ├── date_policy.py      # 日期策略
│   │   ├── progress.py         # 进度
│   │   ├── python_runtime.py   # Python 代码执行
│   │   └── tools/              # Agent 工具集
│   │
│   ├── monitoring/             # 监控
│   │   ├── service.py          # 监控服务
│   │   └── display.py          # 仪表盘渲染
│   │
│   ├── scheduler/              # 定时任务
│   │   └── service.py          # 调度器
│   │
│   ├── trading/                # 交易
│   │   ├── broker.py           # Broker 接口
│   │   ├── miniqmt_client.py   # MiniQMT 客户端
│   │   └── models.py           # 交易模型
│   │
│   ├── backtesting/            # 回测
│   │   ├── service.py          # 回测服务
│   │   └── strategy_spec.py    # 策略规范
│   │
│   ├── rag/                    # 知识库
│   │   ├── knowledge.py        # 知识检索（975 行）
│   │   └── chan_knowledge.py   # 缠论知识
│   │
│   └── api/                    # HTTP API
│       ├── app.py              # FastAPI 应用
│       └── routes/             # 路由
│
├── skills/                     # 48 个 Skills
├── knowledge/                  # 知识库
│   └── chan/rules/            # 缠论规则
├── tests/                      # 测试（66 个测试文件）
├── docs/                       # 文档
│   └── SATS_ARCHITECTURE.md   # 架构文档
├── reports/                    # 生成报告
├── data/                       # 数据（DuckDB + 缓存）
├── pyproject.toml             # 项目元数据
├── requirements.txt           # 依赖
├── AGENTS.md                  # Agent 行为规范
├── CLAUDE.md                  # Claude 编码指南
└── README.md                  # 用户手册
```

---

> **免责声明**：SATS 是研究辅助工具，所有分析结果不构成投资建议。股票交易存在风险，请谨慎决策。
