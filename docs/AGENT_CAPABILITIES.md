# SATS Agent 能力与数据接口目录

本文是自然对话 Agent、新 Agent 和集成方的能力导航。运行时事实来源始终是代码注册表和：

```bash
sats catalog
sats catalog --section all --json
```

不要根据本文中的数量猜测当前能力；接口、Skills、规则、信号和因子会随代码变化，`sats catalog` 会动态读取当前注册表。

当前自然语言默认入口是 Codex-style conversation 工具循环：`sats chat ...`、REPL 普通输入和 `/chat ...` 默认进入 conversation 引擎，由模型逐轮输出 `call_tool`、`ask_clarification`、`request_confirmation` 或 `final_answer`，再由 SATS runtime 调用注册工具、记录 observation、执行权限门控和 trace。旧聊天路径通过 `sats chat --engine legacy ...` 保留。

## 推荐发现流程

Agent 应按以下顺序工作：

1. 已知 SATS 常规研究能力时，直接使用现有 `research.*`、`data.*`、`factor.*` 或 `workflow.*` 工具。
2. 不确定数据接口时，调用 `data.astock_catalog`，按 provider、关键词、分类、实时性和是否写库过滤。
3. 使用目录返回的 operation 调用 `data.astock_fetch`。
4. 检查返回的 `provenance`、`missing_fields` 和 `truncated`，缺失数据必须明确报告，不得由模型补造。
5. 需要了解非数据能力时，调用 `catalog.capabilities` 或 CLI `sats catalog`。

示例：

```json
{
  "tool": "data.astock_catalog",
  "arguments": {
    "provider": "tushare",
    "query": "资金流",
    "limit": 20
  }
}
```

```json
{
  "tool": "data.astock_fetch",
  "arguments": {
    "operation": "tushare.dataset.fetch",
    "params": {
      "dataset": "moneyflow",
      "params": {
        "ts_code": "000001.SZ",
        "start_date": "20260601",
        "end_date": "20260622"
      }
    },
    "fields": ["ts_code", "trade_date", "net_mf_amount"],
    "limit": 100
  }
}
```

## 数据边界

### AStockDataProvider

`AStockDataProvider` 是 A 股业务的统一数据门面。常规行情、股票列表、指标输入、指数、市场宽度、涨跌停情绪、热点板块、财务、新闻和公司事件均应通过该门面进入。

数据优先级通常是：

```text
TickFlow 实时行情/K线
  → Tushare 财务、资金流、板块、指数、新闻等补充
  → AkShare 可选公开数据兜底
  → DuckDB 本地缓存
```

业务模块和 Agent 不得直接实例化 TickFlow、Tushare 或 AkShare provider。

### TickFlow

TickFlow 的公开适配器接口均由 AStock facade 暴露，包括：

- universe、universe symbols、instruments、stock basic
- 实时报价、通用 K 线、历史日 K、当日日 K
- 实时和历史分钟 K、日内分时
- daily-basic-like 衍生指标
- market depth、复权因子、指标输入

目录查询：

```bash
sats catalog --section providers --provider tickflow --json
```

### Tushare

Tushare 使用白名单 dataset。Agent 不执行任意 Tushare API 名称，而是：

1. 用 `data.astock_catalog` 发现 dataset。
2. 使用 `tushare.dataset.fetch` operation。
3. 参数只能来自目录中的 `input_fields`，输出字段只能来自 `output_fields`。

目录查询：

```bash
sats catalog --section providers --provider tushare --query 财务 --limit 50 --json
```

### AkShare

AkShare 同样使用仓库内生成的白名单数据字典。它覆盖股票、基金、指数、行业、宏观、期货、期权、债券、外汇和其他公开数据。

Agent 只能调用目录中存在的 dataset。带 `api_key`、token、secret、password 等敏感参数的接口会显示在目录中，但标记为不可由 Agent 调用，以免凭据进入聊天和工具 trace。

```bash
sats catalog --section providers --provider akshare --category 宏观经济 --limit 50 --json
```

## 统一返回结构

`data.astock_fetch` 返回：

```text
operation       实际执行的白名单 operation
dataset         dataset 取数时的名称
data / rows     有界结果
row_count       原始结果数量
columns         返回列
provenance      数据来源、缓存和行数信息
missing_fields  缺失或不可用的数据
truncated       是否因安全上限截断
```

单次取数有强制上限。需要更多结果时应缩小日期、股票池或字段范围，不应把全市场或整个数据字典一次性送入模型上下文。

## 统一能力目录

CLI 和 REPL：

```bash
sats catalog
sats catalog --section commands
sats catalog --section agent-tools --json
sats catalog --section skills
sats catalog --section knowledge
sats catalog --section providers --provider tickflow
sats catalog --section screening-rules
sats catalog --section signals --category chan
sats catalog --section factors --category value
sats catalog --section api
```

REPL 中使用相同参数：

```text
/catalog --section providers --provider tushare --query 资金流
```

Agent 使用：

```json
{
  "tool": "catalog.capabilities",
  "arguments": {"section": "agent-tools", "limit": 50}
}
```

目录 JSON 顶层固定包含：

```text
schema_version, project_version, generated_at,
section, filters, counts, data, consistency
```

## Skills、知识库、记忆和数据

- Skills：研究方法和执行指导，位于 `skills/<id>/SKILL.md`，不是实时数据。
- 知识库：经过分块索引的本地资料，用于 RAG 检索，可能包含 Skills 和知识文档。
- 记忆：用户偏好、会话摘要和历史事实，不属于公开能力目录，也不会被目录输出。
- 实时数据：必须来自 AStock/provider 工具并带 provenance。

## 副作用与交易

- `readonly`：目录、知识检索和不落库的数据读取。
- `write_db`：可能刷新 DuckDB 行情或基础数据缓存；交易时段获取的当天行情只用于本次计算，不写入 DuckDB。
- `write_artifact`：生成报告、策略或回测产物。
- `long_running`：训练或长时间任务。
- `live_trade`：真实交易；必须同时具备显式 auto-trade side、QMT broker 和 live-trading 权限。

能力目录只描述接口，不扩大 Agent 权限。`data.astock_fetch` 不能绕过交易、shell、敏感参数或市场数据真实性限制。
