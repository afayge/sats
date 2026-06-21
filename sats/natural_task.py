from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DialogueMode(str, Enum):
    PLAN_ONLY = "plan_only"
    DRY_RUN = "dry_run"
    EXECUTE = "execute"


class SideEffectLevel(str, Enum):
    READONLY = "readonly"
    WRITE_DB = "write_db"
    WRITE_ARTIFACT = "write_artifact"
    LONG_RUNNING = "long_running"
    LIVE_TRADE = "live_trade"


class WorkflowRunStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScreenedAnalysisMode(str, Enum):
    AUTO = "auto"
    BATCH = "batch"
    GROUP = "group"
    PER_STOCK = "per_stock"


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    status: str = "pending"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NaturalTaskSpec:
    objective: str
    workflow_kind: str = ""
    dialogue_mode: DialogueMode = DialogueMode.EXECUTE
    side_effect_level: SideEffectLevel = SideEffectLevel.READONLY
    status: WorkflowRunStatus = WorkflowRunStatus.PLANNED
    analysis_mode: ScreenedAnalysisMode = ScreenedAnalysisMode.AUTO
    analysis_mode_reason: str = ""
    candidate_limit: int = 0
    success_criteria: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    verification_checks: tuple[VerificationCheck, ...] = field(default_factory=tuple)
    requires_approval: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["dialogue_mode"] = self.dialogue_mode.value
        payload["side_effect_level"] = self.side_effect_level.value
        payload["status"] = self.status.value
        payload["analysis_mode"] = self.analysis_mode.value
        payload["verification_checks"] = [item.to_dict() for item in self.verification_checks]
        return payload


DEFAULT_SCREENED_CANDIDATE_LIMIT = 20
DEFAULT_PER_STOCK_CANDIDATE_LIMIT = 5


def extract_candidate_limit(text: str, *, default: int = DEFAULT_SCREENED_CANDIDATE_LIMIT) -> int:
    source = str(text or "")
    patterns = (
        r"(?:前|top\s*)(\d{1,3})",
        r"(?:最多|不超过|限制|只看|分析)(\d{1,3})(?:只|个|支)?",
        r"(\d{1,3})(?:只|个|支)(?:股票|标的|候选)?",
    )
    for pattern in patterns:
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if match:
            return max(1, min(200, int(match.group(1))))
    return default


def requested_screened_analysis_mode(text: str) -> ScreenedAnalysisMode:
    source = str(text or "")
    lowered = source.lower()
    if any(term in source for term in ("逐股", "逐只", "逐一", "每只", "每个标的")):
        return ScreenedAnalysisMode.PER_STOCK
    if any(term in source for term in ("分组", "分类", "分层", "按风险", "按信号", "按行业", "按主题")):
        return ScreenedAnalysisMode.GROUP
    if any(term in source for term in ("整体", "一次性", "批量", "总体", "集合")) or "batch" in lowered:
        return ScreenedAnalysisMode.BATCH
    return ScreenedAnalysisMode.AUTO


def choose_screened_analysis_mode(
    text: str,
    *,
    candidate_count: int | None = None,
    requested: ScreenedAnalysisMode | str | None = None,
) -> tuple[ScreenedAnalysisMode, str]:
    requested_mode = _coerce_mode(requested) or requested_screened_analysis_mode(text)
    if requested_mode != ScreenedAnalysisMode.AUTO:
        return requested_mode, f"用户明确要求 {requested_mode.value} 分析模式。"
    if candidate_count is not None:
        if candidate_count <= DEFAULT_PER_STOCK_CANDIDATE_LIMIT and _asks_for_detail(text):
            return ScreenedAnalysisMode.PER_STOCK, "候选数量不超过 5 且用户要求详细分析。"
        if candidate_count > DEFAULT_PER_STOCK_CANDIDATE_LIMIT:
            return ScreenedAnalysisMode.GROUP, "候选数量超过 5，默认按信号/风险分组。"
    return ScreenedAnalysisMode.BATCH, "用户未指定模式，默认对筛选股票做集合分析。"


def default_limit_for_mode(mode: ScreenedAnalysisMode | str, requested_limit: int | None = None) -> int:
    value = _coerce_mode(mode) or ScreenedAnalysisMode.AUTO
    if requested_limit and requested_limit > 0:
        return requested_limit
    if value == ScreenedAnalysisMode.PER_STOCK:
        return DEFAULT_PER_STOCK_CANDIDATE_LIMIT
    return DEFAULT_SCREENED_CANDIDATE_LIMIT


def build_screened_natural_task_spec(
    message: str,
    *,
    candidate_count: int | None = None,
    candidate_limit: int | None = None,
    dialogue_mode: DialogueMode = DialogueMode.EXECUTE,
) -> NaturalTaskSpec:
    requested = requested_screened_analysis_mode(message)
    selected_mode, reason = choose_screened_analysis_mode(message, candidate_count=candidate_count, requested=requested)
    explicit_limit = candidate_limit if candidate_limit and candidate_limit > 0 else extract_candidate_limit(message, default=0)
    limit = default_limit_for_mode(selected_mode, explicit_limit)
    return NaturalTaskSpec(
        objective=str(message or "").strip() or "筛选股票分析",
        workflow_kind="screened_stock_analysis_plan",
        dialogue_mode=dialogue_mode,
        side_effect_level=SideEffectLevel.WRITE_DB,
        analysis_mode=selected_mode,
        analysis_mode_reason=reason,
        candidate_limit=limit,
        success_criteria=(
            "识别筛选规则、交易日和候选股票范围。",
            "按 batch/group/per_stock 策略分析筛选股票集合。",
            "输出次日交易计划和风险边界。",
            "验证执行步骤、数据来源和候选数量限制。",
        ),
        assumptions=("对筛选股票进行分析默认是集合分析，不默认逐股展开。",),
        boundaries=("不执行实盘交易；只生成计划或 dry-run 审计。", "不允许 LLM 填造价格、K线或成交量。"),
        verification_checks=(
            VerificationCheck("screening_context_resolved"),
            VerificationCheck("candidate_limit_applied"),
            VerificationCheck("analysis_mode_selected"),
            VerificationCheck("trade_plan_generated"),
        ),
    )


def _asks_for_detail(text: str) -> bool:
    return any(term in str(text or "") for term in ("详细", "深度", "展开", "完整", "逐项"))


def _coerce_mode(value: ScreenedAnalysisMode | str | None) -> ScreenedAnalysisMode | None:
    if isinstance(value, ScreenedAnalysisMode):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return ScreenedAnalysisMode(text)
    except ValueError:
        return None
