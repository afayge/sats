from __future__ import annotations

import argparse
import io
import shlex
import shutil
import sys
import time
import uuid
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from prompt_toolkit import PromptSession as ToolkitPromptSession
from prompt_toolkit import print_formatted_text
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from sats import __version__
from sats.agent import AgentExecutionPolicy, run_agent_once
from sats.agent.progress import agent_progress_event_sink
from sats.chat import ChatResult, ChatSession, format_chat_result, render_chat_result
from sats.natural_output import build_output_semantic_lexicon, render_natural_output
from sats.chat_reference import build_chat_reference_context
from sats.chat_runtime import confirm_pending_runtime_action, format_runtime_trace, reject_pending_runtime_action
from sats.config import load_settings
from sats.conversation import (
    confirm_pending_conversation_action,
    continue_conversation_after_clarification,
    format_conversation_plan,
    reject_pending_conversation_action,
    run_conversation_once,
)
from sats.history import InteractionHistoryStore
from sats.memory import ChatMemoryStore
from sats.output_saver import CapturedOutput, SaveRequest, extract_report_path, parse_save_request, save_captured_output
from sats.progress import create_progress
from sats.runtime_status import runtime_is_running
from sats.scheduler import SCHEDULER_SERVICE_NAME
from sats.storage import DuckDBStorage
from sats.stock_question import extract_stock_symbols

CLI_COMMANDS = [
    "init",
    "screen",
    "results",
    "result-rules",
    "quote",
    "period-change",
    "analyze",
    "analyze-dsa",
    "dsa",
    "deep-analysis",
    "serenity-screen",
    "trading-committee",
    "analyze-chan",
    "chan-kb",
    "discover",
    "chat",
    "web",
    "model",
    "memory",
    "history",
    "knowledge",
    "indicators",
    "factor",
    "skills",
    "watchlist",
    "monitor",
    "monitor-display",
    "schedule",
    "qmt",
    "portfolio",
    "catalog",
    "threads",
    "serve",
]

BUILTIN_COMMANDS = ["help", "exit", "quit", "clear", "save", "new", "confirm", "reject", "answer", "trace", "goal", "plan", "engine"]
INTERRUPT_MESSAGE = "已中断当前执行，返回 sats>。"

HELP_COMMANDS = [
    ("/help", "查看命令"),
    ("/new", "开启新对话"),
    ("/confirm", "确认运行待执行动作"),
    ("/reject", "取消待执行动作"),
    ("/answer", "回答澄清问题并继续"),
    ("/trace", "查看对话 turn trace"),
    ("/plan", "只生成 Conversation 计划"),
    ("/engine", "切换自然语言引擎"),
    ("/clear", "清屏"),
    ("/save", "保存上一条输出"),
    ("/exit", "退出"),
    ("/quit", "退出"),
    ("/init", "初始化配置"),
    ("/screen", "全 A 股筛选"),
    ("/results", "查询筛选结果"),
    ("/result-rules", "查看结果规则名"),
    ("/quote", "实时价格"),
    ("/period-change", "区间涨跌幅"),
    ("/analyze", "统一信号分析"),
    ("/analyze-dsa", "分析 DSA 股票"),
    ("/dsa", "原生 DSA 分析"),
    ("/deep-analysis", "原生个股深研"),
    ("/serenity-screen", "Serenity AI 卡位筛选"),
    ("/trading-committee", "投资委员会多团队分析"),
    ("/analyze-chan", "分析缠论股票"),
    ("/chan-kb", "搜索缠论知识库"),
    ("/discover", "短线机会发现"),
    ("/chat", "LLM 聊天"),
    ("/web", "网络搜索/社交热榜"),
    ("/goal", "设置/查看 Agent 目标"),
    ("/model", "模型配置切换"),
    ("/memory", "管理聊天记忆"),
    ("/history", "查询执行历史"),
    ("/knowledge", "管理本地知识库"),
    ("/indicators", "计算技术指标"),
    ("/factor", "股票因子分析/选股"),
    ("/skills", "查看本地 skills"),
    ("/watchlist", "编辑关注列表"),
    ("/monitor", "实时监控"),
    ("/monitor-display", "监控信息显示"),
    ("/schedule", "定时任务"),
    ("/qmt", "QMT 实盘交易"),
    ("/portfolio", "盘中 10 选 5 组合 Agent"),
    ("/catalog", "统一能力目录"),
    ("/threads", "管理对话线程"),
    ("/serve", "启动 API 服务"),
]

HELP_EXAMPLES = [
    ("用缠论分析002436", "自然语言触发真实缠论分析"),
    ("分析002436 2026-05-15", "自然语言触发真实股票分析"),
    ("/screen --trade-date 20260514 --rule price_volume_ma", "全 A 股筛选"),
    ("/screen --trade-date 20260514 --rule rps-breakout", "RPS 强势突破筛选"),
    ("/screen --trade-date 20260514 --rule monthly_base_breakout", "月K箱体突破筛选"),
    ("/results --trade-date 20260514 --passed", "查询通过股票"),
    ("/quote --stocks 000001,600519", "查看实时价格"),
    ("/period-change --stocks 000001,600519 --days 60", "查看近 60 个自然日涨跌幅"),
    ("/period-change --indices 上证指数,沪深300 --days 180", "查看指数近 180 个自然日涨跌幅"),
    ("/analyze --stocks 000938 --signals ma_kline,chan", "统一信号分析"),
    ("/analyze --from-screened --trade-date 20260514 --rule price_volume_ma --signals all", "分析筛选结果"),
    ("/analyze signals --category kline", "查看信号策略"),
    ("/dsa --stocks 000001,600519 --trade-date 20260514", "原生 DSA 分析"),
    ("/dsa --from-screened --trade-date 20260514 --explain-rating", "分析筛选结果"),
    ("/deep-analysis --stocks 000001,600519 --trade-date 20260514", "原生个股深研"),
    ("/serenity-screen --theme AI半导体 --trade-date 20260514 --top 10", "Serenity AI 卡位筛选"),
    ("/trading-committee --stocks 000001,600519 --trade-date 20260514", "投资委员会多团队分析"),
    ("/analyze-dsa --stocks 000001,600519", "分析股票"),
    ("/analyze-chan --stocks 000001 --chan-rule chan-signals", "缠论指定股票分析"),
    ("/discover --limit 5", "短线机会发现"),
    ("/indicators --stocks 000001.SZ --trade-date 20260514", "计算技术指标"),
    ("/factor list --zoo barra_style", "查看 Barra 风格近似因子"),
    ("/factor pick --profile balanced --trade-date 20260514 --top 20", "默认画像多因子选股"),
    ("/factor pick --factors barra_style_value,barra_style_quality --trade-date 20260514 --top 20", "多因子选股"),
    ("/factor ml status", "检查 Qlib/ML 依赖"),
    ("/factor ml train --profile balanced --model lightgbm --train-start 20250101 --train-end 20260430 --valid-end 20260514", "训练因子 ML 模型"),
    ("/factor ml predict --model-run factor_ml_xxxxxxxx --trade-date 20260514 --top 20 --write-screening", "因子 ML 预测并写入筛选结果"),
    ("/chat 分析000001的因子暴露", "自然语言因子暴露分析"),
    ("/new 茅台估值复盘", "开启新的多轮对话"),
    ("/chat 写一个5日/20日均线策略并回测000001", "生成待确认 runtime 动作"),
    ("/web search 贵州茅台 最新公告 --limit 5", "公开网络搜索"),
    ("/web open https://example.com --query 关键事实", "抓取并检索指定网页"),
    ("/web cache clear --expired-only", "清理过期网页索引"),
    ("/web hot --platforms weibo,zhihu --limit 20", "社交热榜"),
    ("/web hot --platforms xueqiu --limit 20", "雪球热股/热点"),
    ("/web mentions --keyword 贵州茅台", "热榜关键词命中"),
    ("筛选短线机会并保存报告", "自然语言自主执行 Agent"),
    ("/plan 用 price_volume_ma 筛选并对筛选股票制定明天交易计划", "只生成自然任务计划"),
    ("/goal 明天按信号自动买入不超过2万", "设置并运行 Agent 目标"),
    ("/confirm act_xxxxxxxx", "确认 runtime 动作"),
    ("/trace", "查看最近一次对话 trace"),
    ("/watchlist", "编辑关注列表"),
    ("/watchlist add --stocks 000001.SZ,600519.SH", "批量关注股票"),
    ("/watchlist clear", "清空关注列表"),
    ("/monitor start --rules chan_signals", "启动实时监控"),
    ("/monitor plans list", "查看可执行监控计划"),
    ("/monitor plans validate --file plan.json", "校验监控计划 JSON"),
    ("/monitor plans import --file plan.json", "导入监控计划草稿"),
    ("/monitor-display start", "当前终端打开监控显示"),
    ("/schedule list", "查看定时任务"),
    ("/schedule add --name daily-discover --type chat --text \"预测未来几天大概率上涨的股票\" --daily --time 08:45", "添加定时聊天任务"),
    ("/qmt positions", "查看 QMT 持仓"),
    ("/monitor positions list", "同步并查看监控持仓"),
    ("/qmt buy --symbol 000001 --quantity 100 --price-type latest", "QMT 实盘买入"),
    ("/portfolio run --phase afternoon-buy --mode paper", "尾盘 10 选 5 并自动模拟买入"),
    ("/portfolio run --phase report --mode paper", "生成交易日 Markdown 总结"),
    ("/portfolio positions --mode paper", "查看模拟组合持仓"),
    ("/portfolio orders list --mode live", "查看待人工确认实盘委托"),
    ("/portfolio schedule install --mode paper", "安装交易日盘中组合任务"),
    ("/model status", "查看当前模型"),
    ("/model ping --timeout 20", "检查主模型连通性"),
    ("/model use XIAOMIMIMO --target light", "切换轻量模型"),
    ("/chat 帮我解释 price_volume_ma", "LLM 问答"),
    ("/save --format pdf", "保存上一条输出"),
    ("/memory search 股票", "搜索本地记忆"),
    ("/history list", "查看交互历史"),
    ("/history search 股票", "搜索交互历史"),
    ("/history show hist_xxxxxxxx", "查看单条历史"),
    ("/history delete hist_xxxxxxxx", "删除单条历史"),
    ("/knowledge search --query 三买 --knowledge chan", "搜索本地知识库"),
    ("/knowledge ingest --knowledge chan --path knowledge/chan/rules", "导入知识库文档"),
    ("/knowledge sync-stock-basic", "同步股票名称代码库"),
    ("/catalog --section providers --provider tushare --query 资金流 --json", "查询数据接口目录"),
]

