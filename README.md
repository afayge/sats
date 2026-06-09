# SATS

SATS 是一个股票自动交易系统的早期版本。当前版本已经实现 A 股筛选规则、统一信号分析、LLM Provider 基础层、CLI、FastAPI API、DuckDB 本地存储和测试用例。

当前提供趋势确认 + 温和放量、`price_volume_ma` 量价换手均线、缠论买卖点和 `signal_composite` 交织信号等筛选/分析能力。LLM Provider 已可供后续 AI 评估和实时监控模块调用，但真实交易执行仍在后续版本中实现。

## 安装

建议使用 Python 3.12 或更新版本。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## 初始化配置

生成本地 `.env` 文件：

```bash
python -m sats init
```

然后编辑 `.env`：

```env
SATS_DB_PATH=data/sats.duckdb
TUSHARE_TOKEN=你的TushareToken
TUSHARE_TIMEOUT_SECONDS=30
TUSHARE_MAX_RETRIES=2
TICKFLOW_API_KEY=你的TickFlowKey
TICKFLOW_BASE_URL=https://api.tickflow.org
TICKFLOW_TIMEOUT_SECONDS=30
TICKFLOW_MAX_RETRIES=3

DEEPSEEK_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_API_KEY=你的DeepSeekKey
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_LIGHT_MODEL=deepseek-chat

XIAOMIMIMO_PROVIDER=mimo
XIAOMIMIMO_BASE_URL=https://api.xiaomimimo.com/v1
XIAOMIMIMO_API_KEY=你的MiMoKey
XIAOMIMIMO_MODEL=MiMo-72B-A27B
XIAOMIMIMO_LIGHT_MODEL=MiMo-72B-A27B

DEFAULT_MODEL=DEEPSEEK
DEFAULT_LIGHT_MODEL=XIAOMIMIMO
LLM_TEMPERATURE=0.0
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=2
```

SATS 统一通过 `AStockDataProvider` 获取 A 股市场数据。业务模块不会直接接入 TickFlow、Tushare 或 AkShare；这些 provider 只作为 `sats/data/` 下的后端 adapter。默认优先级为 `TickFlow -> Tushare -> AkShare`：行情、K 线、实时 quote、指数和分钟 K 优先走 TickFlow；筛选输入、`daily_basic`、资金流、财务、同花顺行业/概念热点和涨跌停/炸板情绪统计主要由 Tushare 补齐；AkShare 作为可选补充，用于全市场宽度、东财实时扩展、筹码和个股摘要等字段。每个结果会保留 `data_source` / `data_sources` / `missing_fields`，缺失数据只标记，不伪造。

A 股情绪指标使用 Tushare `limit_list_d` 的涨停、跌停和炸板数据构建。公式为：`大盘系数=涨停数×3÷300×100`，`超短情绪=(涨停数+炸板数÷10)÷130×100`，`亏钱效应=(炸板数×5+跌停数×10)÷200×100`。阶段按优先级划分为：`冰点`（亏钱效应 > 50）、`冰冰点`（亏钱效应 > 30）、`高潮`（大盘系数或超短情绪 > 80）、`退潮`（大盘系数和超短情绪均 < 25）、`强势`（大盘系数或超短情绪 > 60）和 `正常`。Tushare 不可用时会用实时 quote 近似统计涨停/跌停，炸板数标记为缺失。

筛选和指标计算会沿用现有 DuckDB 缓存。`daily_basic` 没有完整实时等价数据源，PE/PB/PS/股息率等估值字段不能可靠替代；但当前筛选需要的换手率、流通市值、流通股本等字段，可在当天由实时行情或当日 K 线 + 股本数据合成内存 overlay。历史日期缺失 `daily_basic` 时不会用实时行情冒充。

## LLM Provider

SATS 的 LLM Provider 使用“模型配置组 + 默认选择”的方式管理，后续股票评估、预测分类和实时监控会通过 `sats.llm.ChatLLM` 调用模型。

核心配置：

- `<PROFILE>_PROVIDER`：模型供应商 adapter，例如 `deepseek`、`openrouter`、`qwen`、`moonshot`、`mimo`、`ollama`。
- `<PROFILE>_MODEL`：主模型名称，例如 `deepseek-chat`、`qwen-plus`。
- `<PROFILE>_LIGHT_MODEL`：轻量任务模型；为空时回退到同组 `<PROFILE>_MODEL`。
- `<PROFILE>_BASE_URL` / `<PROFILE>_API_KEY`：OpenAI-compatible endpoint 和密钥。
- `DEFAULT_MODEL`：主模型配置组名称，例如 `DEEPSEEK`。
- `DEFAULT_LIGHT_MODEL`：轻量模型配置组名称；未设置时跟随 `DEFAULT_MODEL`。
- `LLM_TEMPERATURE`：生成温度，默认 `0.0`。
- `LLM_TIMEOUT_SECONDS`：单次调用超时时间，默认 `120`；常规 light 调用和 fallback default 调用都使用这个超时时间。
- `LLM_MAX_RETRIES`：重试次数，默认 `2`。
- `LLM_REASONING_EFFORT`：可选，传给需要显式开启 reasoning 的中转服务。

支持的 OpenAI-compatible providers：

```text
openai, openrouter, deepseek, gemini, groq, dashscope, qwen,
zhipu, moonshot, minimax, mimo, zai, ollama
```

最小调用示例：

```python
from sats.llm import ChatLLM, extract_json_object

llm = ChatLLM()
response = llm.chat([
    {"role": "system", "content": "你是A股研究助手。"},
    {"role": "user", "content": "用JSON给出000001.SZ的简短观察。"},
])

data = extract_json_object(response.content or "")
print(response.content)
print(data)
```

旧配置 `LANGCHAIN_*`、`OPENAI_MODEL`、`LLM_PROVIDER` 已移除，不再作为 fallback；请迁移到 `<PROFILE>_* + DEFAULT_*`。当前版本不实现 Vibe-Trading 的 `openai-codex` OAuth provider。

可以用命令查看和切换模型：

```bash
sats model status
sats model list
sats model use DEEPSEEK --target main
sats model use XIAOMIMIMO --target light
```

如果配置了 `DEFAULT_LIGHT_MODEL`，SATS 会把自然语言预处理、普通聊天最终回答、工具调用后的总结、自然语言选股/机会排序、长期记忆抽取、会话滚动摘要和监控信号摘要这类常规 LLM 任务切到轻量 profile；light 调用失败时会自动尝试默认主模型，两次调用都遵循 `LLM_TIMEOUT_SECONDS`，若主模型也失败则保持各功能原有降级/报错行为。DSA 原生复核、缠论候选复核、信号 AI 生成/解释和规则生成/确认等高风险或深度复核路径仍继续使用主模型。

## LLM 聊天模式与 Skills

交互式 CLI 支持直接调用当前 `.env` 配置的 LLM。进入 `sats` 后，普通文本默认作为聊天消息发送给模型；但只要本轮问题包含 A 股代码，SATS 都会先获取真实日线、指标、价格上下文、15m/30m 分钟 K，以及真实 A 股大盘指数和市场宽度数据，再把结构化数据注入 LLM。大盘类问题会先经过轻量预处理规划“大盘研究篮子”，再按真实数据取数后交给常规 light/fallback 流程分析；默认候选池包含上证指数、深证成指、创业板指、深证100、沪深300、中证500、科创50和北证50。对“给出几个未来几天可能上涨的股票”等自然语言选股问题，SATS 会先用 Analyze 的中短期上涨信号做临时全市场筛选，再补充大盘、行情和财务/估值上下文做候选排序。LLM 只回答、解释和建议命令，不会自动交易。

聊天默认启用本地记忆：SATS 会把会话消息、滚动摘要和长期记忆保存到 `.env` 中 `SATS_DB_PATH` 指向的 DuckDB。长期记忆通过关键词和标签检索注入上下文，第一版不使用外部向量库或云端数据库。临时问题可以使用 `--no-memory` 跳过记忆读取和写入。

聊天还会加载本地股票知识库 RAG：单股自然语言分析会默认把技术、信号、缠论、大盘、A 股情绪、基本面、风险和 `stock_basic` 股票名称代码表等股票域 collection 纳入检索范围；其他问题仍按意图或显式 `--knowledge` 选择相关知识库。SATS 参考 open-webui 的知识库/文件/collection/chunk 流程，但首版使用本地 DuckDB 混合关键词检索，不依赖外部向量库。RAG 只提供方法论、规则说明、股票名称/代码映射和引用证据，不会替代真实行情；真实价格、指标、大盘宽度和交易判断仍必须来自 SATS 结构化市场数据。

