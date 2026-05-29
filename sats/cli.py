from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
from prompt_toolkit.utils import get_cwidth

from sats.analysis.chan_llm_review import DEFAULT_CHAN_RULE_NAME, run_chan_llm_review
from sats.analysis.daily_stock_analysis import run_daily_stock_analysis_for_symbols, run_screened_stock_analysis
from sats.analysis.dsa_native import run_dsa_analysis
from sats.analysis.opportunity_discovery import (
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_DISCOVERY_LIMIT,
    DEFAULT_DISCOVERY_SIGNALS,
    format_opportunity_discovery,
    run_opportunity_discovery,
)
from sats.analysis.stock_llm_context import ensure_stock_analysis_data
from sats.chat import format_chat_result, run_chat_once
from sats.config import init_env_file, load_settings
from sats.data.astock_provider import AStockDataProvider
from sats.indicators import IndicatorCalculator, format_indicator_results
from sats.llm.model_config import current_model_status, discover_model_profiles, update_default_model_selection
from sats.memory import ChatMemoryStore, format_memory_list
from sats.monitoring import MonitorConfig, MonitorDisplay, MonitorService, format_monitor_dashboard
from sats.progress import create_progress
from sats.rag.chan_knowledge import search_chan_knowledge
from sats.rag.knowledge import KnowledgeStore, format_knowledge_list, format_search_results
from sats.screening.registry import get_rule, list_rules
from sats.screening.service import evaluate_inputs
from sats.scheduler import (
    SCHEDULER_SERVICE_NAME,
    SchedulerConfig,
    SchedulerService,
    ScheduledTaskRunner,
    compute_next_run,
    format_task_schedule,
    parse_schedule_days,
    validate_time_of_day,
)
from sats.skills import default_skills_dir, format_skill_list, load_skills
from sats.screening.service import evaluate_and_store
from sats.signals import SignalInput, analyze_signal_inputs, format_signal_analysis, format_signal_definitions
from sats.storage.duckdb import DuckDBStorage
from sats.stock_basic_lookup import load_stock_basic_frame, resolve_symbol_or_name_values
from sats.symbols import parse_symbol_csv
from sats.trading import OrderRequest, broker_from_settings
from sats.trading.monitor_provider import AutoTradeConfig, QmtTradingProvider
from sats.trading.qmt_bridge import QmtBridgeConfig, run_bridge
from sats.trading.sync import sync_positions_to_monitor
from sats.watchlist_editor import (
    delete_watchlist_symbols,
    format_watchlist,
    import_screened_to_watchlist,
    run_watchlist_editor,
    select_and_delete_watchlist,
    select_and_import_watchlist,
    upsert_watchlist_symbols,
)

DEFAULT_RULE = "ma_volume_relative_strength"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _progress_for_args(args: argparse.Namespace, *, json_mode: bool | None = None):
    if json_mode is None:
        json_mode = bool(getattr(args, "json", False))
    return create_progress(json_mode=json_mode, request=_progress_request(args))


def _progress_request(args: argparse.Namespace) -> str:
    command = str(getattr(args, "command", "") or "").strip()
    if not command:
        return ""
    parts = ["sats", command]
    for name in (
        "analyze_action",
        "monitor_command",
        "monitor_positions_command",
        "monitor_watchlist_command",
        "monitor_candidates_command",
        "monitor_display_command",
        "schedule_command",
        "qmt_command",
        "qmt_bridge_command",
        "qmt_sync_command",
    ):
        value = str(getattr(args, name, "") or "").strip()
        if value:
            parts.append(value)
    for option in (
        "stocks",
        "symbols",
        "trade_date",
        "rule",
        "signals",
        "period",
        "mode",
        "limit",
        "candidate_limit",
    ):
        value = getattr(args, option, None)
        if value not in (None, "", False):
            parts.append(f"--{option.replace('_', '-')} {value}")
    if getattr(args, "from_screened", False):
        parts.append("--from-screened")
    if getattr(args, "json", False):
        parts.append("--json")
    return " ".join(parts)


def _announce_analyzing(progress, *, json_mode: bool = False) -> None:
    if not json_mode and not getattr(progress, "enabled", False):
        print("analyzing...", flush=True)


