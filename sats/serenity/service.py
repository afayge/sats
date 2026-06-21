from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from sats.analysis.stock_picking_agent import resolve_theme_universe
from sats.config import Settings, load_settings
from sats.data.astock_provider import AStockDataProvider
from sats.indicators import IndicatorCalculator, IndicatorInput
from sats.llm import ChatLLM
from sats.screening.base import ScreeningInput, ScreeningResult
from sats.serenity.models import (
    SerenityCandidateResult,
    SerenityScreenRequest,
    SerenityScreenResult,
)
from sats.serenity.scoring import (
    preliminary_serenity_score,
    rank_serenity_candidates,
    score_serenity_candidate,
)
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols, parse_symbol_csv


SERENITY_RULE_NAME = "serenity_bottleneck"
DEFAULT_THEME = "AI 产业链"
SUPPORTED_MARKETS = (".SH", ".SZ", ".BJ")


def run_serenity_screen(
    *,
    query: str = "",
    theme: str = "",
    symbols: list[str] | tuple[str, ...] | str | None = None,
    trade_date: str,
    limit: int = 10,
    candidate_limit: int = 30,
    lookback_days: int = 180,
    llm_review: bool = True,
    report: bool = True,
    settings: Settings | None = None,
    storage: DuckDBStorage | None = None,
    astock_provider: Any | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
    llm: Any | None = None,
    llm_timeout_seconds: int = 45,
    reports_dir: Path | None = None,
    progress: Any | None = None,
) -> SerenityScreenResult:
    settings = settings or load_settings()
    storage = storage or DuckDBStorage(settings.db_path)
    provider = astock_provider or AStockDataProvider(settings)
    clean_symbols = _normalize_symbol_input(symbols)
    supported, rejected = _supported_common_a_symbols(
        clean_symbols,
        provider=provider,
        storage=storage,
    )
    if rejected:
        raise ValueError(f"serenity-screen v1 仅支持 A 股普通股票，不支持: {', '.join(rejected)}")

    clean_limit = max(1, min(int(limit or 10), 50))
    clean_candidate_limit = max(clean_limit, min(int(candidate_limit or 30), 100))
    clean_theme = str(theme or "").strip() or _theme_from_query(query) or DEFAULT_THEME
    request = SerenityScreenRequest(
        query=str(query or "").strip(),
        theme=clean_theme,
        symbols=tuple(supported),
        trade_date=str(trade_date),
        limit=clean_limit,
        candidate_limit=clean_candidate_limit,
        lookback_days=max(30, int(lookback_days or 180)),
        llm_review=bool(llm_review),
        report=bool(report),
    )

    universe, candidate_source, warnings = _resolve_candidate_universe(
        request,
        provider=provider,
        storage=storage,
        settings=settings,
        llm_factory=llm_factory if request.llm_review else None,
        progress=progress,
    )
    if not universe:
        return SerenityScreenResult(
            request=request,
            candidate_source=candidate_source,
            warnings=tuple(warnings),
            message="无可分析的 Serenity 候选股票",
        )

    shortlisted = _preliminary_shortlist(
        universe,
        request=request,
        provider=provider,
        storage=storage,
        progress=progress,
    )
    if not shortlisted:
        return SerenityScreenResult(
            request=request,
            candidate_source=candidate_source,
            universe_count=len(universe),
            warnings=tuple(warnings),
            message="候选池没有通过基础数据校验",
        )

    scored = _enhance_and_score(
        shortlisted,
        request=request,
        provider=provider,
        storage=storage,
        progress=progress,
    )
    if request.llm_review and scored:
        scored = _attach_llm_reviews(
            scored,
            llm=llm,
            llm_factory=llm_factory,
            timeout_seconds=llm_timeout_seconds,
            progress=progress,
        )
    _store_screening_results(storage, scored)

    ranked = rank_serenity_candidates(scored, limit=request.limit)
    result = SerenityScreenResult(
        request=request,
        candidates=ranked,
        candidate_source=candidate_source,
        universe_count=len(universe),
        shortlisted_count=len(shortlisted),
        passed_count=sum(1 for item in scored if item.passed),
        layer_priorities=_layer_priorities(scored),
        warnings=tuple(warnings),
    )
    if report:
        result = write_serenity_artifacts(
            result,
            reports_dir=reports_dir or Path(settings.project_root) / "reports" / "serenity",
        )
    return result


