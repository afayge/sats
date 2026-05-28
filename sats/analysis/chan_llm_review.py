from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sats.llm import ChatLLM, extract_json_object
from sats.rag.chan_knowledge import search_chan_knowledge
from sats.screening.base import ScreeningResult
from sats.storage.duckdb import DuckDBStorage

CHAN_THIRD_BUY_RULE_NAME = "chan_third_buy"
CHAN_COMPOSITE_RULE_NAME = "chan_composite"
CHAN_SIGNALS_RULE_NAME = "chan_signals"
DEFAULT_CHAN_RULE_NAME = CHAN_SIGNALS_RULE_NAME

_CHAN_KNOWLEDGE_RULE_IDS = {
    CHAN_THIRD_BUY_RULE_NAME: ("chan_third_buy",),
    CHAN_COMPOSITE_RULE_NAME: (
        "chan_first_buy",
        "chan_second_buy",
        "chan_third_buy",
        "chan_second_third_overlap",
        "chan_center_oscillation_low",
    ),
    CHAN_SIGNALS_RULE_NAME: (
        "chan_first_buy",
        "chan_second_buy",
        "chan_third_buy",
        "chan_first_sell",
        "chan_second_sell",
        "chan_third_sell",
        "chan_second_third_overlap",
        "chan_center_oscillation_low",
        "chan_center_oscillation_high",
        "chan_bottom_fractal_confirm",
        "chan_top_fractal_confirm",
    ),
}


@dataclass(frozen=True, slots=True)
class ChanLLMReviewResult:
    reviewed_codes: list[str]
    reviews: list[dict[str, Any]]
    report_path: Path | None
    message: str = ""


def run_chan_llm_review(
    *,
    storage: DuckDBStorage,
    trade_date: str,
    reports_dir: Path,
    top: int = 20,
    screening_rule_name: str | None = None,
    chan_rule_name: str = DEFAULT_CHAN_RULE_NAME,
    screening_results: list[ScreeningResult] | None = None,
    names: dict[str, str] | None = None,
    stock_contexts: dict[str, dict[str, Any]] | None = None,
    llm_factory: Callable[[], Any] = ChatLLM,
) -> ChanLLMReviewResult:
    if screening_results is None:
        rows = storage.list_screening_results(
            trade_date=trade_date,
            rule_name=screening_rule_name,
            passed=True,
        )[:top]
        names = {
            row["ts_code"]: row.get("name", "")
            for row in storage.list_screening_stocks(
                trade_date=trade_date,
                rule_name=screening_rule_name,
                passed=True,
            )
        }
    else:
        rows = [_screening_result_row(row) for row in screening_results][:top]
        names = names or {}
    if not rows:
        return ChanLLMReviewResult([], [], None, message=f"无{_rule_title(chan_rule_name)}候选股票")

    stock_contexts = stock_contexts or {}
    candidates = []
    for row in rows:
        ts_code = str(row["ts_code"])
        context = stock_contexts.get(ts_code, {})
        data_sources = _candidate_data_sources(row["metrics"])
        if isinstance(context.get("data_sources"), dict):
            data_sources.update(context["data_sources"])
        missing_fields = _candidate_missing_fields(row["metrics"])
        if isinstance(context.get("missing_fields"), list):
            missing_fields = sorted({*missing_fields, *[str(value) for value in context["missing_fields"]]})
        candidate = {
            "ts_code": row["ts_code"],
            "name": names.get(row["ts_code"], ""),
            "trade_date": row.get("trade_date", trade_date),
            "screening_rule_name": row.get("rule_name", screening_rule_name or ""),
            "chan_rule_name": chan_rule_name,
            "passed": bool(row.get("passed")),
            "score": row["score"],
            "matched_conditions": row.get("matched_conditions", []),
            "failed_conditions": row.get("failed_conditions", []),
            "metrics": row["metrics"],
            "data_sources": data_sources,
            "missing_fields": missing_fields,
            "rag_evidence": _candidate_evidence(chan_rule_name, row["metrics"]),
        }
        if context:
            candidate.update(
                {
                    "price_context": context.get("price_context", {}),
                    "daily_tail": context.get("daily_tail", []),
                    "minute_curves": context.get("minute_curves", {}),
                }
            )
        candidates.append(candidate)
    llm = llm_factory()
    response = llm.chat(_messages(candidates, chan_rule_name=chan_rule_name))
    parsed = extract_json_object(response.content or "") or {}
    reviews = parsed.get("reviews") if isinstance(parsed.get("reviews"), list) else []
    enriched_reviews = _enrich_reviews(candidates, reviews)

    report_path = _write_report(
        reports_dir=reports_dir,
        trade_date=trade_date,
        screening_rule_name=screening_rule_name,
        chan_rule_name=chan_rule_name,
        candidates=candidates,
        reviews=enriched_reviews,
        raw_content=response.content or "",
    )
    return ChanLLMReviewResult(
        reviewed_codes=[str(item["ts_code"]) for item in candidates],
        reviews=enriched_reviews,
        report_path=report_path,
    )