BANNER_CONTROL_COMMANDS = [
    ("/help", "查看命令"),
    ("/exit", "退出"),
    ("/clear", "清屏"),
    ("/save", "保存上一条输出"),
    ("Ctrl+C", "中断当前执行"),
]

HELP_SHORTCUTS = [
    ("Ctrl+C", "中断当前执行"),
]

BANNER_LOGO_PREFIX_STYLE = "fg:#6b7280 bold"
BANNER_LOGO_WORD_STYLE = "fg:#f8fafc bold"
BANNER_VERSION_STYLE = "fg:#9ca3af"
BANNER_LOGO_CELL = "██"
BANNER_LOGO_GAP_CELLS = 1
BANNER_LOGO_GLYPHS = {
    ">": (
        "█    ",
        "  █  ",
        "    █",
        "  █  ",
        "█    ",
    ),
    "_": (
        "     ",
        "     ",
        "     ",
        "     ",
        "█████",
    ),
    "S": (
        "█████",
        "█    ",
        "█████",
        "    █",
        "█████",
    ),
    "A": (
        " ███ ",
        "█   █",
        "█████",
        "█   █",
        "█   █",
    ),
    "T": (
        "█████",
        "  █  ",
        "  █  ",
        "  █  ",
        "  █  ",
    ),
    " ": (
        "",
        "",
        "",
        "",
        "",
    ),
}
BANNER_LOGO_SEQUENCE = (
    (">", BANNER_LOGO_PREFIX_STYLE),
    ("_", BANNER_LOGO_PREFIX_STYLE),
    (" ", ""),
    ("S", BANNER_LOGO_WORD_STYLE),
    ("A", BANNER_LOGO_WORD_STYLE),
    ("T", BANNER_LOGO_WORD_STYLE),
    ("S", BANNER_LOGO_WORD_STYLE),
)

_HELP_DESCRIPTIONS = dict(HELP_COMMANDS)

COMPLETION_DESCRIPTIONS = {
    **{f"/{command}": _HELP_DESCRIPTIONS.get(f"/{command}", "CLI 命令") for command in CLI_COMMANDS},
    **{f"/{command}": _HELP_DESCRIPTIONS.get(f"/{command}", "内置命令") for command in BUILTIN_COMMANDS},
    "--trade-date": "交易日 YYYYMMDD",
    "--rule": "筛选规则名",
    "--chan-rule": "缠论规则名",
    "--passed": "只显示通过结果",
    "--db": "DuckDB 路径",
    "--symbol": "单只股票代码或名称",
    "--stocks": "指定股票代码或名称列表",
    "--indices": "指定指数代码或名称列表",
    "--days": "从今天向前计算的自然日数",
    "--from-screened": "使用筛选结果",
    "--signals": "信号策略列表",
    "--factor": "因子 ID",
    "--factors": "因子 ID 列表",
    "--zoo": "因子库",
    "--theme": "因子主题",
    "--universe": "因子适用范围",
    "--weight": "因子权重方式",
    "--neutralize": "中性化方式",
    "--write-screening": "写入筛选结果",
    "--profile": "因子筛选画像",
    "--screening-profile": "筛选结果规则后缀",
    "--horizon": "收益标签周期",
    "--groups": "分组数量",
    "--category": "信号分类",
    "--json": "输出 JSON",
    "--noreport": "不生成报告",
    "--explain-rating": "显示评级解释",
    "--llm-timeout": "LLM 超时时间",
    "--no-llm": "跳过 LLM",
    "--period": "K 线周期",
    "--phase": "深研阶段",
    "--debate-rounds": "研究员辩论轮数",
    "--risk-rounds": "风控辩论轮数",
    "--mode": "数据模式",
    "--count": "数量限制",
    "--start-date": "开始日期",
    "--end-date": "结束日期",
    "--top": "最大候选数",
    "--limit": "返回数量",
    "--candidate-limit": "候选池数量",
    "--trusted-domains": "限定搜索域名",
    "--freshness": "搜索新鲜度",
    "--context-size": "原生 RAG 搜索上下文深度",
    "--providers": "搜索提供方列表",
    "--platforms": "社交平台列表，支持 xueqiu/xueqiu_stock/xueqiu_spot",
    "--keyword": "关键词",
    "--hot-sector-days": "热点板块天数",
    "--no-hot-sector": "禁用热点板块",
    "--host": "服务监听地址",
    "--port": "服务端口",
    "--no-memory": "禁用聊天记忆",
    "--kind": "历史类型",
    "--knowledge": "指定聊天知识库",
    "--format": "保存格式",
    "--path": "保存路径",
    "--source": "保存来源",
    "--target": "切换目标",
    "--yes": "确认执行",
    "--quantity": "持仓数量",
    "--interval": "执行间隔",
    "--daily": "每天执行",
    "--weekly": "每周执行",
    "--days": "星期几",
    "--time": "执行时间",
    "--type": "任务类型",
    "--price-type": "委托价格类型",
    "--price": "限价价格",
    "--dry-run": "只校验不下单",
    "--account-id": "QMT 账户",
    "--account-type": "账户类型",
    "--qmt-path": "QMT userdata_mini",
    "--session-id": "QMT 会话 ID",
    "--token": "Bridge Token",
    "--broker": "交易券商",
    "--auto-trade": "自动交易动作",
    "--live-trading": "允许 Agent 实盘交易",
    "--max-order-value": "单笔金额上限",
    "--max-position-pct": "仓位比例上限",
    "--sell-ratio": "卖出比例",
    "--max-iterations": "Agent 最大步骤数",
    "--command-timeout": "Agent 命令超时秒数",
    "--python-timeout": "Agent Python 超时秒数",
    "--plan-only": "只生成计划",
    "--engine": "对话引擎",
    "--agent": "显式启用 Agent",
    "--dry-run": "跳过高风险副作用",
    "--lists": "列表名称",
    "--llm-review": "启用 LLM 复核",
    "--once": "只运行一次",
    "--refresh": "刷新间隔",
    "--plain": "纯文本输出",
    "--new-terminal": "新终端窗口",
    "--file": "JSON 文件路径",
    "--plan-id": "监控计划 ID",
    "--item-id": "监控计划项目 ID",
    "--group-id": "监控触发组 ID",
    "--section": "能力目录分区",
    "--provider": "数据提供方",
    "--query": "搜索关键词",
    "--offset": "分页偏移",
    "--writes-db": "筛选写库能力",
    "--select-watchlist": "选择导入关注列表",
    "--no-select-watchlist": "跳过关注列表选择",
    "positions": "QMT 持仓",
    "plans": "可执行监控计划",
    "validate": "校验 JSON",
    "import": "导入草稿",
    "activate": "启用计划",
    "disable-item": "停用计划项目",
    "disable-group": "停用触发组",
    "signals": "信号策略",
    "factor": "因子分析",
    "pick": "因子选股",
    "ml": "因子 ML",
    "setup": "安装可选依赖",
    "train": "训练模型",
    "evaluate": "评估模型",
    "predict": "模型预测",
    "watchlist": "关注列表",
    "buy-candidates": "买入候选",
    "add": "新增记录",
    "remove": "移除记录",
    "start": "启动服务",
    "stop": "停止服务",
    "status": "查看状态",
    "run": "前台运行",
    "collect": "采集阶段",
    "score": "评分阶段",
    "panel": "评委面板阶段",
    "report": "报告阶段",
    "run-loop": "前台调度循环",
    "runs": "执行记录",
    "qmt": "QMT 交易",
    "bridge": "QMT Bridge",
    "asset": "账户资产",
    "orders": "委托记录",
    "trades": "成交记录",
    "model": "模型配置",
    "use": "切换配置",
    "buy": "买入",
    "sell": "卖出",
    "cancel": "撤单",
    "enable": "启用任务",
    "disable": "停用任务",
    "list": "列出记录",
    "forget": "删除记忆",
    "delete": "删除记录",
    "show": "查看详情",
    "clear": "清空记录",
    "search": "搜索记录",
    "open": "打开公开网页",
    "cache": "网页索引缓存",
    "hot": "社交热榜",
    "mentions": "热榜命中",
    "xueqiu": "雪球热股/热点",
    "xueqiu_stock": "雪球热股",
    "xueqiu_spot": "雪球热点",
    "ingest": "导入知识库",
    "sync-stock-basic": "同步股票列表",
}