def write_serenity_artifacts(
    result: SerenityScreenResult,
    *,
    reports_dir: Path,
) -> SerenityScreenResult:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    base = f"serenity_screen_{result.request.trade_date}_{stamp}"
    markdown_path = reports_dir / f"{base}.md"
    json_path = reports_dir / f"{base}.json"
    updated = replace(result, markdown_report_path=markdown_path, json_artifact_path=json_path)
    markdown_path.write_text(updated.to_markdown(), encoding="utf-8")
    json_path.write_text(
        json.dumps(updated.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return updated


def _resolve_candidate_universe(
    request: SerenityScreenRequest,
    *,
    provider: Any,
    storage: DuckDBStorage,
    settings: Settings,
    llm_factory: Callable[..., Any] | None,
    progress: Any | None,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    if request.symbols:
        rows = _explicit_candidate_rows(
            list(request.symbols),
            provider=provider,
            storage=storage,
        )
        return rows, "explicit_symbols", []

    warnings: list[str] = []

    def load() -> list[dict[str, Any]]:
        try:
            universe = resolve_theme_universe(
                f"{request.theme} 相关股票",
                provider,
                storage,
                llm_factory,
                settings=settings,
                trade_date=request.trade_date,
                llm_enabled=llm_factory is not None,
                max_symbols=min(max(request.candidate_limit * 3, 30), 100),
            )
        except Exception as exc:
            warnings.append(f"theme_universe: {type(exc).__name__}: {exc}")
            return []
        warnings.extend(str(item) for item in (getattr(universe, "warnings", ()) or ()))
        rows = []
        for stock in getattr(universe, "stocks", ()) or ():
            ts_code = str(getattr(stock, "ts_code", "") or "").strip()
            if not _is_supported_symbol(ts_code):
                continue
            rows.append(
                {
                    "ts_code": ts_code,
                    "name": str(getattr(stock, "name", "") or ""),
                    "relation_reason": str(getattr(stock, "reason", "") or ""),
                    "candidate_source": str(getattr(stock, "source", "") or getattr(universe, "source", "")),
                }
            )
        if not rows:
            rows = _local_theme_candidates(
                request.theme,
                provider=provider,
                storage=storage,
                limit=min(max(request.candidate_limit * 3, 30), 100),
            )
        candidate_source = str(getattr(universe, "source", "") or "local_stock_basic")
        for row in rows:
            row.setdefault("candidate_source", candidate_source)
        return _dedupe_candidates(rows)

    if progress is None:
        rows = load()
    else:
        with progress.step("Serenity 主题候选池") as step:
            rows = load()
            step.complete(message=f"{len(rows)} 只")
    source = rows[0].get("candidate_source", "theme_universe") if rows else "theme_universe"
    return rows, str(source or "theme_universe"), warnings


def _explicit_candidate_rows(
    symbols: list[str],
    *,
    provider: Any,
    storage: DuckDBStorage,
) -> list[dict[str, Any]]:
    basic = _safe_frame(lambda: provider.load_stock_basic(storage=storage))
    names = {}
    industries = {}
    if not basic.empty and "ts_code" in basic.columns:
        for _, row in basic.iterrows():
            ts_code = str(row.get("ts_code") or "")
            names[ts_code] = str(row.get("name") or "")
            industries[ts_code] = str(row.get("industry") or "")
    return [
        {
            "ts_code": symbol,
            "name": names.get(symbol, ""),
            "industry": industries.get(symbol, ""),
            "candidate_source": "explicit_symbols",
        }
        for symbol in symbols
    ]


def _local_theme_candidates(
    theme: str,
    *,
    provider: Any,
    storage: DuckDBStorage,
    limit: int,
) -> list[dict[str, Any]]:
    basic = _safe_frame(lambda: provider.load_stock_basic(storage=storage))
    if basic.empty:
        return []
    terms = _theme_terms(theme)
    rows: list[dict[str, Any]] = []
    for _, row in basic.iterrows():
        ts_code = str(row.get("ts_code") or "")
        text = " ".join(str(row.get(key) or "") for key in ("name", "industry", "market")).lower()
        hits = [term for term in terms if term in text]
        if not hits or not _is_supported_symbol(ts_code):
            continue
        rows.append(
            {
                "ts_code": ts_code,
                "name": str(row.get("name") or ""),
                "industry": str(row.get("industry") or ""),
                "relation_reason": f"stock_basic 命中主题词: {', '.join(hits[:4])}",
                "candidate_source": "local_stock_basic",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _preliminary_shortlist(
    universe: list[dict[str, Any]],
    *,
    request: SerenityScreenRequest,
    provider: Any,
    storage: DuckDBStorage,
    progress: Any | None,
) -> list[dict[str, Any]]:
    symbols = [str(item.get("ts_code") or "") for item in universe]
    relation_lookup = {str(item.get("ts_code") or ""): dict(item) for item in universe}

    def load_inputs() -> list[ScreeningInput]:
        return _safe_list(
            lambda: provider.load_screening_inputs(
                symbols,
                request.trade_date,
                storage=storage,
                trade_days=min(max(request.lookback_days // 3, 60), 121),
                rule_name=None,
            )
        )

    if progress is None:
        inputs = load_inputs()
    else:
        with progress.step("Serenity 本地初筛") as step:
            inputs = load_inputs()
            step.complete(message=f"{len(inputs)} 只")

    by_symbol = {item.ts_code: item for item in inputs}
    candidates: list[dict[str, Any]] = []
    for symbol in symbols:
        relation = relation_lookup[symbol]
        item = by_symbol.get(symbol)
        if item is None:
            payload = dict(relation)
        else:
            payload = _preliminary_payload(item, relation=relation, theme=request.theme)
        payload["preliminary_score"] = preliminary_serenity_score(payload)
        candidates.append(payload)
    candidates.sort(
        key=lambda item: (-float(item.get("preliminary_score") or 0), str(item.get("ts_code") or ""))
    )
    return candidates[: request.candidate_limit]


def _preliminary_payload(
    item: ScreeningInput,
    *,
    relation: dict[str, Any],
    theme: str,
) -> dict[str, Any]:
    latest_daily = _latest_row(item.daily)
    latest_basic = _latest_row(item.daily_basic)
    stock_basic = dict(item.stock_basic or {})
    return {
        **relation,
        "ts_code": item.ts_code,
        "name": str(stock_basic.get("name") or relation.get("name") or ""),
        "industry": str(stock_basic.get("industry") or relation.get("industry") or ""),
        "theme": theme,
        "stock_basic": stock_basic,
        "market_cap_yi": _market_cap_yi(latest_basic),
        "amount": _num(latest_daily.get("amount")),
        "turnover_rate": _num(latest_basic.get("turnover_rate")),
        "data_sources": {
            "preliminary": str(item.metadata.get("data_source") or "AStockDataProvider.load_screening_inputs")
        },
    }


def _enhance_and_score(
    shortlist: list[dict[str, Any]],
    *,
    request: SerenityScreenRequest,
    provider: Any,
    storage: DuckDBStorage,
    progress: Any | None,
) -> list[SerenityCandidateResult]:
    symbols = [str(item.get("ts_code") or "") for item in shortlist]
    relation_lookup = {str(item.get("ts_code") or ""): item for item in shortlist}

    def load() -> dict[str, Any]:
        return {
            "indicator_inputs": _safe_list(
                lambda: provider.load_indicator_inputs(
                    symbols,
                    request.trade_date,
                    lookback_days=request.lookback_days,
                    storage=storage,
                )
            ),
            "statements": _safe_dict(
                lambda: provider.load_statement_context(symbols, trade_date=request.trade_date)
            ),
            "news": _safe_dict(
                lambda: provider.load_company_news_context(
                    symbols,
                    trade_date=request.trade_date,
                    lookback_days=90,
                )
            ),
            "holder_activity": _safe_dict(
                lambda: provider.load_holder_activity_context(
                    symbols,
                    trade_date=request.trade_date,
                    lookback_days=180,
                )
            ),
            "fundamental_extra": _safe_dict(lambda: provider.load_fundamental_context(symbols)),
            "chips": _safe_dict(lambda: provider.load_chip_context(symbols)),
            "quotes": _safe_dict(lambda: provider.load_realtime_quote_lookup(symbols)),
            "hot_sectors": _safe_dict(
                lambda: provider.load_hot_sector_context(request.trade_date, storage=storage)
            ),
        }

    if progress is None:
        context = load()
    else:
        with progress.step("Serenity 证据增强") as step:
            context = load()
            step.complete()

    inputs = {
        item.ts_code: item
        for item in context.get("indicator_inputs", [])
        if isinstance(item, IndicatorInput)
    }
    stock_hot = (context.get("hot_sectors") or {}).get("stock_hot_sectors") or {}
    results: list[SerenityCandidateResult] = []
    analysis_step = progress.step("Serenity 评分", total=len(shortlist)) if progress is not None else None
    try:
        for index, relation in enumerate(shortlist, start=1):
            symbol = str(relation.get("ts_code") or "")
            item = inputs.get(symbol)
            payload = _enhanced_payload(
                symbol,
                relation=relation_lookup[symbol],
                item=item,
                request=request,
                statements=(context.get("statements") or {}).get(symbol, {}),
                news=(context.get("news") or {}).get(symbol, {}),
                holder_activity=(context.get("holder_activity") or {}).get(symbol, {}),
                fundamental_extra=(context.get("fundamental_extra") or {}).get(symbol, {}),
                chip=(context.get("chips") or {}).get(symbol, {}),
                quote=(context.get("quotes") or {}).get(symbol, {}),
                hot_sectors=stock_hot.get(symbol, []) if isinstance(stock_hot, dict) else [],
            )
            results.append(score_serenity_candidate(payload))
            if analysis_step is not None:
                analysis_step.update(index, message=symbol)
    finally:
        if analysis_step is not None:
            analysis_step.complete()
    return results


def _enhanced_payload(
    symbol: str,
    *,
    relation: dict[str, Any],
    item: IndicatorInput | None,
    request: SerenityScreenRequest,
    statements: dict[str, Any],
    news: dict[str, Any],
    holder_activity: dict[str, Any],
    fundamental_extra: dict[str, Any],
    chip: dict[str, Any],
    quote: dict[str, Any],
    hot_sectors: list[dict[str, Any]],
) -> dict[str, Any]:
    indicator = IndicatorCalculator().calculate(item).to_dict() if item is not None else {}
    stock_basic = dict(item.stock_basic or {}) if item is not None else dict(relation.get("stock_basic") or {})
    latest_daily = _latest_row(item.daily) if item is not None else {}
    latest_basic = _latest_row(item.daily_basic) if item is not None else {}
    data_sources = dict(item.data_sources or {}) if item is not None else {}
    for key, context in (
        ("statements", statements),
        ("news", news),
        ("holder_activity", holder_activity),
    ):
        if isinstance(context, dict) and context.get("data_source"):
            data_sources[key] = str(context.get("data_source"))
    return {
        **relation,
        "ts_code": symbol,
        "name": str(
            stock_basic.get("name")
            or relation.get("name")
            or quote.get("name")
            or fundamental_extra.get("name")
            or ""
        ),
        "trade_date": request.trade_date,
        "theme": request.theme,
        "industry": str(
            stock_basic.get("industry")
            or relation.get("industry")
            or fundamental_extra.get("industry")
            or ""
        ),
        "stock_basic": stock_basic,
        "indicator": indicator,
        "fundamentals_frame": item.fundamentals if item is not None else pd.DataFrame(),
        "statement_frames": _statement_frame(statements),
        "statements": statements,
        "news": news,
        "events": holder_activity,
        "holder_activity": holder_activity,
        "fundamental_extra": fundamental_extra,
        "chip": chip,
        "quote": quote,
        "hot_sectors": hot_sectors,
        "market_cap_yi": _market_cap_yi(latest_basic),
        "total_mv": latest_basic.get("total_mv"),
        "pe": latest_basic.get("pe"),
        "pb": latest_basic.get("pb"),
        "amount": _num(latest_daily.get("amount")),
        "data_sources": data_sources,
        "preliminary_score": float(relation.get("preliminary_score") or 0),
    }


def _attach_llm_reviews(
    candidates: list[SerenityCandidateResult],
    *,
    llm: Any | None,
    llm_factory: Callable[..., Any] | None,
    timeout_seconds: int,
    progress: Any | None,
) -> list[SerenityCandidateResult]:
    chat_llm = llm
    if chat_llm is None and llm_factory is not None:
        try:
            chat_llm = llm_factory(timeout_seconds=timeout_seconds)
        except TypeError:
            try:
                chat_llm = llm_factory()
            except Exception:
                chat_llm = None
        except Exception:
            chat_llm = None
    if chat_llm is None:
        return [
            replace(item, llm_review={"status": "unavailable", "comment": ""})
            for item in candidates
        ]
    reviewed: list[SerenityCandidateResult] = []
    step = progress.step("Serenity LLM 复核", total=len(candidates)) if progress is not None else None
    try:
        for index, item in enumerate(candidates, start=1):
            try:
                comment = _llm_review_comment(item, chat_llm, timeout_seconds=timeout_seconds)
                review = {"status": "ok" if comment else "empty", "comment": comment}
            except Exception:
                review = {"status": "unavailable", "comment": ""}
            reviewed.append(replace(item, llm_review=review))
            if step is not None:
                step.update(index, message=item.ts_code)
    finally:
        if step is not None:
            step.complete()
    return reviewed


def _llm_review_comment(
    item: SerenityCandidateResult,
    llm: Any,
    *,
    timeout_seconds: int,
) -> str:
    payload = {
        "ts_code": item.ts_code,
        "name": item.name,
        "final_score": item.final_score,
        "coverage_pct": item.coverage_pct,
        "chain_tier": item.chain_tier,
        "constrained_link": item.constrained_link,
        "factors": [factor.to_dict() for factor in item.factors],
        "penalties": item.penalties,
        "evidence": [evidence.to_dict() for evidence in item.evidence],
        "missing_fields": list(item.missing_fields),
    }
    prompt = (
        "只基于以下 SATS Serenity 确定性评分和公开证据，用不超过180字补充四点："
        "卡住什么、市场可能忽略什么、最强反方理由、下一步验证。"
        "不得修改分数，不得编造客户、订单、价格、财务数字或买卖指令：\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )
    response = llm.chat([{"role": "user", "content": prompt}], timeout=timeout_seconds)
    return str(getattr(response, "content", "") or "").strip()


def _store_screening_results(
    storage: DuckDBStorage,
    candidates: list[SerenityCandidateResult],
) -> None:
    rows = []
    for item in candidates:
        metrics = item.to_dict()
        metrics["matched_signal_labels"] = [
            item.chain_tier,
            item.verdict,
            *[
                evidence.strength
                for evidence in item.evidence
                if evidence.strength in {"strong", "medium"}
            ][:2],
        ]
        matched = [factor.key for factor in item.factors if factor.available and factor.rating >= 3]
        failed = [factor.key for factor in item.factors if not factor.available or factor.rating < 3]
        rows.append(
            ScreeningResult(
                trade_date=item.trade_date,
                ts_code=item.ts_code,
                rule_name=SERENITY_RULE_NAME,
                passed=item.passed,
                score=item.final_score,
                matched_conditions=matched,
                failed_conditions=failed,
                metrics=metrics,
            )
        )
    storage.upsert_screening_results(rows)


def _layer_priorities(
    candidates: list[SerenityCandidateResult],
) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, list[SerenityCandidateResult]] = {}
    for item in candidates:
        grouped.setdefault(item.chain_tier, []).append(item)
    rows = [
        {
            "layer": layer,
            "count": len(items),
            "top_score": max(item.final_score for item in items),
            "passed_count": sum(1 for item in items if item.passed),
        }
        for layer, items in grouped.items()
    ]
    rows.sort(key=lambda item: (-float(item["top_score"]), -int(item["count"]), str(item["layer"])))
    return tuple(rows)


def _statement_frame(context: dict[str, Any]) -> pd.DataFrame:
    items = context.get("items") if isinstance(context, dict) else []
    return pd.DataFrame(items) if isinstance(items, list) else pd.DataFrame()


def _latest_row(frame: pd.DataFrame | None) -> dict[str, Any]:
    if frame is None or frame.empty:
        return {}
    data = frame.copy()
    for key in ("trade_date", "end_date", "ann_date"):
        if key in data.columns:
            data = data.sort_values(key)
            break
    return data.iloc[-1].dropna().to_dict()


def _market_cap_yi(row: dict[str, Any]) -> float | None:
    value = _num(row.get("total_mv") or row.get("circ_mv"))
    if value is None:
        return None
    return value / 10_000.0 if value > 10_000 else value


def _theme_from_query(query: str) -> str:
    text = str(query or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?i)(?:用\s*)?serenity(?:-skill)?", " ", text)
    text = re.sub(r"(?i)(?:前\s*\d+\s*只|top\s*\d+)", " ", text)
    for suffix in ("选股", "筛选", "推荐", "找标的", "找股票", "股票", "标的"):
        text = text.replace(suffix, " ")
    for prefix in ("请", "帮我", "按照"):
        text = text.replace(prefix, " ")
    cleaned = " ".join(text.split()).strip("，,。 ")
    return cleaned[:80]


def _theme_terms(theme: str) -> tuple[str, ...]:
    text = str(theme or "").lower()
    defaults = (
        "半导体",
        "芯片",
        "光通信",
        "光模块",
        "先进封装",
        "算力",
        "液冷",
        "机器人",
        "人工智能",
        "ai",
    )
    terms = [term for term in defaults if term in text]
    return tuple(terms or defaults)


def _normalize_symbol_input(
    symbols: list[str] | tuple[str, ...] | str | None,
) -> list[str]:
    if isinstance(symbols, str):
        return parse_symbol_csv(symbols, required=False)
    return normalize_symbols(symbols or [], required=False)


def _supported_common_a_symbols(
    symbols: list[str],
    *,
    provider: Any,
    storage: DuckDBStorage,
) -> tuple[list[str], list[str]]:
    if not symbols:
        return [], []
    stock_basic = _safe_frame(lambda: provider.load_stock_basic(storage=storage))
    listed_symbols = (
        {
            str(value or "").strip().upper()
            for value in stock_basic.get("ts_code", pd.Series(dtype=str)).tolist()
        }
        if not stock_basic.empty
        else set()
    )
    supported: list[str] = []
    rejected: list[str] = []
    for symbol in symbols:
        if _is_supported_symbol(symbol) and (not listed_symbols or symbol in listed_symbols):
            supported.append(symbol)
        else:
            rejected.append(symbol)
    return supported, rejected


def _is_supported_symbol(symbol: str) -> bool:
    raw = str(symbol or "").strip().upper()
    code = raw.split(".", 1)[0]
    return (
        raw.endswith(SUPPORTED_MARKETS)
        and code.isdigit()
        and code.startswith(("0", "3", "4", "6", "8", "9"))
    )


def _dedupe_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("ts_code") or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(row)
    return result


def _safe_list(loader: Callable[[], Any]) -> list[Any]:
    try:
        value = loader()
    except Exception:
        return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _safe_dict(loader: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = loader()
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _safe_frame(loader: Callable[[], Any]) -> pd.DataFrame:
    try:
        value = loader()
    except Exception:
        return pd.DataFrame()
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _num(value: Any) -> float | None:
    try:
        if value in (None, "") or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
