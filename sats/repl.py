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

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from sats import __version__
from sats.agent import AgentExecutionPolicy, run_agent_once
from sats.agent.progress import agent_progress_event_sink
from sats.chat import ChatResult, ChatSession, format_chat_result
from sats.chat_reference import build_chat_reference_context
from sats.chat_runtime import confirm_pending_runtime_action, format_runtime_trace, reject_pending_runtime_action
from sats.config import load_settings
from sats.history import InteractionHistoryStore
from sats.memory import ChatMemoryStore
from sats.output_saver import CapturedOutput, SaveRequest, extract_report_path, parse_save_request, save_captured_output
from sats.progress import create_progress
from sats.stock_question import extract_stock_symbols

CLI_COMMANDS = [
    "init",
    "screen",
    "results",
    "result-rules",
    "quote",
    "analyze",
    "analyze-dsa",
    "dsa",
    "analyze-chan",
    "chan-kb",
    "discover",
    "chat",
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
    "serve",
]

BUILTIN_COMMANDS = ["help", "exit", "quit", "clear", "save", "new", "confirm", "reject", "trace", "goal"]
INTERRUPT_MESSAGE = "已中断当前执行，返回 sats>。"

HELP_COMMANDS = [
    ("/help", "查看命令"),
    ("/new", "开启新对话"),
    ("/confirm", "确认运行待执行动作"),
    ("/reject", "取消待执行动作"),
    ("/trace", "查看对话 turn trace"),
    ("/clear", "清屏"),
    ("/save", "保存上一条输出"),
    ("/exit", "退出"),
    ("/quit", "退出"),
    ("/init", "初始化配置"),
    ("/screen", "全 A 股筛选"),
    ("/results", "查询筛选结果"),
    ("/result-rules", "查看结果规则名"),
    ("/quote", "实时价格"),
    ("/analyze", "统一信号分析"),
    ("/analyze-dsa", "分析 DSA 股票"),
    ("/dsa", "原生 DSA 分析"),
    ("/analyze-chan", "分析缠论股票"),
    ("/chan-kb", "搜索缠论知识库"),
    ("/discover", "短线机会发现"),
    ("/chat", "LLM 聊天"),
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
    ("/serve", "启动 API 服务"),
]

HELP_EXAMPLES = [
    ("用缠论分析002436", "自然语言触发真实缠论分析"),
    ("分析002436 2026-05-15", "自然语言触发真实股票分析"),
    ("/screen --trade-date 20260514 --rule price_volume_ma", "全 A 股筛选"),
    ("/screen --trade-date 20260514 --rule monthly_base_breakout", "月K箱体突破筛选"),
    ("/results --trade-date 20260514 --passed", "查询通过股票"),
    ("/quote --stocks 000001,600519", "查看实时价格"),
    ("/analyze --stocks 000938 --signals ma_kline,chan", "统一信号分析"),
    ("/analyze --from-screened --trade-date 20260514 --rule price_volume_ma --signals all", "分析筛选结果"),
    ("/analyze signals --category kline", "查看信号策略"),
    ("/dsa --stocks 000001,600519 --trade-date 20260514", "原生 DSA 分析"),
    ("/dsa --from-screened --trade-date 20260514 --explain-rating", "分析筛选结果"),
    ("/analyze-dsa --stocks 000001,600519", "分析股票"),
    ("/analyze-chan --stocks 000001 --chan-rule chan-signals", "缠论指定股票分析"),
    ("/discover --limit 5", "短线机会发现"),
    ("/indicators --symbols 000001.SZ --trade-date 20260514", "计算技术指标"),
    ("/factor list --zoo barra_style", "查看 Barra 风格近似因子"),
    ("/factor pick --profile balanced --trade-date 20260514 --top 20", "默认画像多因子选股"),
    ("/factor pick --factors barra_style_value,barra_style_quality --trade-date 20260514 --top 20", "多因子选股"),
    ("/factor ml status", "检查 Qlib/ML 依赖"),
    ("/factor ml train --profile balanced --model lightgbm --train-start 20250101 --train-end 20260430 --valid-end 20260514", "训练因子 ML 模型"),
    ("/factor ml predict --model-run factor_ml_xxxxxxxx --trade-date 20260514 --top 20 --write-screening", "因子 ML 预测并写入筛选结果"),
    ("/chat 分析000001的因子暴露", "自然语言因子暴露分析"),
    ("/new 茅台估值复盘", "开启新的多轮对话"),
    ("/chat 写一个5日/20日均线策略并回测000001", "生成待确认 runtime 动作"),
    ("筛选短线机会并保存报告", "自然语言自主执行 Agent"),
    ("/goal 明天按信号自动买入不超过2万", "设置并运行 Agent 目标"),
    ("/confirm act_xxxxxxxx", "确认 runtime 动作"),
    ("/trace", "查看最近一次对话 trace"),
    ("/watchlist", "编辑关注列表"),
    ("/watchlist add --stocks 000001.SZ,600519.SH", "批量关注股票"),
    ("/watchlist clear", "清空关注列表"),
    ("/monitor start --rules chan_signals", "启动实时监控"),
    ("/monitor-display start", "当前终端打开监控显示"),
    ("/schedule list", "查看定时任务"),
    ("/schedule add --name daily-discover --type chat --text \"预测未来几天大概率上涨的股票\" --daily --time 08:45", "添加定时聊天任务"),
    ("/qmt positions", "查看 QMT 持仓"),
    ("/qmt buy --symbol 000001 --quantity 100 --price-type latest", "QMT 实盘买入"),
    ("/model status", "查看当前模型"),
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
]