COMPLETION_WORDS = list(COMPLETION_DESCRIPTIONS)

PROMPT_STYLE = "fg:#7dd3fc"
PROMPT_MESSAGE = FormattedText([(PROMPT_STYLE, "sats> ")])
CLARIFY_PROMPT_MESSAGE = FormattedText([(PROMPT_STYLE, "clarify> ")])
MUTED_STYLE = "fg:#9ca3af"
COMMAND_STYLE = "fg:#f5f5f5"
DESC_STYLE = COMMAND_STYLE
SEPARATOR_STYLE = MUTED_STYLE
STATUS_BAR_STYLE = "bg:#2f2f2f fg:#f5f5f5"
COMPLETION_LIST_MIN_WIDTH = 28
COMPLETION_MENU_STYLE = "bg:#1f2937 fg:#f5f5f5"
COMPLETION_MENU_SELECTED_STYLE = "bg:#2563eb fg:#ffffff"
REPL_STYLE = Style.from_dict(
    {
        "bottom-toolbar": "bg:#2f2f2f",
        "bottom-toolbar.text": "bg:#2f2f2f #f5f5f5",
        "completion-menu.completion": COMPLETION_MENU_STYLE,
        "completion-menu.completion.current": COMPLETION_MENU_SELECTED_STYLE,
        "completion-menu.meta.completion": COMPLETION_MENU_STYLE,
        "completion-menu.meta.completion.current": COMPLETION_MENU_SELECTED_STYLE,
    }
)


class InputSeparatorProcessor(Processor):
    def __init__(self, prompt_width: int | Callable[[], int]) -> None:
        self.prompt_width = prompt_width

    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        if ti.lineno != ti.document.line_count - 1 or ti.width <= 0:
            return Transformation(ti.fragments)

        prefix_width = self._prompt_width() if ti.lineno == 0 else 0
        input_width = get_cwidth(fragment_list_to_text(ti.fragments))
        current_column = (prefix_width + input_width) % ti.width
        padding = (ti.width - current_column) % ti.width
        return Transformation(
            [
                *ti.fragments,
                ("", " " * padding),
                (SEPARATOR_STYLE, "─" * ti.width),
            ]
        )

    def _prompt_width(self) -> int:
        width = self.prompt_width() if callable(self.prompt_width) else self.prompt_width
        return max(0, int(width or 0))


class ReplPromptSession(ToolkitPromptSession):
    def _create_layout(self):
        layout = super()._create_layout()
        main_input = layout.container.children[0].alternative_content
        for completion_float in main_input.floats[:2]:
            completion_float.content = HSplit(
                [
                    Window(height=1),
                    completion_float.content,
                ]
            )
        return layout


@dataclass(slots=True)
class ReplState:
    last_output: CapturedOutput | None = None
    last_stock_output: CapturedOutput | None = None
    started_at: float = field(default_factory=lambda: time.monotonic())
    last_duration_seconds: float | None = None
    status_bar_cache: str = ""
    status_bar_cache_at: float = 0.0
    session_id: str = "repl"
    history_store: InteractionHistoryStore | None = None
    chat_session: ChatSession | None = None
    agent_goal: str = ""
    engine: str = "conversation"
    pending_clarification_id: str = ""
    pending_clarification_prompt: str = ""


@dataclass(frozen=True, slots=True)
class ReplExecutionRecord:
    kind: str
    request: str
    source: str
    output: str
    status: str = "done"
    report_path: str = ""
    session_id: str = ""


def run_repl() -> int:
    settings = load_settings()
    render_startup_banner(settings.db_path)
    chat_session = ChatSession(settings=settings)
    state = ReplState(
        session_id=str(getattr(chat_session, "session_id", "") or "repl"),
        history_store=InteractionHistoryStore(settings.db_path),
        chat_session=chat_session,
    )
    session = ReplPromptSession(
        history=_history(),
        completer=build_repl_completer(),
        complete_while_typing=True,
        input_processors=[InputSeparatorProcessor(lambda: _display_width(fragment_list_to_text(_prompt_message(state))))],
        bottom_toolbar=_status_toolbar(settings, state),
        style=REPL_STYLE,
    )
    while True:
        try:
            _print_input_separator()
            line = session.prompt(_prompt_message(state))
        except KeyboardInterrupt:
            print("^C")
            continue
        except EOFError:
            print("bye")
            return 0
        if not handle_repl_line(line, printer=print, chat_session=chat_session, state=state):
            return 0


def _prompt_message(state: ReplState) -> FormattedText:
    return CLARIFY_PROMPT_MESSAGE if str(state.pending_clarification_id or "").strip() else PROMPT_MESSAGE


def build_repl_completer() -> WordCompleter:
    return WordCompleter(
        COMPLETION_WORDS,
        display_dict=_completion_display_dict(),
        meta_dict=COMPLETION_DESCRIPTIONS,
        ignore_case=True,
        sentence=True,
    )


def _completion_display_dict() -> dict[str, str]:
    display_width = max(
        COMPLETION_LIST_MIN_WIDTH,
        max((_display_width(word) for word in COMPLETION_WORDS), default=0),
    )
    return {word: _left_align_display(word, display_width) for word in COMPLETION_WORDS}


def render_startup_banner(
    db_path: Path | str,
    *,
    printer: Callable[[str], None] | None = None,
    formatted_printer: Callable[[FormattedText], None] | None = None,
    terminal_width: int | None = None,
) -> None:
    plain_printer = printer or print
    rich_printer = formatted_printer or print_formatted_text
    help_lines = _banner_help_lines(db_path)
    width = _banner_width(terminal_width)
    content_width = max(1, width - 4)

    _print_plain_fragments(FormattedText([("", "")]), width, rich_printer)
    _print_banner_title(width, rich_printer)
    _print_plain_fragments(FormattedText([("", "")]), width, rich_printer)
    plain_printer(_top_border(width))
    for line in help_lines:
        _print_box_fragments(line, content_width, rich_printer)
    plain_printer(_bottom_border(width))


