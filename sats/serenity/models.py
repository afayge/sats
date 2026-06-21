from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SerenityScreenRequest:
    query: str = ""
    theme: str = "AI 产业链"
    symbols: tuple[str, ...] = ()
    trade_date: str = ""
    limit: int = 10
    candidate_limit: int = 30
    lookback_days: int = 180
    llm_review: bool = True
    report: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbols"] = list(self.symbols)
        return payload


@dataclass(frozen=True, slots=True)
class SerenityFactorResult:
    key: str
    label: str
    rating: float
    weight: float
    points: float
    available: bool
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SerenityEvidence:
    claim: str
    source: str
    strength: str
    dataset: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SerenityCandidateResult:
    rank: int
    ts_code: str
    name: str
    trade_date: str
    final_score: float
    raw_factor_points: float
    penalty_points: float
    verdict: str
    passed: bool
    coverage_pct: float
    ai_chain_hit: bool
    chain_tier: str
    chain_tier_weight: float
    scarce_layer: str
    constrained_link: str
    factors: tuple[SerenityFactorResult, ...]
    penalties: dict[str, float] = field(default_factory=dict)
    evidence: tuple[SerenityEvidence, ...] = ()
    missing_fields: tuple[str, ...] = ()
    bear_case: tuple[str, ...] = ()
    kill_switches: tuple[str, ...] = ()
    next_checks: tuple[str, ...] = ()
    llm_review: dict[str, Any] = field(default_factory=dict)
    data_sources: dict[str, str] = field(default_factory=dict)
    preliminary_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "ts_code": self.ts_code,
            "name": self.name,
            "trade_date": self.trade_date,
            "final_score": self.final_score,
            "raw_factor_points": self.raw_factor_points,
            "penalty_points": self.penalty_points,
            "verdict": self.verdict,
            "passed": self.passed,
            "coverage_pct": self.coverage_pct,
            "ai_chain_hit": self.ai_chain_hit,
            "chain_tier": self.chain_tier,
            "chain_tier_weight": self.chain_tier_weight,
            "scarce_layer": self.scarce_layer,
            "constrained_link": self.constrained_link,
            "factors": [item.to_dict() for item in self.factors],
            "penalties": dict(self.penalties),
            "evidence": [item.to_dict() for item in self.evidence],
            "missing_fields": list(self.missing_fields),
            "bear_case": list(self.bear_case),
            "kill_switches": list(self.kill_switches),
            "next_checks": list(self.next_checks),
            "llm_review": dict(self.llm_review),
            "data_sources": dict(self.data_sources),
            "preliminary_score": self.preliminary_score,
        }


@dataclass(frozen=True, slots=True)
class SerenityScreenResult:
    request: SerenityScreenRequest
    candidates: tuple[SerenityCandidateResult, ...] = ()
    candidate_source: str = ""
    universe_count: int = 0
    shortlisted_count: int = 0
    passed_count: int = 0
    layer_priorities: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    message: str = ""
    markdown_report_path: Path | None = None
    json_artifact_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "candidate_source": self.candidate_source,
            "universe_count": self.universe_count,
            "shortlisted_count": self.shortlisted_count,
            "passed_count": self.passed_count,
            "layer_priorities": list(self.layer_priorities),
            "candidates": [item.to_dict() for item in self.candidates],
            "warnings": list(self.warnings),
            "message": self.message,
            "markdown_report_path": str(self.markdown_report_path or ""),
            "json_artifact_path": str(self.json_artifact_path or ""),
            "data_policy": (
                "研究优先级，不是买卖指令。SATS 只使用 AStockDataProvider、"
                "DuckDB cache 和已记录公开证据，不补造缺失数字。"
            ),
        }

    def to_markdown(self) -> str:
        if not self.candidates:
            return self.message or "无 Serenity 筛选结果"
        lines = [
            "# SATS Serenity AI 卡位筛选",
            "",
            f"- 主题: {self.request.theme}",
            f"- 交易日: {self.request.trade_date}",
            f"- 候选来源: {self.candidate_source or 'unknown'}",
            f"- 候选池: {self.universe_count}，增强分析: {self.shortlisted_count}，入选: {self.passed_count}",
            "- 定位: 研究优先级，不是买卖指令。",
            "",
            "## 供应链层级优先级",
            "",
        ]
        if self.layer_priorities:
            for item in self.layer_priorities:
                lines.append(
                    f"- {item.get('layer')}: {item.get('count', 0)} 只，"
                    f"最高分 {float(item.get('top_score') or 0):.1f}"
                )
        else:
            lines.append("- 暂无可排序层级")

        lines.extend(
            [
                "",
                "## 候选排名",
                "",
                "| 排名 | 股票 | 总分 | 评级 | 覆盖率 | 层级 | 入选 |",
                "|---:|---|---:|---|---:|---|---|",
            ]
        )
        for item in self.candidates:
            passed = "是" if item.passed else "否"
            lines.append(
                f"| {item.rank} | {item.ts_code} {item.name} | {item.final_score:.1f} | "
                f"{item.verdict} | {item.coverage_pct:.0f}% | {item.chain_tier} | {passed} |"
            )

        for item in self.candidates:
            lines.extend(_candidate_markdown(item))

        if self.warnings:
            lines.extend(["", "## 运行提示", ""])
            lines.extend(f"- {warning}" for warning in self.warnings)
        return "\n".join(lines).rstrip() + "\n"


def _candidate_markdown(item: SerenityCandidateResult) -> list[str]:
    lines = [
        "",
        f"## {item.rank}. {item.ts_code} {item.name}",
        "",
        f"- 总分: {item.final_score:.1f}/100；评级: {item.verdict}；覆盖率: {item.coverage_pct:.0f}%",
        f"- 卡住的环节: {item.constrained_link or '未确认'}",
        f"- 产业链位置: {item.chain_tier}；稀缺层判断: {item.scarce_layer or '未确认'}",
        f"- AI 链命中: {'是' if item.ai_chain_hit else '否'}；入选: {'是' if item.passed else '否'}",
        "",
        "### 因子评分",
        "",
        "| 因子 | 评分 | 权重 | 得分 | 依据 |",
        "|---|---:|---:|---:|---|",
    ]
    for factor in item.factors:
        basis = factor.summary or ("数据缺失" if not factor.available else "")
        lines.append(
            f"| {factor.label} | {factor.rating:.1f}/5 | {factor.weight:.0f} | "
            f"{factor.points:.1f} | {basis} |"
        )
    lines.extend(["", "### 风险罚分", ""])
    if item.penalties:
        lines.extend(f"- {key}: -{value:.1f}" for key, value in item.penalties.items())
    else:
        lines.append("- 未触发已接入的风险罚分")

    lines.extend(["", "### 证据", ""])
    if item.evidence:
        for evidence in item.evidence:
            source = f" ({evidence.source})" if evidence.source else ""
            lines.append(f"- [{evidence.strength}] {evidence.claim}{source}")
    else:
        lines.append("- 未取得强/中等证据，仅可作为早期线索")

    lines.extend(["", "### 反方理由与失效条件", ""])
    risks = [*item.bear_case, *item.kill_switches]
    lines.extend(f"- {value}" for value in risks) if risks else lines.append("- 暂无结构化反方证据")

    lines.extend(["", "### 下一步验证", ""])
    lines.extend(f"- {value}" for value in item.next_checks) if item.next_checks else lines.append("- 补查公告、订单、认证、产能与替代路线")

    comment = str(item.llm_review.get("comment") or "").strip()
    if comment:
        lines.extend(["", "### LLM 研究补充", "", comment])
    elif item.llm_review.get("status") == "unavailable":
        lines.extend(["", "### LLM 研究补充", "", "- LLM 不可用，确定性评分仍有效"])

    if item.missing_fields:
        lines.extend(["", "### 数据缺口", ""])
        lines.extend(f"- {field}" for field in item.missing_fields)
    lines.extend(["", "### 数据来源", ""])
    if item.data_sources:
        lines.extend(f"- {key}: {source}" for key, source in sorted(item.data_sources.items()))
    else:
        lines.append("- 未记录可用数据来源")
    return lines
