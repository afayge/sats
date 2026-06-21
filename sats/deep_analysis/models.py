from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class DeepAnalysisRequest:
    symbols: tuple[str, ...]
    trade_date: str
    phase: str = "run"
    lookback_days: int = 180
    llm_review: bool = True
    report: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbols"] = list(self.symbols)
        return payload


@dataclass(frozen=True, slots=True)
class DeepDimensionResult:
    key: str
    label: str
    score: float
    weight: float
    quality: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    data_sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["missing_fields"] = list(self.missing_fields)
        return payload


@dataclass(frozen=True, slots=True)
class DeepInvestorVote:
    investor_id: str
    name: str
    group: str
    signal: str
    score: float
    headline: str
    reasoning: str
    matched_rules: tuple[str, ...] = ()
    failed_rules: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["matched_rules"] = list(self.matched_rules)
        payload["failed_rules"] = list(self.failed_rules)
        return payload


@dataclass(frozen=True, slots=True)
class DeepStockAnalysis:
    ts_code: str
    name: str
    trade_date: str
    overall_score: float
    verdict_label: str
    dimensions: tuple[DeepDimensionResult, ...]
    investor_votes: tuple[DeepInvestorVote, ...]
    synthesis: dict[str, Any]
    missing_fields: tuple[str, ...] = ()
    data_sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "trade_date": self.trade_date,
            "overall_score": self.overall_score,
            "verdict_label": self.verdict_label,
            "dimensions": [item.to_dict() for item in self.dimensions],
            "investor_votes": [item.to_dict() for item in self.investor_votes],
            "synthesis": self.synthesis,
            "missing_fields": list(self.missing_fields),
            "data_sources": dict(self.data_sources),
        }


@dataclass(frozen=True, slots=True)
class DeepAnalysisResult:
    request: DeepAnalysisRequest
    analyses: tuple[DeepStockAnalysis, ...] = ()
    message: str = ""
    markdown_report_path: Path | None = None
    json_artifact_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "analyses": [item.to_dict() for item in self.analyses],
            "message": self.message,
            "markdown_report_path": str(self.markdown_report_path or ""),
            "json_artifact_path": str(self.json_artifact_path or ""),
        }

    def to_markdown(self) -> str:
        if not self.analyses:
            return self.message or "无深研结果"
        blocks = ["# SATS 原生个股深研报告", ""]
        for item in self.analyses:
            blocks.extend(_stock_markdown(item))
        return "\n".join(blocks).rstrip() + "\n"


def _stock_markdown(item: DeepStockAnalysis) -> list[str]:
    syn = item.synthesis
    lines = [
        f"## {item.ts_code} {item.name or ''}".rstrip(),
        "",
        f"- 交易日: {item.trade_date}",
        f"- 综合评分: {item.overall_score:.1f}/100",
        f"- 结论: {item.verdict_label}",
        f"- 核心判断: {syn.get('core_conclusion', '暂无')}",
        "",
        "### 核心多空理由",
    ]
    llm_review = syn.get("llm_review") if isinstance(syn.get("llm_review"), dict) else {}
    if llm_review.get("comment"):
        lines.insert(6, f"- LLM 复核: {llm_review.get('comment')}")
    elif llm_review.get("status") == "unavailable":
        lines.insert(6, "- LLM 复核: 不可用，已使用本地确定性规则")
    bull = syn.get("bull_reasons") or []
    bear = syn.get("bear_reasons") or []
    if bull:
        lines.append("看多:")
        lines.extend(f"- {reason}" for reason in bull)
    if bear:
        lines.append("看空:")
        lines.extend(f"- {reason}" for reason in bear)
    if not bull and not bear:
        lines.append("- 暂无足够证据形成明确多空理由")

    lines.extend(["", "### 投资人面板"])
    for vote in item.investor_votes[:12]:
        lines.append(f"- {vote.name}: {vote.signal} {vote.score:.0f} - {vote.headline}")
    if not item.investor_votes:
        lines.append("- 本阶段未生成投资人面板")

    lines.extend(["", "### 维度评分"])
    for dim in item.dimensions:
        missing = f" 缺失: {', '.join(dim.missing_fields)}" if dim.missing_fields else ""
        lines.append(f"- {dim.label}: {dim.score:.1f}/10 [{dim.quality}] - {dim.summary}{missing}")

    risks = syn.get("risks") or []
    lines.extend(["", "### 风险与缺口"])
    if risks:
        lines.extend(f"- {risk}" for risk in risks)
    if item.missing_fields:
        lines.append(f"- 数据缺口: {', '.join(item.missing_fields)}")
    if not risks and not item.missing_fields:
        lines.append("- 暂无突出风险或数据缺口")

    zones = syn.get("observation_zones") or {}
    lines.extend(["", "### 估值观察区"])
    if zones:
        for key, value in zones.items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- 估值观察区需等待更多真实财务和历史估值数据")
    lines.append("")
    return lines