```text
sats> 帮我解释 price_volume_ma 策略
sats> 用缠论分析002436
sats> 继续分析它的三买结构
sats> 分析002436 2026-05-15
sats> 继续分析它们 2026-05-18
sats> 看002436 MACD 20260515
sats> 今天A股大盘分析，明天和下周走势预测
sats> 给出几个股票，预计未来几天有上涨趋势的股票
sats> 新增一个低位放量突破筛选规则
sats> 确认生成规则 nl_low_volume_breakout
sats> /chat 帮我解释筛选结果怎么查询
sats> /chat --knowledge chan 解释三买和背驰
sats> /chat --no-memory 临时问题
sats> 上一个输出到markdown文件
sats> /save --format pdf
sats> 筛选短线机会并保存报告
sats> /chat 生成一个均线策略并回测000001
sats> /goal 明天按信号自动买入不超过2万
sats> /knowledge search --query 三买 --knowledge chan
sats> /knowledge ingest --knowledge chan --path knowledge/chan/rules
sats> /knowledge sync-stock-basic
```

聊天模式会先做轻量输入规划，再匹配相关 skill、拉取真实研究数据，必要时调用 SATS 白名单内部分析能力，最后才把结构化上下文交给 LLM。为避免模拟行情，普通文本、`/chat` 和 `sats chat` 遇到股票代码或可唯一识别的股票名称都会先统一解析为 `ts_code`，再同时取真实个股数据和真实大盘数据；如果个股日线/分钟 K 或核心指数日线等硬数据缺失，SATS 会直接报错并停止调用 LLM。大盘类问题支持把“明天”“后天”“明后”“下周”这类相对时间直接映射为分析 horizon，不要求用户额外给出单一指数或绝对日期。大盘市场宽度、财务、热点板块或实时 quote 等辅助数据缺失时会标记 `missing_fields`，不会用旧数据冒充。指定交易日时使用该日数据；指定日内时点时，15m/30m 曲线会截断到该时点。REPL 同一个 `ChatSession` 内，`继续分析它/它们/这只/这些` 等追问会继承上一轮股票代码和时间；一次性 `sats chat` 不从长期记忆恢复股票代码，追问时仍需写明代码或股票名称。若股票简称匹配到多个结果，SATS 会要求用户改用 6 位代码。

REPL 支持把上一条输出保存为文件：可以输入 `/save --format md|pdf`，也可以用“上一个输出到 markdown 文件”“保存刚才对话为 PDF”这类自然语言请求。默认文件写入 `reports/saved_outputs/`；需要指定路径时使用 `/save --path <PATH>`。

聊天也支持用自然语言创建新的筛选规则。用户先描述规则，例如“新增一个低位放量突破筛选规则”，SATS 会生成包含中文决策名称、`rule_name`、数据依赖、条件、评分和风险说明的规则计划；如果描述里包含当前 `ScreeningInput` 不支持的数据，例如新闻、分钟级盘口、筹码或资金流，SATS 会先要求用户确认降级方式。只有在同一个 REPL 会话里回复 `确认生成规则 <rule_name>` 后，SATS 才会用受控模板生成 Python 文件到 `sats/screening/rules/generated/`，并自动加入 `screen --rule <rule_name>` 可用的筛选规则注册表。一次性 `sats chat` 适合生成计划，真正写代码建议在 REPL 中完成二次确认。

自然语言默认进入 Agent-first runtime：REPL 普通输入、`/chat ...` 和 `sats chat ...` 都会先由 Agent 理解目标，再选择普通 `chat.answer`、A 股数据 resolver、research、factor、SATS argv 命令、受限 Python 或交易工具。普通解释和总结只是 Agent 的只读聊天子工具；需要取数、筛选、回测、保存报告、监控、定时或交易时，Agent 会生成多步计划并写入 `turn/event/item/artifact` trace。`/goal <目标>` 保留为强制目标执行入口；`sats agent ...` 仅作为兼容入口，不再是推荐用法。

Agent 工具分为 `chat.*`、`data.*`、`research.*`、`factor.*`、`sats_command.*` 和 `trade.*`。股票/指数价格、成交量、K 线、分钟 K、报价和指标输入采用 DuckDB-first resolver：先读本地 DuckDB，缺失、覆盖不足或实时 quote 过期时才调用 `AStockDataProvider`，provider 成功后写回 DuckDB。LLM 不能自造行情数据；没有 SATS provenance 的市场数据不能进入分析、Python 策略或交易决策。Agent 自动执行 SATS 命令时只使用 argv runner，不开放任意 shell。LLM 生成 Python 只在受限 runtime 中运行，禁止 import、文件、网络和 subprocess，只能通过注入的 resolver 获取行情。

自动交易默认关闭。只有本轮显式传入 `--auto-trade buy,sell`，并在需要实盘时同时传入 `--broker qmt --live-trading`，Agent 才能提交 QMT 订单；未传 `--live-trading` 时交易命令会 dry-run。下单前 SATS 会重新用 DuckDB/provider resolver 获取最新 quote，并校验 100 股买入整数倍、可用持仓、`--max-order-value`、`--max-position-pct` 和 `--sell-ratio`，所有订单写入 broker order、monitor trade event 和 agent trace。

一次性命令也可以调用聊天：

```bash
python -m sats chat 帮我解释筛选规则
sats chat 帮我解释筛选规则
python -m sats chat 今天A股大盘分析，明天和下周走势预测
python -m sats chat 生成一份000001均线研究报告并保存
python -m sats chat 写一个5日和20日均线策略并回测000001
python -m sats chat --confirm act_xxxxxxxx
python -m sats chat --trace turn_xxxxxxxx
python -m sats chat --knowledge chan 解释三买和背驰
python -m sats chat --no-memory 临时问题
sats chat --no-memory 临时问题
python -m sats chat --max-iterations 4 筛选短线机会并保存报告
sats chat 生成一个均线策略并回测000001
sats chat --auto-trade buy --broker qmt --live-trading --max-order-value 20000 明天按信号自动买入不超过2万
```

SATS 本地 skills 位于工程根目录 `skills/<skill_id>/SKILL.md`。Agent 可以通过只读 `chat.list_skills` / `chat.load_skill` 按需查看本地 skill 的完整内容；缠论问题可调用本地缠论知识库，知识库问题可调用 RAG search。A 股大盘、个股研究、自然语言选股、内部分析、Tushare 白名单数据、因子分析/选股/ML、monitor、schedule、qmt 等 SATS 能力都以工具形式暴露给 Agent，但仍按只读、写产物/写库、长任务、实盘交易分级执行，不会执行任意 shell 命令或未封装的外部接口。

管理本地知识库：

```bash
python -m sats knowledge list
python -m sats knowledge add --name chan --description "缠论规则和买卖点"
python -m sats knowledge ingest --knowledge chan --path knowledge/chan/rules --tags chan,缠论
python -m sats knowledge search --query 三买 --knowledge chan --limit 6
python -m sats knowledge sync-stock-basic

sats knowledge list
sats knowledge add --name technical --description "技术指标和信号"
sats knowledge ingest --knowledge technical --path skills/technical-basic/SKILL.md --tags 技术指标
sats knowledge search --query MACD --knowledge technical
sats knowledge search --query 紫光股份 --knowledge stock-basic
```

`knowledge sync-stock-basic` 会把当前 DuckDB 缓存中的 Tushare/TickFlow `stock_basic` 股票列表同步成 `stock-basic` 知识库文档块。日常输入 `--stocks`、`--symbols`、`--symbol` 时也可以直接写可唯一识别的股票名称，例如 `紫光股份`；SATS 会用本地 `stock_basic` 解析为 `000938.SZ` 后再走真实行情数据获取。

`.sats_history` 只用于提示符阶段的斜杠命令回放；REPL 中普通聊天、`/chat`、斜杠 CLI 命令及其结果会写入 DuckDB 的交互历史表。历史记录独立于长期记忆，不会被 `memory clear` 清除。查看和管理交互历史：

```bash
python -m sats history list
python -m sats history search 股票 --kind chat
python -m sats history show hist_xxxxxxxx
python -m sats history delete hist_xxxxxxxx

sats history list
sats history search 股票 --kind command
sats history show hist_xxxxxxxx
sats history delete hist_xxxxxxxx
```