def render_help_box(
    *,
    printer: Callable[[str], None] | None = None,
    formatted_printer: Callable[[FormattedText], None] | None = None,
    terminal_width: int | None = None,
) -> None:
    plain_printer = printer or print
    rich_printer = formatted_printer or print_formatted_text
    width = _banner_width(terminal_width)
    content_width = max(1, width - 4)
    command_width = _help_command_width(content_width)

    plain_printer(_top_border(width))
    _print_box_fragments(FormattedText([(MUTED_STYLE, "可用命令")]), content_width, rich_printer)
    for command, description in HELP_COMMANDS:
        _print_box_fragments(_help_row_fragments(command, description, command_width), content_width, rich_printer)
    _print_box_fragments(FormattedText([("", "")]), content_width, rich_printer)
    _print_box_fragments(FormattedText([(MUTED_STYLE, "快捷键")]), content_width, rich_printer)
    for shortcut, description in HELP_SHORTCUTS:
        _print_box_fragments(_help_row_fragments(shortcut, description, command_width), content_width, rich_printer)
    _print_box_fragments(FormattedText([("", "")]), content_width, rich_printer)
    _print_box_fragments(FormattedText([(MUTED_STYLE, "示例")]), content_width, rich_printer)
    for command, description in HELP_EXAMPLES:
        _print_box_fragments(_help_row_fragments(command, description, command_width), content_width, rich_printer)
    plain_printer(_bottom_border(width))


def _help_command_width(content_width: int | None = None) -> int:
    commands = [command for command, _ in HELP_COMMANDS]
    commands.extend(command for command, _ in HELP_EXAMPLES)
    width = max((_display_width(command) for command in commands), default=0)
    if content_width is None:
        return width
    return min(width, max(8, content_width - 18))


def _help_row_fragments(
    command: str,
    description: str,
    command_width: int,
    *,
    command_style: str = COMMAND_STYLE,
    desc_style: str = DESC_STYLE,
) -> FormattedText:
    command_text = _truncate_to_width(command, command_width)
    return FormattedText(
        [
            (command_style, command_text),
            ("", " " * (max(0, command_width - _display_width(command_text)) + 2)),
            (desc_style, description),
        ]
    )


def _banner_help_lines(db_path: Path | str) -> list[FormattedText]:
    command_width = _banner_command_width()
    return [
        FormattedText([(MUTED_STYLE, "输入 /commands 开始")]),
        *[_help_row_fragments(command, description, command_width) for command, description in BANNER_CONTROL_COMMANDS],
        FormattedText([(DESC_STYLE, f"DB: {db_path}")]),
    ]


def _banner_command_width() -> int:
    commands = [command for command, _ in BANNER_CONTROL_COMMANDS]
    return max((_display_width(command) for command in commands), default=0)


def _banner_width(terminal_width: int | None = None) -> int:
    width = terminal_width if terminal_width is not None else shutil.get_terminal_size((80, 20)).columns
    return max(8, width)


def _top_border(width: int) -> str:
    return f"┌{'─' * max(0, width - 2)}┐"


def _bottom_border(width: int) -> str:
    return f"└{'─' * max(0, width - 2)}┘"


def _box_line(text: str, width: int) -> str:
    return f"│ {_pad_to_width(_truncate_to_width(text, width), width)} │"


def _print_box_fragments(
    fragments: FormattedText,
    width: int,
    formatted_printer: Callable[[FormattedText], None],
) -> None:
    truncated = _truncate_fragments_to_width(fragments, width)
    text_width = _display_width(fragment_list_to_text(truncated))
    padding = " " * max(0, width - text_width)
    formatted_printer(FormattedText([("", "│ "), *truncated, ("", f"{padding} │")]))


def _print_plain_fragments(
    fragments: FormattedText,
    width: int,
    formatted_printer: Callable[[FormattedText], None],
) -> None:
    truncated = _truncate_fragments_to_width(fragments, width)
    text_width = _display_width(fragment_list_to_text(truncated))
    padding = " " * max(0, width - text_width)
    formatted_printer(FormattedText([*truncated, ("", padding)]))


def _print_banner_title(width: int, formatted_printer: Callable[[FormattedText], None]) -> None:
    for fragments in _banner_logo_rows():
        _print_plain_fragments(fragments, width, formatted_printer)


def _banner_logo_rows() -> list[FormattedText]:
    cells: dict[tuple[int, int], str] = {}
    x = 0
    height = max((len(BANNER_LOGO_GLYPHS[glyph]) for glyph, _ in BANNER_LOGO_SEQUENCE), default=0)

    for glyph, style in BANNER_LOGO_SEQUENCE:
        rows = BANNER_LOGO_GLYPHS[glyph]
        glyph_width = max((len(row) for row in rows), default=0)
        if style:
            for y, row in enumerate(rows):
                for dx, mark in enumerate(row):
                    if mark == "█":
                        cell = (y, x + dx)
                        cells[cell] = style
        x += glyph_width + BANNER_LOGO_GAP_CELLS

    width = max((col + 1 for _, col in cells), default=0)
    rows: list[FormattedText] = []
    for y in range(height):
        fragments: list[tuple[str, str]] = []
        for col in range(width):
            style = cells.get((y, col))
            fragments.append((style or "", BANNER_LOGO_CELL if style else "  "))
        if y == 0:
            fragments.extend([("", "  "), (BANNER_VERSION_STYLE, f"v{__version__}")])
        rows.append(FormattedText(fragments))
    return rows


def _pad_to_width(text: str, width: int) -> str:
    return text + (" " * max(0, width - _display_width(text)))