def _add_monitor_trade_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--broker", choices=["noop", "qmt"], default="noop", help="Broker used by auto trading")
    parser.add_argument("--auto-trade", default="", help="Comma-separated actions: buy,sell")
    parser.add_argument("--max-order-value", type=float, default=20000.0, help="Max buy order value")
    parser.add_argument("--max-position-pct", type=float, default=0.2, help="Max buy value as total asset ratio")
    parser.add_argument("--sell-ratio", type=float, default=1.0, help="Sell ratio for position signals")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sats", description="SATS A股自动交易系统")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="Create .env configuration")
    init.add_argument("--overwrite", action="store_true", help="Overwrite existing .env")

    screen = sub.add_parser("screen", help="Run A股筛选规则")
    screen.add_argument("--rule", default=DEFAULT_RULE, help=f"Rule name. Available: {', '.join(list_rules())}")
    screen.add_argument("--trade-date", required=True, help="交易日 YYYYMMDD")
    watchlist_select = screen.add_mutually_exclusive_group()
    watchlist_select.add_argument("--select-watchlist", action="store_true", help="Select passed stocks to add to watchlist")
    watchlist_select.add_argument("--no-select-watchlist", action="store_true", help="Do not prompt for watchlist import")
    screen.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    results = sub.add_parser("results", help="Query screening results")
    results.add_argument("--trade-date")
    results.add_argument("--rule", default=None)
    results.add_argument("--passed", action="store_true", help="Only passed rows")
    results.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    result_rules = sub.add_parser("result-rules", help="List rule names saved in screening results")
    result_rules.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    quote = sub.add_parser("quote", help="Show realtime stock quotes with moving averages")
    quote.add_argument("--stocks", required=True, help="Comma-separated symbols or stock names, e.g. 000001,600519.SH,紫光股份")
    quote.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    analyze = sub.add_parser("analyze", help="Unified stock signal analysis")
    analyze.add_argument("analyze_action", nargs="?", choices=["signals"], help="Use 'signals' to list signal strategies")
    analyze_source = analyze.add_mutually_exclusive_group()
    analyze_source.add_argument("--stocks", help="Comma-separated symbols or stock names, e.g. 000001,600519.SH,紫光股份")
    analyze_source.add_argument("--from-screened", action="store_true", help="Analyze passed screening results")
    analyze.add_argument("--signals", default="all", help="Comma-separated signal groups or ids")
    analyze.add_argument("--trade-date", help="交易日 YYYYMMDD; defaults to latest trading day")
    analyze.add_argument("--rule", default=DEFAULT_RULE, help="Screening rule for --from-screened")
    analyze.add_argument("--lookback-days", type=int, default=180, help="Historical lookback trading days")
    analyze.add_argument("--category", help="Signal category for 'analyze signals'")
    analyze.add_argument("--json", action="store_true", help="Print full JSON output")
    analyze.add_argument("--noreport", action="store_true", help="Do not generate Markdown report")
    analyze.add_argument("--llm-review", action="store_true", help="Use LLM to review local signal results")
    analyze.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    analyze_dsa = sub.add_parser("analyze-dsa", help="Analyze stocks with external daily_stock_analysis")
    analyze_dsa.add_argument("--trade-date", help="交易日 YYYYMMDD; defaults to latest trading day")
    analyze_dsa.add_argument("--rule", default=None, help="Screening rule name")
    analyze_dsa.add_argument("--stocks", help="Comma-separated symbols or stock names for daily_stock_analysis --stocks")
    analyze_dsa.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    dsa = sub.add_parser("dsa", help="Run native DSA stock analysis")
    dsa_source = dsa.add_mutually_exclusive_group(required=True)
    dsa_source.add_argument("--stocks", help="Comma-separated symbols or stock names, e.g. 000001,600519.SH,紫光股份")
    dsa_source.add_argument("--from-screened", action="store_true", help="Analyze passed screening results")
    dsa.add_argument("--trade-date", help="交易日 YYYYMMDD; defaults to today")
    dsa.add_argument("--rule", default=None, help="Screening rule name for --from-screened")
    dsa.add_argument("--lookback-days", type=int, default=180, help="Historical lookback window")
    dsa.add_argument("--explain-rating", action="store_true", help="Show native DSA rating adjustment reasons")
    dsa.add_argument("--llm-timeout", type=int, default=20, help="LLM timeout seconds for native DSA review")
    dsa.add_argument("--no-llm", action="store_true", help="Skip native DSA LLM review and use local rules only")
    dsa.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    analyze_chan = sub.add_parser("analyze-chan", help="Review chan screening results with LLM")
    analyze_chan.add_argument("--trade-date", help="交易日 YYYYMMDD; defaults to latest trading day")
    analyze_chan.add_argument("--rule", default=None, help="Screening rule name filter")
    analyze_chan.add_argument("--chan-rule", default=DEFAULT_CHAN_RULE_NAME, help="chan_third_buy, chan_composite or chan_signals")
    analyze_chan.add_argument("--top", type=int, default=20, help="Maximum candidates to review")
    analyze_chan.add_argument("--stocks", help="Comma-separated symbols or stock names for temporary chan rule evaluation")
    analyze_chan.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    chan_kb = sub.add_parser("chan-kb", help="Search local Chan theory knowledge cards")
    chan_kb_sub = chan_kb.add_subparsers(dest="chan_kb_command")
    chan_kb_search = chan_kb_sub.add_parser("search", help="Search Chan theory RAG cards")
    chan_kb_search.add_argument("query", nargs=argparse.REMAINDER, help="Search query")

    discover = sub.add_parser("discover", help="Discover short-term A-share opportunities")
    discover.add_argument("--trade-date", help="交易日 YYYYMMDD; defaults to latest trading day")
    discover.add_argument("--signals", default=DEFAULT_DISCOVERY_SIGNALS, help="Analyze signal groups or ids")
    discover.add_argument("--limit", type=int, default=DEFAULT_DISCOVERY_LIMIT, help="Final stock count")
    discover.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATE_LIMIT, help="Local candidates sent to LLM")
    discover.add_argument("--lookback-days", type=int, default=180, help="Historical lookback trading days")
    discover.add_argument("--hot-sector-days", type=int, choices=[3, 4, 5], default=5, help="Hot sector lookback trading days")
    discover.add_argument("--no-hot-sector", action="store_true", help="Disable hot sector priority weighting")
    discover.add_argument("--json", action="store_true", help="Print full JSON output")
    discover.add_argument("--noreport", action="store_true", help="Do not generate Markdown report")
    discover.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    chat = sub.add_parser("chat", help="Chat with the configured LLM")
    chat.add_argument("--no-memory", action="store_true", help="Disable local chat memory for this message")
    chat.add_argument("--knowledge", help="Knowledge base name/id to force into this chat")
    chat.add_argument("message", nargs=argparse.REMAINDER, help="Message to send to the LLM")

    model = sub.add_parser("model", help="Manage LLM model profiles")
    model_sub = model.add_subparsers(dest="model_command")
    model_sub.add_parser("status", help="Show active model profiles")
    model_sub.add_parser("list", help="List configured model profiles")
    model_use = model_sub.add_parser("use", help="Switch default model profile")
    model_use.add_argument("profile", help="Model profile name, e.g. DEEPSEEK")
    model_use.add_argument("--target", choices=["main", "light", "both"], default="main")

    memory = sub.add_parser("memory", help="Manage local chat memory")
    memory_sub = memory.add_subparsers(dest="memory_command")
    memory_sub.add_parser("list", help="List active memories")
    memory_search = memory_sub.add_parser("search", help="Search active memories")
    memory_search.add_argument("query", nargs=argparse.REMAINDER, help="Search query")
    memory_forget = memory_sub.add_parser("forget", help="Archive one memory")
    memory_forget.add_argument("memory_id", help="Memory id to forget")
    memory_clear = memory_sub.add_parser("clear", help="Clear all local chat memory")
    memory_clear.add_argument("--yes", action="store_true", help="Confirm clearing all chat memory")

    knowledge = sub.add_parser("knowledge", help="Manage local RAG knowledge bases")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command")
    knowledge_sub.add_parser("list", help="List knowledge bases")
    knowledge_add = knowledge_sub.add_parser("add", help="Add or update a knowledge base")
    knowledge_add.add_argument("--name", required=True, help="Knowledge base name")
    knowledge_add.add_argument("--description", default="", help="Knowledge base description")
    knowledge_add.add_argument("--tags", default="", help="Comma-separated tags")
    knowledge_ingest = knowledge_sub.add_parser("ingest", help="Ingest file or directory into a knowledge base")
    knowledge_ingest.add_argument("--knowledge", required=True, help="Knowledge base name/id")
    knowledge_ingest.add_argument("--path", type=Path, required=True, help="File or directory to ingest")
    knowledge_ingest.add_argument("--tags", default="", help="Comma-separated tags")
    knowledge_search = knowledge_sub.add_parser("search", help="Search knowledge chunks")
    knowledge_search.add_argument("--query", required=True, help="Search query")
    knowledge_search.add_argument("--knowledge", help="Optional knowledge base name/id")
    knowledge_search.add_argument("--limit", type=int, default=6, help="Maximum results")
    knowledge_sub.add_parser("sync-stock-basic", help="Sync cached stock_basic into the stock-basic knowledge base")

    indicators = sub.add_parser("indicators", help="Calculate daily technical indicators")
    indicators.add_argument("--symbols", required=True, help="Comma-separated symbols or stock names, e.g. 000001.SZ,600519.SH,紫光股份")
    indicators.add_argument("--trade-date", help="交易日 YYYYMMDD; defaults to latest trading day")
    indicators.add_argument("--lookback-days", type=int, default=180, help="Historical calendar/trading lookback window")
    indicators.add_argument("--json", action="store_true", help="Print full JSON output")
    indicators.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    sub.add_parser("skills", help="List local SATS skills")

    watchlist = sub.add_parser("watchlist", help="Edit monitor watchlist")
    watchlist_sub = watchlist.add_subparsers(dest="watchlist_command")
    watchlist.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    watchlist_sub.add_parser("list", help="List watchlist").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    watchlist_add = watchlist_sub.add_parser("add", help="Add watchlist symbols")
    watchlist_add.add_argument("--symbols", required=True, help="Comma-separated symbols or stock names")
    watchlist_add.add_argument("--name", default="", help="Optional name used for all symbols")
    watchlist_add.add_argument("--note", default="")
    watchlist_add.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    watchlist_remove = watchlist_sub.add_parser("remove", help="Remove watchlist symbols")
    watchlist_remove.add_argument("--symbols", required=True, help="Comma-separated symbols or stock names")
    watchlist_remove.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    watchlist_delete = watchlist_sub.add_parser("select-delete", help="Interactively select watchlist symbols to delete")
    watchlist_delete.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    watchlist_import = watchlist_sub.add_parser("import-screened", help="Select passed screened stocks to add to watchlist")
    watchlist_import.add_argument("--trade-date", required=True, help="交易日 YYYYMMDD")
    watchlist_import.add_argument("--rule", default=DEFAULT_RULE, help="Screening rule name")
    watchlist_import.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    monitor = sub.add_parser("monitor", help="Manage realtime monitoring")
    monitor_sub = monitor.add_subparsers(dest="monitor_command")
    monitor_positions = monitor_sub.add_parser("positions", help="Manage monitor positions")
    monitor_positions_sub = monitor_positions.add_subparsers(dest="monitor_positions_command")
    monitor_positions_add = monitor_positions_sub.add_parser("add", help="Add or update a position")
    monitor_positions_add.add_argument("--symbol", required=True, help="A-share symbol or stock name")
    monitor_positions_add.add_argument("--name", default="")
    monitor_positions_add.add_argument("--buy-price", type=float, required=True)
    monitor_positions_add.add_argument("--quantity", type=float, required=True)
    monitor_positions_add.add_argument("--buy-date", default="")
    monitor_positions_add.add_argument("--note", default="")
    monitor_positions_add.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_positions_sub.add_parser("list", help="List positions").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_positions_remove = monitor_positions_sub.add_parser("remove", help="Remove a position")
    monitor_positions_remove.add_argument("--symbol", required=True, help="A-share symbol or stock name")
    monitor_positions_remove.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    monitor_watchlist = monitor_sub.add_parser("watchlist", help="Manage monitor watchlist")
    monitor_watchlist_sub = monitor_watchlist.add_subparsers(dest="monitor_watchlist_command")
    monitor_watchlist_add = monitor_watchlist_sub.add_parser("add", help="Add or update a watchlist symbol")
    monitor_watchlist_add.add_argument("--symbol", required=True, help="A-share symbol or stock name")
    monitor_watchlist_add.add_argument("--name", default="")
    monitor_watchlist_add.add_argument("--note", default="")
    monitor_watchlist_add.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_watchlist_sub.add_parser("list", help="List watchlist").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_watchlist_remove = monitor_watchlist_sub.add_parser("remove", help="Remove a watchlist symbol")
    monitor_watchlist_remove.add_argument("--symbol", required=True, help="A-share symbol or stock name")
    monitor_watchlist_remove.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    monitor_candidates = monitor_sub.add_parser("buy-candidates", help="Manage monitor buy candidates")
    monitor_candidates_sub = monitor_candidates.add_subparsers(dest="monitor_candidates_command")
    monitor_candidates_sub.add_parser("list", help="List buy candidates").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_candidates_remove = monitor_candidates_sub.add_parser("remove", help="Remove a buy candidate")
    monitor_candidates_remove.add_argument("--symbol", required=True, help="A-share symbol or stock name")
    monitor_candidates_remove.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    monitor_start = monitor_sub.add_parser("start", help="Start realtime monitor in background")
    monitor_start.add_argument("--rules", default="chan_signals")
    monitor_start.add_argument("--lists", default="positions,watchlist")
    monitor_start.add_argument("--interval", type=int, default=60)
    monitor_start.add_argument("--llm-review", action="store_true")
    _add_monitor_trade_args(monitor_start)
    monitor_start.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_run = monitor_sub.add_parser("run", help="Run realtime monitor in foreground")
    monitor_run.add_argument("--rules", default="chan_signals")
    monitor_run.add_argument("--lists", default="positions,watchlist")
    monitor_run.add_argument("--interval", type=int, default=60)
    monitor_run.add_argument("--llm-review", action="store_true")
    monitor_run.add_argument("--once", action="store_true", help="Run one polling cycle then exit")
    _add_monitor_trade_args(monitor_run)
    monitor_run.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_sub.add_parser("stop", help="Stop realtime monitor").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_sub.add_parser("status", help="Show realtime monitor status").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    monitor_display = sub.add_parser("monitor-display", help="Show realtime monitor dashboard")
    monitor_display_sub = monitor_display.add_subparsers(dest="monitor_display_command")
    monitor_display_start = monitor_display_sub.add_parser("start", help="Run dashboard in current terminal")
    monitor_display_start.add_argument("--refresh", type=int, default=3)
    monitor_display_start.add_argument("--new-terminal", action="store_true", help="Open dashboard in a new macOS Terminal window")
    monitor_display_start.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_display_run = monitor_display_sub.add_parser("run", help="Run dashboard in current terminal")
    monitor_display_run.add_argument("--refresh", type=int, default=3)
    monitor_display_run.add_argument("--plain", action="store_true", help="Print one plain snapshot instead of curses")
    monitor_display_run.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    monitor_display_sub.add_parser("stop", help="Stop dashboard process if known").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    schedule = sub.add_parser("schedule", help="Manage scheduled SATS tasks")
    schedule_sub = schedule.add_subparsers(dest="schedule_command")
    schedule_add = schedule_sub.add_parser("add", help="Add a scheduled task")
    schedule_add.add_argument("--name", required=True, help="Task name")
    schedule_add.add_argument("--type", choices=["cli", "chat"], required=True, help="Task type")
    schedule_add.add_argument("--text", required=True, help="SATS CLI argv text or chat message")
    schedule_freq = schedule_add.add_mutually_exclusive_group()
    schedule_freq.add_argument("--daily", action="store_true", help="Run every day")
    schedule_freq.add_argument("--weekly", action="store_true", help="Run every week on --days")
    schedule_add.add_argument("--days", default="", help="Weekly weekdays, e.g. mon,wed,fri")
    schedule_add.add_argument("--time", required=True, help="Run time HH:MM")
    schedule_add.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    schedule_sub.add_parser("list", help="List scheduled tasks").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    schedule_runs = schedule_sub.add_parser("runs", help="List recent scheduled task runs")
    schedule_runs.add_argument("--limit", type=int, default=20)
    schedule_runs.add_argument("--name", help="Filter by task name")
    schedule_runs.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    for schedule_command in ("enable", "disable", "remove", "run"):
        schedule_item = schedule_sub.add_parser(schedule_command, help=f"{schedule_command} scheduled task")
        schedule_item.add_argument("name")
        schedule_item.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    schedule_start = schedule_sub.add_parser("start", help="Start scheduler in background")
    schedule_start.add_argument("--interval", type=int, default=30)
    schedule_start.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    schedule_loop = schedule_sub.add_parser("run-loop", help="Run scheduler in foreground")
    schedule_loop.add_argument("--interval", type=int, default=30)
    schedule_loop.add_argument("--once", action="store_true")
    schedule_loop.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    schedule_sub.add_parser("stop", help="Stop scheduler process if known").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    schedule_sub.add_parser("status", help="Show scheduler status").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    qmt = sub.add_parser("qmt", help="Connect to MiniQMT/QMT broker")
    qmt.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    qmt_sub = qmt.add_subparsers(dest="qmt_command")
    qmt_bridge = qmt_sub.add_parser("bridge", help="Run Windows MiniQMT bridge")
    qmt_bridge_sub = qmt_bridge.add_subparsers(dest="qmt_bridge_command")
    qmt_bridge_run = qmt_bridge_sub.add_parser("run", help="Run bridge HTTP service")
    qmt_bridge_run.add_argument("--host", default="127.0.0.1")
    qmt_bridge_run.add_argument("--port", type=int, default=8765)
    qmt_bridge_run.add_argument("--qmt-path", default="")
    qmt_bridge_run.add_argument("--account-id", default="")
    qmt_bridge_run.add_argument("--account-type", default="STOCK")
    qmt_bridge_run.add_argument("--session-id", type=int, default=0)
    qmt_bridge_run.add_argument("--token", default="")
    qmt_bridge_run.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    qmt_sub.add_parser("status", help="Show QMT bridge status").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    qmt_sub.add_parser("asset", help="Query QMT account asset").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    qmt_sub.add_parser("positions", help="Query QMT positions").add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    qmt_sync = qmt_sub.add_parser("sync", help="Sync QMT data to SATS")
    qmt_sync_sub = qmt_sync.add_subparsers(dest="qmt_sync_command")
    qmt_sync_positions = qmt_sync_sub.add_parser("positions", help="Sync QMT positions to monitor positions")
    qmt_sync_positions.add_argument("--prune-missing", action="store_true")
    qmt_sync_positions.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    qmt_orders = qmt_sub.add_parser("orders", help="Query QMT orders")
    qmt_orders.add_argument("--open", action="store_true", help="Only open orders")
    qmt_orders.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    qmt_trades = qmt_sub.add_parser("trades", help="Query QMT trades")
    qmt_trades.add_argument("--limit", type=int, default=50)
    qmt_trades.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    for side in ("buy", "sell"):
        qmt_order = qmt_sub.add_parser(side, help=f"Send live QMT {side} order")
        qmt_order.add_argument("--symbol", required=True, help="A-share symbol or stock name")
        qmt_order.add_argument("--quantity", type=int, required=True)
        qmt_order.add_argument("--price-type", choices=["latest", "limit"], default="latest")
        qmt_order.add_argument("--price", type=float)
        qmt_order.add_argument("--dry-run", action="store_true", help="Validate and audit without sending order")
        qmt_order.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")
    qmt_cancel = qmt_sub.add_parser("cancel", help="Cancel QMT order")
    qmt_cancel.add_argument("--order-id", required=True)
    qmt_cancel.add_argument("--db", type=Path, help="DuckDB path; defaults to SATS_DB_PATH")

    serve = sub.add_parser("serve", help="Start FastAPI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        from sats.repl import run_repl

        return run_repl()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(overwrite=args.overwrite)
    if args.command == "screen":
        return cmd_screen(args)
    if args.command == "results":
        return cmd_results(args)
    if args.command == "result-rules":
        return cmd_result_rules(args)
    if args.command == "quote":
        return cmd_quote(args)
    if args.command == "analyze":
        return cmd_analyze(args)
    if args.command == "analyze-dsa":
        return cmd_analyze_dsa(args)
    if args.command == "dsa":
        return cmd_dsa(args)
    if args.command == "analyze-chan":
        return cmd_analyze_chan(args)
    if args.command == "chan-kb":
        return cmd_chan_kb(args)
    if args.command == "discover":
        return cmd_discover(args)
    if args.command == "chat":
        return cmd_chat(args)
    if args.command == "model":
        return cmd_model(args)
    if args.command == "memory":
        return cmd_memory(args)
    if args.command == "knowledge":
        return cmd_knowledge(args)
    if args.command == "indicators":
        return cmd_indicators(args)
    if args.command == "skills":
        return cmd_skills(args)
    if args.command == "watchlist":
        return cmd_watchlist(args)
    if args.command == "monitor":
        return cmd_monitor(args)
    if args.command == "monitor-display":
        return cmd_monitor_display(args)
    if args.command == "schedule":
        return cmd_schedule(args)
    if args.command == "qmt":
        return cmd_qmt(args)
    if args.command == "serve":
        return cmd_serve(args)
    parser.print_help()
    return 2


def cmd_init(*, overwrite: bool = False) -> int:
    settings = load_settings()
    created = init_env_file(settings.env_path, overwrite=overwrite)
    if created:
        print(f"Created {settings.env_path}")
        return 0
    print(f"Config already exists: {settings.env_path}")
    return 0


def _resolve_analysis_trade_date(
    trade_date: str | None,
    *,
    storage: DuckDBStorage,
    provider: AStockDataProvider | None = None,
) -> str:
    if trade_date:
        return str(trade_date)
    today = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")
    if provider is not None:
        try:
            dates = provider._recent_trade_dates(today, count=1)
        except Exception:
            dates = []
        if dates:
            return str(dates[-1])
    cached = _latest_cached_trade_date(storage, today)
    if cached:
        return cached
    return _previous_weekday(today)


def _canonical_chan_rule_name(value: str) -> str:
    rule_name = get_rule(value).name
    allowed = {"chan_third_buy", "chan_composite", "chan_signals"}
    if rule_name not in allowed:
        raise SystemExit("--chan-rule only supports chan_third_buy, chan_composite or chan_signals")
    return rule_name


def _latest_cached_trade_date(storage: DuckDBStorage, today: str) -> str | None:
    try:
        storage.initialize()
        with storage.connect() as con:
            row = con.execute(
                "SELECT MAX(trade_date) FROM stock_daily WHERE trade_date <= ?",
                [today],
            ).fetchone()
    except Exception:
        return None
    value = row[0] if row else None
    return str(value) if value else None


def _previous_weekday(today: str) -> str:
    current = datetime.strptime(today, "%Y%m%d")
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.strftime("%Y%m%d")


def cmd_screen(args: argparse.Namespace) -> int:
    settings = load_settings()
    db_path = args.db or settings.db_path
    storage = DuckDBStorage(db_path)
    progress = _progress_for_args(args)
    try:
        provider = AStockDataProvider(settings)
        rule_name = get_rule(args.rule).name
        with progress.step("AStock 股票数据") as step:
            inputs = provider.load_all_screening_inputs(args.trade_date, storage=storage, rule_name=rule_name)
            step.complete(message=f"{len(inputs)} 只")
        if not inputs:
            raise ValueError("No active A-share symbols returned by AStock provider")
        evaluate_and_store(inputs, rule_name=rule_name, storage=storage, progress=progress)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        progress.close()
    stock_names = {item.ts_code: str(item.stock_basic.get("name") or "") for item in inputs}
    rows = storage.list_screening_stocks(trade_date=args.trade_date, rule_name=rule_name, passed=True)
    for row in rows:
        if not row.get("name"):
            row["name"] = stock_names.get(str(row.get("ts_code") or ""), "")
    print(_format_stock_list(rows))
    if _should_prompt_watchlist_import(args):
        select_and_import_watchlist(storage, rows)
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    settings = load_settings()
    storage = DuckDBStorage(args.db or settings.db_path)
    rule_name = get_rule(args.rule).name if args.rule else None
    rows = storage.list_screening_stocks(
        trade_date=args.trade_date,
        rule_name=rule_name,
        passed=True if args.passed else None,
    )
    print(_format_result_stock_list(rows))
    return 0


def cmd_result_rules(args: argparse.Namespace) -> int:
    settings = load_settings()
    storage = DuckDBStorage(args.db or settings.db_path)
    print(_format_numbered_values(storage.list_screening_rule_names()))
    return 0


def cmd_quote(args: argparse.Namespace) -> int:
    settings = load_settings()
    storage = DuckDBStorage(args.db or settings.db_path)
    settings = _settings_with_db_path(settings, _storage_db_path(storage, args.db or settings.db_path))
    symbols = _parse_symbols_or_names(args.stocks, settings)
    provider = AStockDataProvider(settings)
    trade_date = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")
    start_date = (datetime.now(SHANGHAI_TZ) - timedelta(days=420)).strftime("%Y%m%d")
    progress = _progress_for_args(args)
    try:
        with progress.step("AStock 实时行情") as step:
            quotes = provider.load_realtime_quotes(symbols=symbols)
            step.complete(message=f"{len(quotes)} 条")
        if quotes.empty:
            raise SystemExit("未获取到实时行情")
        with progress.step("AStock 历史日线") as step:
            daily = provider.load_historical_daily_klines(
                symbols,
                start_date=start_date,
                end_date=trade_date,
                storage=storage,
            )
            step.complete(message=f"{len(daily)} 条")
        with progress.step("AStock 实时日线") as step:
            realtime_daily = provider.load_realtime_daily_quotes(symbols, trade_date=trade_date)
            step.complete(message=f"{len(realtime_daily)} 条")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        progress.close()
    quote_lookup = _records_by_symbol(quotes)
    stock_basic = pd.DataFrame()
    if any(not _coalesce_text(quote_lookup.get(symbol, {}).get("name")) for symbol in symbols):
        stock_basic = load_stock_basic_frame(settings)
    ma_lookup = _quote_moving_average_lookup(daily, realtime_daily)
    print(_format_quote_table(symbols, quotes, ma_lookup, stock_basic))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    if getattr(args, "analyze_action", None) == "signals":
        print(format_signal_definitions(category=getattr(args, "category", None)))
        return 0
    if not getattr(args, "stocks", None) and not getattr(args, "from_screened", False):
        raise SystemExit("analyze requires --stocks or --from-screened; use 'analyze signals' to list strategies")

    settings = load_settings()
    storage = DuckDBStorage(args.db or settings.db_path)
    settings = _settings_with_db_path(settings, _storage_db_path(storage, args.db or settings.db_path))
    provider = AStockDataProvider(settings)
    trade_date = _resolve_analysis_trade_date(getattr(args, "trade_date", None), storage=storage, provider=provider)
    if getattr(args, "from_screened", False):
        rule_name = get_rule(args.rule or DEFAULT_RULE).name
        rows = storage.list_screening_stocks(trade_date=trade_date, rule_name=rule_name, passed=True)
        symbols = [str(row.get("ts_code") or "").strip() for row in rows if str(row.get("ts_code") or "").strip()]
        source_label = rule_name
    else:
        symbols = _parse_symbols_or_names(args.stocks, settings)
        source_label = "stocks"
    if not symbols:
        print("无可分析股票")
        return 0
    progress = _progress_for_args(args)
    _announce_analyzing(progress, json_mode=bool(getattr(args, "json", False)))
    if not getattr(args, "trade_date", None) and not getattr(args, "json", False):
        print(f"trade_date: {trade_date}")
    try:
        try:
            with progress.step("AStock 股票数据") as step:
                inputs = provider.load_screening_inputs(
                    symbols,
                    trade_date,
                    storage=storage,
                    trade_days=max(80, int(args.lookback_days)),
                    rule_name="signal_composite",
                )
                step.complete(message=f"{len(inputs)} 只")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        signal_inputs = [_signal_input_from_screening_input(item) for item in inputs]
        result = analyze_signal_inputs(
            signal_inputs,
            selected_signals=args.signals,
            trade_date=trade_date,
            reports_dir=settings.project_root / "reports",
            report=not args.noreport,
            source_label=source_label,
            llm_review=args.llm_review,
            progress=progress,
        )
    finally:
        progress.close()
    if result.message:
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
            return 0
        print(result.message)
        return 0
    if result.llm_unavailable:
        print("提示: 大模型不可用，已使用本地规则评级。")
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(format_signal_analysis(result.results))
        if result.report_path:
            print(f"报告: {result.report_path}")
    return 0


def cmd_analyze_dsa(args: argparse.Namespace) -> int:
    settings = load_settings()
    storage = DuckDBStorage(args.db or settings.db_path)
    settings = _settings_with_db_path(settings, _storage_db_path(storage, args.db or settings.db_path))
    if getattr(args, "stocks", None) and getattr(args, "rule", None):
        raise SystemExit("analyze-dsa --rule only supports screened results, not --stocks")
    provider = AStockDataProvider(settings)
    trade_date = _resolve_analysis_trade_date(args.trade_date, storage=storage, provider=provider)
    progress = _progress_for_args(args)
    _announce_analyzing(progress)
    if not args.trade_date:
        print(f"trade_date: {trade_date}")
    try:
        with progress.step("daily_stock_analysis") as step:
            if getattr(args, "stocks", None):
                result = run_daily_stock_analysis_for_symbols(
                    _parse_symbols_or_names(args.stocks, settings),
                    trade_date=trade_date,
                    reports_dir=settings.project_root / "reports",
                    sats_env_path=settings.env_path,
                    source_label="stocks",
                )
            else:
                rule_name = get_rule(args.rule or DEFAULT_RULE).name
                result = run_screened_stock_analysis(
                    storage=storage,
                    trade_date=trade_date,
                    rule_name=rule_name,
                    reports_dir=settings.project_root / "reports",
                    sats_env_path=settings.env_path,
                )
            step.complete()
    finally:
        progress.close()
    if result.message:
        print(result.message)
        return 0
    print(_format_analysis_rankings(result.rankings))
    if result.archived_report is not None:
        print(f"报告: {result.archived_report}")
    return 0


def cmd_dsa(args: argparse.Namespace) -> int:
    settings = load_settings()
    storage = DuckDBStorage(args.db or settings.db_path)
    settings = _settings_with_db_path(settings, _storage_db_path(storage, args.db or settings.db_path))
    progress = _progress_for_args(args)
    if getattr(args, "from_screened", False):
        if not args.trade_date:
            raise SystemExit("dsa --from-screened requires --trade-date")
        rule_name = get_rule(args.rule or DEFAULT_RULE).name
        _announce_analyzing(progress)
        with progress.step("读取筛选结果") as step:
            rows = storage.list_screening_stocks(
                trade_date=args.trade_date,
                rule_name=rule_name,
                passed=True,
            )
            step.complete(message=f"{len(rows)} 只")
        symbols = [str(row.get("ts_code") or "").strip() for row in rows if str(row.get("ts_code") or "").strip()]
        if not symbols:
            progress.close()
            print("无通过筛选股票")
            return 0
        try:
            result = run_dsa_analysis(
                symbols,
                trade_date=args.trade_date,
                reports_dir=settings.project_root / "reports",
                settings=settings,
                storage=storage,
                lookback_days=args.lookback_days,
                source_label=rule_name,
                llm_timeout_seconds=getattr(args, "llm_timeout", 20),
                llm_enabled=not getattr(args, "no_llm", False),
                progress=progress,
            )
        finally:
            progress.close()
        if result.message:
            print(result.message)
            return 0
        if getattr(result, "llm_unavailable", False):
            print("提示: 大模型不可用，已使用本地规则评级。")
        print(_format_analysis_rankings(result.rankings, explain_rating=getattr(args, "explain_rating", False)))
        if result.archived_report is not None:
            print(f"报告: {result.archived_report}")
        return 0
    if getattr(args, "rule", None):
        raise SystemExit("dsa --rule only supports --from-screened")
    symbols = _parse_symbols_or_names(args.stocks, settings)
    _announce_analyzing(progress)
    try:
        result = run_dsa_analysis(
            symbols,
            trade_date=args.trade_date,
            reports_dir=settings.project_root / "reports",
            settings=settings,
            storage=storage,
            lookback_days=args.lookback_days,
            source_label="stocks",
            llm_timeout_seconds=getattr(args, "llm_timeout", 20),
            llm_enabled=not getattr(args, "no_llm", False),
            progress=progress,
        )
    finally:
        progress.close()
    if result.message:
        print(result.message)
        return 0
    if getattr(result, "llm_unavailable", False):
        print("提示: 大模型不可用，已使用本地规则评级。")
    print(_format_analysis_rankings(result.rankings, explain_rating=getattr(args, "explain_rating", False)))
    if result.archived_report is not None:
        print(f"报告: {result.archived_report}")
    return 0


def cmd_analyze_chan(args: argparse.Namespace) -> int:
    settings = load_settings()
    storage = DuckDBStorage(args.db or settings.db_path)
    settings = _settings_with_db_path(settings, _storage_db_path(storage, args.db or settings.db_path))
    screening_rule_name = get_rule(args.rule).name if getattr(args, "rule", None) else None
    chan_rule_name = _canonical_chan_rule_name(getattr(args, "chan_rule", DEFAULT_CHAN_RULE_NAME))
    if getattr(args, "stocks", None) and screening_rule_name:
        raise SystemExit("analyze-chan --rule only supports saved screening results; use --chan-rule with --stocks")
    provider = AStockDataProvider(settings)
    trade_date = _resolve_analysis_trade_date(getattr(args, "trade_date", None), storage=storage, provider=provider)
    progress = _progress_for_args(args)
    _announce_analyzing(progress)
    if not getattr(args, "trade_date", None) and not args.json:
        print(f"trade_date: {trade_date}")
    if getattr(args, "stocks", None):
        symbols = _parse_symbols_or_names(args.stocks, settings)
        try:
            with progress.step("股票上下文") as step:
                stock_contexts = ensure_stock_analysis_data(
                    symbols,
                    trade_date,
                    settings=settings,
                    storage=storage,
                    periods=("15m", "30m"),
                )
                step.complete()
            with progress.step("AStock 股票数据") as step:
                inputs = provider.load_screening_inputs(symbols, trade_date, storage=storage, rule_name=chan_rule_name)
                step.complete(message=f"{len(inputs)} 只")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        screening_results = evaluate_inputs(inputs, rule_name=chan_rule_name, progress=progress)
        names = {
            item.ts_code: str(item.stock_basic.get("name") or "")
            for item in inputs
        }
        with progress.step(f"{getattr(settings, 'openai_model', 'LLM')} 复核") as step:
            result = run_chan_llm_review(
                storage=storage,
                trade_date=trade_date,
                reports_dir=settings.project_root / "reports",
                top=args.top,
                screening_rule_name=chan_rule_name,
                chan_rule_name=chan_rule_name,
                screening_results=screening_results,
                names=names,
                stock_contexts=stock_contexts,
            )
            step.complete()
    else:
        with progress.step(f"{getattr(settings, 'openai_model', 'LLM')} 复核") as step:
            result = run_chan_llm_review(
                storage=storage,
                trade_date=trade_date,
                reports_dir=settings.project_root / "reports",
                top=args.top,
                screening_rule_name=screening_rule_name,
                chan_rule_name=chan_rule_name,
            )
            step.complete()
    progress.close()
    if result.message:
        print(result.message)
        return 0
    print(_format_chan_reviews(result.reviews))
    if result.report_path is not None:
        print(f"报告: {result.report_path}")
    return 0


def cmd_chan_kb(args: argparse.Namespace) -> int:
    if args.chan_kb_command != "search":
        raise SystemExit("chan-kb requires subcommand: search")
    query = " ".join(args.query).strip()
    if not query:
        raise SystemExit("chan-kb search query is required")
    rows = search_chan_knowledge(query)
    if not rows:
        print("无结果")
        return 0
    lines = []
    for index, row in enumerate(rows, start=1):
        pages = ",".join(str(page) for page in row.get("source_pages", []))
        lines.append(f"{index}. {row.get('rule_id')} {row.get('label')} p{pages} {row.get('definition')}")
    print("\n".join(lines))
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    settings = load_settings()
    storage = DuckDBStorage(args.db or settings.db_path)
    provider = AStockDataProvider(settings)
    trade_date = _resolve_analysis_trade_date(getattr(args, "trade_date", None), storage=storage, provider=provider)
    progress = _progress_for_args(args)
    _announce_analyzing(progress, json_mode=bool(args.json))
    if not getattr(args, "trade_date", None) and not args.json:
        print(f"trade_date: {trade_date}")
    try:
        kwargs = {
            "settings": settings,
            "storage": storage,
            "provider": provider,
            "trade_date": trade_date,
            "signals": args.signals,
            "limit": args.limit,
            "candidate_limit": args.candidate_limit,
            "lookback_days": args.lookback_days,
            "hot_sector_enabled": not args.no_hot_sector,
            "hot_sector_days": args.hot_sector_days,
            "reports_dir": settings.project_root / "reports",
            "report": not args.noreport,
        }
        if getattr(progress, "enabled", False):
            kwargs["progress"] = progress
        result = run_opportunity_discovery(**kwargs)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        progress.close()
    if result.message:
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
            return 0
        print(result.message)
        return 0
    if result.llm_unavailable:
        print("提示: 大模型不可用，已使用本地信号排序。")
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(format_opportunity_discovery(result))
        if result.report_path:
            print(f"报告: {result.report_path}")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    message = " ".join(args.message).strip()
    if not message:
        raise SystemExit("chat message is required")
    settings = load_settings()
    progress = _progress_for_args(args)
    try:
        kwargs = {"settings": settings, "memory_enabled": not args.no_memory}
        if getattr(args, "knowledge", None):
            kwargs["knowledge"] = args.knowledge
        if getattr(progress, "enabled", False):
            kwargs["progress"] = progress
        result = run_chat_once(message, **kwargs)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        progress.close()
    print(format_chat_result(result))
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    if args.model_command is None:
        raise SystemExit("model requires subcommand: status, list or use")
    if args.model_command == "status":
        load_settings()
        print(_format_model_status())
        return 0
    if args.model_command == "list":
        load_settings()
        print(_format_model_profiles())
        return 0
    if args.model_command == "use":
        update_default_model_selection(_default_env_path(), args.profile, target=args.target)
        print(f"已切换 {args.target}: {str(args.profile).strip().upper()}")
        return 0
    raise SystemExit(f"unknown model command: {args.model_command}")


def _default_env_path() -> Path:
    cwd = Path.cwd().resolve()
    if (cwd / ".env").exists():
        return cwd / ".env"
    return Path(__file__).resolve().parents[1] / ".env"


def cmd_memory(args: argparse.Namespace) -> int:
    if args.memory_command is None:
        raise SystemExit("memory requires subcommand: list, search, forget or clear")
    settings = load_settings()
    store = ChatMemoryStore(settings.db_path)
    if args.memory_command == "list":
        print(format_memory_list(store.list_memories()))
        return 0
    if args.memory_command == "search":
        query = " ".join(args.query).strip()
        if not query:
            raise SystemExit("memory search query is required")
        print(format_memory_list(store.search_memories(query, limit=20)))
        return 0
    if args.memory_command == "forget":
        if store.archive_memory(args.memory_id):
            print(f"已删除记忆 {args.memory_id}")
        else:
            print(f"未找到记忆 {args.memory_id}")
        return 0
    if args.memory_command == "clear":
        if not args.yes:
            raise SystemExit("memory clear requires --yes")
        count = store.clear_all_memory()
        print(f"已清空 {count} 条记忆" if count else "无记忆")
        return 0
    raise SystemExit("unknown memory command")


def cmd_knowledge(args: argparse.Namespace) -> int:
    if args.knowledge_command is None:
        raise SystemExit("knowledge requires subcommand: list, add, ingest or search")
    settings = load_settings()
    store = KnowledgeStore(settings.db_path)
    if args.knowledge_command == "list":
        print(format_knowledge_list(store.list_knowledge_bases()))
        return 0
    if args.knowledge_command == "add":
        kb = store.add_knowledge_base(
            name=args.name,
            description=args.description,
            tags=_parse_tags(args.tags),
        )
        print(f"已保存知识库 {kb.name} ({kb.collection_name})")
        return 0
    if args.knowledge_command == "ingest":
        count = store.ingest_path(
            args.knowledge,
            args.path,
            tags=_parse_tags(args.tags),
            project_root=Path(getattr(settings, "project_root", ".")).resolve(),
        )
        print(f"已入库 {count} 个知识块")
        return 0
    if args.knowledge_command == "search":
        rows = store.search(args.query, knowledge=args.knowledge, limit=args.limit)
        print(format_search_results(rows))
        return 0
    if args.knowledge_command == "sync-stock-basic":
        count = store.sync_stock_basic(settings=settings)
        print(f"已同步 {count} 条 stock_basic 股票知识")
        return 0
    raise SystemExit("unknown knowledge command")


def cmd_indicators(args: argparse.Namespace) -> int:
    settings = load_settings()
    storage = DuckDBStorage(args.db or settings.db_path)
    settings = _settings_with_db_path(settings, _storage_db_path(storage, args.db or settings.db_path))
    symbols = _parse_symbols_or_names(args.symbols, settings)
    provider = AStockDataProvider(settings)
    trade_date = _resolve_analysis_trade_date(getattr(args, "trade_date", None), storage=storage, provider=provider)
    progress = _progress_for_args(args)
    try:
        try:
            with progress.step("AStock 指标数据") as step:
                inputs = provider.load_indicator_inputs(
                    symbols,
                    trade_date,
                    lookback_days=args.lookback_days,
                    storage=storage,
                )
                step.complete(message=f"{len(inputs)} 只")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        calculator = IndicatorCalculator()
        results = []
        with progress.step("指标计算", total=len(inputs)) as step:
            for index, item in enumerate(inputs, start=1):
                results.append(calculator.calculate(item))
                step.update(index)
    finally:
        progress.close()
    if args.json:
        print(json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2, default=str))
    else:
        print(format_indicator_results(results))
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    settings = load_settings()
    skills = load_skills(default_skills_dir(settings.project_root))
    print(format_skill_list(skills))
    return 0


