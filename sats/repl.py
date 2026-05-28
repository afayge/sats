from __future__ import annotations

import argparse
import io
import shlex
import shutil
import sys
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, field
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
from sats.chat import ChatSession, format_chat_result
from sats.chat_reference import build_chat_reference_context
from sats.config import load_settings
from sats.output_saver import CapturedOutput, SaveRequest, extract_report_path, parse_save_request, save_captured_output
from sats.progress import create_progress
from sats.stock_question import extract_stock_symbols

CLI_COMMANDS = [
    "init",
    "screen",
    "results",
    "result-rules",
    "analyze",
    "analyze-dsa",
    "dsa",
    "analyze-chan",
    "chan-kb",
    "discover",
    "chat",
    "model",
    "memory",
    "indicators",
    "skills",
    "watchlist",
    "minute-k",
    "minute-k-clear",
    "monitor",
    "monitor-display",
    "schedule",
    "qmt",
    "serve",
]

BUILTIN_COMMANDS = ["help", "exit", "quit", "clear", "save"]

HELP_COMMANDS = [
    ("/help", "查看命令"),
    ("/clear", "清屏"),
    ("/save", "保存上一条输出"),
    ("/exit", "退出"),
    ("/quit", "退出"),
    ("/init", "初始化配置"),
    ("/screen", "全 A 股筛选"),
    ("/results", "查询筛选结果"),
    ("/result-rules", "查看结果规则名"),
    ("/analyze", "统一信号分析"),
    ("/analyze-dsa", "分析 DSA 股票"),
    ("/dsa", "原生 DSA 分析"),
    ("/analyze-chan", "分析缠论股票"),
    ("/chan-kb", "搜索缠论知识库"),
    ("/discover", "短线机会发现"),
    ("/chat", "LLM 聊天"),
    ("/model", "模型配置切换"),
    ("/memory", "管理聊天记忆"),
    ("/indicators", "计算技术指标"),
    ("/skills", "查看本地 skills"),
    ("/watchlist", "编辑关注列表"),
    ("/minute-k", "获取分钟 K 线"),
    ("/minute-k-clear", "清理分钟 K 缓存"),
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
    ("/results --trade-date 20260514 --passed", "查询通过股票"),
    ("/analyze --stocks 000938 --signals ma_kline,chan", "统一信号分析"),
    ("/analyze --from-screened --trade-date 20260514 --rule price_volume_ma --signals all", "分析筛选结果"),
    ("/analyze signals --category kline", "查看信号策略"),
    ("/dsa --stocks 000001,600519 --trade-date 20260514", "原生 DSA 分析"),
    ("/dsa --from-screened --trade-date 20260514 --explain-rating", "分析筛选结果"),
    ("/analyze-dsa --stocks 000001,600519", "分析股票"),
    ("/analyze-chan --stocks 000001 --chan-rule chan-signals", "缠论指定股票分析"),
    ("/discover --limit 5", "短线机会发现"),
    ("/indicators --symbols 000001.SZ --trade-date 20260514", "计算技术指标"),
    ("/minute-k --symbols 000001.SZ --period 1m --mode realtime", "获取实时分钟 K"),
    ("/watchlist", "编辑关注列表"),
    ("/watchlist add --symbols 000001.SZ,600519.SH", "批量关注股票"),
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
]

BANNER_CONTROL_COMMANDS = [
    ("/help", "查看命令"),
    ("/exit", "退出"),
    ("/clear", "清屏"),
    ("/save", "保存上一条输出"),
]

BANNER_COMMON_COMMANDS = [
    ("用缠论分析002436", "自然语言缠论分析"),
    ("/screen --trade-date YYYYMMDD", "全 A 股筛选"),
    ("/results --passed", "查询筛选结果"),
    ("/analyze --stocks 000938 --signals ma_kline,chan", "统一信号分析"),
    ("/dsa --stocks 000001,600519", "原生 DSA 分析"),
    ("/dsa --from-screened --trade-date YYYYMMDD", "分析筛选结果"),
    ("/analyze-dsa --stocks 000001,600519", "分析股票"),
    ("/analyze-chan --stocks 000001", "缠论指定股票分析"),
    ("/discover --limit 5", "短线机会发现"),
    ("/save --format md", "保存上一条输出"),
    ("/watchlist", "编辑关注列表"),
    ("/monitor start", "启动实时监控"),
    ("/monitor-display start", "当前终端打开监控显示"),
    ("/schedule list", "查看定时任务"),
    ("/qmt positions", "查看 QMT 持仓"),
    ("/model status", "查看模型配置"),
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
    "--symbols": "股票代码列表",
    "--stocks": "指定股票列表",
    "--from-screened": "使用筛选结果",
    "--signals": "信号策略列表",
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
    "--max-order-value": "单笔金额上限",
    "--max-position-pct": "仓位比例上限",
    "--sell-ratio": "卖出比例",
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
    "clear": "清空记录",
    "search": "搜索记录",
}

