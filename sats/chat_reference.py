from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sats.config import Settings
from sats.output_saver import CapturedOutput
from sats.screening.registry import get_rule
from sats.stock_question import extract_stock_symbols, extract_trade_date
from sats.storage.duckdb import DuckDBStorage


@dataclass(frozen=True, slots=True)
class ChatReferenceContext:
    system_message: str
    symbols: list[str]
    trade_date: str | None = None
    source: str = "output"
    data_name: str = "上条输出"


def build_chat_reference_context(
    message: str,
    last_output: CapturedOutput | None,
    settings: Settings,
    *,
    storage_factory: Callable[[Path], DuckDBStorage] = DuckDBStorage,
) -> ChatReferenceContext | None:
    if last_output is None or not str(last_output.content or "").strip():
        return None
    if not is_reference_question(message):
        return None
    if _is_screening_output(last_output):
        try:
            return _build_screening_reference(message, last_output, settings, storage_factory=storage_factory)
        except Exception:
            return _build_plain_output_reference(message, last_output)
    return _build_plain_output_reference(message, last_output)


def is_reference_question(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if any(
        term in text
        for term in (
            "上面",
            "上面的",
            "上方",
            "上述",
            "刚才",
            "上一条",
            "前面",
            "之前",
            "这些",
            "这批",
            "这组",
            "列表",
            "名单",
            "结果",
            "筛出的",
            "筛出来的",
            "上一批",
            "上个命令",
            "上一命令",
            "第一命令",
            "第一条命令",
        )
    ):
        return True
    has_collective_stock_ref = any(term in text for term in ("股票", "个股", "标的", "候选"))
    has_context_ref = any(term in text for term in ("上面", "上方", "上述", "刚才", "这些", "这批", "上一批"))
    return has_collective_stock_ref and has_context_ref


def _is_screening_output(last_output: CapturedOutput) -> bool:
    request = str(last_output.request or "").strip()
    source = str(last_output.source or "").strip()
    return source in {"/results", "/screen"} or request.startswith(("/results", "/screen"))


def _build_screening_reference(
    message: str,
    last_output: CapturedOutput,
    settings: Settings,
    *,
    storage_factory: Callable[[Path], DuckDBStorage],
) -> ChatReferenceContext:
    symbols = extract_stock_symbols(last_output.content)
    args = _parse_screening_request(last_output.request)
    db_path = args.get("db") or getattr(settings, "db_path", None)
    rows: list[dict[str, Any]] = []
    if db_path is not None:
        storage = storage_factory(Path(db_path))
        rows = storage.list_screening_stocks(
            trade_date=args.get("trade_date"),
            rule_name=args.get("rule_name"),
            passed=args.get("passed"),
        )
    rows = _filter_rows_by_symbols(rows, symbols)
    trade_date = str(args.get("trade_date") or "") or extract_trade_date(last_output.request) or None
    system_message = _results_system_message(
        user_message=message,
        last_output=last_output,
        rows=rows,
        symbols=symbols,
        trade_date=trade_date,
    )
    return ChatReferenceContext(
        system_message=system_message,
        symbols=symbols,
        trade_date=trade_date,
        source=last_output.source or "/results",
        data_name="筛选结果",
    )


def _build_plain_output_reference(message: str, last_output: CapturedOutput) -> ChatReferenceContext | None:
    symbols = extract_stock_symbols(last_output.content)
    content = _truncate_text(last_output.content, 4000)
    if not content.strip() and not symbols:
        return None
    lines = [
        "SATS 上一条输出上下文:",
        f"- source: {last_output.source or 'output'}",
        f"- request: {last_output.request or 'unknown'}",
        f"- referenced_by_user: {message}",
        "- policy: 用户正在引用上一条可见输出；只能基于下方内容和已注入的真实数据分析，不得编造缺失字段。",
    ]
    if symbols:
        lines.append(f"- symbols: {', '.join(symbols)}")
    lines.extend(["", "上一条可见输出:", content])
    return ChatReferenceContext(
        system_message="\n".join(lines),
        symbols=symbols,
        trade_date=extract_trade_date(last_output.request),
        source=last_output.source or "output",
        data_name="上条输出",
    )


def _parse_results_request(request: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {"trade_date": None, "rule_name": None, "passed": None, "db": None}
    try:
        argv = shlex.split(str(request or "").strip())
    except ValueError:
        return parsed
    if argv and argv[0] == "/results":
        argv = argv[1:]
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--trade-date" and index + 1 < len(argv):
            parsed["trade_date"] = argv[index + 1]
            index += 2
            continue
        if token == "--rule" and index + 1 < len(argv):
            parsed["rule_name"] = _normalize_rule_name(argv[index + 1])
            index += 2
            continue
        if token == "--db" and index + 1 < len(argv):
            parsed["db"] = Path(argv[index + 1])
            index += 2
            continue
        if token == "--passed":
            parsed["passed"] = True
        index += 1
    return parsed


def _parse_screening_request(request: str) -> dict[str, Any]:
    parsed = _parse_results_request(request)
    try:
        argv = shlex.split(str(request or "").strip())
    except ValueError:
        return parsed
    if not argv:
        return parsed
    if argv[0] == "/screen":
        parsed["passed"] = True
        index = 1
        while index < len(argv):
            token = argv[index]
            if token == "--trade-date" and index + 1 < len(argv):
                parsed["trade_date"] = argv[index + 1]
                index += 2
                continue
            if token == "--rule" and index + 1 < len(argv):
                parsed["rule_name"] = _normalize_rule_name(argv[index + 1])
                index += 2
                continue
            if token == "--db" and index + 1 < len(argv):
                parsed["db"] = Path(argv[index + 1])
                index += 2
                continue
            index += 1
    return parsed


def _normalize_rule_name(rule_name: str) -> str:
    try:
        return get_rule(rule_name).name
    except Exception:
        return str(rule_name or "").strip()


def _filter_rows_by_symbols(rows: list[dict[str, Any]], symbols: list[str]) -> list[dict[str, Any]]:
    if not symbols:
        return rows
    wanted = {symbol: index for index, symbol in enumerate(symbols)}
    filtered = [row for row in rows if str(row.get("ts_code") or "") in wanted]
    return sorted(filtered, key=lambda row: wanted.get(str(row.get("ts_code") or ""), len(wanted)))


def _results_system_message(
    *,
    user_message: str,
    last_output: CapturedOutput,
    rows: list[dict[str, Any]],
    symbols: list[str],
    trade_date: str | None,
) -> str:
    lines = [
        "SATS 上一条 /results 筛选结果上下文:",
        f"- request: {last_output.request or '/results'}",
        f"- referenced_by_user: {user_message}",
        f"- trade_date: {trade_date or 'unknown'}",
        f"- symbols_from_visible_output: {', '.join(symbols) if symbols else 'none'}",
        "- policy: 用户正在引用上一条筛选结果；最高评分必须使用 screening_results.score；不得从文本猜测未提供分数。",
        "- policy: 如果用户要求剔除某前缀，先排除对应股票；如果要求只看/筛出某前缀，则只保留对应股票。",
    ]
    prefix_policy = _prefix_policy(user_message)
    if prefix_policy:
        lines.append(f"- prefix_policy: {prefix_policy}")
    if rows:
        payload = [_row_payload(row) for row in rows]
        lines.extend(["", "structured_screening_results_json:", json.dumps(payload, ensure_ascii=False, default=str)])
    else:
        lines.extend(["", "上一条可见输出:", _truncate_text(last_output.content, 4000)])
    return "\n".join(lines)


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    return {
        "ts_code": str(row.get("ts_code") or ""),
        "name": str(row.get("name") or ""),
        "rule_name": str(row.get("rule_name") or ""),
        "score": row.get("score"),
        "matched_labels": row.get("matched_labels") if isinstance(row.get("matched_labels"), list) else [],
        "metrics": metrics,
    }


def _prefix_policy(message: str) -> str:
    text = str(message or "")
    if "688" not in text:
        return ""
    include_terms = ("只看", "仅看", "筛出", "挑出", "选出688", "保留688", "688开头股票")
    exclude_terms = ("剔除", "排除", "去掉", "不要", "提出")
    if any(term in text for term in exclude_terms):
        return "exclude_prefix=688"
    if any(term in text for term in include_terms):
        return "include_prefix=688"
    return ""


def _truncate_text(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."