def cmd_watchlist(args: argparse.Namespace) -> int:
    settings = load_settings()
    storage = DuckDBStorage(getattr(args, "db", None) or settings.db_path)
    settings = _settings_with_db_path(settings, _storage_db_path(storage, getattr(args, "db", None) or settings.db_path))
    command = getattr(args, "watchlist_command", None)
    if command is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return run_watchlist_editor(storage)
        print(format_watchlist(storage.list_monitor_watchlist()))
        return 0
    if command == "list":
        print(format_watchlist(storage.list_monitor_watchlist()))
        return 0
    if command == "add":
        count = upsert_watchlist_symbols(storage, _parse_symbols_or_names(args.symbols, settings), name=args.name, note=args.note)
        print(f"已加入关注列表 {count} 只股票" if count else "未加入股票")
        return 0
    if command == "remove":
        count = delete_watchlist_symbols(storage, _parse_symbols_or_names(args.symbols, settings))
        print(f"已删除 {count} 只股票" if count else "未找到关注股票")
        return 0
    if command == "select-delete":
        select_and_delete_watchlist(storage)
        return 0
    if command == "import-screened":
        rule_name = get_rule(args.rule).name if args.rule else None
        import_screened_to_watchlist(storage, trade_date=args.trade_date, rule_name=rule_name)
        return 0
    raise SystemExit("watchlist requires list, add, remove, select-delete or import-screened")