COMPLETION_WORDS = list(COMPLETION_DESCRIPTIONS)

PROMPT_MESSAGE = FormattedText([("", "sats> ")])
MUTED_STYLE = "fg:#9ca3af"
COMMAND_STYLE = "fg:#22d3ee"
DESC_STYLE = "fg:#ffffff"
SEPARATOR_STYLE = MUTED_STYLE
STATUS_BAR_STYLE = "bg:#2f2f2f fg:#f5f5f5"
REPL_STYLE = Style.from_dict(
    {
        "bottom-toolbar": "bg:#2f2f2f",
        "bottom-toolbar.text": "bg:#2f2f2f #f5f5f5",
    }
)


@dataclass(slots=True)
class ReplState:
    last_output: CapturedOutput | None = None
    last_stock_output: CapturedOutput | None = None
    started_at: float = field(default_factory=lambda: time.monotonic())
    last_duration_seconds: float | None = None


def run_repl() -> int:
    settings = load_settings()
    render_startup_banner(settings.db_path)
    chat_session = ChatSession(settings=settings)
    state = ReplState()
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
        display_dict={word: word for word in COMPLETION_WORDS},
        meta_dict=COMPLETION_DESCRIPTIONS,
        ignore_case=True,
        sentence=True,
    )


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
    try:
        if not text.startswith("/"):
            return _handle_chat(text, chat_session=chat_session, printer=printer, state=state)
        try:
            argv = repl_command_to_argv(text)
        except ValueError as exc:
            printer(f"解析失败: {exc}")
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
        if command == "clear":
            printer("\033[2J\033[H")
            return True
        if command == "save":
            _handle_save_command(argv[1:], state=state, printer=printer)
            return True
        if command == "chat":
            chat_args = list(argv[1:])
            use_memory = None
            if chat_args and chat_args[0] == "--no-memory":
                use_memory = False
                chat_args = chat_args[1:]
            message = " ".join(chat_args).strip()
            if not message:
                printer("错误: chat message is required")
                return True
            return _handle_chat(message, chat_session=chat_session, printer=printer, use_memory=use_memory, state=state)
        if command not in CLI_COMMANDS:
            printer(f"未知命令: /{command}。输入 /help 查看可用命令。")
            return True

        command_runner = runner or _run_cli_command
        buffer = io.StringIO()
        original_stdout = sys.stdout
        try:
            with redirect_stdout(_TeeStdout(printer, buffer, original_stdout=original_stdout)):
                command_runner(argv)
        except SystemExit as exc:
            _print_system_exit(exc, printer)
        except ValueError as exc:
            printer(f"错误: {exc}")
        except Exception as exc:  # pragma: no cover - defensive REPL boundary
            printer(f"错误: {exc}")
        else:
            _remember_output(state, buffer.getvalue().rstrip(), request=text, source=f"/{command}")
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
) -> bool:
    state = state or ReplState()
    save_request = parse_save_request(message)
    if save_request is not None and save_request.is_pure:
        _save_last_output(save_request, state=state, printer=printer)
        return True
    chat_message = save_request.cleaned_text if save_request is not None and save_request.cleaned_text else message
    session = chat_session or ChatSession()
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
        result = session.ask(chat_message, **kwargs)
    except ValueError as exc:
        printer(f"错误: {exc}")
        return True
    except Exception as exc:  # pragma: no cover - defensive REPL boundary
        printer(f"LLM错误: {exc}")
        return True
    finally:
        progress.close()
    output = format_chat_result(result)
    printer(output)
    captured = _remember_output(state, output, request=chat_message, source="chat")
    if save_request is not None:
        _save_output(captured, save_request, printer=printer)
    return True


def repl_command_to_argv(line: str) -> list[str]:
    text = str(line or "").strip()
    if not text.startswith("/"):
        return []
    return shlex.split(text[1:])


def help_text() -> str:
    command_width = _help_command_width()
    command_lines = [f"  {_left_align_display(command, command_width)}  {description}" for command, description in HELP_COMMANDS]
    example_lines = [f"  {_left_align_display(command, command_width)}  {description}" for command, description in HELP_EXAMPLES]
    return "\n".join(["可用命令:", *command_lines, "", "示例:", *example_lines])


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


def _print_system_exit(exc: SystemExit, printer: Callable[[str], None]) -> None:
    code = exc.code
    if code in (None, 0):
        return
    if isinstance(code, str):
        printer(f"错误: {code}")
        return
    printer(f"命令退出: {code}")


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