def _messages(candidates: list[dict[str, Any]], *, chan_rule_name: str) -> list[dict[str, str]]:
    payload = json.dumps(candidates, ensure_ascii=False, default=str)
    return [
        {
            "role": "system",
            "content": (
                "你是A股缠论选股复核助手。只根据用户给出的 trade_date、data_sources、结构化指标和RAG规则依据判断，"
                "不得编造价格、涨跌幅、成交量、新闻、公告、题材或基本面。字段缺失时必须在 risk_flags 中说明。"
                "不要改变硬筛选结果。输出必须是JSON对象。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"请按{_rule_title(chan_rule_name)}语境复核这些筛选结果。"
                "返回JSON：{\"reviews\":[{\"ts_code\":\"...\","
                "\"signal_quality\":\"高|中|低\","
                "\"buy_point_quality\":\"高|中|低\","
                "\"risk_flags\":[\"...\"],\"watch_levels\":{\"support\":0,\"invalid\":0},"
                "\"source_refs\":[\"rule_id page\"],\"summary\":\"...\"}]}。\n"
                f"候选数据：{payload}"
            ),
        },
    ]


def _write_report(
    *,
    reports_dir: Path,
    trade_date: str,
    screening_rule_name: str | None,
    chan_rule_name: str,
    candidates: list[dict[str, Any]],
    reviews: list[Any],
    raw_content: str,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screening_label = _safe_label(screening_rule_name or "all_screened")
    chan_label = _safe_label(chan_rule_name)
    path = reports_dir / f"chan_llm_review_{trade_date}_{screening_label}_as_{chan_label}_{timestamp}.md"
    review_lookup = {
        str(item.get("ts_code")): item
        for item in reviews
        if isinstance(item, dict) and item.get("ts_code")
    }
    lines = [
        f"# {_rule_title(chan_rule_name)}LLM复核 {trade_date}",
        "",
        "本报告只基于 SATS 硬筛选指标生成，不构成投资建议。",
        "",
        "| 股票 | 评分 | 通过 | 信号质量 | 风险 | 观察位 | 来源 | 摘要 |",
        "| --- | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for item in candidates:
        ts_code = str(item["ts_code"])
        review = review_lookup.get(ts_code, {})
        risk_flags = review.get("risk_flags", [])
        if isinstance(risk_flags, list):
            risk_text = "；".join(str(value) for value in risk_flags)
        else:
            risk_text = str(risk_flags or "")
        watch_levels = review.get("watch_levels", {})
        watch_text = json.dumps(watch_levels, ensure_ascii=False, default=str) if watch_levels else ""
        source_refs = review.get("source_refs") or _default_source_refs(item)
        if isinstance(source_refs, list):
            source_text = "；".join(str(value) for value in source_refs)
        else:
            source_text = str(source_refs or "")
        name = str(item.get("name") or "")
        label = f"{ts_code} {name}".strip()
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(label),
                    _cell(item.get("score")),
                    _cell("是" if item.get("passed") else "否"),
                    _cell(review.get("signal_quality") or review.get("buy_point_quality", "")),
                    _cell(risk_text),
                    _cell(watch_text),
                    _cell(source_text),
                    _cell(review.get("summary", "")),
                ]
            )
            + " |"
        )
    if not review_lookup:
        lines.extend(["", "## Raw LLM Output", "", raw_content or "无"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _screening_result_row(row: ScreeningResult) -> dict[str, Any]:
    return {
        "trade_date": row.trade_date,
        "ts_code": row.ts_code,
        "rule_name": row.rule_name,
        "passed": row.passed,
        "score": row.score,
        "matched_conditions": row.matched_conditions,
        "failed_conditions": row.failed_conditions,
        "metrics": row.metrics,
    }


def _enrich_reviews(candidates: list[dict[str, Any]], reviews: list[Any]) -> list[dict[str, Any]]:
    candidate_by_code = {str(item["ts_code"]): item for item in candidates}
    enriched = []
    for index, item in enumerate(reviews):
        if not isinstance(item, dict):
            continue
        review = dict(item)
        ts_code = str(review.get("ts_code") or "")
        candidate = candidate_by_code.get(ts_code)
        if candidate is None and index < len(candidates):
            candidate = candidates[index]
            ts_code = str(candidate["ts_code"])
            review.setdefault("ts_code", ts_code)
        if candidate is not None:
            review.setdefault("name", candidate.get("name", ""))
            review.setdefault("screening_rule_name", candidate.get("screening_rule_name", ""))
            review.setdefault("chan_rule_name", candidate.get("chan_rule_name", ""))
        enriched.append(review)
    return enriched


def _candidate_data_sources(metrics: dict[str, Any]) -> dict[str, Any]:
    sources = {}
    nested = metrics.get("data_sources") if isinstance(metrics, dict) else None
    if isinstance(nested, dict):
        sources.update({str(key): value for key, value in nested.items() if value})
    for key in ("data_source", "daily_source", "daily_basic_source", "minute_30m_source", "stock_basic_source"):
        value = metrics.get(key) if isinstance(metrics, dict) else None
        if value:
            sources[key] = value
    return sources


def _candidate_missing_fields(metrics: dict[str, Any]) -> list[str]:
    missing = []
    if not metrics:
        missing.append("metrics")
    if not _candidate_data_sources(metrics):
        missing.append("data_sources")
    return missing


def _cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")


def _candidate_evidence(chan_rule_name: str, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    rule_ids = []
    for key in ("matched_chan_rule_names", "chan_signal_labels", "matched_conditions"):
        values = metrics.get(key, [])
        if isinstance(values, list):
            rule_ids.extend(
                str(value)
                for value in values
                if str(value) in _all_chan_knowledge_rule_ids()
            )
    if not rule_ids:
        rule_ids.extend(_CHAN_KNOWLEDGE_RULE_IDS.get(chan_rule_name, ()))
    labels = metrics.get("matched_chan_rules", [])
    label_text = " ".join(str(value) for value in labels) if isinstance(labels, list) else str(labels or "")
    query = " ".join([chan_rule_name, _rule_title(chan_rule_name), label_text, " ".join(rule_ids)])
    return search_chan_knowledge(query, rule_ids=rule_ids or None, limit=6)


def _default_source_refs(candidate: dict[str, Any]) -> list[str]:
    refs = []
    for item in candidate.get("rag_evidence", []) or []:
        if isinstance(item, dict):
            pages = ",".join(str(page) for page in item.get("source_pages", []))
            refs.append(f"{item.get('rule_id')} p{pages}")
    return refs


def _rule_title(rule_name: str) -> str:
    return {
        "chan_third_buy": "缠论三买",
        "chan_composite": "综合缠论",
        "chan_signals": "缠论买卖点",
    }.get(rule_name, str(rule_name))


def _all_chan_knowledge_rule_ids() -> set[str]:
    ids: set[str] = set()
    for values in _CHAN_KNOWLEDGE_RULE_IDS.values():
        ids.update(values)
    return ids


def _safe_label(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value or "unknown"))