def cmd_monitor(args: argparse.Namespace) -> int:
    if args.monitor_command is None:
        raise SystemExit("monitor requires subcommand")
    settings = load_settings()
    storage = DuckDBStorage(getattr(args, "db", None) or settings.db_path)
    settings = _settings_with_db_path(settings, _storage_db_path(storage, getattr(args, "db", None) or settings.db_path))
    command = args.monitor_command
    if command == "positions":
        return _cmd_monitor_positions(args, storage, settings)
    if command == "watchlist":
        return _cmd_monitor_watchlist(args, storage, settings)
    if command == "buy-candidates":
        return _cmd_monitor_buy_candidates(args, storage, settings)
    if command == "start":
        return _cmd_monitor_start(args, settings, storage)
    if command == "run":
        return _cmd_monitor_run(args, settings, storage)
    if command == "stop":
        return _cmd_monitor_stop(storage)
    if command == "status":
        print(_format_monitor_runtime(storage.get_monitor_runtime("monitor")))
        return 0
    raise SystemExit(f"unknown monitor command: {command}")


def _cmd_monitor_positions(args: argparse.Namespace, storage: DuckDBStorage, settings) -> int:
    command = args.monitor_positions_command
    if command == "add":
        storage.upsert_monitor_position(
            ts_code=_parse_symbol_or_name(args.symbol, settings),
            name=args.name,
            quantity=args.quantity,
            buy_price=args.buy_price,
            buy_date=args.buy_date,
            note=args.note,
        )
        print("已保存持仓")
        return 0
    if command == "list":
        print(_format_monitor_table(storage.list_monitor_positions()))
        return 0
    if command == "remove":
        print("已删除持仓" if storage.delete_monitor_position(_parse_symbol_or_name(args.symbol, settings)) else "未找到持仓")
        return 0
    raise SystemExit("monitor positions requires add, list or remove")