交互式 CLI 中也可以使用：

```text
sats> /history list
sats> /history search 股票
sats> /history show hist_xxxxxxxx
sats> /history delete hist_xxxxxxxx
```

聊天内容和长期记忆由 DuckDB 记忆表管理。查看和管理本地记忆：

```bash
python -m sats memory list
python -m sats memory search 股票
python -m sats memory forget mem_xxxxxxxx
python -m sats memory clear --yes

sats memory list
sats memory search 股票
sats memory forget mem_xxxxxxxx
sats memory clear --yes
```

交互式 CLI 中也可以使用：

```text
sats> /memory list
sats> /memory search 股票
sats> /memory forget mem_xxxxxxxx
sats> /memory clear --yes
```

查看可用 skills：

```bash
python -m sats skills
sats skills
```

交互式 CLI 中也可以使用：

```text
sats> /skills
```

Skills 按 `category` 分组显示，并支持 `source`、`requires_tools`、`triggers` front matter。现有核心 skill 包括 `sats-market-assistant`、`chan-theory`、`tickflow`、`tushare-data`，并已从 Vibe-Trading 改写引入一批 SATS 化研究 skill：

- 数据源：`data-routing`、`akshare`；`tushare-data` 合并了 Vibe-Trading 的 Tushare 数据研究指引。
- A 股核心：`ashare-pre-st-filter`、`fundamental-filter`、`financial-statement`、`valuation-model`、`regulatory-knowledge`；`financial-statement` 也合并了 China-market 的财报质量、杜邦、Z/F/M 分数和红旗检查框架。
- 技术分析：`candlestick`、`elliott-wave`、`technical-basic`、`volatility`、`market-microstructure`、`minute-analysis`。
- DSA 策略：`bull-trend`、`shrink-pullback`、`ma-golden-cross`、`volume-breakout`、`box-oscillation`、`bottom-volume`、`one-yang-three-yin`、`dragon-head`、`hot-theme`、`emotion-cycle`、`expectation-repricing`、`growth-quality`；这些来自 `daily_stock_analysis/strategies/*.yaml` 的自然语言策略包，已改写为 SATS Markdown skill。
- China-market 选股/估值：`quant-factor-screener`、`high-dividend-strategy`、`undervalued-stock-screener`、`small-cap-growth-identifier`、`tech-hype-vs-fundamentals`。
- China-market ESG/组合/风控：`esg-screener`、`portfolio-health-check`、`risk-adjusted-return-optimizer`、`suitability-report-generator`。
- China-market 事件/情绪：`event-driven-detector`、`insider-trading-analyzer`、`sentiment-reality-gap`。
- 研究输出：`risk-analysis`、`report-generate`、`sector-rotation`、`sentiment-analysis`、`corporate-events`；`sector-rotation` 合并了 China-market 的宏观周期和行业轮动框架。
- 工作流模板：`workflow-templates` 收纳 `equity_research_team`、`investment_committee`、`quant_strategy_desk`、`risk_committee`、`portfolio_review_board`、`sector_rotation_team`、`factor_research_committee`、`fundamental_research_team`、`technical_analysis_panel` 等 SATS 单助手研究流程。

这些 skill 是 LLM 上下文，不等于完整 Vibe-Trading swarm，也不等于迁入 `daily_stock_analysis` 的 multi-agent runtime；DSA 策略只作为 SATS 原生 `dsa`、聊天规划和知识库可用的方法论补充。China-market 的 `findata-toolkit-cn` 可执行脚本没有迁入 SATS，也不会通过聊天或 REPL 暴露任意 shell 入口；其数据能力只映射为 SATS 已封装的数据边界说明，真实行情、财务、公告、资金流和 A 股大盘仍必须通过 `AStockDataProvider` 以及 `sats/data/` 下的 TickFlow/Tushare/AkShare 适配层取得。通用 Tushare 数据集接口 v1 只读、限量返回、不写 DuckDB；若某个 skill 依赖的数据源或工具尚未接入，回答必须明确标注限制。涉及股票或交易判断的回答不构成投资建议。

## CLI 使用

SATS 的命令行功能统一提供三种入口。新增用户可调用功能时，也会同步补齐这三种用法和对应文档：

- `sats`：进入交互式 CLI 后使用 `/command ...`。
- `sats <command> ...`：通过安装后的 console script 执行一次性命令。
- `python -m sats <command> ...`：不依赖 console script 的模块执行方式。

安装为可执行命令后，直接输入 `sats` 会进入交互式 CLI：

```bash
sats
```

交互界面启动时会显示两个与终端宽度一致的方框：标题方框显示当前版本号，命令提示方框显示常用命令和 DuckDB 路径。等待输入时，`sats> ` 上下会显示与终端同宽的淡灰色横线；底部状态栏会显示当前 LLM 模型。参数和现有一次性 CLI 完全一致：

```text
sats> 帮我解释 price_volume_ma 策略
sats> 用缠论分析002436
sats> 分析002436 2026-05-15
sats> /chat 帮我解释筛选规则
sats> /chat --no-memory 临时问题
sats> /memory search 股票
sats> /history list
sats> /screen --trade-date 20260514 --rule price_volume_ma
sats> /results --trade-date 20260514 --passed
sats> /result-rules
sats> /skills
sats> /exit
```

内置控制命令：

- `/help`：以终端宽度方框查看可用命令和示例，命令和说明使用同色显示，并按列左对齐。
- `/clear`：清屏。
- `/exit`、`/quit`：退出交互界面。
- 普通文本：概念问题调用 LLM 聊天；包含股票代码的分析问题会先注入真实行情和 15m/30m 曲线。
- `/chat ...`：同样会对股票代码分析问题先取真实数据再调用 LLM。
- `/memory ...`：查看、搜索、删除或清空本地聊天记忆。
- `/history ...`：查看、搜索、展示或删除 REPL 交互历史。

带参数时，`sats` 仍可作为一次性命令使用：

```bash
sats chat 帮我解释筛选规则
sats chat --no-memory 临时问题
sats memory list
sats memory search 股票
sats history list
sats history search 股票 --kind chat
sats history show hist_xxxxxxxx
sats history delete hist_xxxxxxxx
sats indicators --symbols 000001 --trade-date 20260514
sats indicators --symbols 000001
sats dsa --stocks 000001,600519 --trade-date 20260514
sats skills
sats screen --trade-date 20260430
sats results --trade-date 20260430 --passed
```

如果未安装 editable 包，也可以继续使用 `python -m sats ...`。

所有需要输入 A 股股票代码的命令都支持裸代码和带后缀代码，例如 `000001` 会自动转换为 `000001.SZ`，`605300` 会自动转换为 `605300.SH`；DuckDB 存储和命令输出统一使用带后缀的 `ts_code`。

## 进度条

耗时命令在交互式终端中会显示 SATS 内置任务面板，不依赖 `rich` 或 `tqdm`。面板会显示当前请求、当前步骤、步骤表格、耗时和 `#/-` 总体进度条。覆盖的步骤包括全市场数据获取、筛选规则计算、Analyze 信号计算、DSA/外部 DSA 分析、Discover 热点板块和候选增强、LLM 调用、报告生成、分钟 K 获取、指标计算和前台监控数据获取等。

