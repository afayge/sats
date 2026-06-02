from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sats.factors.analysis import FactorAnalysisResult
from sats.factors.composite import FactorPickResult


def write_factor_analysis_report(
    result: FactorAnalysisResult,
    *,
    reports_dir: Path,
    warnings: list[str] | None = None,
) -> Path:
    target_dir = reports_dir / "factors"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{result.trade_date}_{result.factor_id}_analysis.md"
    lines = [
        f"# Factor Analysis: {result.factor_id}",
        "",
        f"- trade_date: {result.trade_date}",
        f"- horizon: {result.horizon}",
        f"- IC: {result.ic_mean}",
        f"- RankIC: {result.rank_ic_mean}",
        f"- ICIR: {result.icir}",
        f"- RankICIR: {result.rank_icir}",
        f"- positive_ratio: {result.positive_ratio}",
        f"- coverage: {result.coverage}",
        f"- nan_ratio: {result.nan_ratio}",
        f"- long_short_spread: {result.long_short_spread}",
        "",
        "## Group Equity",
        "",
    ]
    if result.group_equity:
        lines.extend(f"- {group}: {value}" for group, value in result.group_equity.items())
    else:
        lines.append("- unavailable")
    all_warnings = [*result.warnings, *(warnings or [])]
    if all_warnings:
        lines.extend(["", "## Data Notes", ""])
        lines.extend(f"- {item}" for item in all_warnings)
    lines.extend(["", f"Generated at {datetime.now().isoformat(timespec='seconds')}"])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_factor_pick_report(
    result: FactorPickResult,
    *,
    reports_dir: Path,
    warnings: list[str] | None = None,
) -> Path:
    target_dir = reports_dir / "factors"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{result.trade_date}_{result.run_id}_pick.md"
    lines = [
        f"# Factor Pick: {result.run_id}",
        "",
        f"- trade_date: {result.trade_date}",
        f"- factors: {', '.join(result.factors)}",
        f"- weighting: {result.weighting}",
        f"- neutralization: {result.neutralization}",
        "",
        "## Candidates",
        "",
    ]
    if result.candidates:
        lines.append("| rank | ts_code | name | score |")
        lines.append("| ---: | --- | --- | ---: |")
        for item in result.candidates:
            lines.append(f"| {item.rank} | {item.ts_code} | {item.name} | {item.score} |")
    else:
        lines.append("No candidates.")
    all_warnings = [*result.warnings, *(warnings or [])]
    if all_warnings:
        lines.extend(["", "## Data Notes", ""])
        lines.extend(f"- {item}" for item in all_warnings)
    lines.extend(["", f"Generated at {datetime.now().isoformat(timespec='seconds')}"])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