BANNER_CONTROL_COMMANDS = [
    ("/help", "查看命令"),
    ("/exit", "退出"),
    ("/clear", "清屏"),
    ("/save", "保存上一条输出"),
    ("Ctrl+C", "中断当前执行"),
]

BANNER_COMMON_COMMANDS = [
    ("用缠论分析002436", "自然语言缠论分析"),
    ("/screen --trade-date YYYYMMDD", "全 A 股筛选"),
    ("/results --passed", "查询筛选结果"),
    ("/quote --stocks 000001,600519", "查看实时价格"),
    ("/analyze --stocks 000938 --signals ma_kline,chan", "统一信号分析"),
    ("/dsa --stocks 000001,600519", "原生 DSA 分析"),
    ("/dsa --from-screened --trade-date YYYYMMDD", "分析筛选结果"),
    ("/analyze-dsa --stocks 000001,600519", "分析股票"),
    ("/analyze-chan --stocks 000001", "缠论指定股票分析"),
    ("/discover --limit 5", "短线机会发现"),
    ("/factor list --zoo barra_style", "股票因子"),
    ("/factor pick --profile balanced", "画像选股"),
    ("/factor ml status", "ML 依赖"),
    ("/save --format md", "保存上一条输出"),
    ("/watchlist", "编辑关注列表"),
    ("/monitor start", "启动实时监控"),
    ("/monitor-display start", "当前终端打开监控显示"),
    ("/schedule list", "查看定时任务"),
    ("/qmt positions", "查看 QMT 持仓"),
    ("/model status", "查看模型配置"),
]

HELP_SHORTCUTS = [
    ("Ctrl+C", "中断当前执行"),
]

_HELP_DESCRIPTIONS = dict(HELP_COMMANDS)

COMPLETION_DESCRIPTIONS = {
    **{f"/{command}": _HELP_DESCRIPTIONS.get(f"/{command}", "CLI 命令") for command in CLI_COMMANDS},
    **{f"/{command}": _HELP_DESCRIPTIONS.get(f"/{command}", "内置命令") for command in BUILTIN_COMMANDS},
    "--trade-date": "交易日 YYYYMMDD",
    "--rule": "筛选规则名",
    "--chan-rule": "缠论规则名",
    "--passed": "只显示通过结果",
    "--db": "DuckDB 路径",
    "--symbols": "股票代码或名称列表",
    "--stocks": "指定股票代码或名称列表",
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
    "--mode": "数据模式",
    "--count": "数量限制",
    "--start-date": "开始日期",
    "--end-date": "结束日期",
    "--top": "最大候选数",
    "--limit": "返回数量",
    "--candidate-limit": "候选池数量",
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
    "--buy-price": "买入价格",
    "--quantity": "持仓数量",
    "--buy-date": "买入日期",
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
    "--lists": "列表名称",
    "--llm-review": "启用 LLM 复核",
    "--once": "只运行一次",
    "--refresh": "刷新间隔",
    "--plain": "纯文本输出",
    "--new-terminal": "新终端窗口",
    "--select-watchlist": "选择导入关注列表",
    "--no-select-watchlist": "跳过关注列表选择",
    "positions": "持仓管理",
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
    "run-loop": "前台调度循环",
    "runs": "执行记录",
    "qmt": "QMT 交易",
    "bridge": "QMT Bridge",
    "sync": "同步数据",
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
    "ingest": "导入知识库",
    "sync-stock-basic": "同步股票列表",
}

COMPLETION_WORDS = list(COMPLETION_DESCRIPTIONS)

PROMPT_MESSAGE = FormattedText([("", "sats> ")])
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


@dataclass(slots=True)
class ReplState:
    last_output: CapturedOutput | None = None
    last_stock_output: CapturedOutput | None = None
    started_at: float = field(default_factory=lambda: time.monotonic())
    last_duration_seconds: float | None = None
    session_id: str = "repl"
    history_store: InteractionHistoryStore | None = None
    chat_session: ChatSession | None = None
    agent_goal: str = ""


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
    session = PromptSession(
        history=_history(),
        completer=build_repl_completer(),
        complete_while_typing=True,
        bottom_toolbar=_status_toolbar(settings, state),
        style=REPL_STYLE,
    )
    while True:
        try:
            _print_input_separator()
            line = session.prompt(PROMPT_MESSAGE)
        except KeyboardInterrupt:
            print("^C")
            continue
        except EOFError:
            print("bye")
            return 0
        _print_input_separator()
        if not handle_repl_line(line, printer=print, chat_session=chat_session, state=state):
            return 0


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
    title = f">_ SATS v{__version__}"
    help_lines = _banner_help_lines(db_path)
    width = _banner_width(terminal_width)
    content_width = max(1, width - 4)

    plain_printer(_top_border(width))
    _print_banner_title(content_width, rich_printer)
    plain_printer(_bottom_border(width))
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