```text
┌────────────────────────────────────────── SATS ──────────────────────────────────────────┐
│Running agent                                      18.2s  ####----------------  1/5       │
│Request: sats discover --limit 5                                                        │
│                                                                                         │
│Current  deepseek-v4-pro 排名                                                            │
│                                                                                         │
│  State       Tool                       Time  Detail                                     │
│  ────────────────────────────────────────────────────────────────────────────────────── │
│  ok          全市场数据                  5.1s  5300 只                                  │
│  running     deepseek-v4-pro 排名       13.1s                                             │
│  ────────────────────────────────────────────────────────────────────────────────────── │
│  Recent details                                                                         │
│  ok  全市场数据: 5300 只                                                                 │
│  running  deepseek-v4-pro 排名: 正在对候选池排序                                         │
│                                                                                         │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

非交互终端、管道输出和测试捕获默认不显示进度条；`--json` 模式也会自动静默，确保 stdout 仍是可解析 JSON。部分分析命令在非 JSON 的非交互输出中仍保留 `analyzing...`，用于兼容已有脚本。

## 实时价格

`quote` 用于查看指定股票的实时价格表。命令会先获取真实实时行情，再结合历史日线计算 `MA5 / MA20 / MA60 / MA250`，并按 `周线 / 月线 / 季线 / 年线` 输出对应价格值。

```bash
python -m sats quote --stocks 000001,600519
sats quote --stocks 000001,紫光股份
```

交互式 CLI：

```text
sats> /quote --stocks 000001,600519
sats> /quote --stocks 000001,紫光股份
```

输出列固定为：`序号 股票代码 股票名称 现价 涨跌幅 周线 月线 季线 年线`。

参数说明：

- `--stocks`：逗号分隔的股票代码或可唯一识别的股票名称，支持裸代码、带后缀代码和 `stock_basic` 名称。
- `--db PATH`：指定 DuckDB 文件；不传则使用 `.env` 中的 `SATS_DB_PATH`。

## 技术指标

`indicators` 用于计算日线级技术指标。未传 `--trade-date` 时使用 SATS 现有最新交易日解析逻辑；行情通过 `AStockDataProvider` 获取，默认优先 TickFlow K 线，再用 Tushare 缓存/日线兜底。资金流和完整基本面以 Tushare 为准，TickFlow 暂只提供行情和股本类 daily_basic-like 备份，AkShare 只作为可选补充。

计算内容包括 MA/SMA/EMA、MACD、RSI(6/12/24)、布林带、ATR、KDJ/随机指标、蜡烛图形态、成交量分析、支撑/阻力、启发式艾略特波浪、主力资金流 1d/5d/10d，以及 PE/PB/市值/营收/利润/ROE/负债率等基本面字段。

```bash
python -m sats indicators --symbols 000001.SZ,600519.SH --trade-date 20260514
sats indicators --symbols 000001,600519 --trade-date 20260514
python -m sats indicators --symbols 000001
python -m sats indicators --symbols 000001 --trade-date 20260514 --json
```

交互式 CLI：

```text
sats> /indicators --symbols 000001 --trade-date 20260514
sats> /indicators --symbols 000001 --trade-date 20260514 --json
```

参数说明：

- `--symbols`：逗号分隔的股票代码或可唯一识别的股票名称，支持 `000001.SZ,600519.SH`、裸代码 `000001,600519` 或 `紫光股份`；存储和输出会统一为带后缀的 `ts_code`。
- `--trade-date`：指标计算截止交易日，格式 `YYYYMMDD`。
- `--lookback-days`：历史窗口，默认 `180`。
- `--json`：输出完整结构化 JSON；不传时输出紧凑文本摘要。
- `--db PATH`：指定 DuckDB 文件；不传则使用 `.env` 中的 `SATS_DB_PATH`。

艾略特波浪为启发式峰谷识别，只作为辅助结构提示，不作为严格交易结论。

## 股票因子

`factor` 是 SATS 原生因子入口，使用 `sats.factors` 的宽表 panel 计算、分析和多因子选股。当前内置三类因子库：

- `alpha101`：迁入本地已实现的 101 Formulaic Alphas，文档中常称 WorldQuant 101 Alpha / Kakushadze 101 Formulaic Alphas，来源参考 arXiv `101 Formulaic Alphas`。
- `gtja191`：迁入本地已实现的国泰君安 Alpha191 短周期量价因子，保留 WMA/SMA/REGBETA 等近似实现说明。
- `barra_style`：SATS 自研公开风格近似因子，只实现 size/value/quality/momentum/beta/liquidity/crowding 等可解释代理，不复制 MSCI Barra 专有模型、协方差矩阵、优化器或官方 security exposure 数据。

```bash
sats factor list --zoo alpha101
sats factor list --zoo gtja191 --theme volume
sats factor list --zoo barra_style
sats factor show --factor gtja191_001
sats factor analyze --factor gtja191_001 --trade-date 20260514 --lookback-days 260
sats factor pick --profile balanced --trade-date 20260514 --top 20
sats factor pick --factors barra_style_value,barra_style_quality,barra_style_momentum --trade-date 20260514 --top 20 --neutralize industry
sats factor pick --factors alpha101_001,gtja191_001,barra_style_value --trade-date 20260514 --weight ic --write-screening --screening-profile alpha_mix
sats factor ml status
sats factor ml setup
sats factor ml train --profile balanced --model lightgbm --train-start 20250101 --train-end 20260430 --valid-end 20260514
sats factor ml predict --model-run factor_ml_xxxxxxxx --trade-date 20260514 --top 20 --write-screening
```

交互式 CLI：

```text
sats> /factor list --zoo barra_style
sats> /factor analyze --factor barra_style_value --trade-date 20260514
sats> /factor pick --profile balanced --trade-date 20260514 --top 20
sats> /factor pick --factors barra_style_value,barra_style_quality --trade-date 20260514 --top 20
sats> /factor ml status
sats> /factor ml train --profile balanced --model lightgbm --train-start 20250101 --train-end 20260430 --valid-end 20260514
sats> /factor ml predict --model-run factor_ml_xxxxxxxx --trade-date 20260514 --top 20 --write-screening
sats> /chat 分析000001的因子暴露
```

因子数据统一来自 `AStockDataProvider` 和 DuckDB 缓存，宽表字段包含 `open/high/low/close/volume/vwap/amount/industry/pe/pb/ps/turnover_rate/float_mv/total_mv/roe/debt_to_assets/main_net_amount` 等。缺少 `roe/debt_to_assets/dividend_yield/state_ownership` 时，相关 Barra 风格因子会明确 skip 或 degraded，不会补假值。`factor analyze` 输出 IC、RankIC、ICIR、RankICIR、覆盖率、缺失率、分组收益和 long-short spread；`factor pick` 支持多因子 z-score、行业中性、等权或 IC 权重、TopN 选股。未传 `--factors` 时会使用因子画像：`balanced` 默认综合价值/质量/动量/流动性/拥挤度，`short_term` 偏短周期量价，`fundamental_quality` 偏估值和质量。

`discover` 和自然语言选股 Agent 默认会叠加 `balanced` 因子画像作为软排序证据；因子缺失只写入候选的 `missing_fields`，不会淘汰股票。单股/多股自然语言分析上下文会附带轻量 `factor_summary`，用于解释因子暴露、优势、短板和数据缺失。因子分数和 ML 预测都只是研究证据，不代表确定收益或交易指令。

因子运行只写轻量摘要：`factor_runs` 记录参数、指标和报告路径，`factor_candidates` 记录候选排名和得分，不默认保存全量逐日逐票因子矩阵。`--write-screening` 会把 TopN 写入 `screening_results`，普通因子选股规则名为 `factor:<screening-profile>`，因子 ML 预测规则名为 `factor_ml:<model_run_id>`，可继续被 `results`、`analyze --from-screened`、`dsa --from-screened` 复用。默认 Markdown 报告写入 `reports/factors/`。

`factor ml` 是 SATS 原生 LightGBM/XGBoost 因子训练预测入口：从当前因子画像或 `--factors` 构造监督样本，标签为未来 `--horizon` 日收益，模型文件写入 `models/factors/<model_run_id>/model.pkl`。它不走 Qlib `qrun/provider_uri`，但沿用可选依赖自愈：`factor ml status` 只检查 `pyqlib/lightgbm/xgboost/scikit-learn` 是否可导入，不安装也不写文件；`factor ml setup` 和后续 `train/evaluate/predict` 路径会在当前 Python 位于项目 `.venv` 内时自动执行 `sys.executable -m pip install pyqlib lightgbm xgboost scikit-learn`，安装成功后同步 `requirements.txt` 与 `pyproject.toml` 的 `ml/deep` optional extras。普通 `factor list/show/analyze/pick`、默认聊天因子摘要和 `discover` 不触发 ML 依赖安装。深度模型 `torch` 只在 `deep` extra 中声明，不随第一批 Qlib/ML setup 自动安装。

## 统一 Analyze 信号分析

`analyze` 是新的统一股票分析入口，通过 `--signals` 选择一个或多个信号策略。第一版已把 Abu README 第 28-31 节的四类交织策略改写为 SATS 本地启发式信号：`graph_graph`、`ma_graph`、`kline_graph`、`ma_kline`，并融合图形、趋势线、均线、K 线、波浪、谐波和本地缠论信号。该功能只给出本地分析提示，不会自动交易。

```bash
sats analyze --stocks 000938 --signals ma_kline,chan
sats analyze --stocks 000938 --signals graph_graph,kline_graph --trade-date 20260520 --noreport
sats analyze --from-screened --trade-date 20260519 --rule price_volume_ma --signals all
sats analyze signals
sats analyze signals --category kline
```

交互式 CLI：

```text
sats> /analyze --stocks 000938 --signals ma_kline,chan
sats> /analyze --from-screened --trade-date 20260519 --rule price_volume_ma --signals all
sats> /analyze signals --category ma_kline
```

参数说明：

- `--stocks`：逗号分隔的股票代码或可唯一识别的股票名称，支持裸代码、带后缀代码和 `stock_basic` 名称。
- `--from-screened`：读取 DuckDB 中指定日期、指定规则的 `passed=true` 筛选结果。
- `--signals`：信号组或信号 id，支持 `all`、`graph`、`trendline`、`ma`、`kline`、`wave`、`harmonic`、`chan`、`graph_graph`、`ma_graph`、`kline_graph`、`ma_kline`，也可逗号组合。
- `analyze signals --category CATEGORY`：查看某类可用信号策略。
- `--report` 为默认行为；使用 `--noreport` 才跳过 Markdown 报告生成。
- `--json`：输出完整结构化结果。
- `--llm-review`：可选调用 LLM 做简短解释；LLM 不可用时仍输出本地信号结果并提示。

默认文本输出包含综合评分、买/卖/观望倾向、命中的子信号、趋势结论和关键支撑/压力位；默认报告写入 `reports/signal_analysis_*.md`。

## 自然语言短线机会发现

`discover` 用于回答“给出几个未来几天可能上涨的股票”这类无指定代码的选股研究问题。无自然语言参数时保持旧的短线机会发现流程；带自然语言参数时会先运行 SATS 自然语言选股 Agent：根据用户约束匹配现有 skills，检索本地 DuckDB knowledge collections，再解析是否存在 `MLCC相关股票`、`固态电池概念股`、`HBM产业链` 这类主题股票池。没有明确主题时临时全量读取 A 股股票池和日线数据；有主题时依次查询真实同花顺行业/概念板块、申万行业分类和申万行业成分构成(分级)，若这些结构化板块都没有可校验的 A 股成分股，才询问轻量 LLM“该主题相关 A 股股票有哪些”，并用本地 `stock_basic` 校验 LLM 明确列出的具体股票，再对完整股票池运行 Analyze 的 `short_up` 中短期上涨信号。它不会运行 `screen --rule`，也不会写入 `screening_results`。

默认会启用热点板块优先逻辑：SATS 从 Tushare 同花顺行业/概念接口读取 `ths_index`、`ths_daily`、`ths_member`，用最近 5 个交易日的 5 日表现、3 日表现、上涨天数和最新涨跌幅计算持续热度。热点行业和概念里的股票会获得最多 12 分的排名加权，但不是硬过滤；非热点里技术信号很强的股票仍可入选。若 Tushare 无权限、接口不可用或缓存为空，会在 `missing_fields` 标记并退回纯信号排序。

候选池质量优先：全市场或主题股票池先通过 Analyze `short_up` 筛出有买入事件、评分达标且卖出信号不强的候选，再做本地排序、热点加分和缠论买点加分。`ranking_score` 会合并本地信号分、热点分和 Analyze 已产出的缠论买点分；送入 LLM 的分析池默认最多 50 只，候选不足时只使用实际符合条件的股票，不补齐弱信号。

主题股票池不会用相似大板块替代用户主题：如果 `MLCC` 没有同花顺精确概念板块或申万行业命中，SATS 不会自动关联到 `电子元件` 或 `消费电子`，也不会从 LLM 返回的板块名继续扩展成分股。LLM 兜底只保留它明确列出的具体 A 股股票，不设置数量上限或下限，输出会标注 `主题股票池: LLM 主题线索 MLCC，经本地 stock_basic 校验，共 N 只` 和 `短期信号候选: M 只`；`N` 是完整主题股票池，`M` 是通过真实 Analyze 短线信号的候选。后续排序、触发、失效和风险仍全部来自 SATS 真实行情、指标、信号和大盘上下文。

```bash
sats discover
sats discover --trade-date 20260521 --limit 5
sats discover --signals short_up --candidate-limit 50
sats discover --hot-sector-days 3
sats discover --no-hot-sector
sats discover 热点板块共振，避开 ST，未来几天可能上涨
sats discover --limit 5 低估值基本面稳健，资金流改善
sats discover MLCC相关股票，未来几天可能上涨
sats discover --json
```

交互式 CLI：

```text
sats> /discover --limit 5
sats> /discover --limit 5 按缠论三买和热点板块共振选股
sats> /discover --limit 5 MLCC相关股票，避开风险过高的
sats> 给出几个股票，预计未来几天有上涨趋势的股票
```

参数说明：

- `--signals`：默认 `short_up`，只保留 `ma_kline`、`kline_graph`、`ma_graph`、`graph_graph`、`chan`、`trendline` 中买入方向的中短期信号。
- `--candidate-limit`：送入 LLM 排序的本地候选数上限，默认 `50`；若真实符合 short_up 质量条件的候选不足该数量，不会补齐。
- `--limit`：最终输出股票数，默认 `5`。
- `--hot-sector-days`：热点板块持续性参考天数，只支持 `3`、`4`、`5`，默认 `5`。
- `--no-hot-sector`：关闭热点板块加权，按本地信号分排序。
- `--noreport`：跳过 Markdown 报告；默认报告写入 `reports/opportunity_discovery_*.md`。

自然语言选股 Agent v1 支持短线技术、热点板块、缠论结构、基本面质量、风险优先和主题股票池等研究框架。skills/RAG 只提供方法论、规则说明和来源证据；真实候选仍必须来自 SATS 结构化行情、指标、大盘和数据源适配器。JSON 输出会包含 `agent_plan.theme` 和 `theme_universe`，其中 `theme_universe.stocks` 保留完整主题股票池，`opportunity_discovery.candidates` 只表示通过短线信号的候选。如果 LLM 不可用，SATS 会输出本地信号排序并提示 `大模型不可用，已使用本地信号排序。`。所有结果都是观察候选和触发条件，不构成投资建议，也不保证未来上涨。

## DSA 原生股票分析

`dsa` 用于复刻 `daily_stock_analysis main.py --stocks` 的一次性个股批量分析能力。SATS 原生实现不 shell 调用外部 `daily_stock_analysis`，而是直接复用当前 SATS 的数据源、指标系统和 LLM 配置生成 DSA-like Markdown 报告。原生 DSA 会按 DSA 风格给出 `强烈买入`、`买入`、`持有`、`观望`、`减仓`、`卖出`、`强烈卖出` 等操作评级，目标是靠近日线 DSA 的评级风格和风控约束，不保证逐字逐分一致。

`daily_stock_analysis/strategies/*.yaml` 中的策略 skill 已改写进 SATS 本地 `skills/`，例如多头趋势、缩量回踩、均线金叉、放量突破、箱体震荡、底部放量、龙头、热点题材、情绪周期、预期重估和成长质量。聊天会把这些 skill 当作方法论和 RAG 上下文使用；真正的数据获取和 DSA-like 报告仍由 SATS 原生 `AStockDataProvider`、指标系统和 `sats dsa` 执行。

数据源统一由 `AStockDataProvider` 调度：

- TickFlow：优先提供日线 K、实时 quote、名称和当日 daily_basic-like 行情字段。
- Tushare：补充 `daily_basic`、资金流、利润表、财务指标和资产负债表等严肃基本面字段。
- AkShare：可选补充东财实时扩展字段、筹码/获利盘估算、板块/财务摘要；未安装或接口失败时自动降级。
- 新闻/舆情：v1 不启用外部搜索，报告中会标记 `新闻/舆情：未启用`。

原生 DSA 的最终评分和评级由本地规则裁决；LLM 只用于生成摘要、风险和理由，不会直接覆盖 `score/advice/trend`。报告会生成靠近 `daily_stock_analysis` 的决策仪表盘结构：信息面、核心结论、数据视角、战术计划、风险提示和数据源；其中新闻/舆情在 v1 中明确标记为未启用，热点板块、筹码、资金流、基本面缺失时会写入 `missing_fields`。当乖离率超过 5%、MA10/MA20 乖离过高、RSI/KDJ 超买、BOLL 上轨外、获利盘过高、资金未确认、接近压力位或基本面偏弱时，买入类评级会优先下调到 `持有/观望`；高位过热但趋势仍偏多时不会直接给出激进 `减仓`。`dsa` 默认启用 LLM 复核，但单次调用默认 20 秒超时；如果 LLM 不可用、调用失败或返回非法评级，本轮剩余股票会直接降级到本地规则，终端提示 `大模型不可用，已使用本地规则评级。`，并继续生成排名和报告。需要最快本地分析时可加 `--no-llm`。

`dsa --from-screened` 会继续分析 SATS 原生支持的全部股票；但 `688` 科创板和北交所等外部 `daily_stock_analysis` 不支持的股票会在终端和报告中单独标记为 `daily_stock_analysis 不支持`，并排在“原生额外股票”分组，便于和外部 `analyze-dsa` 报告对照。

```bash
python -m sats dsa --stocks 000001,600519
sats dsa --stocks 000001,600519 --trade-date 20260518
sats dsa --from-screened --trade-date 20260518 --rule chan-composite
sats dsa --from-screened --trade-date 20260518 --rule price_volume_ma --explain-rating
sats dsa --from-screened --trade-date 20260518 --rule price_volume_ma --no-llm
```

交互式 CLI：

```text
sats> /dsa --stocks 000001,600519 --trade-date 20260518
sats> /dsa --from-screened --trade-date 20260518 --rule chan-composite --explain-rating
sats> /dsa --from-screened --trade-date 20260518 --rule price_volume_ma --llm-timeout 5
```

参数说明：

- `--stocks`：逗号分隔的股票代码或可唯一识别的股票名称，支持裸代码、带后缀代码和 `stock_basic` 名称。
- `--from-screened`：读取 DuckDB 中指定日期、指定规则的 `passed=true` 筛选结果。
- `--trade-date`：分析截止交易日，默认使用当前上海日期。
- `--rule`：配合 `--from-screened` 使用，默认 `ma_volume_relative_strength`。
- `--lookback-days`：技术指标历史窗口，默认 `180`。
- `--explain-rating`：在终端排名下显示原始评级和稳定性调整原因。
- `--llm-timeout`：原生 DSA 单次 LLM 复核超时时间，默认 `20` 秒。
- `--no-llm`：跳过 LLM 复核，只使用本地规则评级。
- `--db PATH`：指定 DuckDB 文件；不传则使用 `.env` 中的 `SATS_DB_PATH`。

运行全 A 股筛选：

```bash
python -m sats screen --trade-date 20260430
python -m sats screen --trade-date 20260430 --rule price_volume_ma
python -m sats screen --trade-date 20260430 --rule price-volume-ma
python -m sats screen --trade-date 20260430 --rule monthly_base_breakout
python -m sats screen --trade-date 20260430 --rule chan-third-buy
python -m sats screen --trade-date 20260430 --rule chan-composite
```

`screen` 会自动获取当前上市沪深北全部 A 股股票池，并逐只执行筛选规则。默认规则为 `ma_volume_relative_strength`。终端只输出通过筛选的股票代码和股票名称，完整筛选结果写入 DuckDB。

输出示例：

```text
1. 000001.SZ 平安银行
2. 000002.SZ 万科A
```

数据获取会按交易日批量拉取并缓存到 DuckDB：

- `stock_basic(list_status="L")` 获取当前上市沪深北 A 股股票池和股票名称
- `daily(trade_date=...)` 批量获取全市场日线
- `daily_basic(trade_date=...)` 批量获取全市场每日指标

实际取数由 `AStockDataProvider` 统一调度：股票池优先尝试 TickFlow `universes.get("CN_Equity_A")` + `instruments.batch(...)`，再降级到 Tushare 当前上市 A 股；日线优先 TickFlow `klines.batch(period="1d", adjust="none")`，再用 Tushare 日线/本地缓存补齐；盘中实时日线优先 TickFlow quote，Tushare 和 AkShare 作为可用字段补充。TickFlow K 线统一转成 SATS/Tushare 口径：股票代码为 `000001.SZ` 格式，成交量为“手”，成交额为“千元”。重复运行同一交易日附近的筛选时，会优先使用本地 DuckDB 缓存，只补充缺失交易日的数据。

SATS 不会把前一交易日行情当作请求交易日结果使用。若请求日期是今天且处于 A 股连续竞价时段（上海时间 09:30-11:30、13:00-15:00），系统会强制使用实时日线：先尝试 Tushare `rt_k`，失败后尝试 TickFlow quote，两者都失败时才使用请求交易日的 DuckDB 同日缓存。实时日线和合成的 `daily_basic-like` 只作为本次筛选内存 overlay，不写入 `stock_daily` / `stock_daily_basic`，避免盘中数据污染盘后缓存。非交易时段仍优先使用完整的 `daily` / `daily_basic`；若当天盘后数据尚未更新，才会尝试实时 fallback。

TickFlow Provider 当前能力：

- 股票池和标的信息：`list_universes()`、`load_universe_symbols()`、`load_instruments()`。
- 实时行情：`load_realtime_quotes(symbols=...)` 或 `load_realtime_quotes(universe_id=...)`。
- K 线：`load_klines(..., period="1d|1w|1M|1Q|1Y")`。
- 分钟 K：`load_realtime_minute_klines()`、`load_historical_minute_klines()`，周期为 `1m/5m/15m/30m/60m`；按需实时获取，只在本次调用内存中保存，不写入 DuckDB。
- 日内分时：`load_intraday_timeshare()`，第一版复用 TickFlow 日内分钟 K 接口并标记 `data_source=tickflow_intraday_kline_alias`。
- 五档盘口和除权因子：`load_market_depth()`、`load_ex_factors()`。
- 当天 `daily_basic` 替代：`load_realtime_daily_basic_like()`，只合成当前筛选需要的换手率、市值和股本字段，并在结果 metadata 中记录 `daily_basic_source`。

查询筛选结果：

```bash
python -m sats results --trade-date 20260430
```

查看数据库中已有筛选结果使用过哪些规则名：

```bash
python -m sats result-rules
```

输出示例：

```text
1. ma_volume_relative_strength
2. price_volume_ma
```

然后可以复制规则名查询对应结果：

```bash
python -m sats results --rule ma_volume_relative_strength
python -m sats results --rule price_volume_ma
```

只查询通过筛选的股票：

```bash
python -m sats results --trade-date 20260430 --passed
```

`results` 终端输出会按列对齐显示股票代码、名称和规则名；如果是 `chan_composite` / `chan_signals` 等缠论规则，会显示命中的缠论子规则；如果是 `signal_composite`，会显示命中的交织信号标签：

```text
1. 000001.SZ 平安银行 chan_composite   三买
2. 000002.SZ 万科A    price_volume_ma
3. 000938.SZ 紫光股份 signal_composite 蛟龙出海买入点 ✚ K线信号
```

`results` 参数说明：

- `--trade-date YYYYMMDD`：按交易日过滤结果，例如 `--trade-date 20260430`；不传则查询所有日期。
- `--rule RULE_NAME`：按筛选规则过滤结果；可用规则包括 `ma_volume_relative_strength`、`price_volume_ma`、`monthly_base_breakout`、`chan_third_buy`、`chan_composite`、`chan_signals` 和 `signal_composite`，也支持 `ma-volume-relative-strength`、`price-volume-ma`、`monthly-base-breakout`、`chan-third-buy`、`chan-composite`、`chan-stock-select`、`chan-signals`、`chan-ai-select`、`signal-composite`、`abu-signals` 别名。
- `--passed`：只显示通过筛选的股票；不传则显示查询条件下的全部股票。
- `--db PATH`：指定 DuckDB 文件；不传则使用 `.env` 中的 `SATS_DB_PATH`。

分钟 K 数据按需通过 TickFlow 获取，只在调用内存中保存；CLI 不再提供 `minute-k` 或 `minute-k-clear` 命令，也不会写入 DuckDB 缓存表。TickFlow 分钟 K 批量请求按每批最多 100 只股票切分，并按 30 次/分钟节流；若批量接口异常，会降级为单票请求并按 60 次/分钟节流。

## 关注列表编辑

关注列表复用实时监控系统的 `monitor_watchlist` 表。直接运行 `watchlist` 会在终端中打开交互编辑界面；非 TTY 环境会只打印当前关注列表。

```bash
python -m sats watchlist
sats watchlist
sats watchlist list
sats watchlist add --stocks 000001,605300
sats watchlist remove --stocks 000001
sats watchlist clear
sats watchlist import-screened --trade-date 20260514 --rule price_volume_ma
```

交互界面快捷键：

```text
A 添加股票，支持逗号分隔多个代码
D 选择关注列表中的股票并回车删除
Q 退出
```

交互式 CLI 中也可以使用：

```text
sats> /watchlist
sats> /watchlist add --stocks 000001,605300
sats> /watchlist clear
sats> /watchlist import-screened --trade-date 20260514
```

运行 `screen` 筛选后，如果当前是真实交互终端，SATS 会弹出通过筛选股票的选择列表；默认不勾选任何股票，选中的股票会加入关注列表。脚本或管道中不会弹出选择器；也可以用 `--select-watchlist` 强制弹出，或用 `--no-select-watchlist` 禁用：

```bash
sats screen --trade-date 20260514 --rule price_volume_ma --select-watchlist
sats screen --trade-date 20260514 --no-select-watchlist
```

## 实时监控与信息显示

实时监控系统默认监控持仓列表和关注列表，v1 只支持 `chan_signals` 缠论买卖点规则。行情统一通过 `AStockDataProvider` 获取，优先使用 TickFlow 实时 quote 和 `30m` 分钟 K，并保留后续 Tushare/AkShare 降级扩展入口；实时价格不写入 DuckDB，只有触发事件时把当时证据写入 `monitor_events.metrics_json`。

维护持仓、关注和待买入列表：

```bash
python -m sats monitor positions add --symbol 000001 --name 平安银行 --buy-price 10.50 --quantity 100
python -m sats monitor positions list
python -m sats monitor watchlist add --symbol 605300 --name 川金诺
python -m sats monitor watchlist list
python -m sats monitor buy-candidates list
```

启动、停止和查看后台监控：

```bash
python -m sats monitor start --rules chan_signals --lists positions,watchlist --interval 60
python -m sats monitor status
python -m sats monitor stop
python -m sats monitor run --rules chan_signals --once
```

`monitor start` 会在后台启动轮询进程并把状态写入 DuckDB；`monitor run` 在当前终端运行。监控到关注股票出现买点时，会写入 `monitor_events` 并同步进入 `monitor_buy_candidates`；监控到持仓股票出现卖点或持币信号时，默认只写入卖出建议和 `monitor_trade_events` 占位事件。只有显式传入 `--broker qmt --auto-trade buy,sell` 这类参数时，监控才会通过 QMT broker 发出实盘委托，并按单笔金额、仓位比例和可用持仓做本地校验。

信息显示系统：

```bash
python -m sats monitor-display start
python -m sats monitor-display start --new-terminal
python -m sats monitor-display run
python -m sats monitor-display run --plain
python -m sats monitor-display stop
```

`monitor-display start` 默认在当前终端显示；如需 macOS Terminal 独立窗口，使用 `monitor-display start --new-terminal`。显示系统每轮从 DuckDB 读取持仓、关注、待买入、监控事件、定时任务记录和运行状态，并通过 `AStockDataProvider` 查询实时 quote 计算关注股票价格/涨幅、持仓实时价格、盈亏和盈亏比。盈利用红色、亏损用绿色显示；长列表在终端窗口内滚动展示。

## MiniQMT/QMT 实盘交易

SATS 提供受控 MiniQMT/QMT broker 接入。Windows 机器负责运行 bridge 并连接已登录的国金证券 QMT/MiniQMT；SATS 主机通过 HTTP API 查询资产、持仓、委托、成交，并可发送买入、卖出、撤单请求。`xtquant` 只需要安装在 Windows bridge 环境，SATS 主机不强依赖。

`.env` 示例：

```env
SATS_BROKER_PROVIDER=qmt
SATS_QMT_BRIDGE_URL=http://windows-host:8765
SATS_QMT_TOKEN=
SATS_QMT_ACCOUNT_ID=
SATS_QMT_ACCOUNT_TYPE=STOCK
SATS_QMT_USERDATA_PATH=
SATS_QMT_SESSION_ID=
```

Windows bridge：

```bash
python -m sats qmt bridge run --qmt-path "C:\国金QMT\userdata_mini" --account-id 123456789 --host 127.0.0.1 --port 8765
```

SATS 主机操作：

```bash
python -m sats qmt status
python -m sats qmt asset
python -m sats qmt positions
python -m sats qmt sync positions
python -m sats qmt orders --open
python -m sats qmt trades --limit 50
python -m sats qmt buy --symbol 000001 --quantity 100 --price-type latest
python -m sats qmt sell --symbol 000001 --quantity 100 --price-type limit --price 10.50
python -m sats qmt cancel --order-id <qmt_order_id>
```

`buy` / `sell` 默认是实盘委托；`--dry-run` 只校验并写审计记录，不调用 QMT 下单接口。所有委托、撤单和同步结果会写入 `broker_orders`、`broker_trades`、`broker_order_events`；`qmt sync positions` 会同步真实持仓到 `broker_positions`，并 upsert 到 `monitor_positions` 供 `monitor-display` 展示。跨机器 bridge 若绑定 `0.0.0.0`，必须配置 `SATS_QMT_TOKEN` 或 `--token`。

## 定时任务

SATS 内置定时调度只执行 SATS CLI 子命令或 SATS 自然语言聊天，不执行任意 shell。任务定义和执行记录写入 DuckDB；多个终端使用同一个 `SATS_DB_PATH` 时，`monitor-display` 可以看到最近定时任务结果摘要。

```bash
python -m sats schedule add --name daily-discover --type chat --text "预测未来几天大概率上涨的股票" --daily --time 08:45
python -m sats schedule add --name weekly-screen --type cli --text "screen --rule price_volume_ma --trade-date 20260522" --weekly --days mon,wed,fri --time 09:10
python -m sats schedule list
python -m sats schedule runs --limit 20
python -m sats schedule start
python -m sats schedule status
python -m sats schedule stop
```

`schedule start` 在后台启动调度进程，`schedule run-loop` 在当前终端前台运行，`schedule run <name>` 可以立即执行一次任务且不改变原本的下次执行时间。调度默认使用 `Asia/Shanghai` 时区；同一任务同时只允许一个实例运行。

组合示例：

```bash
python -m sats results --trade-date 20260430 --rule ma_volume_relative_strength
python -m sats results --trade-date 20260430 --rule price_volume_ma --passed
python -m sats results --trade-date 20260430 --passed --db data/sats.duckdb
python -m sats result-rules --db data/sats.duckdb
```

指定 DuckDB 文件：

```bash
python -m sats screen \
  --trade-date 20260430 \
  --db data/sats.duckdb
```

分析通过筛选的股票：

```bash
python -m sats analyze --stocks 000938 --signals ma_kline,chan
python -m sats analyze --from-screened --trade-date 20260430 --rule price_volume_ma --signals all
python -m sats analyze signals --category kline
python -m sats analyze-dsa --trade-date 20260430
python -m sats analyze-dsa --trade-date 20260430 --rule ma_volume_relative_strength
python -m sats analyze-dsa --trade-date 20260430 --rule price_volume_ma
python -m sats analyze-dsa --stocks 000001,600519
python -m sats analyze-dsa --trade-date 20260430 --db data/sats.duckdb
python -m sats analyze-chan --trade-date 20260430 --top 20
python -m sats analyze-chan --trade-date 20260430 --rule price_volume_ma
python -m sats analyze-chan --trade-date 20260430 --rule chan-signals --top 20
python -m sats analyze-chan --stocks 000001,600519 --chan-rule chan-composite
python -m sats chan-kb search 三买
```

`analyze-dsa` 是外部 `daily_stock_analysis/main.py --stocks` 的桥接入口；不传 `--stocks` 时会读取 DuckDB 中 `passed=true` 的筛选结果，再把股票列表交给 daily_stock_analysis。`dsa` 才是 SATS 自有原生 DSA 分析入口。执行时终端先显示 `analyzing...`，外部分析过程日志不展开；完成后，报告会归档到 SATS 的 `reports/` 目录，终端按评分降序显示排名。`--trade-date` 不传时默认使用今天；如果今天不是交易日，会自动选择上一个交易日。

```text
analyzing...
1. 600519 贵州茅台 评分 82 买入 看多
2. 000001 平安银行 评分 68 观望 震荡
报告: /Users/elliotge/python/SATS/reports/daily_stock_analysis_20260430_ma_volume_relative_strength_20260430_153000.md
```

`analyze-chan` 默认读取指定交易日所有 `passed=true` 的筛选结果，并按 `chan_signals` 缠论买卖点语境复核。`--rule` 只用于过滤已保存的筛选结果，语义与 `analyze-dsa --rule` 一致；`--chan-rule` 用于选择缠论复核或临时评估规则，可传 `chan-third-buy`、`chan-composite`、`chan-signals`。传入 `--stocks` 时会临时拉取这些股票的数据、运行 `--chan-rule` 对应缠论规则，再把评估结果和本地缠论 RAG 规则依据一起注入 SATS LLM Provider；临时结果不写入正式筛选记录。报告归档到 `reports/chan_llm_review_*_as_*.md`。

## FastAPI 使用

启动服务：

```bash
python -m sats serve --host 127.0.0.1 --port 8000
```

打开页面：

- 首页：http://127.0.0.1:8000/
- Swagger：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/health

主要接口：

```text
POST /api/screen
GET  /api/screen/results
GET  /api/market/minute-k
```

示例请求：

```json
{
  "trade_date": "20260430",
  "rule": "ma_volume_relative_strength"
}
```

`POST /api/screen` 会通过 `AStockDataProvider` 默认筛选当前上市沪深北全部 A 股。接口返回摘要和通过筛选的股票详情，完整结果通过 `GET /api/screen/results` 查询。

分钟 K 接口示例：

```text
GET /api/market/minute-k?symbols=000001.SZ,600519.SH&period=1m&mode=realtime&count=20
GET /api/market/minute-k?symbols=000001.SZ&period=5m&mode=history&start_date=20260501&end_date=20260514
```

分钟 K API 只返回本次实时获取结果，不写入 DuckDB。

## 当前筛选规则

可用规则：

```text
chan_composite
chan_signals
chan_third_buy
ma_volume_relative_strength
monthly_base_breakout
price_volume_ma
signal_composite
```

`ma_volume_relative_strength` 是默认规则，硬条件：

- 最近 3 个交易日收盘价均高于 MA5
- 最近 4 个交易日至少 3 日收阳
- 最近 3 日累计涨幅不超过 9%
- 最新收盘价相对 MA5 偏离不超过 4%
- 最新收盘价位于当日振幅上半区
- 当日成交量为 5 日均量的 1.2 到 2.0 倍；平台突破时允许略高放量
- 当日涨幅为正
- 最近 10 日涨幅不超过 18%
- 均线多头排列：MA5 > MA10 > MA20 > MA60

`price_volume_ma` 是量价换手均线策略，硬条件：

- 当日涨幅 3%-5%
- 当日成交量 / 前 5 日均量 > 1.0
- 换手率 5%-10%
- 流通市值 50亿-200亿人民币
- 均线多头排列：MA5 > MA10 > MA20 > MA60
- 默认排除 ST/*ST 和北交所股票；不因 `688` 前缀排除

该规则对齐 `/Users/elliotge/python/stock_vol/main.py` 默认非实时输出口径：结果为“当前逻辑 + 外部脚本口径”的并集。当前逻辑使用批量 `daily/daily_basic` 做前置过滤，要求最近 6 个交易日都有量能数据，再用 `pro_bar(adj="qfq", ma=[5,10,20,60])` 判断均线；外部脚本口径会对候选股逐只调用 `daily(ts_code,start_date,end_date)` 重新计算量比和均线。由于需要额外逐股请求，`price_volume_ma` 会比默认规则更慢。

`monthly_base_breakout` 是月K箱体突破策略，别名 `monthly-base-breakout`。它识别长期月线箱体中的绿色颈线/箱体上沿和橙色波段回踩结构，并把通过结果标记为 `early_breakout` 或 `confirmed_run`。该规则优先使用 TickFlow `1M` 月K；月K不可用时尝试用长日线聚合。规则不依赖 `daily_basic`，也不默认排除 ST 或北交所股票。

`chan_third_buy` 是缠论三买代理策略。它先用日线识别近 20 日箱体、近 10 日放量突破、突破后回抽不跌回箱体、不过度追高等条件，再只对日线预筛候选通过 TickFlow 拉取 `30m` 分钟 K，确认 30 分钟回抽不破箱体、最新收盘重新站上 MA5 且 MACD 柱改善。该规则需要 `TICKFLOW_API_KEY`；当日交易时段会合并历史 `30m` 窗口和当日实时 `30m`，实时 `30m` 优先使用 TickFlow 分钟 K 批量接口（30/min、100 标的/次）。若批量日内接口异常，SATS 会自动降级为单票实时分钟 K，请求速度受 60/min 限流约束；实时分钟 K 失败时停止筛选，不读取 DuckDB 缓存兜底。

`chan_composite` 是综合缠论选股策略，别名 `chan-composite` / `chan-stock-select`。它对同一只股票依次评估一买、二买、三买、二三买重合和中枢低吸，结果只写入一条综合记录；命中的子规则会写入 `metrics_json.matched_chan_rules`，例如 `["一买", "三买"]`。该规则与 `chan_third_buy` 一样不依赖 `daily_basic`，先用日线做廉价预筛，只对候选拉取 TickFlow `30m` 分钟 K；当日交易时段可通过现有 `screen` CLI 或 `POST /api/screen` 反复调用，用于外部盘中监控。

`chan_signals` 是缠论买卖点信号规则，别名 `chan-signals` / `chan-ai-select`。它复用现有买点代理规则，并补充一卖、二卖、三卖、中枢高抛、底分型确认、顶分型确认和持股/持币级别原则；每只股票仍只写一条综合记录，`metrics_json.chan_signals` 保存每个信号的方向、评分、观察位、风险和 PDF 规则依据。该规则同样跳过 `daily_basic`，日线预筛后只对候选拉取 TickFlow `30m`。

`signal_composite` 是 Abu 交织信号风格的综合规则，别名 `signal-composite` / `abu-signals`。它复用 `sats.signals` 的本地信号 registry，综合图形、趋势线、均线、K 线、波浪、谐波和缠论融合确认；命中的标签写入 `metrics_json.matched_signal_labels`。该规则不自动交易，适合作为 `screen --rule signal-composite` 的候选池或 `analyze --signals ...` 的分析依据。

缠论 RAG 规则卡片位于 `knowledge/chan/rules/`，可用 `python -m sats chan-kb search 关键词` 检索，例如查询“三买”“三卖”“背驰”“区间套”。规则卡片来自《缠中说禅：教你炒股票108课》的可规则化摘要，提供给 LLM 解释和复核使用。

筛选结果会写入 DuckDB 的 `screening_results` 表，并记录：

- `trade_date`
- `ts_code`
- `rule_name`
- `passed`
- `score`
- `matched_conditions`
- `failed_conditions`
- `metrics_json`

## 测试

运行测试：

```bash
python -m unittest discover -s tests
```

测试覆盖：

- 3 日站上 MA5
- 4 日 3 阳
- MA5 偏离
- 收盘位于当日振幅上半区
- 温和放量边界
- 当日涨幅为正
- 10 日涨幅限制
- 均线多头排列
- `AStockDataProvider` 统一 A 股股票池、行情、筛选输入、指标输入、大盘、分钟 K、财务和热点板块数据入口
- Tushare 批量数据获取和 DuckDB 缓存
- TickFlow 股票池、实时行情、K 线、分钟 K、日内分时、五档盘口和除权因子适配
- 当天 `daily_basic` 缺失时的实时合成替代，以及历史日期不冒充实时数据
- `price_volume_ma` 涨幅、量比、换手率、流通市值和均线筛选
- DuckDB 写入、CLI 全市场筛选和 FastAPI 查询
- LLM Provider 环境变量映射、工厂参数、reasoning 内容保留和 JSON 提取

## 当前限制

当前版本已实现 A 股筛选首版、统一 A 股数据入口和 LLM Provider 基础层，筛选入口通过 `AStockDataProvider` 获取当前上市沪深北全部 A 股。

尚未实现：

- Buy / Overweight / Hold / Underweight / Sell 分类
- miniQMT 实盘交易
- 纸面交易账户
- 持仓同步
- Web 图形化操作台

这些模块会在后续版本中继续补齐。