def _cmd_monitor_watchlist(args: argparse.Namespace, storage: DuckDBStorage, settings) -> int:
    command = args.monitor_watchlist_command
    if command == "add":
        storage.upsert_monitor_watchlist(
            ts_code=_parse_symbol_or_name(args.symbol, settings),
            name=args.name,
            note=args.note,
        )
        print("已保存关注股票")
        return 0
    if command == "list":
        print(_format_monitor_table(storage.list_monitor_watchlist()))
        return 0
    if command == "remove":
        print("已删除关注股票" if storage.delete_monitor_watchlist(_parse_symbol_or_name(args.symbol, settings)) else "未找到关注股票")
        return 0
    raise SystemExit("monitor watchlist requires add, list or remove")


def _cmd_monitor_buy_candidates(args: argparse.Namespace, storage: DuckDBStorage, settings) -> int:
    command = args.monitor_candidates_command
    if command == "list":
        print(_format_monitor_table(storage.list_monitor_buy_candidates()))
        return 0
    if command == "remove":
        print("已删除待买入股票" if storage.delete_monitor_buy_candidate(_parse_symbol_or_name(args.symbol, settings)) else "未找到待买入股票")
        return 0
    raise SystemExit("monitor buy-candidates requires list or remove")


def _cmd_monitor_start(args: argparse.Namespace, settings, storage: DuckDBStorage) -> int:
    rule_names = _parse_csv(args.rules)
    lists = _parse_csv(args.lists)
    _validate_monitor_rules(rule_names)
    cmd = [
        sys.executable,
        "-m",
        "sats",
        "monitor",
        "run",
        "--rules",
        ",".join(rule_names),
        "--lists",
        ",".join(lists),
        "--interval",
        str(args.interval),
        "--db",
        str(storage.db_path),
    ]
    if args.llm_review:
        cmd.append("--llm-review")
    if getattr(args, "broker", "noop") != "noop":
        cmd.extend(["--broker", args.broker])
    if getattr(args, "auto_trade", ""):
        cmd.extend(["--auto-trade", args.auto_trade])
    cmd.extend(
        [
            "--max-order-value",
            str(args.max_order_value),
            "--max-position-pct",
            str(args.max_position_pct),
            "--sell-ratio",
            str(args.sell_ratio),
        ]
    )
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    storage.upsert_monitor_runtime(
        service_name="monitor",
        status="running",
        pid=process.pid,
        params={
            "rules": rule_names,
            "lists": lists,
            "interval": args.interval,
            "llm_review": args.llm_review,
            "broker": args.broker,
            "auto_trade": args.auto_trade,
        },
        heartbeat=True,
    )
    print(f"实时监控已启动 PID {process.pid}")
    return 0


def _cmd_monitor_run(args: argparse.Namespace, settings, storage: DuckDBStorage) -> int:
    rule_names = tuple(_parse_csv(args.rules))
    lists = tuple(_parse_csv(args.lists))
    config = MonitorConfig(
        rules=rule_names,
        lists=lists,
        interval_seconds=args.interval,
        llm_review=args.llm_review,
        max_cycles=1 if args.once else None,
        broker=args.broker,
        auto_trade=tuple(_parse_optional_csv(args.auto_trade)),
        max_order_value=args.max_order_value,
        max_position_pct=args.max_position_pct,
        sell_ratio=args.sell_ratio,
    )
    progress = _progress_for_args(args)
    trading_provider = _build_monitor_trading_provider(args, settings, storage)
    service = MonitorService(settings=settings, storage=storage, progress=progress, trading_provider=trading_provider)
    try:
        service.run_forever(config)
    except KeyboardInterrupt:
        print("实时监控已中断")
    finally:
        progress.close()
        if args.once:
            storage.upsert_monitor_runtime(service_name="monitor", status="stopped", pid=None, params={"rules": list(rule_names), "lists": list(lists)})
    return 0


def _build_monitor_trading_provider(args: argparse.Namespace, settings, storage: DuckDBStorage):
    auto_trade = set(_parse_optional_csv(getattr(args, "auto_trade", "")))
    broker = str(getattr(args, "broker", "noop") or "noop").lower()
    if broker != "qmt" or not auto_trade:
        return None
    invalid = auto_trade - {"buy", "sell"}
    if invalid:
        raise SystemExit(f"unsupported auto-trade action: {', '.join(sorted(invalid))}")
    client = broker_from_settings(settings)
    return QmtTradingProvider(
        client=client,
        storage=storage,
        config=AutoTradeConfig(
            enabled_actions=auto_trade,
            max_order_value=float(getattr(args, "max_order_value", 20000.0) or 0.0),
            max_position_pct=float(getattr(args, "max_position_pct", 0.2) or 0.0),
            sell_ratio=float(getattr(args, "sell_ratio", 1.0) or 1.0),
        ),
    )