def _help_row_fragments(command: str, description: str, command_width: int) -> FormattedText:
    command_text = _truncate_to_width(command, command_width)
    return FormattedText(
        [
            (COMMAND_STYLE, command_text),
            ("", " " * (max(0, command_width - _display_width(command_text)) + 2)),
            (DESC_STYLE, description),
        ]
    )


def _banner_help_lines(db_path: Path | str) -> list[FormattedText]:
    command_width = _banner_command_width()
    return [
        FormattedText([(MUTED_STYLE, "输入 /commands 开始")]),
        *[_help_row_fragments(command, description, command_width) for command, description in BANNER_CONTROL_COMMANDS],
        FormattedText([(MUTED_STYLE, "常用:")]),
        *[_help_row_fragments(command, description, command_width) for command, description in BANNER_COMMON_COMMANDS],
        FormattedText([(DESC_STYLE, f"DB: {db_path}")]),
    ]


def _banner_command_width() -> int:
    commands = [command for command, _ in BANNER_CONTROL_COMMANDS]
    commands.extend(command for command, _ in BANNER_COMMON_COMMANDS)
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


def _print_banner_title(width: int, formatted_printer: Callable[[FormattedText], None]) -> None:
    title = f">_ SATS v{__version__}"
    if _display_width(title) > width:
        plain_title = _truncate_to_width(title, width)
        padding = " " * max(0, width - _display_width(plain_title))
        formatted_printer(FormattedText([("", f"│ {plain_title}{padding} │")]))
        return
    padding = " " * max(0, width - _display_width(title))
    formatted_printer(
        FormattedText(
            [
                ("", "│ "),
                ("fg:#2563eb bold", ">_ SATS"),
                ("", " "),
                ("fg:#9ca3af", f"v{__version__}"),
                ("", f"{padding} │"),
            ]
        )
    )


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
        text = f" {provider}:{model}  ｜  {elapsed}  ｜  Last：{last}"
        return FormattedText([("class:bottom-toolbar.text", text)])

    return toolbar


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
    if not text:
        return True
    started_at = time.monotonic()
    update_duration = True
    interrupted_record: ReplExecutionRecord | None = None
    try:
        if not text.startswith("/"):
            interrupted_record = ReplExecutionRecord(
                kind="chat",
                request=text,
                source="chat",
                output=INTERRUPT_MESSAGE,
                status="interrupted",
                session_id=_record_session_id(chat_session, state),
            )
            record = _handle_chat(text, chat_session=chat_session, printer=printer, state=state)
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
        if command == "clear":
            printer("\033[2J\033[H")
            return True
        if command in {"confirm", "reject", "trace"}:
            output, status = _handle_runtime_builtin(
                command,
                argv[1:],
                current_session=_active_chat_session(chat_session, state),
                state=state,
            )
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
            agent_enabled = True
            if chat_args and chat_args[0] == "--no-memory":
                use_memory = False
                chat_args = chat_args[1:]
            if chat_args and chat_args[0] == "--no-agent":
                agent_enabled = False
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
            record = _handle_chat(message, chat_session=chat_session, printer=printer, use_memory=use_memory, state=state, agent_enabled=agent_enabled)
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
    use_memory: bool | None = None,
    state: ReplState | None = None,
    agent_enabled: bool = True,
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
    agent_enabled = agent_enabled and type(session) is ChatSession
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
        if agent_enabled:
            agent_result = run_agent_once(
                chat_message,
                settings=settings,
                policy=AgentExecutionPolicy(),
                session_id=session_id or "repl_agent",
                event_sink=agent_progress_event_sink(progress),
            )
            result = _chat_result_from_agent(agent_result)
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
    printer(output)
    source = "agent" if agent_enabled else "chat"
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
            return f"错误: /{command} ACTION_ID", "error"
        action_id = args[0]
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
    return ChatResult(
        content=str(getattr(result, "content", "") or ""),
        skill_names=tuple(getattr(result, "skill_names", ()) or ()),
        tool_call_count=int(getattr(result, "tool_call_count", 0) or 0),
        data_names=tuple(getattr(result, "data_names", ()) or ()),
        artifacts=tuple(getattr(result, "artifacts", ()) or ()),
        turn_id=getattr(result, "turn_id", None),
        session_id=str(getattr(result, "session_id", "") or ""),
    )


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
        result = save_captured_output(captured, request, output_dir=output_dir)
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
