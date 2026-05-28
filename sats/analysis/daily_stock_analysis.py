from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from sats.analysis.dsa_native import (
    DsaAnalysisRanking as AnalysisRanking,
    DsaAnalysisRunResult as AnalysisRunResult,
)
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols

DEFAULT_DAILY_STOCK_ANALYSIS_DIR = Path("/Users/elliotge/python/daily_stock_analysis")
_BSE_PREFIXES = ("43", "81", "82", "83", "87", "88", "92")


def run_screened_stock_analysis(
    *,
    storage: DuckDBStorage,
    trade_date: str,
    rule_name: str | None,
    reports_dir: Path,
    analysis_dir: Path = DEFAULT_DAILY_STOCK_ANALYSIS_DIR,
    sats_env_path: Path | None = None,
    python_executable: str = sys.executable,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    lookback_days: int = 180,
) -> AnalysisRunResult:
    del lookback_days
    rows = storage.list_screening_stocks(
        trade_date=trade_date,
        rule_name=rule_name,
        passed=True,
    )
    if not rows:
        return AnalysisRunResult([], [], [], None, None, message="无通过筛选股票")

    symbols = [str(row.get("ts_code") or "").strip() for row in rows if str(row.get("ts_code") or "").strip()]
    return run_daily_stock_analysis_for_symbols(
        symbols,
        trade_date=trade_date,
        reports_dir=reports_dir,
        analysis_dir=analysis_dir,
        sats_env_path=sats_env_path,
        python_executable=python_executable,
        runner=runner,
        source_label=rule_name or "screened",
    )


def run_daily_stock_analysis_for_symbols(
    symbols: Sequence[str],
    *,
    trade_date: str,
    reports_dir: Path,
    analysis_dir: Path = DEFAULT_DAILY_STOCK_ANALYSIS_DIR,
    sats_env_path: Path | None = None,
    python_executable: str = sys.executable,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    source_label: str = "stocks",
) -> AnalysisRunResult:
    normalized = normalize_symbols(symbols, required=False)
    stock_codes: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for symbol in normalized:
        code = to_daily_stock_analysis_code(symbol)
        if code is None:
            skipped.append(symbol)
            continue
        if code not in seen:
            seen.add(code)
            stock_codes.append(code)

    if not stock_codes:
        return AnalysisRunResult([], skipped, [], None, None, message="无可供 daily_stock_analysis 分析股票")

    main_py = analysis_dir / "main.py"
    if not main_py.exists():
        return AnalysisRunResult([], skipped, [], None, None, message=f"daily_stock_analysis 不存在: {main_py}")

    external_reports_dir = analysis_dir / "reports"
    before = _report_mtimes(external_reports_dir)
    cmd = [
        python_executable,
        str(main_py),
        "--stocks",
        ",".join(stock_codes),
        "--no-notify",
        "--no-market-review",
        "--force-run",
    ]
    completed = runner(
        cmd,
        cwd=str(analysis_dir),
        env=_daily_stock_analysis_env(sats_env_path, analysis_dir / ".env"),
        capture_output=True,
        text=True,
    )
    if getattr(completed, "returncode", 0) != 0:
        tail = _process_output_tail(completed)
        message = "daily_stock_analysis 分析失败"
        if tail:
            message = f"{message}: {tail}"
        return AnalysisRunResult(stock_codes, skipped, [], None, None, message=message)

    source_report = _latest_changed_report(external_reports_dir, before)
    report_text = ""
    if source_report is not None and source_report.exists():
        report_text = source_report.read_text(encoding="utf-8")
    if not report_text:
        report_text = "\n".join(
            str(part or "")
            for part in (getattr(completed, "stdout", ""), getattr(completed, "stderr", ""))
            if str(part or "").strip()
        )
    rankings = parse_report_rankings(report_text)
    archived_report = _archive_report(
        source_report,
        reports_dir=reports_dir,
        trade_date=trade_date,
        rule_name=source_label,
    ) if source_report is not None else None
    if source_report is None and not rankings:
        tail = _process_output_tail(completed)
        message = "daily_stock_analysis 未生成可解析报告"
        if tail:
            message = f"{message}: {tail}"
        return AnalysisRunResult(stock_codes, skipped, [], None, None, message=message)
    return AnalysisRunResult(
        analyzed_codes=stock_codes,
        skipped_codes=skipped,
        rankings=rankings,
        source_report=source_report,
        archived_report=archived_report,
    )


def to_daily_stock_analysis_code(ts_code: str) -> str | None:
    raw = (ts_code or "").strip().upper()
    if not raw:
        return None
    if raw.endswith(".BJ"):
        return None
    code = raw.split(".", 1)[0]
    if not code.isdigit():
        return None
    if code.startswith("688"):
        return None
    if code.startswith(_BSE_PREFIXES):
        return None
    return code


def _process_output_tail(completed: subprocess.CompletedProcess) -> str:
    output = "\n".join(
        str(part or "").strip()
        for part in (getattr(completed, "stderr", ""), getattr(completed, "stdout", ""))
        if str(part or "").strip()
    )
    if not output:
        return ""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return "\n".join(lines[-5:])


def parse_report_rankings(text: str) -> list[AnalysisRanking]:
    rows: list[AnalysisRanking] = []
    pattern = re.compile(
        r"^\s*(?:[-*]\s*)?(?:[^\w\d\s*]+\s*)?\*?"
        r"\*?(?P<name>[^()\n*]+?)\((?P<code>[A-Za-z0-9]+)\)\*?\*?"
        r"\s*:\s*(?P<advice>[^|]+)\|\s*(?:评分|Score)\s*"
        r"(?P<score>\d+(?:\.\d+)?)\s*\|\s*(?P<trend>.+?)\s*$"
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        rows.append(
            AnalysisRanking(
                code=match.group("code").strip(),
                name=match.group("name").strip(),
                score=float(match.group("score")),
                advice=match.group("advice").strip(),
                trend=match.group("trend").strip(),
            )
        )
    return sorted(rows, key=lambda row: row.score, reverse=True)


def _report_mtimes(reports_dir: Path) -> dict[Path, int]:
    if not reports_dir.exists():
        return {}
    return {
        path: path.stat().st_mtime_ns
        for path in reports_dir.glob("report_*.md")
        if path.is_file()
    }


def _latest_changed_report(reports_dir: Path, before: dict[Path, int]) -> Path | None:
    if not reports_dir.exists():
        return None
    candidates = []
    for path in reports_dir.glob("report_*.md"):
        if not path.is_file():
            continue
        mtime_ns = path.stat().st_mtime_ns
        if before.get(path) != mtime_ns:
            candidates.append((mtime_ns, path))
    if not candidates:
        candidates = [(path.stat().st_mtime_ns, path) for path in reports_dir.glob("report_*.md") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _archive_report(source: Path, *, reports_dir: Path, trade_date: str, rule_name: str) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_rule = re.sub(r"[^A-Za-z0-9_.-]+", "_", rule_name)
    destination = reports_dir / f"daily_stock_analysis_{trade_date}_{safe_rule}_{timestamp}.md"
    shutil.copy2(source, destination)
    return destination


def _daily_stock_analysis_env(sats_env_path: Path | None, daily_env_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    if sats_env_path is not None:
        for key in _env_file_keys(sats_env_path):
            env.pop(key, None)
    env["ENV_FILE"] = str(daily_env_path)
    return env


def _env_file_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys
