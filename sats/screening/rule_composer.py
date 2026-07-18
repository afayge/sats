from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from pprint import pformat
from typing import Any, Iterable

import pandas as pd

from sats.screening.base import ScreeningInput, ScreeningRule


_RULE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_CONFIRM_RE = re.compile(r"^\s*确认生成规则\s+([a-zA-Z][a-zA-Z0-9_-]*)\s*$")
_EXPLICIT_RULE_NAME_RE = re.compile(r"(?:rule_name|规则名)\s*[:：=]?\s*([a-zA-Z][a-zA-Z0-9_-]*)")
_UNSUPPORTED_TERMS = {
    "新闻": "新闻/舆情数据当前不在 ScreeningInput 中，不能作为生成规则硬条件。",
    "舆情": "新闻/舆情数据当前不在 ScreeningInput 中，不能作为生成规则硬条件。",
    "龙虎榜": "龙虎榜当前不在 ScreeningInput 中，不能作为生成规则硬条件。",
    "筹码": "筹码/获利盘当前不在 ScreeningInput 中，不能作为生成规则硬条件。",
    "获利盘": "筹码/获利盘当前不在 ScreeningInput 中，不能作为生成规则硬条件。",
    "分钟": "分钟级数据当前不是 ScreeningRule 的通用输入，不能作为生成筛选规则硬条件。",
    "盘口": "实时盘口当前不是 ScreeningRule 的通用输入，不能作为生成筛选规则硬条件。",
    "资金流": "资金流当前不是 ScreeningInput 的稳定字段，不能作为生成规则硬条件。",
}


@dataclass(frozen=True, slots=True)
class RuleGenerationPlan:
    decision_name: str
    rule_name: str
    goal: str
    data_dependencies: tuple[str, ...]
    conditions: tuple[dict[str, Any], ...]
    pass_condition: str
    risk_notes: tuple[str, ...]
    questions: tuple[str, ...] = ()
    unsupported_requirements: tuple[str, ...] = ()
    source_text: str = ""

    @property
    def ready(self) -> bool:
        return not self.questions and bool(self.conditions)

    def to_spec(self) -> dict[str, Any]:
        return {
            "decision_name": self.decision_name,
            "rule_name": self.rule_name,
            "goal": self.goal,
            "data_dependencies": list(self.data_dependencies),
            "conditions": [dict(condition) for condition in self.conditions],
            "pass_condition": self.pass_condition,
            "risk_notes": list(self.risk_notes),
        }


@dataclass(frozen=True, slots=True)
class GeneratedRuleResult:
    rule_name: str
    class_name: str
    path: Path


def is_rule_generation_request(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    create_terms = ("新增", "增加", "生成", "创建", "新建", "设计", "加入")
    rule_terms = ("筛选规则", "选股规则", "筛选功能", "新决策", "screening rule")
    lowered = value.lower()
    return any(term in value for term in create_terms) and any(term in lowered for term in rule_terms)


def parse_rule_generation_confirmation(text: str) -> str | None:
    match = _CONFIRM_RE.match(str(text or ""))
    if not match:
        return None
    return _normalize_rule_name(match.group(1))


def compose_rule_generation_plan(
    description: str,
    *,
    existing_rule_names: Iterable[str] = (),
    ignore_unsupported: bool = False,
) -> RuleGenerationPlan:
    text = str(description or "").strip()
    existing = {_normalize_rule_name(name) for name in existing_rule_names}
    rule_name = _requested_or_default_rule_name(text)
    decision_name = _decision_name(text, rule_name)
    conditions = _conditions_from_text(text)
    unsupported = () if ignore_unsupported else _unsupported_requirements(text)
    questions: list[str] = []
    if not _RULE_NAME_RE.match(rule_name):
        questions.append("规则名只能包含英文字母、数字和下划线，并且必须以字母开头。")
    if rule_name in existing:
        questions.append(f"规则名 {rule_name} 已存在，请提供一个新的 rule_name。")
    if unsupported:
        questions.append("检测到当前 ScreeningInput 不支持的数据，请确认是否去掉这些条件或改成日线/daily_basic 可表达的条件。")
    if not _has_core_condition(conditions):
        questions.append("请至少补充一个可由日线、daily_basic、stock_basic 或相对强弱表达的筛选条件。")
    data_dependencies = _data_dependencies(conditions)
    return RuleGenerationPlan(
        decision_name=decision_name,
        rule_name=rule_name,
        goal=_goal(text, decision_name),
        data_dependencies=data_dependencies,
        conditions=tuple(conditions),
        pass_condition="全部条件满足时通过筛选；评分按条件权重折算为 0-100。",
        risk_notes=(
            "生成规则只用于研究筛选，不构成投资建议，也不会自动交易。",
            "缺少必需字段时对应条件会失败，不会编造数据。",
            "生成规则不会覆盖已有规则；如需调整，请生成新的 rule_name。",
        ),
        questions=tuple(questions),
        unsupported_requirements=unsupported,
        source_text=text,
    )


def compose_rule_generation_plan_from_spec(
    spec: dict[str, Any],
    *,
    existing_rule_names: Iterable[str] = (),
) -> RuleGenerationPlan:
    payload = dict(spec or {})
    conditions = tuple(dict(item) for item in payload.get("conditions") or () if isinstance(item, dict))
    if not conditions:
        raise ValueError("semantic screening spec has no conditions")
    rule_name = _normalize_rule_name(str(payload.get("rule_name") or ""))
    if not rule_name:
        digest = hashlib.sha256(
            repr(sorted((str(item.get("id") or ""), str(item.get("kind") or "")) for item in conditions)).encode("utf-8")
        ).hexdigest()[:6]
        tags = [str(item) for item in payload.get("semantic_tags") or () if str(item).strip()]
        slug = "_".join(tags[:3]) or "dynamic"
        rule_name = f"nl_{slug}_{digest}"
    questions: list[str] = []
    if not _RULE_NAME_RE.match(rule_name):
        questions.append("规则名只能包含英文字母、数字和下划线，并且必须以字母开头。")
    if rule_name in {_normalize_rule_name(name) for name in existing_rule_names}:
        questions.append(f"规则名 {rule_name} 已存在，请提供一个新的 rule_name。")
    return RuleGenerationPlan(
        decision_name=str(payload.get("decision_name") or "自然语义筛选"),
        rule_name=rule_name,
        goal=str(payload.get("goal") or "保存已验证的自然语义临时筛选规则"),
        data_dependencies=tuple(str(item) for item in payload.get("data_dependencies") or ("日线 OHLCV", "stock_basic")),
        conditions=conditions,
        pass_condition=str(payload.get("pass_condition") or "全部 required=true 的硬条件满足时通过；软条件只参与评分。"),
        risk_notes=tuple(str(item) for item in payload.get("risk_notes") or (
            "生成规则只用于研究筛选，不构成投资建议，也不会自动交易。",
            "缺少必需字段时对应条件会失败，不会编造数据。",
        )),
        questions=tuple(questions),
        source_text=str(payload.get("source_text") or ""),
    )


def revise_rule_generation_plan(
    plan: RuleGenerationPlan,
    answer: str,
    *,
    existing_rule_names: Iterable[str] = (),
) -> RuleGenerationPlan:
    text = f"{plan.source_text}\n用户补充：{answer}"
    ignore_unsupported = any(term in str(answer or "") for term in ("去掉", "忽略", "不需要", "不要", "降级"))
    return compose_rule_generation_plan(text, existing_rule_names=existing_rule_names, ignore_unsupported=ignore_unsupported)


def format_rule_generation_plan(plan: RuleGenerationPlan) -> str:
    lines = [
        "## 新筛选规则生成计划",
        f"- 决策名称: {plan.decision_name}",
        f"- rule_name: {plan.rule_name}",
        f"- 目标: {plan.goal}",
        f"- 数据依赖: {', '.join(plan.data_dependencies) if plan.data_dependencies else '待补充'}",
        "- 条件:",
    ]
    if plan.conditions:
        for index, condition in enumerate(plan.conditions, start=1):
            lines.append(f"  {index}. {condition.get('label', condition.get('id', 'condition'))}")
    else:
        lines.append("  1. 待补充")
    lines.extend(
        [
            f"- 通过条件: {plan.pass_condition}",
            "- 风险说明:",
        ]
    )
    for note in plan.risk_notes:
        lines.append(f"  - {note}")
    if plan.unsupported_requirements:
        lines.append("- 暂不支持的数据需求:")
        for item in plan.unsupported_requirements:
            lines.append(f"  - {item}")
    if plan.questions:
        lines.append("- 需要你确认:")
        for index, question in enumerate(plan.questions, start=1):
            lines.append(f"  {index}. {question}")
        lines.append("请直接回复补充答案，我会更新计划。")
    else:
        lines.append(f"如需生成代码，回复: 确认生成规则 {plan.rule_name}")
    return "\n".join(lines)


def format_generated_rule_result(result: GeneratedRuleResult) -> str:
    return "\n".join(
        [
            f"已生成筛选规则: {result.rule_name}",
            f"文件: {result.path}",
            f"类名: {result.class_name}",
            f"运行示例: sats screen --rule {result.rule_name} --trade-date YYYYMMDD",
        ]
    )


def generate_rule_code(plan: RuleGenerationPlan, *, generated_dir: Path | None = None) -> GeneratedRuleResult:
    if plan.questions:
        raise ValueError("规则计划仍有待确认问题，不能生成代码")
    if not _RULE_NAME_RE.match(plan.rule_name):
        raise ValueError(f"invalid rule name: {plan.rule_name}")
    from sats.screening.registry import list_rules

    if plan.rule_name in list_rules():
        raise ValueError(f"screening rule already exists: {plan.rule_name}")
    target_dir = generated_dir or default_generated_rules_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    init_file = target_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text('"""Generated SATS screening rules."""\n', encoding="utf-8")
    path = target_dir / f"{plan.rule_name}.py"
    if path.exists():
        raise ValueError(f"generated rule already exists: {plan.rule_name}")
    class_name = _class_name(plan.rule_name)
    content = _render_rule_code(plan, class_name)
    _validate_generated_ast(content)
    path.write_text(content, encoding="utf-8")
    try:
        _validate_generated_module(path, class_name, plan.rule_name)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    importlib.invalidate_caches()
    return GeneratedRuleResult(rule_name=plan.rule_name, class_name=class_name, path=path)


def default_generated_rules_dir() -> Path:
    return Path(__file__).resolve().parent / "rules" / "generated"


def _conditions_from_text(text: str) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    _add_condition(conditions, {"id": "not_st", "label": "排除 ST/退市风险股票", "kind": "exclude_st", "weight": 5})
    if any(term in text for term in ("排除北交", "不含北交", "非北交", "不选北交", "排除BJ", "排除 bj")):
        _add_condition(conditions, {"id": "not_bse", "label": "排除北交所股票", "kind": "exclude_bse", "weight": 5})
    if any(term in text for term in ("低位", "底部", "低吸")):
        _add_condition(
            conditions,
            {
                "id": "low_position_60d",
                "label": "收盘价位于 60 日价格区间低位 35% 以内",
                "kind": "range_position_lte",
                "window": 60,
                "max": 0.35,
                "weight": 15,
            },
        )
    if "放量" in text:
        minimum = 1.2 if "温和" in text else 1.5
        _add_condition(
            conditions,
            {
                "id": "volume_ratio_5d",
                "label": f"最新成交量相对前 5 日均量放大到 {minimum} 倍以上",
                "kind": "volume_ratio_gte",
                "window": 5,
                "min": minimum,
                "weight": 15,
            },
        )
    if "缩量" in text:
        _add_condition(
            conditions,
            {
                "id": "volume_ratio_shrink_5d",
                "label": "最新成交量低于前 5 日均量的 80%",
                "kind": "volume_ratio_lte",
                "window": 5,
                "max": 0.8,
                "weight": 10,
            },
        )
    if "突破" in text:
        _add_condition(
            conditions,
            {
                "id": "breakout_20d_high",
                "label": "收盘价突破前 20 日高点",
                "kind": "breakout_high",
                "window": 20,
                "tolerance": 0.0,
                "weight": 20,
            },
        )
    if any(term in text for term in ("多头排列", "均线多头")):
        _add_condition(
            conditions,
            {
                "id": "ma_bull_stack_5_10_20_60",
                "label": "MA5 > MA10 > MA20 > MA60 多头排列",
                "kind": "ma_stack",
                "windows": [5, 10, 20, 60],
                "weight": 15,
            },
        )
    for window in _mentioned_ma_windows(text):
        _add_condition(
            conditions,
            {
                "id": f"close_above_ma{window}",
                "label": f"收盘价站上 MA{window}",
                "kind": "close_above_ma",
                "window": window,
                "weight": 10,
            },
        )
    pct_between = _range_after_keyword(text, "涨幅")
    if pct_between is not None:
        _add_condition(
            conditions,
            {
                "id": "pct_chg_between",
                "label": f"最新涨跌幅在 {pct_between[0]}% 到 {pct_between[1]}% 之间",
                "kind": "pct_chg_between",
                "min": pct_between[0],
                "max": pct_between[1],
                "weight": 10,
            },
        )
    turnover_between = _range_after_keyword(text, "换手")
    if turnover_between is not None:
        _add_condition(
            conditions,
            {
                "id": "turnover_between",
                "label": f"换手率在 {turnover_between[0]}% 到 {turnover_between[1]}% 之间",
                "kind": "turnover_between",
                "min": turnover_between[0],
                "max": turnover_between[1],
                "weight": 10,
            },
        )
    mv_range = _market_value_range(text)
    if mv_range is not None:
        _add_condition(
            conditions,
            {
                "id": "circ_mv_between",
                "label": f"流通市值在 {mv_range[0] / 10000:.0f} 亿到 {mv_range[1] / 10000:.0f} 亿之间",
                "kind": "circ_mv_between",
                "min": mv_range[0],
                "max": mv_range[1],
                "weight": 10,
            },
        )
    pe_max = _max_after_keyword(text, "PE")
    if pe_max is not None:
        _add_condition(
            conditions,
            {"id": "pe_ttm_lte", "label": f"PE_TTM 小于等于 {pe_max}", "kind": "daily_basic_max", "column": "pe_ttm", "max": pe_max, "weight": 8},
        )
    pb_max = _max_after_keyword(text, "PB")
    if pb_max is not None:
        _add_condition(
            conditions,
            {"id": "pb_lte", "label": f"PB 小于等于 {pb_max}", "kind": "daily_basic_max", "column": "pb", "max": pb_max, "weight": 8},
        )
    roe_min = _min_after_keyword(text, "ROE")
    if roe_min is not None:
        _add_condition(
            conditions,
            {"id": "roe_gte", "label": f"ROE 大于等于 {roe_min}", "kind": "daily_basic_min", "column": "roe", "min": roe_min, "weight": 8},
        )
    if any(term in text for term in ("相对强", "跑赢行业", "强于行业", "跑赢指数")):
        _add_condition(
            conditions,
            {
                "id": "relative_strength_20d",
                "label": "近 20 日涨幅跑赢行业或指数 0% 以上",
                "kind": "relative_strength_gte",
                "window": 20,
                "min": 0.0,
                "weight": 12,
            },
        )
    max_window = _max_required_window(conditions)
    if max_window > 0:
        _add_condition(
            conditions,
            {"id": f"daily_rows_{max_window}", "label": f"至少 {max_window} 个交易日数据", "kind": "min_daily_rows", "min": max_window, "weight": 3},
            prepend=True,
        )
    return conditions


def _add_condition(conditions: list[dict[str, Any]], condition: dict[str, Any], *, prepend: bool = False) -> None:
    if any(item.get("id") == condition.get("id") for item in conditions):
        return
    if prepend:
        conditions.insert(0, condition)
    else:
        conditions.append(condition)


def _has_core_condition(conditions: list[dict[str, Any]]) -> bool:
    guard_kinds = {"exclude_st", "exclude_bse", "min_daily_rows"}
    return any(condition.get("kind") not in guard_kinds for condition in conditions)


def _requested_or_default_rule_name(text: str) -> str:
    matches = _EXPLICIT_RULE_NAME_RE.findall(text)
    if matches:
        return _normalize_rule_name(matches[-1])
    tokens: list[str] = []
    mapping = [
        (("低位", "底部", "低吸"), "low"),
        (("放量",), "volume"),
        (("缩量",), "shrink"),
        (("突破",), "breakout"),
        (("多头排列", "均线多头", "均线"), "ma"),
        (("换手",), "turnover"),
        (("市值",), "mv"),
        (("相对强", "跑赢行业", "跑赢指数"), "rs"),
        (("估值", "PE", "PB", "ROE"), "value"),
    ]
    for terms, token in mapping:
        if any(term in text for term in terms) and token not in tokens:
            tokens.append(token)
    if not tokens:
        tokens.append("custom_screening")
    return "nl_" + "_".join(tokens[:5])


def _normalize_rule_name(value: str) -> str:
    return str(value or "").strip().replace("-", "_")


def _decision_name(text: str, rule_name: str) -> str:
    if all(term in text for term in ("低位", "放量", "突破")):
        return "低位放量突破筛选"
    if "放量" in text and "突破" in text:
        return "放量突破筛选"
    return rule_name.removeprefix("nl_").replace("_", " ").title().replace(" ", "") + "筛选"


def _goal(text: str, decision_name: str) -> str:
    source = text.replace("\n", " ").strip()
    if len(source) > 80:
        source = source[:77] + "..."
    return f"根据用户描述“{source}”生成 {decision_name}，用于全市场研究筛选。"


def _unsupported_requirements(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for term, message in _UNSUPPORTED_TERMS.items():
        if term in text and message not in found:
            found.append(message)
    return tuple(found)


def _data_dependencies(conditions: list[dict[str, Any]]) -> tuple[str, ...]:
    dependencies = ["日线 OHLCV"]
    if any(condition.get("kind") in {"turnover_between", "circ_mv_between", "daily_basic_max", "daily_basic_min"} for condition in conditions):
        dependencies.append("Tushare daily_basic")
    if any(condition.get("kind") == "exclude_st" for condition in conditions):
        dependencies.append("stock_basic")
    if any(condition.get("kind") == "relative_strength_gte" for condition in conditions):
        dependencies.append("行业/指数对照日线")
    return tuple(dependencies)


def _mentioned_ma_windows(text: str) -> list[int]:
    windows: list[int] = []
    for match in re.finditer(r"(?:MA|ma|均线)\s*(5|10|20|30|60|120)", text):
        value = int(match.group(1))
        if value not in windows:
            windows.append(value)
    return windows


def _range_after_keyword(text: str, keyword: str) -> tuple[float, float] | None:
    pattern = rf"{re.escape(keyword)}[^0-9]{{0,8}}(\d+(?:\.\d+)?)\s*(?:-|~|到|至)\s*(\d+(?:\.\d+)?)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    lower = float(match.group(1))
    upper = float(match.group(2))
    return (min(lower, upper), max(lower, upper))


def _market_value_range(text: str) -> tuple[float, float] | None:
    match = re.search(r"市值[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:-|~|到|至)\s*(\d+(?:\.\d+)?)\s*亿", text)
    if not match:
        return None
    lower = float(match.group(1)) * 10000.0
    upper = float(match.group(2)) * 10000.0
    return (min(lower, upper), max(lower, upper))


def _max_after_keyword(text: str, keyword: str) -> float | None:
    pattern = rf"{re.escape(keyword)}[^0-9]{{0,8}}(?:小于|低于|<=|<|不超过|不高于)\s*(\d+(?:\.\d+)?)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _min_after_keyword(text: str, keyword: str) -> float | None:
    pattern = rf"{re.escape(keyword)}[^0-9]{{0,8}}(?:大于|高于|>=|>|不低于)\s*(\d+(?:\.\d+)?)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _max_required_window(conditions: list[dict[str, Any]]) -> int:
    result = 0
    for condition in conditions:
        if "window" in condition:
            result = max(result, int(condition["window"]))
        if condition.get("kind") == "ma_stack":
            result = max(result, max(int(item) for item in condition.get("windows", [])))
    return result


def _class_name(rule_name: str) -> str:
    return "".join(part.capitalize() for part in rule_name.split("_") if part) + "Rule"


def _render_rule_code(plan: RuleGenerationPlan, class_name: str) -> str:
    spec = plan.to_spec()
    spec["conditions"] = [dict(condition) for condition in plan.conditions]
    return (
        "from __future__ import annotations\n\n"
        "from sats.screening.base import ScreeningInput, ScreeningResult, ScreeningRule\n"
        "from sats.screening.generated_rule_runtime import evaluate_generated_rule\n\n"
        f"_RULE_SPEC = {pformat(spec, width=100, sort_dicts=False)}\n\n\n"
        f"class {class_name}(ScreeningRule):\n"
        f"    name = {plan.rule_name!r}\n\n"
        "    def evaluate(self, data: ScreeningInput) -> ScreeningResult:\n"
        "        return evaluate_generated_rule(data, rule_name=self.name, spec=_RULE_SPEC)\n"
    )


def _validate_generated_ast(content: str) -> None:
    tree = ast.parse(content)
    allowed_imports = {
        "__future__",
        "sats.screening.base",
        "sats.screening.generated_rule_runtime",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            raise ValueError("generated rules may not use import statements")
        if isinstance(node, ast.ImportFrom) and (node.module or "") not in allowed_imports:
            raise ValueError(f"generated rule import is not allowed: {node.module}")


def _validate_generated_module(path: Path, class_name: str, rule_name: str) -> None:
    module_name = f"sats.screening.rules.generated.{rule_name}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"failed to load generated rule module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    rule_cls = getattr(module, class_name, None)
    if not isinstance(rule_cls, type) or not issubclass(rule_cls, ScreeningRule):
        raise ValueError("generated rule class must inherit ScreeningRule")
    rule = rule_cls()
    if rule.name != rule_name:
        raise ValueError("generated rule class name does not match rule_name")
    result = rule.evaluate(_synthetic_input())
    if result.rule_name != rule_name:
        raise ValueError("generated rule returned an unexpected rule_name")


def _synthetic_input() -> ScreeningInput:
    dates = pd.bdate_range(end="2026-04-30", periods=80).strftime("%Y%m%d").tolist()
    rows = []
    for index, trade_date in enumerate(dates):
        close = 10.0 + index * 0.03
        volume = 1000.0
        if index == len(dates) - 1:
            close = max(close, max(row["high"] for row in rows[-20:]) + 0.2)
            volume = 1800.0
        rows.append(
            {
                "trade_date": trade_date,
                "open": close - 0.05,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "vol": volume,
                "pct_chg": 2.0,
            }
        )
    daily = pd.DataFrame(rows)
    daily_basic = pd.DataFrame(
        {
            "trade_date": dates,
            "turnover_rate": [6.0] * len(dates),
            "circ_mv": [1_000_000.0] * len(dates),
            "pe_ttm": [20.0] * len(dates),
            "pb": [2.0] * len(dates),
            "roe": [12.0] * len(dates),
        }
    )
    benchmark = pd.DataFrame({"trade_date": dates, "close": [10.0 + index * 0.01 for index in range(len(dates))]})
    return ScreeningInput(
        ts_code="000001.SZ",
        trade_date=dates[-1],
        daily=daily,
        daily_basic=daily_basic,
        stock_basic={"name": "测试股份"},
        industry_daily=benchmark,
    )