def _truncate_to_width(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    if width <= 0:
        return ""
    if width <= 3:
        return "." * width
    limit = width - 3
    output = ""
    current_width = 0
    for char in text:
        char_width = _display_width(char)
        if current_width + char_width > limit:
            break
        output += char
        current_width += char_width
    return f"{output}..."


def _truncate_fragments_to_width(fragments: FormattedText, width: int) -> list[tuple[str, str]]:
    if _display_width(fragment_list_to_text(fragments)) <= width:
        return list(fragments)
    if width <= 0:
        return []
    if width <= 3:
        return [("", "." * width)]
    limit = width - 3
    output: list[tuple[str, str]] = []
    current_width = 0
    last_style = ""
    for style, text in fragments:
        last_style = style
        for char in text:
            char_width = _display_width(char)
            if current_width + char_width > limit:
                output.append((last_style, "..."))
                return output
            output.append((style, char))
            current_width += char_width
    return output


def _display_width(text: str) -> int:
    return get_cwidth(text)


def _print_input_separator(
    *,
    formatted_printer: Callable[[FormattedText], None] | None = None,
    terminal_width: int | None = None,
) -> None:
    width = _banner_width(terminal_width)
    printer = formatted_printer or print_formatted_text
    printer(FormattedText([(SEPARATOR_STYLE, "─" * width)]))


def _status_toolbar(settings, state: ReplState) -> Callable[[], FormattedText]:
    def toolbar() -> FormattedText:
        provider = _provider_label(str(getattr(settings, "llm_provider", "") or "openai"))
        model = str(getattr(settings, "openai_model", "") or "LLM")
        elapsed = _format_duration(time.monotonic() - state.started_at)
        last = _format_duration(state.last_duration_seconds) if state.last_duration_seconds is not None else "--"
        left = f"  {provider}:{model}  ｜  {elapsed}  ｜  Last：{last}"
        right = f"{_runtime_status_bar(settings, state)}  "
        text = _align_status_toolbar(left, right, terminal_width=shutil.get_terminal_size().columns)
        return FormattedText([("class:bottom-toolbar.text", text)])

    return toolbar


def _align_status_toolbar(left: str, right: str, *, terminal_width: int) -> str:
    gap = terminal_width - get_cwidth(left) - get_cwidth(right)
    if gap >= 1:
        return f"{left}{' ' * gap}{right}"
    separator = "  ｜  "
    right_width = get_cwidth(right)
    available_left = terminal_width - right_width - get_cwidth(separator)
    if available_left > 0:
        left = _truncate_to_width(left, available_left)
        return f"{left}{separator}{right}"
    return _truncate_to_width(right, terminal_width)


def _runtime_status_bar(settings, state: ReplState) -> str:
    now = time.monotonic()
    if state.status_bar_cache and now - state.status_bar_cache_at < 5.0:
        return state.status_bar_cache
    try:
        storage = DuckDBStorage(getattr(settings, "db_path"))
        monitor_runtime = storage.get_monitor_runtime("monitor")
        monitor_status = "run" if runtime_is_running(monitor_runtime) else "stop"
        scheduler_runtime = storage.get_monitor_runtime(SCHEDULER_SERVICE_NAME)
        scheduler_status = "run" if runtime_is_running(scheduler_runtime) else "stop"
        tasks = storage.list_scheduled_tasks()
        enabled_count = sum(1 for task in tasks if task.get("enabled"))
        status = (
            f"monitor:{monitor_status} ｜ scheduler:{scheduler_status} ｜ "
            f"schedule:{enabled_count}/{len(tasks)} ｜ {_portfolio_status_bar(storage)}"
        )
    except Exception:
        status = "monitor:stop ｜ scheduler:stop ｜ schedule:0/0 ｜ pf:--"
    state.status_bar_cache = status
    state.status_bar_cache_at = now
    return status


def _portfolio_status_bar(storage: DuckDBStorage) -> str:
    try:
        from sats.portfolio.storage import PortfolioStore

        store = PortfolioStore(storage)
        account = store.paper_account("default")
        if not account:
            return "pf:--"
        total_asset = _status_number(account.get("total_asset"))
        initial_cash = _status_number(account.get("initial_cash"))
        market_value = _status_number(account.get("market_value"))
        if total_asset is None or initial_cash is None or total_asset <= 0 or initial_cash <= 0:
            return "pf:--"
        positions = store.paper_positions("default")
        market_regime = store.latest_market_regime()
        score = _status_number(market_regime.get("score")) if market_regime else None
        score_text = "--" if score is None else f"{score:.0f}"
        exposure_pct = market_value / total_asset * 100.0 if market_value is not None else 0.0
        return_pct = (total_asset / initial_cash - 1.0) * 100.0
        return f"pf:持{len(positions)} 仓{exposure_pct:.0f}% 盈{return_pct:+.1f}% 盘{score_text}"
    except Exception:
        return "pf:--"


def _status_number(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _refresh_runtime_status_bar(state: ReplState, command: str) -> None:
    if command in {"monitor", "schedule", "portfolio"}:
        state.status_bar_cache = ""
        state.status_bar_cache_at = 0.0


def _provider_label(provider: str) -> str:
    raw = str(provider or "").strip()
    if not raw:
        return "OpenAI"
    mapping = {
        "openai": "OpenAI",
        "deepseek": "DeepSeek",
        "zhipu": "Zhipu",
    }
    normalized = raw.lower()
    return mapping.get(normalized, raw[:1].upper() + raw[1:])


def _format_duration(seconds: float | None) -> str:
    value = max(0.0, float(seconds or 0.0))
    if value < 60:
        return f"{value:.1f}s"
    if value < 3600:
        minutes = int(value // 60)
        rest = int(value % 60)
        return f"{minutes}m{rest:02d}s"
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    return f"{hours}h{minutes:02d}m"


def _record_last_duration(state: ReplState, started_at: float) -> None:
    state.last_duration_seconds = max(0.0, time.monotonic() - started_at)


def _record_session_id(session: object | None, state: ReplState) -> str:
    session = _active_chat_session(session, state)
    return str(getattr(session, "session_id", "") or state.session_id or "repl")


def _active_chat_session(session: object | None, state: ReplState) -> object | None:
    return state.chat_session or session


def _new_chat_session_id() -> str:
    return f"chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _handle_new_chat_session(
    title: str,
    *,
    current_session: ChatSession | None,
    state: ReplState,
    printer: Callable[[str], None],
) -> None:
    settings = getattr(current_session, "settings", None) or load_settings()
    session_id = _new_chat_session_id()
    kwargs = {"settings": settings, "session_id": session_id}
    if current_session is not None:
        kwargs.update(
            {
                "skills": getattr(current_session, "skills", None),
                "llm_factory": getattr(current_session, "llm_factory", None),
                "memory_extractor": getattr(current_session, "memory_extractor", None),
                "memory_enabled": bool(getattr(current_session, "memory_enabled", True)),
                "max_history_messages": int(getattr(current_session, "max_history_messages", 12)),
                "summary_threshold_messages": int(getattr(current_session, "summary_threshold_messages", 24)),
                "summary_refresh_messages": int(getattr(current_session, "summary_refresh_messages", 8)),
                "tools_enabled": bool(getattr(current_session, "tools_enabled", True)),
                "max_tool_iterations": int(getattr(current_session, "max_tool_iterations", 4)),
                "preprocess_enabled": bool(getattr(current_session, "preprocess_enabled", True)),
                "knowledge": getattr(current_session, "knowledge", None),
            }
        )
    session = ChatSession(**kwargs)
    state.chat_session = session
    state.session_id = session_id
    state.last_output = None
    state.last_stock_output = None
    clean_title = str(title or "").strip()
    try:
        store = session._ensure_memory_store()
        if store is not None:
            store.create_session(
                session_id,
                title=clean_title,
                model_name=str(getattr(settings, "openai_model", "") or ""),
            )
    except Exception:
        pass
    if clean_title:
        printer(f"新对话: {session_id} ({clean_title})")
    else:
        printer(f"新对话: {session_id}")


def _record_interaction_history(
    record: ReplExecutionRecord | None,
    *,
    state: ReplState,
    started_at: float,
) -> None:
    if record is None or state.history_store is None:
        return
    try:
        state.history_store.add_record(
            kind=record.kind,
            request=record.request,
            source=record.source,
            output=record.output,
            status=record.status,
            duration_seconds=max(0.0, time.monotonic() - started_at),
            report_path=record.report_path,
            session_id=record.session_id or state.session_id,
        )
    except Exception:
        return


def handle_repl_line(
    line: str,
    *,
    runner: Callable[[list[str]], int] | None = None,
    chat_session: ChatSession | None = None,
    printer: Callable[[str], None] = print,
    formatted_printer: Callable[[FormattedText], None] | None = None,
    state: ReplState | None = None,
) -> bool:
    state = state or ReplState()
    text = str(line or "").strip()
    if not text and str(state.pending_clarification_id or "").strip():
        output, status = _reject_pending_repl_clarification(current_session=_active_chat_session(chat_session, state), state=state)
        printer(output)
        _remember_output(state, output, request="", source="/reject")
        return True
    if not text:
        return True
    started_at = time.monotonic()
    update_duration = True
    interrupted_record: ReplExecutionRecord | None = None
    try:
        if not text.startswith("/"):
            if str(state.pending_clarification_id or "").strip():
                record = _handle_clarification_answer(
                    text,
                    chat_session=chat_session,
                    printer=printer,
                    formatted_printer=formatted_printer,
                    state=state,
                )
                _record_interaction_history(record, state=state, started_at=started_at)
                return True
            interrupted_record = ReplExecutionRecord(
                kind="chat",
                request=text,
                source="chat",
                output=INTERRUPT_MESSAGE,
                status="interrupted",
                session_id=_record_session_id(chat_session, state),
            )
            record = _handle_chat(
                text,
                chat_session=chat_session,
                printer=printer,
                formatted_printer=formatted_printer,
                state=state,
            )
            _record_interaction_history(record, state=state, started_at=started_at)
            return True
        try:
            argv = repl_command_to_argv(text)
        except ValueError as exc:
            message = f"解析失败: {exc}"
            printer(message)
            _record_interaction_history(
                ReplExecutionRecord(
                    kind="command",
                    request=text,
                    source="/",
                    output=message,
                    status="error",
                    session_id=state.session_id,
                ),
                state=state,
                started_at=started_at,
            )
            return True
        if not argv:
            render_help_box(printer=printer, formatted_printer=_help_formatted_printer(printer, formatted_printer))
            return True

        command = argv[0]
        if command in {"exit", "quit"}:
            update_duration = False
            printer("bye")
            return False
        if command == "help":
            render_help_box(printer=printer, formatted_printer=_help_formatted_printer(printer, formatted_printer))
            return True
        if command == "new":
            current_session = _active_chat_session(chat_session, state)
            _handle_new_chat_session(
                " ".join(argv[1:]).strip(),
                current_session=current_session if isinstance(current_session, ChatSession) else None,
                state=state,
                printer=printer,
            )
            _clear_pending_clarification(state)
            return True
        if command == "goal":
            subcommand = str(argv[1]).lower() if len(argv) > 1 else "status"
            if subcommand == "status":
                output = f"当前 Agent 目标: {state.agent_goal}" if state.agent_goal else "当前没有 Agent 目标。"
                printer(output)
                _remember_output(state, output, request=text, source="/goal")
                return True
            if subcommand in {"cancel", "clear"}:
                state.agent_goal = ""
                output = "已取消 Agent 目标。"
                printer(output)
                _remember_output(state, output, request=text, source="/goal")
                return True
            state.agent_goal = " ".join(argv[1:]).strip()
            argv = ["agent", *argv[1:]]
            command = "agent"
        if command == "plan":
            message = " ".join(argv[1:]).strip()
            if not message:
                output = "错误: plan message is required"
            elif state.engine == "agent":
                argv = ["agent", "--plan-only", *argv[1:]]
                command = "agent"
            else:
                settings = getattr(_active_chat_session(chat_session, state), "settings", None) or load_settings()
                output = format_conversation_plan(message, settings=settings)
                printer(output)
                _remember_output(state, output, request=text, source="/plan")
                _record_interaction_history(
                    ReplExecutionRecord(
                        kind="chat",
                        request=text,
                        source="/plan",
                        output=output,
                        status="done",
                        session_id=state.session_id,
                    ),
                    state=state,
                    started_at=started_at,
                )
                return True
            if command != "agent":
                printer(output)
                return True
        if command == "engine":
            requested = str(argv[1]).lower() if len(argv) > 1 else "status"
            if requested == "status":
                output = f"当前自然语言引擎: {state.engine}"
            elif requested in {"conversation", "legacy", "agent"}:
                state.engine = requested
                output = f"已切换自然语言引擎: {state.engine}"
            else:
                output = "错误: engine 只支持 conversation, legacy 或 agent"
            printer(output)
            _remember_output(state, output, request=text, source="/engine")
            return True
        if command == "clear":
            printer("\033[2J\033[H")
            return True
        if command == "answer":
            output, status = _handle_answer_builtin(
                argv[1:],
                current_session=_active_chat_session(chat_session, state),
                state=state,
            )
            current_settings = getattr(_active_chat_session(chat_session, state), "settings", None) or load_settings()
            _print_natural_output(
                output,
                printer=printer,
                formatted_printer=formatted_printer,
                db_path=getattr(current_settings, "db_path", None),
            )
            _remember_output(state, output, request=text, source="/answer")
            _record_interaction_history(
                ReplExecutionRecord(
                    kind="chat",
                    request=text,
                    source="/answer",
                    output=output,
                    status=status,
                    session_id=state.session_id,
                ),
                state=state,
                started_at=started_at,
            )
            return True
        if command in {"confirm", "reject", "trace"}:
            output, status = _handle_runtime_builtin(
                command,
                argv[1:],
                current_session=_active_chat_session(chat_session, state),
                state=state,
            )
            current_settings = getattr(_active_chat_session(chat_session, state), "settings", None) or load_settings()
            if command in {"confirm", "reject"}:
                _print_natural_output(
                    output,
                    printer=printer,
                    formatted_printer=formatted_printer,
                    db_path=getattr(current_settings, "db_path", None),
                )
            else:
                printer(output)
            _remember_output(state, output, request=text, source=f"/{command}")
            _record_interaction_history(
                ReplExecutionRecord(
                    kind="chat",
                    request=text,
                    source=f"/{command}",
                    output=output,
                    status=status,
                    session_id=state.session_id,
                ),
                state=state,
                started_at=started_at,
            )
            return True
        if command == "save":
            _handle_save_command(argv[1:], state=state, printer=printer)
            return True
        if command == "chat":
            chat_args = list(argv[1:])
            use_memory = None
            engine = state.engine
            if chat_args and chat_args[0] == "--no-memory":
                use_memory = False
                chat_args = chat_args[1:]
            if len(chat_args) >= 2 and chat_args[0] == "--engine":
                engine = chat_args[1]
                chat_args = chat_args[2:]
            if chat_args and chat_args[0] == "--agent":
                engine = "agent"
                chat_args = chat_args[1:]
            if chat_args and chat_args[0] == "--no-agent":
                engine = "legacy"
                chat_args = chat_args[1:]
            message = " ".join(chat_args).strip()
            if not message:
                message_text = "错误: chat message is required"
                printer(message_text)
                _record_interaction_history(
                    ReplExecutionRecord(
                        kind="chat",
                        request=text,
                        source="chat",
                        output=message_text,
                        status="error",
                        session_id=_record_session_id(chat_session, state),
                    ),
                    state=state,
                    started_at=started_at,
                )
                return True
            interrupted_record = ReplExecutionRecord(
                kind="chat",
                request=message,
                source="chat",
                output=INTERRUPT_MESSAGE,
                status="interrupted",
                session_id=_record_session_id(chat_session, state),
            )
            record = _handle_chat(
                message,
                chat_session=chat_session,
                printer=printer,
                formatted_printer=formatted_printer,
                use_memory=use_memory,
                state=state,
                engine=engine,
            )
            _record_interaction_history(record, state=state, started_at=started_at)
            return True
        if command not in CLI_COMMANDS and command != "agent":
            message = f"未知命令: /{command}。输入 /help 查看可用命令。"
            printer(message)
            _record_interaction_history(
                ReplExecutionRecord(
                    kind="command",
                    request=text,
                    source=f"/{command}",
                    output=message,
                    status="error",
                    session_id=state.session_id,
                ),
                state=state,
                started_at=started_at,
            )
            return True

        command_runner = runner or _run_cli_command
        buffer = io.StringIO()
        original_stdout = sys.stdout
        tee = _TeeStdout(printer, buffer, original_stdout=original_stdout)
        record_command = command != "history"
        if record_command:
            interrupted_record = ReplExecutionRecord(
                kind="command",
                request=text,
                source=f"/{command}",
                output=INTERRUPT_MESSAGE,
                status="interrupted",
                session_id=state.session_id,
            )
        output = ""
        status = "done"
        try:
            with redirect_stdout(tee):
                command_runner(argv)
        except SystemExit as exc:
            tee.flush()
            message = _system_exit_message(exc)
            if message:
                printer(message)
            output = _merge_history_output(buffer.getvalue().rstrip(), message)
            status = "error" if _system_exit_failed(exc) else "done"
        except ValueError as exc:
            tee.flush()
            message = f"错误: {exc}"
            printer(message)
            output = _merge_history_output(buffer.getvalue().rstrip(), message)
            status = "error"
        except KeyboardInterrupt:
            tee.flush()
            raise
        except Exception as exc:  # pragma: no cover - defensive REPL boundary
            tee.flush()
            message = f"错误: {exc}"
            printer(message)
            output = _merge_history_output(buffer.getvalue().rstrip(), message)
            status = "error"
        else:
            tee.flush()
            output = buffer.getvalue().rstrip()
            _remember_output(state, output, request=text, source=f"/{command}")
        _refresh_runtime_status_bar(state, command)
        if record_command:
            report_path = extract_report_path(output)
            _record_interaction_history(
                ReplExecutionRecord(
                    kind="command",
                    request=text,
                    source=f"/{command}",
                    output=output,
                    status=status,
                    report_path=str(report_path or ""),
                    session_id=state.session_id,
                ),
                state=state,
                started_at=started_at,
            )
        return True
    except KeyboardInterrupt:
        printer(INTERRUPT_MESSAGE)
        _record_interaction_history(interrupted_record, state=state, started_at=started_at)
        return True
    finally:
        if update_duration:
            _record_last_duration(state, started_at)


def _help_formatted_printer(
    printer: Callable[[str], None],
    formatted_printer: Callable[[FormattedText], None] | None,
) -> Callable[[FormattedText], None] | None:
    if formatted_printer is not None:
        return formatted_printer
    if printer is print:
        return None
    return lambda fragments: printer(fragment_list_to_text(fragments))


def _handle_chat(
    message: str,
    *,
    chat_session: ChatSession | None,
    printer: Callable[[str], None],
    formatted_printer: Callable[[FormattedText], None] | None = None,
    use_memory: bool | None = None,
    state: ReplState | None = None,
    engine: str | None = None,
) -> ReplExecutionRecord | None:
    state = state or ReplState()
    save_request = parse_save_request(message)
    if save_request is not None and save_request.is_pure:
        _save_last_output(save_request, state=state, printer=printer)
        return None
    chat_message = save_request.cleaned_text if save_request is not None and save_request.cleaned_text else message
    active_session = _active_chat_session(chat_session, state)
    session = active_session if active_session is not None else ChatSession()
    if isinstance(session, ChatSession):
        state.chat_session = session
    active_engine = str(engine or state.engine or "conversation")
    if type(session) is not ChatSession and active_engine != "agent":
        active_engine = "legacy"
    session_id = _record_session_id(session, state)
    progress = create_progress(request=chat_message)
    try:
        settings = getattr(session, "settings", None) or load_settings()
        reference_context = _build_repl_reference_context(chat_message, state, settings)
        kwargs = {}
        if use_memory is not None:
            kwargs["use_memory"] = use_memory
        if reference_context is not None:
            kwargs["reference_context"] = reference_context
        if getattr(progress, "enabled", False):
            kwargs["progress"] = progress
        if active_engine == "agent":
            agent_result = run_agent_once(
                chat_message,
                settings=settings,
                policy=AgentExecutionPolicy(),
                session_id=session_id or "repl_agent",
                event_sink=agent_progress_event_sink(progress),
                reference_context=reference_context,
            )
            result = _chat_result_from_agent(agent_result)
        elif active_engine == "conversation":
            conversation_result = run_conversation_once(
                chat_message,
                settings=settings,
                policy=AgentExecutionPolicy(),
                session_id=session_id or "repl",
                event_sink=agent_progress_event_sink(progress),
                reference_context=reference_context,
            )
            _update_pending_clarification_state(state, conversation_result)
            result = _chat_result_from_conversation(conversation_result)
        else:
            if isinstance(session, ChatSession):
                kwargs["defer_memory_updates"] = True
            result = session.ask(chat_message, **kwargs)
    except ValueError as exc:
        progress.close()
        output = f"错误: {exc}"
        printer(output)
        return ReplExecutionRecord(
            kind="chat",
            request=chat_message,
            source="chat",
            output=output,
            status="error",
            session_id=session_id,
        )
    except Exception as exc:  # pragma: no cover - defensive REPL boundary
        progress.close()
        output = f"LLM错误: {exc}"
        printer(output)
        return ReplExecutionRecord(
            kind="chat",
            request=chat_message,
            source="chat",
            output=output,
            status="error",
            session_id=session_id,
        )
    finally:
        progress.close()
    output = format_chat_result(result)
    _print_chat_result(
        result,
        printer=printer,
        formatted_printer=formatted_printer,
        db_path=getattr(settings, "db_path", None),
    )
    source = "agent" if active_engine == "agent" else "conversation" if active_engine == "conversation" else "chat"
    captured = _remember_output(state, output, request=chat_message, source=source)
    if save_request is not None:
        _save_output(captured, save_request, printer=printer)
    return ReplExecutionRecord(
        kind="chat",
        request=chat_message,
        source=source,
        output=output,
        status="done",
        report_path=str(captured.report_path or ""),
        session_id=session_id,
    )


def _handle_clarification_answer(
    answer: str,
    *,
    chat_session: ChatSession | None,
    printer: Callable[[str], None],
    formatted_printer: Callable[[FormattedText], None] | None = None,
    state: ReplState,
) -> ReplExecutionRecord:
    active_session = _active_chat_session(chat_session, state)
    settings = getattr(active_session, "settings", None) or load_settings()
    session_id = _record_session_id(active_session, state)
    clarification_id = str(state.pending_clarification_id or "").strip()
    progress = create_progress(request=answer)
    try:
        result = continue_conversation_after_clarification(
            clarification_id,
            answer,
            settings=settings,
            store=ChatMemoryStore(settings.db_path),
            event_sink=agent_progress_event_sink(progress),
        )
    except Exception as exc:
        output = f"错误: {exc}"
        printer(output)
        return ReplExecutionRecord(kind="chat", request=answer, source="clarification", output=output, status="error", session_id=session_id)
    finally:
        progress.close()
    _update_pending_clarification_state(state, result)
    chat_result = _chat_result_from_conversation(result)
    output = format_chat_result(chat_result)
    _print_chat_result(
        chat_result,
        printer=printer,
        formatted_printer=formatted_printer,
        db_path=getattr(settings, "db_path", None),
    )
    captured = _remember_output(state, output, request=answer, source="clarification")
    return ReplExecutionRecord(
        kind="chat",
        request=answer,
        source="clarification",
        output=output,
        status="done",
        report_path=str(captured.report_path or ""),
        session_id=session_id,
    )


def _handle_answer_builtin(
    args: list[str],
    *,
    current_session: object | None,
    state: ReplState,
) -> tuple[str, str]:
    settings = getattr(current_session, "settings", None) or load_settings()
    if not args:
        return "错误: /answer CLARIFICATION_ID ANSWER", "error"
    clarification_id = args[0]
    answer_parts = args[1:]
    if str(state.pending_clarification_id or "").strip() and not str(clarification_id).startswith("act_"):
        clarification_id = state.pending_clarification_id
        answer_parts = args
    answer = " ".join(answer_parts).strip()
    if not answer:
        return "错误: /answer CLARIFICATION_ID ANSWER", "error"
    progress = create_progress(request=answer)
    try:
        result = continue_conversation_after_clarification(
            clarification_id,
            answer,
            settings=settings,
            store=ChatMemoryStore(settings.db_path),
            event_sink=agent_progress_event_sink(progress),
        )
    except Exception as exc:
        return f"错误: {exc}", "error"
    finally:
        progress.close()
    _update_pending_clarification_state(state, result)
    return format_chat_result(_chat_result_from_conversation(result)), "done"


def _reject_pending_repl_clarification(*, current_session: object | None, state: ReplState) -> tuple[str, str]:
    clarification_id = str(state.pending_clarification_id or "").strip()
    if not clarification_id:
        return "当前没有待澄清问题。", "done"
    settings = getattr(current_session, "settings", None) or load_settings()
    try:
        result = reject_pending_conversation_action(
            clarification_id,
            settings=settings,
            store=ChatMemoryStore(settings.db_path),
        )
        output = format_chat_result(_chat_result_from_conversation(result))
    except Exception as exc:
        output = f"错误: {exc}"
        _clear_pending_clarification(state)
        return output, "error"
    _clear_pending_clarification(state)
    return output, "done"


def _update_pending_clarification_state(state: ReplState, result: object) -> None:
    if bool(getattr(result, "requires_clarification", False)):
        state.pending_clarification_id = str(getattr(result, "clarification_id", "") or "")
        state.pending_clarification_prompt = str(getattr(result, "clarification_prompt", "") or "")
        return
    _clear_pending_clarification(state)


def _clear_pending_clarification(state: ReplState) -> None:
    state.pending_clarification_id = ""
    state.pending_clarification_prompt = ""


def _handle_runtime_builtin(
    command: str,
    args: list[str],
    *,
    current_session: object | None,
    state: ReplState,
) -> tuple[str, str]:
    try:
        settings = getattr(current_session, "settings", None) or load_settings()
        store = ChatMemoryStore(settings.db_path)
        if command == "trace":
            turn_id = args[0] if args else ""
            return format_runtime_trace(store, turn_id=turn_id, session_id=state.session_id), "done"
        if not args:
            if command == "reject" and str(state.pending_clarification_id or "").strip():
                return _reject_pending_repl_clarification(current_session=current_session, state=state)
            return f"错误: /{command} ACTION_ID", "error"
        action_id = args[0]
        action = store.get_pending_action(action_id)
        if command == "confirm" and action and str(action.get("action_type") or "") == "conversation_tool":
            result = confirm_pending_conversation_action(action_id, settings=settings, store=store)
            return format_chat_result(_chat_result_from_conversation(result)), "done"
        if command == "reject" and action and str(action.get("action_type") or "") in {"conversation_tool", "conversation_clarification"}:
            result = reject_pending_conversation_action(action_id, settings=settings, store=store)
            if str(action.get("action_type") or "") == "conversation_clarification" and action_id == state.pending_clarification_id:
                _clear_pending_clarification(state)
            return format_chat_result(_chat_result_from_conversation(result)), "done"
        if command == "confirm":
            result = confirm_pending_runtime_action(action_id, settings=settings, store=store)
        else:
            result = reject_pending_runtime_action(action_id, settings=settings, store=store)
        return format_chat_result(_chat_result_from_runtime(result)), "done"
    except Exception as exc:
        return f"错误: {exc}", "error"


def _chat_result_from_runtime(result: object) -> ChatResult:
    pending = getattr(result, "pending_action", None)
    return ChatResult(
        content=str(getattr(result, "content", "") or ""),
        skill_names=(),
        tool_call_count=int(getattr(result, "tool_call_count", 0) or 0),
        data_names=tuple(getattr(result, "data_names", ()) or ()),
        artifacts=tuple(artifact.to_dict() for artifact in getattr(result, "artifacts", ()) or ()),
        requires_confirmation=pending is not None,
        pending_action_id=getattr(pending, "action_id", None) if pending is not None else None,
    )


def _chat_result_from_agent(result: object) -> ChatResult:
    pending = getattr(result, "pending_action", None)
    return ChatResult(
        content=str(getattr(result, "content", "") or ""),
        skill_names=tuple(getattr(result, "skill_names", ()) or ()),
        tool_call_count=int(getattr(result, "tool_call_count", 0) or 0),
        data_names=tuple(getattr(result, "data_names", ()) or ()),
        artifacts=tuple(getattr(result, "artifacts", ()) or ()),
        sources=tuple(getattr(result, "sources", ()) or ()),
        requires_confirmation=bool(pending is not None),
        pending_action_id=str(getattr(pending, "action_id", "") or "") if pending is not None else None,
        turn_id=getattr(result, "turn_id", None),
        session_id=str(getattr(result, "session_id", "") or ""),
    )


def _chat_result_from_conversation(result: object) -> ChatResult:
    return ChatResult(
        content=str(getattr(result, "content", "") or ""),
        skill_names=tuple(getattr(result, "skill_names", ()) or ()),
        tool_call_count=int(getattr(result, "tool_call_count", 0) or 0),
        data_names=tuple(getattr(result, "data_names", ()) or ()),
        artifacts=tuple(getattr(result, "artifacts", ()) or ()),
        sources=tuple(getattr(result, "sources", ()) or ()),
        requires_confirmation=bool(getattr(result, "requires_confirmation", False)),
        pending_action_id=str(getattr(result, "pending_action_id", "") or "") or None,
        turn_id=getattr(result, "turn_id", None),
        session_id=str(getattr(result, "session_id", "") or ""),
    )


def _print_chat_result(
    result: ChatResult,
    *,
    printer: Callable[[str], None],
    formatted_printer: Callable[[FormattedText], None] | None,
    db_path: Path | str | None,
) -> None:
    if printer is not print and formatted_printer is None:
        printer(format_chat_result(result))
        return
    width = _banner_width()
    tty = printer is print or formatted_printer is not None
    rendered = render_chat_result(result, channel="repl", tty=tty, width=width, db_path=db_path)
    if isinstance(rendered, str):
        printer(rendered)
        return
    (_help_formatted_printer(printer, formatted_printer) or print_formatted_text)(rendered)


def _print_natural_output(
    output: str,
    *,
    printer: Callable[[str], None],
    formatted_printer: Callable[[FormattedText], None] | None,
    db_path: Path | str | None,
) -> None:
    if printer is not print and formatted_printer is None:
        printer(output)
        return
    semantic_lexicon = build_output_semantic_lexicon(output, db_path=db_path)
    rendered = render_natural_output(
        output,
        channel="repl",
        tty=True,
        width=_banner_width(),
        semantic_lexicon=semantic_lexicon,
    )
    if isinstance(rendered, str):
        printer(rendered)
        return
    (_help_formatted_printer(printer, formatted_printer) or print_formatted_text)(rendered)


def repl_command_to_argv(line: str) -> list[str]:
    text = str(line or "").strip()
    if not text.startswith("/"):
        return []
    return shlex.split(text[1:])


def help_text() -> str:
    command_width = _help_command_width()
    command_lines = [f"  {_left_align_display(command, command_width)}  {description}" for command, description in HELP_COMMANDS]
    shortcut_lines = [f"  {_left_align_display(command, command_width)}  {description}" for command, description in HELP_SHORTCUTS]
    example_lines = [f"  {_left_align_display(command, command_width)}  {description}" for command, description in HELP_EXAMPLES]
    return "\n".join(["可用命令:", *command_lines, "", "快捷键:", *shortcut_lines, "", "示例:", *example_lines])


def _left_align_display(text: str, width: int) -> str:
    return f"{text}{' ' * max(0, width - _display_width(text))}"


def _history():
    history_path = Path.home() / ".sats_history"
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("ab"):
            pass
    except OSError:
        return InMemoryHistory()
    return SlashCommandFileHistory(str(history_path))


class SlashCommandFileHistory(FileHistory):
    def store_string(self, string: str) -> None:
        if str(string or "").strip().startswith("/"):
            super().store_string(string)


def _run_cli_command(argv: list[str]) -> int:
    from sats.cli import main

    return main(argv)


def _system_exit_message(exc: SystemExit) -> str:
    code = exc.code
    if code in (None, 0):
        return ""
    if isinstance(code, str):
        return f"错误: {code}"
    return f"命令退出: {code}"


def _system_exit_failed(exc: SystemExit) -> bool:
    return exc.code not in (None, 0)


def _merge_history_output(output: str, message: str) -> str:
    parts = [str(output or "").rstrip(), str(message or "").rstrip()]
    return "\n".join(part for part in parts if part)


def _print_system_exit(exc: SystemExit, printer: Callable[[str], None]) -> None:
    message = _system_exit_message(exc)
    if message:
        printer(message)


class _TeeStdout:
    def __init__(
        self,
        printer: Callable[[str], None],
        buffer: io.StringIO,
        *,
        original_stdout,
    ) -> None:
        self.printer = printer
        self._capture_buffer = buffer
        self.original_stdout = original_stdout
        self._pending = ""

    @property
    def encoding(self):
        return getattr(self.original_stdout, "encoding", None)

    @property
    def errors(self):
        return getattr(self.original_stdout, "errors", None)

    @property
    def buffer(self):
        return getattr(self.original_stdout, "buffer")

    def write(self, text: str) -> int:
        value = str(text)
        self._capture_buffer.write(value)
        self._pending += value
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._emit(line)
        return len(value)

    def flush(self) -> None:
        if self._pending:
            self._emit(self._pending)
            self._pending = ""
        if self.printer is print:
            self.original_stdout.flush()

    def isatty(self) -> bool:
        handler = getattr(self.original_stdout, "isatty", None)
        return bool(handler()) if callable(handler) else False

    def fileno(self) -> int:
        handler = getattr(self.original_stdout, "fileno")
        return int(handler())

    def writable(self) -> bool:
        handler = getattr(self.original_stdout, "writable", None)
        return bool(handler()) if callable(handler) else True

    def __getattr__(self, name: str):
        return getattr(self.original_stdout, name)

    def _emit(self, line: str) -> None:
        if self.printer is print:
            self.original_stdout.write(f"{line}\n")
            return
        self.printer(line)


def _handle_save_command(argv: list[str], *, state: ReplState, printer: Callable[[str], None]) -> None:
    parser = argparse.ArgumentParser(prog="/save", add_help=False)
    parser.add_argument("--format", choices=["md", "pdf"], default="md")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--source", choices=["output", "report"], default="output")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        printer("错误: /save --format md|pdf [--path PATH] [--source output|report]")
        return
    request = SaveRequest(format=args.format, source=args.source, path=args.path, is_pure=True)
    _save_last_output(request, state=state, printer=printer)


def _save_last_output(request: SaveRequest, *, state: ReplState, printer: Callable[[str], None]) -> None:
    if state.last_output is None or not state.last_output.content.strip():
        printer("没有可保存的上一条输出。")
        return
    _save_output(state.last_output, request, printer=printer)


def _build_repl_reference_context(message: str, state: ReplState, settings):
    context = build_chat_reference_context(message, state.last_output, settings)
    if context is not None and context.symbols:
        return context
    stock_output = state.last_stock_output
    if stock_output is not None and stock_output is not state.last_output:
        stock_context = build_chat_reference_context(message, stock_output, settings)
        if stock_context is not None and stock_context.symbols:
            return stock_context
    return context


def _save_output(captured: CapturedOutput, request: SaveRequest, *, printer: Callable[[str], None]) -> None:
    settings = load_settings()
    output_dir = Path(getattr(settings, "project_root", ".")) / "reports" / "saved_outputs"
    try:
        result = save_captured_output(captured, request, output_dir=output_dir, db_path=getattr(settings, "db_path", None))
    except Exception as exc:
        printer(f"保存失败: {exc}")
        return
    if result.warning:
        printer(f"提示: {result.warning}")
    printer(f"已保存: {result.path}")


def _remember_output(state: ReplState, content: str, *, request: str, source: str) -> CapturedOutput:
    captured = CapturedOutput(
        content=str(content or "").rstrip(),
        request=request,
        source=source,
        report_path=extract_report_path(content),
    )
    if captured.content.strip():
        state.last_output = captured
        if extract_stock_symbols(captured.content):
            state.last_stock_output = captured
    return captured
