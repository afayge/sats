from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from sats.screening.base import ScreeningInput, ScreeningResult
from sats.screening.generated_rule_runtime import evaluate_generated_rule
from sats.screening.rule_composer import compose_rule_generation_plan


ALLOWED_CONDITION_KINDS = {
    "breakout_high",
    "circ_mv_between",
    "close_above_ma",
    "daily_basic_max",
    "daily_basic_min",
    "exclude_bse",
    "exclude_st",
    "ma_slope_gte",
    "ma_stack",
    "min_daily_rows",
    "pct_chg_between",
    "pct_chg_gte",
    "range_position_lte",
    "recent_close_not_below_ma",
    "recent_low_near_any_ma",
    "relative_strength_gte",
    "turnover_between",
    "volume_ratio_gte",
    "volume_ratio_lte",
    "window_return_gte",
}


@dataclass(frozen=True, slots=True)
class SemanticScreenSpec:
    goal: str
    conditions: tuple[dict[str, Any], ...]
    data_dependencies: tuple[str, ...] = ("日线 OHLCV", "stock_basic")
    assumptions: tuple[str, ...] = ()
    source_text: str = ""
    semantic_tags: tuple[str, ...] = ()
    rule_name: str = ""

    @property
    def spec_hash(self) -> str:
        encoded = json.dumps(
            {
                "goal": self.goal,
                "conditions": [dict(item) for item in self.conditions],
                "data_dependencies": list(self.data_dependencies),
                "assumptions": list(self.assumptions),
                "semantic_tags": list(self.semantic_tags),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:10]

    @property
    def generated_rule_name(self) -> str:
        if self.rule_name:
            return self.rule_name
        slug = "_".join(self.semantic_tags[:3]) or "dynamic"
        return f"nl_{slug}_{self.spec_hash[:6]}"

    def to_runtime_spec(self) -> dict[str, Any]:
        return {
            "decision_name": "自然语义临时筛选",
            "rule_name": self.generated_rule_name,
            "goal": self.goal,
            "data_dependencies": list(self.data_dependencies),
            "conditions": [dict(item) for item in self.conditions],
            "pass_condition": "全部 required=true 的硬条件满足时严格通过；软条件只参与评分。",
            "risk_notes": ["临时规则只用于研究筛选，不自动交易。"],
            "assumptions": list(self.assumptions),
            "semantic_tags": list(self.semantic_tags),
            "source_text": self.source_text,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_runtime_spec()
        payload["spec_hash"] = self.spec_hash
        return payload


def semantic_spec_from_payload(payload: Any, *, message: str = "") -> SemanticScreenSpec | None:
    if not isinstance(payload, dict) or not payload:
        return semantic_spec_from_message(message)
    raw_conditions = payload.get("conditions")
    if not isinstance(raw_conditions, list):
        raise ValueError("semantic_spec.conditions must be an array")
    conditions = tuple(_normalize_condition(item) for item in raw_conditions if isinstance(item, dict))
    if not _has_core_condition(conditions):
        raise ValueError("semantic_spec requires at least one supported screening condition")
    dependencies = tuple(str(item) for item in payload.get("data_dependencies") or ("日线 OHLCV", "stock_basic"))
    assumptions = tuple(str(item) for item in payload.get("assumptions") or ())
    tags = tuple(_safe_tag(item) for item in payload.get("semantic_tags") or () if _safe_tag(item))
    return SemanticScreenSpec(
        goal=str(payload.get("goal") or message or "自然语义筛选"),
        conditions=conditions,
        data_dependencies=dependencies,
        assumptions=assumptions,
        source_text=str(payload.get("source_text") or message or ""),
        semantic_tags=tags,
        rule_name=str(payload.get("rule_name") or "").strip().replace("-", "_"),
    )


def semantic_spec_from_message(message: str) -> SemanticScreenSpec | None:
    text = str(message or "").strip()
    if not text:
        return None
    conditions: list[dict[str, Any]] = []
    assumptions: list[str] = []
    tags: list[str] = []

    has_pullback = any(term in text for term in ("回踩", "回调不破", "缩量调整", "低吸"))
    has_trend = any(term in text for term in ("趋势较强", "强趋势", "多头趋势", "趋势向上", "多头排列"))
    if has_trend or has_pullback:
        tags.append("trend")
        _append_condition(
            conditions,
            {
                "id": "ma_bull_stack_5_10_20",
                "label": "MA5 > MA10 > MA20",
                "kind": "ma_stack",
                "windows": [5, 10, 20],
                "weight": 18,
                "required": True,
                "source": "user" if has_trend else "skill",
            },
        )
        _append_condition(
            conditions,
            {
                "id": "ma20_rising_5d",
                "label": "MA20 最近 5 个交易日保持上行",
                "kind": "ma_slope_gte",
                "window": 20,
                "lookback": 5,
                "min": 0.0,
                "weight": 14,
                "required": True,
                "source": "user" if has_trend else "skill",
            },
        )
    if has_pullback:
        tags.append("pullback")
        assumptions.extend(
            [
                "“关键均线”按 shrink-pullback 方法论解释为回踩 MA5 或 MA10，MA20 为趋势失效线。",
                "回踩观察窗口采用最近 5 个交易日，触及容差为 2%，有效跌破容差为 1%。",
            ]
        )
        _append_condition(
            conditions,
            {
                "id": "recent_pullback_near_ma5_ma10",
                "label": "最近 5 日回踩 MA5 或 MA10 后重新站上支撑均线",
                "kind": "recent_low_near_any_ma",
                "windows": [5, 10],
                "lookback": 5,
                "touch_tolerance": 0.02,
                "break_tolerance": 0.01,
                "require_reclaim": True,
                "weight": 24,
                "required": True,
                "source": "user",
            },
        )
        _append_condition(
            conditions,
            {
                "id": "recent_close_holds_ma20",
                "label": "最近 5 日收盘未有效跌破 MA20",
                "kind": "recent_close_not_below_ma",
                "window": 20,
                "lookback": 5,
                "tolerance": 0.01,
                "weight": 20,
                "required": True,
                "source": "user",
            },
        )
        _append_condition(
            conditions,
            {
                "id": "pullback_volume_not_expanding",
                "label": "最新成交量不高于前 5 日均量",
                "kind": "volume_ratio_lte",
                "window": 5,
                "max": 1.0,
                "weight": 8,
                "required": False,
                "source": "skill",
            },
        )
        _append_condition(
            conditions,
            {
                "id": "relative_strength_20d",
                "label": "近 20 日跑赢行业或指数",
                "kind": "relative_strength_gte",
                "window": 20,
                "min": 0.0,
                "weight": 8,
                "required": False,
                "source": "skill",
            },
        )

    if not conditions:
        plan = compose_rule_generation_plan(text)
        if not plan.ready:
            return None
        for raw in plan.conditions:
            condition = dict(raw)
            condition.setdefault("required", True)
            condition.setdefault("source", "user")
            if str(condition.get("kind") or "") in ALLOWED_CONDITION_KINDS:
                _append_condition(conditions, condition)
        dependencies = tuple(plan.data_dependencies)
    else:
        dependencies = ("日线 OHLCV", "stock_basic", "行业/指数对照日线")

    if not _has_core_condition(tuple(conditions)):
        return None
    required_window = _required_daily_rows(conditions)
    if required_window:
        conditions.insert(
            0,
            {
                "id": f"daily_rows_{required_window}",
                "label": f"至少 {required_window} 个交易日数据",
                "kind": "min_daily_rows",
                "min": required_window,
                "weight": 2,
                "required": True,
                "source": "system",
            },
        )
    return SemanticScreenSpec(
        goal=text,
        conditions=tuple(_normalize_condition(item) for item in conditions),
        data_dependencies=dependencies,
        assumptions=tuple(assumptions),
        source_text=text,
        semantic_tags=tuple(tags or ("custom",)),
    )


def evaluate_semantic_inputs(
    inputs: Iterable[ScreeningInput],
    spec: SemanticScreenSpec,
    *,
    near_miss_limit: int = 10,
) -> dict[str, Any]:
    items = list(inputs)
    runtime_spec = spec.to_runtime_spec()
    results: list[tuple[ScreeningInput, ScreeningResult]] = [
        (item, evaluate_generated_rule(item, rule_name=spec.generated_rule_name, spec=runtime_spec)) for item in items
    ]
    strict = [_screening_row(item, result) for item, result in results if result.passed]
    strict.sort(key=lambda row: (-float(row["score"]), row["ts_code"]))
    near = [_screening_row(item, result) for item, result in results if not result.passed]
    near.sort(
        key=lambda row: (
            int(row["data_issue"]),
            int(row["required_failed_count"]),
            -int(row["required_matched_count"]),
            -float(row["score"]),
            row["ts_code"],
        )
    )
    current_count = 0
    data_issue_count = 0
    sources: dict[str, int] = {}
    for item, result in results:
        latest = _latest_trade_date(item)
        if latest == str(item.trade_date):
            current_count += 1
        if _is_data_issue(result):
            data_issue_count += 1
        source = str(item.metadata.get("data_source") or "unknown")
        sources[source] = sources.get(source, 0) + 1
    return {
        "results": [result.to_dict() for _item, result in results],
        "strict_rows": strict,
        "near_misses": near[: max(1, int(near_miss_limit or 10))],
        "candidate_count": len(strict),
        "near_miss_count": len(near),
        "data_coverage": {
            "input_count": len(items),
            "current_trade_date_count": current_count,
            "data_issue_count": data_issue_count,
            "sources": sources,
        },
    }


def _normalize_condition(raw: dict[str, Any]) -> dict[str, Any]:
    condition = dict(raw)
    kind = str(condition.get("kind") or "").strip()
    if kind not in ALLOWED_CONDITION_KINDS:
        raise ValueError(f"unsupported semantic screening condition: {kind}")
    condition["kind"] = kind
    condition["id"] = str(condition.get("id") or kind)
    condition["label"] = str(condition.get("label") or condition["id"])
    condition["required"] = bool(condition.get("required", True))
    condition["source"] = str(condition.get("source") or "user")
    condition["weight"] = max(0.0, float(condition.get("weight") or 1.0))
    return condition


def _append_condition(conditions: list[dict[str, Any]], condition: dict[str, Any]) -> None:
    if not any(str(item.get("id")) == str(condition.get("id")) for item in conditions):
        conditions.append(condition)


def _has_core_condition(conditions: tuple[dict[str, Any], ...]) -> bool:
    guards = {"exclude_st", "exclude_bse", "min_daily_rows"}
    return any(str(item.get("kind") or "") not in guards for item in conditions)


def _required_daily_rows(conditions: list[dict[str, Any]]) -> int:
    required = 0
    for item in conditions:
        if "window" in item:
            required = max(required, int(item.get("window") or 0) + int(item.get("lookback") or 0))
        windows = item.get("windows")
        if isinstance(windows, list) and windows:
            required = max(required, max(int(value) for value in windows) + int(item.get("lookback") or 0))
    return required


def _screening_row(item: ScreeningInput, result: ScreeningResult) -> dict[str, Any]:
    details = result.metrics.get("condition_details") if isinstance(result.metrics, dict) else []
    details = details if isinstance(details, list) else []
    required_failed = [row for row in details if bool(row.get("required", True)) and not bool(row.get("passed"))]
    required_matched = [row for row in details if bool(row.get("required", True)) and bool(row.get("passed"))]
    soft_failed = [row for row in details if not bool(row.get("required", True)) and not bool(row.get("passed"))]
    return {
        "ts_code": str(item.ts_code),
        "name": str(item.stock_basic.get("name") or item.stock_basic.get("股票简称") or ""),
        "score": float(result.score),
        "passed": bool(result.passed),
        "required_matched_count": len(required_matched),
        "required_failed_count": len(required_failed),
        "matched_conditions": [str(row.get("label") or row.get("id") or "") for row in details if row.get("passed")],
        "failed_conditions": [str(row.get("label") or row.get("id") or "") for row in required_failed],
        "soft_failed_conditions": [str(row.get("label") or row.get("id") or "") for row in soft_failed],
        "condition_details": details,
        "latest_trade_date": _latest_trade_date(item),
        "data_source": str(item.metadata.get("data_source") or "unknown"),
        "data_issue": _is_data_issue(result),
    }


def _is_data_issue(result: ScreeningResult) -> bool:
    return any(str(item) in {"data_window", "daily_trade_date_current"} or str(item).startswith("daily_rows_") for item in result.failed_conditions)


def _latest_trade_date(item: ScreeningInput) -> str:
    frame = item.daily
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return ""
    return str(frame["trade_date"].astype(str).max())


def _safe_tag(value: Any) -> str:
    clean = "".join(char for char in str(value or "").lower().replace("-", "_") if char.isalnum() or char == "_")
    return clean[:24]