def _cmd_monitor_stop(storage: DuckDBStorage) -> int:
    runtime = storage.get_monitor_runtime("monitor")
    pid = runtime.get("pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise SystemExit(f"无法停止监控进程 {pid}: {exc}") from exc
    storage.upsert_monitor_runtime(service_name="monitor", status="stopped", pid=None, params=runtime.get("params", {}))
    print("实时监控已停止")
    return 0


def cmd_qmt(args: argparse.Namespace) -> int:
    if args.qmt_command is None:
        raise SystemExit("qmt requires subcommand")
    settings = load_settings()
    storage = DuckDBStorage(getattr(args, "db", None) or settings.db_path)
    settings = _settings_with_db_path(settings, _storage_db_path(storage, getattr(args, "db", None) or settings.db_path))
    command = args.qmt_command
    if command == "bridge":
        if args.qmt_bridge_command != "run":
            raise SystemExit("qmt bridge requires run")
        config = QmtBridgeConfig(
            qmt_path=args.qmt_path or settings.qmt_userdata_path,
            account_id=args.account_id or settings.qmt_account_id,
            account_type=args.account_type or settings.qmt_account_type,
            session_id=int(args.session_id or settings.qmt_session_id or 0),
            token=args.token or settings.qmt_token,
        )
        if not config.qmt_path or not config.account_id:
            raise SystemExit("qmt bridge run requires --qmt-path and --account-id, or SATS_QMT_USERDATA_PATH/SATS_QMT_ACCOUNT_ID")
        run_bridge(host=args.host, port=args.port, config=config)
        return 0

    if command == "status":
        client = broker_from_settings(settings)
        print(json.dumps(client.status(), ensure_ascii=False, indent=2, default=str))
        return 0
    if command == "asset":
        client = broker_from_settings(settings)
        asset = client.asset()
        storage.upsert_broker_account({"provider": client.provider, **asset.to_dict()})
        print(_format_qmt_asset(asset.to_dict()))
        return 0
    if command == "positions":
        client = broker_from_settings(settings)
        positions = client.positions()
        storage.upsert_broker_positions([item.to_dict() for item in positions], provider=client.provider, account_id=client.account_id)
        print(_format_qmt_positions([item.to_dict() for item in positions]))
        return 0
    if command == "sync":
        client = broker_from_settings(settings)
        if args.qmt_sync_command != "positions":
            raise SystemExit("qmt sync requires positions")
        positions = client.positions()
        storage.upsert_broker_positions([item.to_dict() for item in positions], provider=client.provider, account_id=client.account_id)
        count = sync_positions_to_monitor(storage, positions, provider=client.provider, account_id=client.account_id, prune_missing=args.prune_missing)
        print(f"已同步 QMT 持仓 {count} 只到 monitor positions")
        return 0
    if command == "orders":
        client = broker_from_settings(settings)
        orders = client.orders(open_only=args.open)
        for order in orders:
            storage.insert_broker_order(
                {
                    "sats_order_id": order.sats_order_id or order.order_id,
                    "provider": client.provider,
                    "account_id": client.account_id,
                    "broker_order_id": order.order_id,
                    "ts_code": order.ts_code,
                    "side": order.side,
                    "quantity": order.quantity,
                    "price": order.price,
                    "price_type": order.price_type,
                    "status": order.status,
                    "message": order.message,
                    "response": order.raw,
                }
            )
        print(_format_qmt_orders([item.to_dict() for item in orders]))
        return 0
    if command == "trades":
        client = broker_from_settings(settings)
        trades = client.trades(limit=args.limit)
        for trade in trades:
            storage.insert_broker_trade({"provider": client.provider, "account_id": client.account_id, **trade.to_dict(), "broker_order_id": trade.order_id})
        print(_format_qmt_trades([item.to_dict() for item in trades]))
        return 0
    if command in {"buy", "sell"}:
        client = _dry_run_broker(settings) if getattr(args, "dry_run", False) else broker_from_settings(settings)
        return _cmd_qmt_order(args, storage, client, settings=settings, side=command)
    if command == "cancel":
        client = broker_from_settings(settings)
        result = client.cancel_order(args.order_id)
        storage.insert_broker_order_event(
            {
                "sats_order_id": result.sats_order_id,
                "broker_order_id": result.broker_order_id,
                "provider": client.provider,
                "account_id": client.account_id,
                "event_type": "cancel",
                "status": result.status,
                "message": result.message,
                "payload": result.to_dict(),
            }
        )
        print(f"撤单请求: {result.status} {result.broker_order_id} {result.message}".strip())
        return 0
    raise SystemExit(f"unknown qmt command: {command}")


def cmd_monitor_display(args: argparse.Namespace) -> int:
    if args.monitor_display_command is None:
        raise SystemExit("monitor-display requires subcommand")
    settings = load_settings()
    storage = DuckDBStorage(getattr(args, "db", None) or settings.db_path)
    command = args.monitor_display_command
    if command == "start":
        return _cmd_monitor_display_start(args, settings, storage)
    if command == "run":
        return _cmd_monitor_display_run(args, settings, storage)
    if command == "stop":
        return _cmd_monitor_display_stop(storage)
    raise SystemExit(f"unknown monitor-display command: {command}")


def _cmd_monitor_display_start(args: argparse.Namespace, settings, storage: DuckDBStorage) -> int:
    if not getattr(args, "new_terminal", False):
        setattr(args, "plain", False)
        return _cmd_monitor_display_run(args, settings, storage)
    cmd = [
        sys.executable,
        "-m",
        "sats",
        "monitor-display",
        "run",
        "--refresh",
        str(args.refresh),
        "--db",
        str(storage.db_path),
    ]
    script = " ".join(shlex.quote(part) for part in cmd)
    subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"'])
    storage.upsert_monitor_runtime(
        service_name="monitor-display",
        status="starting",
        pid=None,
        params={"refresh": args.refresh},
        heartbeat=True,
    )
    print("信息显示系统已启动")
    return 0


def _cmd_monitor_display_run(args: argparse.Namespace, settings, storage: DuckDBStorage) -> int:
    storage.upsert_monitor_runtime(
        service_name="monitor-display",
        status="running",
        pid=os.getpid(),
        params={"refresh": args.refresh},
        heartbeat=True,
    )
    display = MonitorDisplay(settings=settings, storage=storage, refresh_seconds=args.refresh)
    try:
        if args.plain:
            print(format_monitor_dashboard(display.snapshot()))
        else:
            display.run()
    finally:
        storage.upsert_monitor_runtime(service_name="monitor-display", status="stopped", pid=None, params={"refresh": args.refresh})
    return 0


def _cmd_monitor_display_stop(storage: DuckDBStorage) -> int:
    runtime = storage.get_monitor_runtime("monitor-display")
    pid = runtime.get("pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise SystemExit(f"无法停止显示进程 {pid}: {exc}") from exc
    storage.upsert_monitor_runtime(service_name="monitor-display", status="stopped", pid=None, params=runtime.get("params", {}))
    print("信息显示系统已停止")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    if args.schedule_command is None:
        raise SystemExit("schedule requires subcommand")
    settings = load_settings()
    storage = DuckDBStorage(getattr(args, "db", None) or settings.db_path)
    command = args.schedule_command
    if command == "add":
        return _cmd_schedule_add(args, storage)
    if command == "list":
        print(_format_scheduled_tasks(storage.list_scheduled_tasks()))
        return 0
    if command == "runs":
        print(_format_scheduled_runs(storage.list_scheduled_task_runs(limit=args.limit, task_name=args.name)))
        return 0
    if command == "enable":
        return _cmd_schedule_enable(args, storage, enabled=True)
    if command == "disable":
        return _cmd_schedule_enable(args, storage, enabled=False)
    if command == "remove":
        print("已删除定时任务" if storage.delete_scheduled_task(args.name) else "未找到定时任务")
        return 0
    if command == "run":
        return _cmd_schedule_run(args, settings, storage)
    if command == "start":
        return _cmd_schedule_start(args, storage)
    if command == "run-loop":
        return _cmd_schedule_run_loop(args, settings, storage)
    if command == "stop":
        return _cmd_schedule_stop(storage)
    if command == "status":
        print(_format_monitor_runtime(storage.get_monitor_runtime(SCHEDULER_SERVICE_NAME)))
        return 0
    raise SystemExit(f"unknown schedule command: {command}")


def _cmd_schedule_add(args: argparse.Namespace, storage: DuckDBStorage) -> int:
    schedule_kind = "weekly" if args.weekly else "daily"
    try:
        time_of_day = validate_time_of_day(args.time)
        days = parse_schedule_days(args.days) if schedule_kind == "weekly" else ()
        if schedule_kind == "weekly" and not days:
            raise ValueError("schedule add --weekly requires --days")
        next_run_at = compute_next_run(
            datetime.now(SHANGHAI_TZ),
            schedule_kind=schedule_kind,
            days=days,
            time_of_day=time_of_day,
        )
        storage.insert_scheduled_task(
            {
                "name": args.name,
                "task_type": args.type,
                "text": args.text,
                "schedule_kind": schedule_kind,
                "days": days,
                "time_of_day": time_of_day,
                "timezone": "Asia/Shanghai",
                "enabled": True,
                "next_run_at": next_run_at,
            }
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"已添加定时任务 {args.name}，下次执行: {next_run_at}")
    return 0


def _cmd_schedule_enable(args: argparse.Namespace, storage: DuckDBStorage, *, enabled: bool) -> int:
    ok = storage.set_scheduled_task_enabled(args.name, enabled)
    if not ok:
        print("未找到定时任务")
        return 0
    print("已启用定时任务" if enabled else "已停用定时任务")
    return 0


def _cmd_schedule_run(args: argparse.Namespace, settings, storage: DuckDBStorage) -> int:
    service = SchedulerService(storage=storage, runner=ScheduledTaskRunner(settings=settings))
    try:
        run = service.run_now(args.name)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(_format_scheduled_runs([run]))
    return 0


def _cmd_schedule_start(args: argparse.Namespace, storage: DuckDBStorage) -> int:
    cmd = [
        sys.executable,
        "-m",
        "sats",
        "schedule",
        "run-loop",
        "--interval",
        str(args.interval),
        "--db",
        str(storage.db_path),
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    storage.upsert_monitor_runtime(
        service_name=SCHEDULER_SERVICE_NAME,
        status="running",
        pid=process.pid,
        params={"interval": args.interval},
        heartbeat=True,
    )
    print(f"定时调度已启动 PID {process.pid}")
    return 0


def _cmd_schedule_run_loop(args: argparse.Namespace, settings, storage: DuckDBStorage) -> int:
    config = SchedulerConfig(interval_seconds=args.interval, max_cycles=1 if args.once else None)
    service = SchedulerService(storage=storage, runner=ScheduledTaskRunner(settings=settings))
    interrupted = False
    try:
        service.run_forever(config)
    except KeyboardInterrupt:
        interrupted = True
        print("定时调度已中断")
    finally:
        if args.once or interrupted:
            storage.upsert_monitor_runtime(
                service_name=SCHEDULER_SERVICE_NAME,
                status="stopped",
                pid=None,
                params={"interval": args.interval},
            )
    return 0


def _cmd_schedule_stop(storage: DuckDBStorage) -> int:
    runtime = storage.get_monitor_runtime(SCHEDULER_SERVICE_NAME)
    pid = runtime.get("pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise SystemExit(f"无法停止定时调度进程 {pid}: {exc}") from exc
    storage.upsert_monitor_runtime(
        service_name=SCHEDULER_SERVICE_NAME,
        status="stopped",
        pid=None,
        params=runtime.get("params", {}),
    )
    print("定时调度已停止")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("uvicorn is not installed; install requirements.txt") from exc
    uvicorn.run("sats.api.app:create_app", factory=True, host=args.host, port=args.port)
    return 0


def _format_stock_list(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "无结果"
    lines = []
    for index, row in enumerate(rows, start=1):
        ts_code = str(row.get("ts_code") or "").strip()
        name = str(row.get("name") or "").strip()
        labels = _matched_labels(row)
        suffix = " ".join(item for item in (name, ",".join(labels)) if item)
        lines.append(f"{index}. {ts_code}{f' {suffix}' if suffix else ''}")
    return "\n".join(lines)


def _signal_input_from_screening_input(item) -> SignalInput:
    return SignalInput(
        ts_code=item.ts_code,
        trade_date=item.trade_date,
        daily=item.daily,
        stock_basic=item.stock_basic,
        metadata=item.metadata,
    )


def _format_result_stock_list(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "无结果"
    display_rows = []
    for index, row in enumerate(rows, start=1):
        display_rows.append(
            {
                "index": str(index),
                "ts_code": str(row.get("ts_code") or "").strip(),
                "name": str(row.get("name") or "").strip(),
                "rule_name": str(row.get("rule_name") or "").strip(),
                "labels": ",".join(_matched_labels(row)),
            }
        )
    index_width = max(len(item["index"]) for item in display_rows)
    code_width = max(get_cwidth(item["ts_code"]) for item in display_rows)
    name_width = max(get_cwidth(item["name"]) for item in display_rows)
    rule_width = max(get_cwidth(item["rule_name"]) for item in display_rows)
    show_name = any(item["name"] for item in display_rows)
    show_rule = any(item["rule_name"] for item in display_rows)
    lines = []
    for item in display_rows:
        parts = [
            f"{item['index']:>{index_width}}.",
            _display_ljust(item["ts_code"], code_width),
        ]
        if show_name:
            parts.append(_display_ljust(item["name"], name_width))
        if show_rule:
            parts.append(_display_ljust(item["rule_name"], rule_width))
        if item["labels"]:
            parts.append(item["labels"])
        lines.append(" ".join(parts).rstrip())
    return "\n".join(lines)


def _format_quote_table(
    symbols: list[str],
    quotes: pd.DataFrame,
    ma_lookup: dict[str, dict[str, float | None]],
    stock_basic: pd.DataFrame,
) -> str:
    headers = {
        "index": "序号",
        "ts_code": "股票代码",
        "name": "股票名称",
        "close": "现价",
        "pct_chg": "涨跌幅",
        "ma5": "周线",
        "ma20": "月线",
        "ma60": "季线",
        "ma250": "年线",
    }
    quote_lookup = _records_by_symbol(quotes)
    name_lookup = _stock_name_lookup(stock_basic)
    rows = []
    for index, symbol in enumerate(symbols, start=1):
        quote = quote_lookup.get(symbol, {})
        ma = ma_lookup.get(symbol, {})
        rows.append(
            {
                "index": f"{index}.",
                "ts_code": symbol,
                "name": _coalesce_text(quote.get("name"), name_lookup.get(symbol)),
                "close": _fmt_optional_price(quote.get("close")),
                "pct_chg": _fmt_optional_pct(quote.get("pct_chg")),
                "ma5": _fmt_optional_price(ma.get("ma5")),
                "ma20": _fmt_optional_price(ma.get("ma20")),
                "ma60": _fmt_optional_price(ma.get("ma60")),
                "ma250": _fmt_optional_price(ma.get("ma250")),
            }
        )
    widths = {
        key: max(get_cwidth(headers[key]), *(get_cwidth(str(row[key])) for row in rows))
        for key in headers
    }
    ordered_keys = ["index", "ts_code", "name", "close", "pct_chg", "ma5", "ma20", "ma60", "ma250"]
    lines = [" ".join(_display_ljust(headers[key], widths[key]) for key in ordered_keys).rstrip()]
    for row in rows:
        lines.append(" ".join(_display_ljust(str(row[key]), widths[key]) for key in ordered_keys).rstrip())
    return "\n".join(lines)


def _display_ljust(text: str, width: int) -> str:
    return text + " " * max(0, width - get_cwidth(text))


def _matched_labels(row: dict[str, object]) -> list[str]:
    raw_labels = row.get("matched_labels")
    if isinstance(raw_labels, list):
        return [str(label).strip() for label in raw_labels if str(label).strip()]
    metrics = row.get("metrics")
    if isinstance(metrics, dict):
        labels = metrics.get("matched_signal_labels") or metrics.get("matched_chan_rules", [])
        if isinstance(labels, list):
            return [str(label).strip() for label in labels if str(label).strip()]
    return []


def _format_numbered_values(values: list[str]) -> str:
    if not values:
        return "无结果"
    return "\n".join(f"{index}. {value}" for index, value in enumerate(values, start=1))


def _quote_moving_average_lookup(daily: pd.DataFrame, realtime_daily: pd.DataFrame) -> dict[str, dict[str, float | None]]:
    columns = ["ts_code", "trade_date", "close"]
    historical = _normalize_quote_daily_frame(daily, columns=columns)
    overlay = _normalize_quote_daily_frame(realtime_daily, columns=columns)
    combined = historical
    if not overlay.empty:
        combined = pd.concat([historical, overlay], ignore_index=True)
    if combined.empty:
        return {}
    combined = combined.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    result: dict[str, dict[str, float | None]] = {}
    for ts_code, group in combined.groupby("ts_code", sort=False):
        closes = pd.to_numeric(group.sort_values("trade_date")["close"], errors="coerce")
        result[str(ts_code)] = {
            "ma5": _rolling_ma_value(closes, 5),
            "ma20": _rolling_ma_value(closes, 20),
            "ma60": _rolling_ma_value(closes, 60),
            "ma250": _rolling_ma_value(closes, 250),
        }
    return result


def _normalize_quote_daily_frame(frame: pd.DataFrame | None, *, columns: list[str]) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.copy()
    for column in columns:
        if column not in data.columns:
            data[column] = pd.NA
    data["ts_code"] = data["ts_code"].astype(str)
    data["trade_date"] = data["trade_date"].astype(str)
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data = data.dropna(subset=["ts_code", "trade_date", "close"])
    if data.empty:
        return pd.DataFrame(columns=columns)
    return data[columns].reset_index(drop=True)


def _rolling_ma_value(close: pd.Series, window: int) -> float | None:
    if len(close.dropna()) < window:
        return None
    value = close.rolling(window, min_periods=window).mean().iloc[-1]
    return None if pd.isna(value) else float(value)


def _records_by_symbol(frame: pd.DataFrame) -> dict[str, dict[str, object]]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    data = frame.drop_duplicates(subset=["ts_code"], keep="last")
    return {
        str(row.get("ts_code") or "").strip(): row.to_dict()
        for _, row in data.iterrows()
        if str(row.get("ts_code") or "").strip()
    }


def _stock_name_lookup(stock_basic: pd.DataFrame) -> dict[str, str]:
    if stock_basic is None or stock_basic.empty:
        return {}
    lookup: dict[str, str] = {}
    for _, row in stock_basic.iterrows():
        ts_code = str(row.get("ts_code") or "").strip()
        name = str(row.get("name") or "").strip()
        if ts_code and name and ts_code not in lookup:
            lookup[ts_code] = name
    return lookup


def _coalesce_text(*values) -> str:
    for value in values:
        if value is None or pd.isna(value):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _parse_symbols(value: str) -> list[str]:
    try:
        return parse_symbol_csv(value)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _parse_symbols_or_names(value: str, settings=None) -> list[str]:
    raw_values = [item.strip() for item in str(value or "").split(",") if item.strip()]
    try:
        symbols = parse_symbol_csv(value)
    except ValueError:
        symbols = []
    if symbols and all(_looks_symbol_value(item) for item in raw_values):
        return symbols
    settings = settings or load_settings()
    stock_basic = load_stock_basic_frame(settings)
    try:
        return resolve_symbol_or_name_values(raw_values, stock_basic)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _looks_symbol_value(value: str) -> bool:
    text = str(value or "").strip().upper()
    return (len(text) == 6 and text.isdigit()) or (len(text) == 9 and text[:6].isdigit() and text[6] == ".")


def _parse_symbol_or_name(value: str, settings=None) -> str:
    return _parse_symbols_or_names(value, settings)[0]


def _parse_symbol(value: str) -> str:
    return _parse_symbols(value)[0]


def _settings_with_db_path(settings, db_path: Path):
    if getattr(settings, "db_path", None) == db_path:
        return settings
    try:
        from dataclasses import replace

        return replace(settings, db_path=db_path)
    except Exception:
        return SimpleNamespace(**{**vars(settings), "db_path": db_path})


def _storage_db_path(storage, fallback: Path | str) -> Path:
    return Path(getattr(storage, "db_path", fallback))


def _parse_csv(value: str) -> list[str]:
    items = [item.strip() for item in str(value or "").split(",") if item.strip()]
    if not items:
        raise SystemExit("At least one value is required")
    return items


def _parse_optional_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _parse_tags(value: str) -> list[str]:
    return _parse_optional_csv(value)


def _should_prompt_watchlist_import(args: argparse.Namespace) -> bool:
    if getattr(args, "no_select_watchlist", False):
        return False
    if getattr(args, "select_watchlist", False):
        return True
    return sys.stdin.isatty() and sys.stdout.isatty()


def _validate_monitor_rules(rule_names: list[str]) -> None:
    for rule_name in rule_names:
        if get_rule(rule_name).name != "chan_signals":
            raise SystemExit("monitor v1 only supports chan_signals")


def _format_monitor_table(rows: list[dict]) -> str:
    if not rows:
        return "无结果"
    lines = []
    for index, row in enumerate(rows, start=1):
        parts = [str(row.get("ts_code") or "")]
        name = str(row.get("name") or "").strip()
        if name:
            parts.append(name)
        for key in ("quantity", "buy_price", "price", "score", "signal_label", "reason", "note"):
            value = row.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        lines.append(f"{index}. {' '.join(parts)}")
    return "\n".join(lines)


def _format_monitor_runtime(row: dict) -> str:
    status = row.get("status", "stopped")
    pid = row.get("pid") or ""
    heartbeat = row.get("heartbeat_at") or ""
    error = row.get("last_error") or ""
    suffix = f" 错误: {error}" if error else ""
    return f"状态: {status} PID: {pid} 心跳: {heartbeat}{suffix}".strip()


def _cmd_qmt_order(args: argparse.Namespace, storage: DuckDBStorage, client, *, settings=None, side: str) -> int:
    ts_code = _parse_symbol_or_name(args.symbol, settings)
    if args.quantity <= 0:
        raise SystemExit("--quantity must be positive")
    if side == "buy" and args.quantity % 100 != 0:
        raise SystemExit("A股买入数量必须是 100 股整数倍")
    if args.price_type == "limit" and (args.price is None or args.price <= 0):
        raise SystemExit("--price-type limit requires --price")
    if side == "sell" and not args.dry_run:
        available = 0.0
        for position in client.positions():
            if position.ts_code == ts_code:
                available = position.available_quantity or position.quantity
                break
        if available < args.quantity:
            raise SystemExit(f"QMT 可用持仓不足: {ts_code} 可用 {available:g}")
    request = OrderRequest(
        symbol=ts_code,
        side=side,
        quantity=int(args.quantity),
        price_type=args.price_type,
        price=args.price,
        dry_run=bool(args.dry_run),
        strategy="sats-qmt-cli",
    )
    if args.dry_run:
        result = {
            "sats_order_id": f"dry-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            "broker_order_id": "",
            "status": "dry_run",
            "message": "dry-run only; no QMT endpoint called",
            "request": request.to_dict(),
            "raw": {},
        }
    else:
        placed = client.place_order(request)
        result = placed.to_dict()
    storage.insert_broker_order(
        {
            "sats_order_id": result["sats_order_id"],
            "provider": client.provider,
            "account_id": client.account_id,
            "broker_order_id": result.get("broker_order_id", ""),
            "ts_code": ts_code,
            "side": side,
            "quantity": args.quantity,
            "price": args.price,
            "price_type": args.price_type,
            "status": result["status"],
            "message": result.get("message", ""),
            "request": request.to_dict(),
            "response": result.get("raw", {}),
        }
    )
    storage.insert_broker_order_event(
        {
            "sats_order_id": result["sats_order_id"],
            "broker_order_id": result.get("broker_order_id", ""),
            "provider": client.provider,
            "account_id": client.account_id,
            "event_type": side,
            "status": result["status"],
            "message": result.get("message", ""),
            "payload": result,
        }
    )
    storage.insert_monitor_trade_event(
        {
            "trade_event_id": result["sats_order_id"],
            "event_id": "",
            "ts_code": ts_code,
            "name": "",
            "action": side,
            "side": side,
            "price": args.price,
            "quantity": args.quantity,
            "status": result["status"],
            "message": f"QMT {side} {result['status']} {result.get('broker_order_id', '')}".strip(),
            "metrics": {"broker_order": result, "request": request.to_dict()},
        }
    )
    print(f"QMT {side}: {result['status']} {result.get('broker_order_id', '')} {result.get('message', '')}".strip())
    return 0


def _dry_run_broker(settings):
    class DryRunBroker:
        provider = "qmt"
        account_id = getattr(settings, "qmt_account_id", "")

    return DryRunBroker()


def _format_qmt_asset(asset: dict) -> str:
    return (
        f"账户: {asset.get('account_id') or ''} {asset.get('account_type') or ''}\n"
        f"可用资金: {_fmt_money(asset.get('available_cash'))}  现金: {_fmt_money(asset.get('cash'))}  "
        f"市值: {_fmt_money(asset.get('market_value'))}  总资产: {_fmt_money(asset.get('total_asset'))}"
    )


def _format_qmt_positions(rows: list[dict]) -> str:
    if not rows:
        return "无持仓"
    return "\n".join(
        f"{index}. {row.get('ts_code') or ''} {row.get('name') or ''} 数量 {row.get('quantity') or 0:g} "
        f"可用 {row.get('available_quantity') or 0:g} 成本 {_fmt_money(row.get('cost_price'))} "
        f"现价 {_fmt_money(row.get('price'))} 盈亏 {_fmt_money(row.get('pnl'))} ({_fmt_pct(row.get('pnl_pct'))})"
        for index, row in enumerate(rows, start=1)
    )


def _format_qmt_orders(rows: list[dict]) -> str:
    if not rows:
        return "无委托"
    return "\n".join(
        f"{index}. {row.get('order_id') or row.get('broker_order_id') or ''} {row.get('ts_code') or ''} "
        f"{row.get('side') or ''} {row.get('quantity') or 0:g} {row.get('price_type') or ''} "
        f"{_fmt_money(row.get('price'))} {row.get('status') or ''} {row.get('message') or ''}".strip()
        for index, row in enumerate(rows, start=1)
    )


def _format_qmt_trades(rows: list[dict]) -> str:
    if not rows:
        return "无成交"
    return "\n".join(
        f"{index}. {row.get('trade_time') or ''} {row.get('trade_id') or ''} {row.get('ts_code') or ''} "
        f"{row.get('side') or ''} {row.get('quantity') or 0:g} @ {_fmt_money(row.get('price'))}".strip()
        for index, row in enumerate(rows, start=1)
    )


def _fmt_money(value) -> str:
    try:
        return f"{float(value or 0.0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _fmt_optional_price(value) -> str:
    try:
        if value is None or pd.isna(value):
            return "--"
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "--"


def _fmt_pct(value) -> str:
    try:
        return f"{float(value or 0.0):+.2f}%"
    except (TypeError, ValueError):
        return "+0.00%"


def _fmt_optional_pct(value) -> str:
    try:
        if value is None or pd.isna(value):
            return "--"
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "--"


def _format_scheduled_tasks(rows: list[dict]) -> str:
    if not rows:
        return "无定时任务"
    display_rows = []
    for index, row in enumerate(rows, start=1):
        display_rows.append(
            {
                "index": str(index),
                "name": str(row.get("name") or ""),
                "enabled": "启用" if row.get("enabled") else "停用",
                "type": str(row.get("task_type") or ""),
                "schedule": format_task_schedule(row),
                "next": str(row.get("next_run_at") or ""),
                "status": str(row.get("last_status") or ""),
            }
        )
    widths = {
        key: max(get_cwidth(item[key]) for item in display_rows)
        for key in ("index", "name", "enabled", "type", "schedule", "next", "status")
    }
    lines = []
    for item in display_rows:
        lines.append(
            " ".join(
                [
                    f"{item['index']:>{widths['index']}}.",
                    _display_ljust(item["name"], widths["name"]),
                    _display_ljust(item["enabled"], widths["enabled"]),
                    _display_ljust(item["type"], widths["type"]),
                    _display_ljust(item["schedule"], widths["schedule"]),
                    _display_ljust(item["next"], widths["next"]),
                    item["status"],
                ]
            ).rstrip()
        )
    return "\n".join(lines)


def _format_scheduled_runs(rows: list[dict]) -> str:
    if not rows:
        return "无执行记录"
    lines = []
    for index, row in enumerate(rows, start=1):
        duration = row.get("duration_seconds")
        duration_text = "" if duration is None else f"{float(duration):.1f}s"
        summary = _scheduled_run_summary(row)
        lines.append(
            f"{index}. {row.get('finished_at') or row.get('created_at') or ''} "
            f"{row.get('task_name')} {row.get('status')} {duration_text} {summary}".strip()
        )
    return "\n".join(lines)


def _format_model_status() -> str:
    status = current_model_status()
    lines = ["模型状态:"]
    for label, title in (("main", "主模型"), ("light", "轻量模型")):
        selection = status[label]
        lines.append(
            f"- {title}: {selection.profile_name} "
            f"{selection.provider}:{selection.model} "
            f"base_url={'yes' if selection.has_base_url else 'no'} "
            f"api_key={'yes' if selection.has_api_key else 'no'}"
        )
    return "\n".join(lines)


def _format_model_profiles() -> str:
    profiles = discover_model_profiles()
    if not profiles:
        return "无模型配置组"
    rows = []
    for index, name in enumerate(sorted(profiles), start=1):
        profile = profiles[name]
        rows.append(
            f"{index}. {profile.name} {profile.provider} "
            f"model={profile.model} light={profile.light_model or profile.model} "
            f"base_url={'yes' if profile.base_url else 'no'} "
            f"api_key={'yes' if profile.api_key else 'no'}"
        )
    return "\n".join(rows)


def _scheduled_run_summary(row: dict) -> str:
    error = str(row.get("error") or "").strip()
    if error:
        return error[:120]
    output = str(row.get("output_text") or "").strip().replace("\n", " ")
    return output[:120]


def _fmt_number(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _format_analysis_rankings(rows, *, explain_rating: bool = False) -> str:
    if not rows:
        return "未解析到评分排名"
    comparable = [row for row in rows if getattr(row, "external_supported", True)]
    native_extra = [row for row in rows if not getattr(row, "external_supported", True)]
    if not native_extra:
        return "\n".join(_format_analysis_ranking_lines(rows, explain_rating=explain_rating))
    lines = ["可比股票"]
    lines.extend(_format_analysis_ranking_lines(comparable, explain_rating=explain_rating))
    lines.append("")
    lines.append("原生额外股票（daily_stock_analysis 不支持）")
    lines.extend(_format_analysis_ranking_lines(native_extra, explain_rating=explain_rating))
    return "\n".join(lines)


def _format_analysis_ranking_lines(rows, *, explain_rating: bool) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        score = int(row.score) if float(row.score).is_integer() else row.score
        suffix = f" {row.external_skip_reason}" if not getattr(row, "external_supported", True) and getattr(row, "external_skip_reason", "") else ""
        lines.append(f"{index}. {row.code} {row.name} 评分 {score} {row.advice} {row.trend}{suffix}")
        adjustment = str(getattr(row, "rating_adjustment", "") or "").strip()
        if explain_rating and adjustment:
            raw_advice = str(getattr(row, "raw_advice", "") or "").strip()
            prefix = f"原始评级 {raw_advice}，" if raw_advice and raw_advice != row.advice else ""
            lines.append(f"   调整: {prefix}{adjustment}")
    return lines


def _format_chan_reviews(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "未解析到缠论复核结果"
    lines = []
    for index, row in enumerate(rows, start=1):
        ts_code = str(row.get("ts_code") or "")
        name = str(row.get("name") or "")
        quality = str(row.get("signal_quality") or row.get("buy_point_quality") or "")
        summary = str(row.get("summary") or "")
        parts = [f"{index}.", ts_code, name, quality, summary]
        lines.append(" ".join(part for part in parts if part).strip())
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
