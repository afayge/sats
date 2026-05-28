from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from sats.chan.engine import evaluate_chan_signals
from sats.llm import ChatLLM
from sats.screening.base import ScreeningInput
from sats.signals.base import SignalAnalysisResult, SignalAnalysisRun, SignalEvent, SignalInput
from sats.signals.registry import COMPOSITE_DEFINITIONS, GROUP_ALIASES, SHORT_UP_CATEGORIES, SIGNAL_DEFINITIONS, get_signal_definition


DEFAULT_SIGNALS = "all"


@dataclass(frozen=True, slots=True)
class CompositeSpec:
    signal_id: str
    primary: tuple[str, ...]
    confirm_categories: tuple[str, ...] = ()
    confirm_signals: tuple[str, ...] = ()


COMPOSITE_SPECS = [
    CompositeSpec("graph_triangle_chan_wave", ("triangle_up_break",), ("chan", "wave", "harmonic", "trendline")),
    CompositeSpec("graph_elliott_c_chan", ("elliott_c_reversal",), ("chan", "harmonic", "graph", "trendline")),
    CompositeSpec("graph_trend_break_risk_chan", ("trend_breakthrough_risk",), ("chan", "graph", "wave")),
    CompositeSpec("graph_cypher_predict_chan", ("cypher_predict", "cypher_bullish"), ("chan", "graph", "trendline")),
    CompositeSpec("graph_cypher_bullish_chan", ("cypher_bullish",), ("chan", "graph", "trendline")),
    CompositeSpec("graph_chan_third_buy", ("chan_third_buy",), ("graph", "wave", "harmonic", "trendline")),
    CompositeSpec("graph_wedge_down_break_chan", ("wedge_down_break",), ("chan", "wave", "harmonic", "trendline")),
    CompositeSpec("graph_bat_chan", ("bat_bullish",), ("chan", "graph", "trendline")),
    CompositeSpec("graph_bat_third_target_chan", ("bat_third_target",), ("chan", "graph", "trendline")),
    CompositeSpec("graph_chan_second_buy", ("chan_second_buy",), ("graph", "wave", "harmonic", "trendline")),
    CompositeSpec("graph_elliott_reversal_chan", ("elliott_c_reversal",), ("chan", "harmonic", "graph", "trendline")),
    CompositeSpec("graph_trend_break_chance_chan", ("trend_breakthrough_chance",), ("chan", "graph", "wave", "harmonic")),
    CompositeSpec("graph_chan_third_sell", ("chan_third_sell",), ("graph", "trendline", "wave")),
    CompositeSpec("graph_head_top_break_chan", ("head_top_break",), ("chan", "trendline", "wave")),
    CompositeSpec("graph_trend_support_break_chan", ("trend_breakthrough_risk",), ("chan", "graph", "wave")),
    CompositeSpec("graph_elliott_b_chan", ("elliott_b_pullback",), ("chan", "harmonic", "graph", "trendline")),
    CompositeSpec("graph_gartley_chan", ("gartley_bullish",), ("chan", "graph", "trendline")),
    CompositeSpec("graph_chan_second_sell", ("chan_second_sell",), ("graph", "trendline", "wave")),
    CompositeSpec("graph_rect_down_target_chan", ("rect_down_target",), ("chan", "trendline", "wave")),
    CompositeSpec("graph_elliott_down_c_chan", ("elliott_c_down_continuation",), ("chan", "graph", "trendline")),
    CompositeSpec("graph_crab_chan", ("crab_bullish",), ("chan", "graph", "trendline")),
    CompositeSpec("graph_flag_down_break_chan", ("flag_down_break",), ("chan", "trendline", "wave")),
    CompositeSpec("graph_chan_first_buy", ("chan_first_buy",), ("graph", "wave", "harmonic", "trendline")),
    CompositeSpec("graph_elliott_up_c_chan", ("elliott_c_up_continuation",), ("chan", "graph", "trendline")),
    CompositeSpec("graph_wedge_up_target_chan", ("wedge_up_target",), ("chan", "trendline", "wave")),
    CompositeSpec("graph_chan_center_b_complete", ("chan_center_oscillation_low",), ("graph", "wave", "harmonic", "trendline")),
    CompositeSpec("ma_golden_spider_pattern", ("ma_golden_spider",), ("graph",)),
    CompositeSpec("ma_granville_b1_wave", ("ma_granville_b1",), ("wave", "graph")),
    CompositeSpec("ma_golden_silver_valley_chan", ("ma_golden_valley", "ma_silver_valley"), ("chan",)),
    CompositeSpec("ma_granville_s6_harmonic", ("ma_granville_s6",), ("harmonic", "graph")),
    CompositeSpec("ma_warplane_trend", ("ma_warplane",), ("trendline",)),
    CompositeSpec("ma_granville_b3_chan", ("ma_granville_b3",), ("chan",)),
    CompositeSpec("ma_cloud_dark_wave", ("ma_cloud_dark",), ("wave", "graph")),
    CompositeSpec("ma_dry_up_jump_pattern", ("ma_dry_up_jump",), ("graph",)),
    CompositeSpec("ma_granville_s7_trend", ("ma_granville_s7",), ("trendline",)),
    CompositeSpec("ma_chopper_harmonic", ("ma_chopper",), ("harmonic", "graph")),
    CompositeSpec("kc_up_pioneer_graph", ("kc_up_pioneer",), ("graph", "chan", "wave", "harmonic", "trendline")),
    CompositeSpec("kc_up_jump_gap_graph", ("kc_up_jump_gap",), ("graph", "chan", "wave", "trendline")),
    CompositeSpec("kc_tower_bottom_graph", ("kc_tower_bottom",), ("graph", "chan", "wave", "trendline")),
    CompositeSpec("kc_up_unbroke_graph", ("kc_up_unbroke",), ("graph", "chan", "wave", "trendline")),
    CompositeSpec("kc_down_blocked_graph", ("kc_down_blocked",), ("graph", "chan", "wave", "trendline")),
    CompositeSpec("kc_up_pinbar_graph", ("kc_up_pinbar",), ("graph", "chan", "wave", "trendline")),
    CompositeSpec("kc_down_pour_graph", ("kc_down_pour",), ("graph", "chan", "wave", "trendline")),
    CompositeSpec("kc_low_five_yang_graph", ("kc_low_five_yang",), ("graph", "chan", "wave", "trendline")),
    CompositeSpec("kc_bullish_harami_graph", ("kc_bullish_harami",), ("graph", "chan", "wave", "trendline")),
    CompositeSpec("kc_double_needle_graph", ("kc_double_needle",), ("graph", "chan", "wave", "trendline")),
    CompositeSpec("ma_b2_kline", ("ma_granville_b2",), ("kline",)),
    CompositeSpec("ma_poison_spider_kline", ("ma_poison_spider",), ("kline",)),
    CompositeSpec("ma_fish_gate_kline", ("ma_fish_gate",), ("kline",)),
    CompositeSpec("ma_death_valley_kline", ("ma_death_valley",), ("kline",)),
    CompositeSpec("ma_alpine_skiing_kline", ("ma_alpine_skiing",), ("kline",)),
    CompositeSpec("ma_b4_kline", ("ma_granville_b4",), ("kline",)),
    CompositeSpec("ma_cloud_moon_kline", ("ma_cloud_moon",), ("kline",)),
    CompositeSpec("ma_dry_dn_jump_kline", ("ma_dry_dn_jump",), ("kline",)),
    CompositeSpec("ma_b3_kline", ("ma_granville_b3",), ("kline",)),
    CompositeSpec("ma_dragon_sea_kline", ("ma_dragon_sea",), ("kline",)),
]


def parse_signal_selection(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        value = DEFAULT_SIGNALS
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    else:
        raw_items = []
        for item in value:
            raw_items.extend(str(item).split(","))
        raw_items = [item.strip() for item in raw_items]
    selected: list[str] = []
    seen = set()
    for item in raw_items:
        if not item:
            continue
        normalized = item.replace("-", "_")
        if normalized not in seen:
            seen.add(normalized)
            selected.append(normalized)
    return selected or [DEFAULT_SIGNALS]


def analyze_signal_input(
    item: SignalInput,
    *,
    selected_signals: str | Iterable[str] | None = None,
    llm_review: bool = False,
) -> SignalAnalysisResult:
    selected = parse_signal_selection(selected_signals)
    daily = _prepare_daily(item.daily, item.trade_date)
    name = str(item.stock_basic.get("name") or "")
    if daily.empty:
        return SignalAnalysisResult(
            ts_code=item.ts_code,
            trade_date=item.trade_date,
            name=name,
            close=0.0,
            score=0.0,
            decision="数据不足",
            trend="数据不足",
            events=[],
            selected_signals=selected,
            key_levels={},
        )

    enriched = _enrich_daily(daily)
    events = []
    events.extend(_detect_ma_signals(enriched))
    events.extend(_detect_kline_signals(enriched))
    events.extend(_detect_graph_signals(enriched))
    events.extend(_detect_trendline_signals(enriched))
    events.extend(_detect_wave_signals(enriched))
    events.extend(_detect_harmonic_signals(enriched))
    events.extend(_detect_chan_signals(item, enriched))
    events.extend(_build_composites(events))
    events = _filter_events(events, selected)
    events = sorted(events, key=lambda event: (event.category.startswith(("ma_", "graph_", "kc_")), event.score), reverse=True)
    events = events[:30]
    close = _num(enriched.iloc[-1].get("close"))
    score, decision, trend = _score_decision(events, enriched)
    key_levels = _key_levels(enriched)
    llm_unavailable = False
    llm_summary = ""
    if llm_review:
        try:
            llm_summary = _llm_review(
                item.ts_code,
                name,
                item.trade_date,
                close,
                events,
                data_sources=_signal_data_sources(item),
            )
        except Exception:
            llm_unavailable = True
    return SignalAnalysisResult(
        ts_code=item.ts_code,
        trade_date=item.trade_date,
        name=name,
        close=close,
        score=score,
        decision=decision,
        trend=trend,
        events=events,
        selected_signals=selected,
        key_levels=key_levels,
        llm_unavailable=llm_unavailable,
        llm_summary=llm_summary,
    )


def analyze_signal_inputs(
    inputs: Iterable[SignalInput],
    *,
    selected_signals: str | Iterable[str] | None = None,
    trade_date: str,
    reports_dir: Path | None = None,
    report: bool = True,
    source_label: str = "stocks",
    llm_review: bool = False,
    progress: Any | None = None,
) -> SignalAnalysisRun:
    items = list(inputs)
    results = []
    if progress is None:
        results = [
            analyze_signal_input(item, selected_signals=selected_signals, llm_review=llm_review)
            for item in items
        ]
    else:
        with progress.step("Analyze 信号计算", total=len(items)) as step:
            for index, item in enumerate(items, start=1):
                results.append(analyze_signal_input(item, selected_signals=selected_signals, llm_review=llm_review))
                step.update(index)
    results.sort(key=lambda item: (-item.score, item.ts_code))
    report_path = None
    if report and reports_dir is not None:
        if progress is None:
            report_path = str(write_signal_report(results, reports_dir=reports_dir, trade_date=trade_date, source_label=source_label))
        else:
            with progress.step("报告生成", total=1) as step:
                report_path = str(write_signal_report(results, reports_dir=reports_dir, trade_date=trade_date, source_label=source_label))
                step.update(1)
    return SignalAnalysisRun(
        trade_date=trade_date,
        results=results,
        report_path=report_path,
        llm_unavailable=any(item.llm_unavailable for item in results),
    )


def format_signal_analysis(results: list[SignalAnalysisResult]) -> str:
    if not results:
        return "无结果"
    lines = []
    for index, result in enumerate(results, start=1):
        labels = ",".join(event.label for event in result.events[:5]) or "无命中信号"
        name = f" {result.name}" if result.name else ""
        levels = result.key_levels
        level_text = ""
        if levels:
            level_text = f" 支撑 {levels.get('support', '')} 压力 {levels.get('resistance', '')}"
        lines.append(
            f"{index}. {result.ts_code}{name} 评分 {_fmt(result.score)} {result.decision} {result.trend} "
            f"信号 {labels}{level_text}".rstrip()
        )
    return "\n".join(lines)


def format_signal_definitions(category: str | None = None) -> str:
    from sats.signals.registry import list_signal_definitions

    rows = list_signal_definitions(category=category)
    if not rows:
        return "无信号策略"
    widths = {
        "category": max(len(item.category) for item in rows),
        "id": max(len(item.signal_id) for item in rows),
        "side": max(len(item.side) for item in rows),
    }
    lines = []
    for item in rows:
        lines.append(
            f"{item.category:<{widths['category']}}  {item.signal_id:<{widths['id']}}  "
            f"{item.side:<{widths['side']}}  {item.label}"
        )
    return "\n".join(lines)


def write_signal_report(
    results: list[SignalAnalysisResult],
    *,
    reports_dir: Path,
    trade_date: str,
    source_label: str,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    safe_source = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in source_label)[:80] or "signals"
    path = reports_dir / f"signal_analysis_{trade_date}_{safe_source}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        f"# SATS 信号分析报告",
        "",
        f"- 交易日: {trade_date}",
        f"- 来源: {source_label}",
        f"- 股票数: {len(results)}",
        "",
        "## 排名",
        "",
        "| 排名 | 代码 | 名称 | 评分 | 决策 | 趋势 | 命中信号 |",
        "| --- | --- | --- | ---: | --- | --- | --- |",
    ]
    for index, result in enumerate(results, start=1):
        labels = "<br>".join(event.label for event in result.events[:6]) or "无"
        lines.append(
            f"| {index} | {result.ts_code} | {result.name} | {_fmt(result.score)} | "
            f"{result.decision} | {result.trend} | {labels} |"
        )
    for result in results:
        lines.extend(["", f"## {result.ts_code} {result.name}".strip(), ""])
        lines.append(f"- 收盘价: {_fmt(result.close)}")
        lines.append(f"- 综合结论: {result.decision} / {result.trend} / 评分 {_fmt(result.score)}")
        if result.key_levels:
            lines.append(f"- 关键位: 支撑 {result.key_levels.get('support')}；压力 {result.key_levels.get('resistance')}")
        if result.llm_unavailable:
            lines.append("- LLM: unavailable，本地规则评级")
        if result.llm_summary:
            lines.append(f"- LLM 复核: {result.llm_summary}")
        lines.extend(["", "| 信号 | 方向 | 置信度 | 理由 | 风险 |", "| --- | --- | ---: | --- | --- |"])
        for event in result.events:
            risks = "；".join(event.risk_flags) if event.risk_flags else ""
            lines.append(f"| {event.label} | {event.side} | {_fmt(event.confidence)} | {event.reason} | {risks} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def screening_result_from_signal_input(item: SignalInput, *, selected_signals: str | Iterable[str] | None = None):
    from sats.screening.base import ScreeningResult

    result = analyze_signal_input(item, selected_signals=selected_signals)
    passed_events = [event for event in result.events if event.side in {"buy", "sell"}]
    return ScreeningResult(
        trade_date=item.trade_date,
        ts_code=item.ts_code,
        rule_name="signal_composite",
        passed=bool(passed_events),
        score=result.score,
        matched_conditions=[event.signal_id for event in passed_events],
        failed_conditions=[] if passed_events else ["no_signal_matched"],
        metrics={
            "matched_signal_labels": [event.label for event in passed_events],
            "matched_chan_rules": [event.label for event in passed_events[:6]],
            "signals": [event.to_dict() for event in result.events],
            "decision": result.decision,
            "trend": result.trend,
            "key_levels": result.key_levels,
        },
    )


def _filter_events(events: list[SignalEvent], selected: list[str]) -> list[SignalEvent]:
    short_up = "short_up" in selected
    categories: set[str] = set()
    ids: set[str] = set()
    for item in selected:
        if item in GROUP_ALIASES:
            categories.update(GROUP_ALIASES[item])
        elif get_signal_definition(item):
            ids.add(item)
        else:
            categories.add(item)
    if "all" in categories:
        return events
    filtered = [
        event
        for event in events
        if event.signal_id in ids or event.category in categories or any(component in ids for component in event.components)
    ]
    if short_up:
        wanted = set(SHORT_UP_CATEGORIES)
        filtered = [event for event in filtered if event.side == "buy" and event.category in wanted]
    return filtered


def _event(signal_id: str, confidence: float, reason: str, evidence: dict[str, Any] | None = None, risks: list[str] | None = None, related_chan: list[str] | None = None, components: list[str] | None = None) -> SignalEvent:
    definition = SIGNAL_DEFINITIONS.get(signal_id) or COMPOSITE_DEFINITIONS[signal_id]
    confidence = max(0.0, min(float(confidence), 1.0))
    return SignalEvent(
        signal_id=signal_id,
        label=definition.label,
        category=definition.category,
        side=definition.side,
        confidence=round(confidence, 3),
        score=round(100.0 * confidence, 2),
        reason=reason,
        evidence=evidence or {},
        risk_flags=risks or [],
        related_chan=related_chan or [],
        components=components or [],
    )


def _build_composites(events: list[SignalEvent]) -> list[SignalEvent]:
    by_id = {event.signal_id: event for event in events}
    composites: list[SignalEvent] = []
    for spec in COMPOSITE_SPECS:
        primary = [by_id[item] for item in spec.primary if item in by_id]
        if not primary:
            continue
        definition = COMPOSITE_DEFINITIONS[spec.signal_id]
        confirms = [
            event
            for event in events
            if event.signal_id not in {primary_event.signal_id for primary_event in primary}
            and (event.category in spec.confirm_categories or event.signal_id in spec.confirm_signals)
            and (event.side == definition.side or event.side == "hold")
        ]
        if spec.confirm_categories and not confirms:
            continue
        components = [event.signal_id for event in [*primary, *confirms[:3]]]
        related_chan = [event.label for event in confirms if event.category == "chan"]
        confidence = min(0.95, max(event.confidence for event in primary) + min(0.18, 0.06 * len(confirms)))
        reason = " + ".join(event.label for event in [*primary, *confirms[:3]])
        composites.append(_event(spec.signal_id, confidence, reason, components=components, related_chan=related_chan))
    return composites


def _prepare_daily(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    aliases = {
        "日期": "trade_date",
        "成交量": "vol",
        "成交额": "amount",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "涨跌幅": "pct_chg",
        "volume": "vol",
    }
    data = data.rename(columns={column: aliases.get(str(column), column) for column in data.columns})
    required = {"trade_date", "open", "high", "low", "close"}
    if not required.issubset(set(data.columns)):
        return pd.DataFrame()
    if "vol" not in data.columns:
        data["vol"] = 0.0
    data["trade_date"] = data["trade_date"].astype(str)
    data = data[data["trade_date"] <= str(trade_date)].copy()
    for column in ["open", "high", "low", "close", "vol", "amount", "pct_chg"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "high", "low", "close"]).sort_values("trade_date").reset_index(drop=True)
    if "pct_chg" not in data.columns:
        data["pct_chg"] = data["close"].pct_change().fillna(0) * 100
    return data


def _enrich_daily(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy().reset_index(drop=True)
    close = result["close"].astype(float)
    high = result["high"].astype(float)
    low = result["low"].astype(float)
    prev_close = close.shift(1)
    for period in (5, 10, 20, 30, 60, 120):
        result[f"ma{period}"] = close.rolling(period, min_periods=min(5, period)).mean()
    for period in (5, 10, 20):
        result[f"ema{period}"] = close.ewm(span=period, adjust=False).mean()
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    result["atr14"] = tr.rolling(14, min_periods=3).mean().fillna(tr.expanding().mean())
    result["body"] = (result["close"] - result["open"]).abs()
    result["range"] = (result["high"] - result["low"]).replace(0, np.nan)
    result["upper_shadow"] = result["high"] - result[["open", "close"]].max(axis=1)
    result["lower_shadow"] = result[["open", "close"]].min(axis=1) - result["low"]
    result["is_bull"] = result["close"] > result["open"]
    result["is_bear"] = result["close"] < result["open"]
    result["volume_ratio_5"] = result["vol"] / result["vol"].shift(1).rolling(5, min_periods=1).mean().replace(0, np.nan)
    return result.fillna({"volume_ratio_5": 1.0})


def _detect_ma_signals(data: pd.DataFrame) -> list[SignalEvent]:
    if len(data) < 8:
        return []
    events: list[SignalEvent] = []
    latest = data.iloc[-1]
    prev = data.iloc[-2]
    close = _num(latest["close"])
    ma5, ma10, ma20, ma30, ma60 = (_num(latest.get(f"ma{p}")) for p in (5, 10, 20, 30, 60))
    prev_ma5, prev_ma10, prev_ma20 = (_num(prev.get(f"ma{p}")) for p in (5, 10, 20))
    vol_ratio = _num(latest.get("volume_ratio_5"), 1.0)
    ma20_slope = _slope(data["ma20"].tail(8))
    ma60_slope = _slope(data["ma60"].tail(12))
    bias20 = (close / ma20 - 1) if ma20 else 0
    bull_stack = ma5 > ma10 > ma20 > ma60 > 0
    bear_stack = 0 < ma5 < ma10 < ma20 < ma60
    if _cross_up(prev_ma5, prev_ma10, ma5, ma10):
        events.append(_event("ma_granville_b2", 0.72, "MA5 上穿 MA10", {"ma5": ma5, "ma10": ma10}))
    if _cross_down(prev_ma5, prev_ma10, ma5, ma10):
        events.append(_event("ma_granville_s6", 0.72, "MA5 下穿 MA10", {"ma5": ma5, "ma10": ma10}))
    if ma20_slope > 2 and abs(bias20) < 0.025 and latest["is_bull"]:
        events.append(_event("ma_granville_b3", 0.66, "上升均线附近企稳", {"bias20": bias20}))
    if ma20_slope < -2 and abs(bias20) < 0.025 and latest["is_bear"]:
        events.append(_event("ma_granville_s7", 0.66, "下降均线附近转弱", {"bias20": bias20}))
    if bull_stack and close > ma5:
        events.append(_event("ma_granville_b1", 0.65, "均线多头排列且站上短均线"))
    if bear_stack and close < ma5:
        events.append(_event("ma_granville_s5", 0.65, "均线空头排列且跌破短均线"))
    if ma20_slope > 1 and bias20 < -0.06:
        events.append(_event("ma_granville_b4", 0.58, "上升趋势中过度回落至均线下方", {"bias20": bias20}))
    if ma20_slope < -1 and bias20 > 0.06:
        events.append(_event("ma_granville_s8", 0.58, "下降趋势中过度反弹至均线上方", {"bias20": bias20}))
    if ma5 > ma10 > ma20 and prev_ma5 <= prev_ma10 <= prev_ma20:
        events.append(_event("ma_golden_spider", 0.76, "短中期均线集中向上发散"))
    if ma5 < ma10 < ma20 and prev_ma5 >= prev_ma10 >= prev_ma20:
        events.append(_event("ma_poison_spider", 0.76, "短中期均线集中向下发散"))
    if bull_stack and vol_ratio > 1.2 and _recent_gain(data, 3) > 0.03:
        events.append(_event("ma_dragon_sea", 0.72, "多头排列后放量上攻", {"volume_ratio_5": vol_ratio}))
    if close < ma5 < ma10 < ma20 and _num(latest.get("pct_chg")) < -3:
        events.append(_event("ma_chopper", 0.76, "放量长阴跌破均线簇"))
    if _cross_up(prev_ma10, prev_ma20, ma10, ma20):
        events.append(_event("ma_golden_valley", 0.68, "MA10 上穿 MA20"))
    if _cross_up(prev_ma5, prev_ma20, ma5, ma20):
        events.append(_event("ma_silver_valley", 0.62, "MA5 上穿 MA20"))
    if _num(latest.get("pct_chg")) > 4 and vol_ratio > 1.5 and close > max(ma5, ma10, ma20):
        events.append(_event("ma_dry_up_jump", 0.7, "放量大阳突破均线簇"))
    if _num(latest.get("pct_chg")) < -4 and vol_ratio > 1.5 and close < min(ma5 or close, ma10 or close, ma20 or close):
        events.append(_event("ma_dry_dn_jump", 0.7, "放量大阴跌破均线簇"))
    if ma60_slope > 0 and ma20_slope > 0 and close > ma20 and _recent_gain(data, 5) > 0.05:
        events.append(_event("ma_fish_gate", 0.66, "中长期均线上行，短线突破加速"))
    if ma60_slope < 0 and ma20_slope < 0 and close < ma20 and _recent_gain(data, 5) < -0.05:
        events.append(_event("ma_death_valley", 0.66, "中长期均线下行，短线破位加速"))
    if ma20_slope > 0 and close > ma20 and latest["lower_shadow"] > latest["body"]:
        events.append(_event("ma_alpine_skiing", 0.58, "上升均线附近下影企稳"))
    if ma20_slope < 0 and close < ma20 and latest["upper_shadow"] > latest["body"]:
        events.append(_event("ma_warplane", 0.58, "下降均线附近上影承压"))
    if ma5 > ma10 and ma10 > ma20 and close > ma5 and data.tail(3)["is_bull"].sum() >= 2:
        events.append(_event("ma_cloud_moon", 0.6, "短均线托举，K 线温和上行"))
    if ma5 < ma10 and ma10 < ma20 and close < ma5 and data.tail(3)["is_bear"].sum() >= 2:
        events.append(_event("ma_cloud_dark", 0.6, "短均线压制，K 线温和下行"))
    return _dedupe(events)


def _detect_kline_signals(data: pd.DataFrame) -> list[SignalEvent]:
    if len(data) < 5:
        return []
    events: list[SignalEvent] = []
    a, b, c = data.iloc[-3], data.iloc[-2], data.iloc[-1]
    last5 = data.tail(5)
    close = _num(c["close"])
    low_zone = _price_rank(data, close) < 0.35
    high_zone = _price_rank(data, close) > 0.65
    vol_ratio = _num(c.get("volume_ratio_5"), 1.0)
    body_ratio = _num(c["body"]) / _num(c["range"], 1.0)
    upper = _num(c["upper_shadow"])
    lower = _num(c["lower_shadow"])
    body = _num(c["body"], 0.0001)
    if c["is_bull"] and b["is_bull"] and close > _num(b["high"]) and lower > body * 0.3:
        events.append(_event("kc_up_pioneer", 0.64, "连续阳线后突破前高"))
    if c["is_bear"] and b["is_bear"] and close < _num(b["low"]) and upper > body * 0.3:
        events.append(_event("kc_down_pioneer", 0.64, "连续阴线后跌破前低"))
    if _num(c["open"]) > _num(b["high"]) and c["is_bull"] and vol_ratio > 1.1:
        events.append(_event("kc_up_jump_gap", 0.7, "高开跳空且收阳", {"volume_ratio_5": vol_ratio}))
    if _num(c["open"]) < _num(b["low"]) and c["is_bear"] and vol_ratio > 1.1:
        events.append(_event("kc_down_jump_gap", 0.7, "低开跳空且收阴", {"volume_ratio_5": vol_ratio}))
    if low_zone and last5["low"].idxmin() in set(last5.index[-3:]) and last5["is_bull"].tail(2).sum() >= 1:
        events.append(_event("kc_tower_bottom", 0.58, "低位筑底后阳线修复"))
    if high_zone and last5["high"].idxmax() in set(last5.index[-3:]) and last5["is_bear"].tail(2).sum() >= 1:
        events.append(_event("kc_tower_top", 0.58, "高位见顶后阴线转弱"))
    if last5["is_bull"].sum() >= 4 and close > _num(last5.iloc[0]["close"]):
        events.append(_event("kc_low_five_yang" if low_zone else "kc_up_unbroke", 0.62, "多根阳线稳步上行"))
    if last5["is_bear"].sum() >= 4 and close < _num(last5.iloc[0]["close"]):
        events.append(_event("kc_high_five_yin" if high_zone else "kc_down_unbroke", 0.62, "多根阴线持续下行"))
    if all(last5["close"].diff().tail(4) > 0):
        events.append(_event("kc_slow_up", 0.55, "收盘价连续缓慢上行"))
    if all(last5["close"].diff().tail(4) < 0):
        events.append(_event("kc_slow_down", 0.55, "收盘价连续缓慢下行"))
    if _recent_gain(data, 3) > 0.07:
        events.append(_event("kc_up_acceleration", 0.6, "三日涨幅加速"))
    if _recent_gain(data, 3) < -0.07:
        events.append(_event("kc_down_acceleration", 0.6, "三日跌幅加速"))
    if lower > body * 2.0 and c["is_bull"]:
        events.append(_event("kc_probe_up", 0.58, "长下影后收阳"))
    if upper > body * 2.0 and c["is_bear"]:
        events.append(_event("kc_probe_down", 0.58, "长上影后收阴"))
    if _bullish_engulfing(b, c):
        events.append(_event("kc_bullish_engulfing", 0.72, "阳包阴形态"))
    if _bearish_engulfing(b, c):
        events.append(_event("kc_bearish_engulfing", 0.72, "阴包阳形态"))
    if a["is_bull"] and b["is_bear"] and c["is_bull"] and _num(c["close"]) > _num(a["close"]):
        events.append(_event("kc_bullish_cannon", 0.68, "两红夹一黑，多方炮"))
        events.append(_event("kc_two_red_one_black", 0.62, "两红夹一黑"))
    if a["is_bear"] and b["is_bull"] and c["is_bear"] and _num(c["close"]) < _num(a["close"]):
        events.append(_event("kc_bearish_cannon", 0.68, "两黑夹一红，空方炮"))
        events.append(_event("kc_two_black_one_red", 0.62, "两黑夹一红"))
    if data.tail(3)["is_bull"].sum() == 3 and all(data.tail(3)["close"].diff().tail(2) > 0):
        events.append(_event("kc_three_white_soldiers", 0.7, "红三兵"))
    if data.tail(3)["is_bear"].sum() == 3 and all(data.tail(3)["close"].diff().tail(2) < 0):
        events.append(_event("kc_three_black_crows", 0.7, "三只乌鸦"))
    if _morning_star(a, b, c):
        events.append(_event("kc_morning_star", 0.74, "早晨之星"))
    if _evening_star(a, b, c):
        events.append(_event("kc_evening_star", 0.74, "黄昏之星"))
    if lower > body * 2.5 and low_zone:
        events.append(_event("kc_hammer", 0.68, "低位锤头线"))
        events.append(_event("kc_single_needle", 0.64, "单针探底"))
    if upper > body * 2.5 and high_zone:
        events.append(_event("kc_shooting_star", 0.68, "高位射击之星"))
    if body_ratio < 0.12 and upper > body * 2.0 and high_zone:
        events.append(_event("kc_gravestone_doji", 0.64, "墓碑十字线"))
    if _piercing(b, c):
        events.append(_event("kc_piercing", 0.68, "曙光初现"))
    if _dark_cloud(b, c):
        events.append(_event("kc_dark_cloud", 0.68, "乌云盖顶"))
    if _harami(b, c, bullish=True):
        events.append(_event("kc_bullish_harami", 0.6, "上涨身怀六甲"))
    if _harami(b, c, bullish=False):
        events.append(_event("kc_bearish_harami", 0.6, "下跌身怀六甲"))
    if _tweezer_bottom(b, c):
        events.append(_event("kc_tweezer_bottom", 0.62, "双低近似，镊子底"))
    if _tweezer_top(b, c):
        events.append(_event("kc_tweezer_top", 0.62, "双高近似，镊子顶"))
    if _double_needle(data.tail(4)):
        events.append(_event("kc_double_needle", 0.7, "双针探底"))
    if body_ratio < 0.18 and _num(c["range"]) > _num(data["range"].tail(20).mean()) * 1.2:
        events.append(_event("kc_down_spinning_top" if low_zone else "kc_up_spinning_top", 0.54, "螺旋桨形态"))
    if c["is_bull"] and lower > body and upper > body:
        events.append(_event("kc_immortal_guide", 0.56, "仙人指路形态"))
    events.extend(_derived_kline_aliases(data, events, low_zone=low_zone, high_zone=high_zone))
    return _dedupe(events)


def _derived_kline_aliases(data: pd.DataFrame, events: list[SignalEvent], *, low_zone: bool, high_zone: bool) -> list[SignalEvent]:
    ids = {event.signal_id for event in events}
    result: list[SignalEvent] = []
    if {"kc_up_unbroke", "kc_three_white_soldiers"} & ids:
        result.append(_event("kc_up_three_methods", 0.58, "升势延续形态"))
        result.append(_event("kc_up_resistance", 0.54, "上升抵抗"))
    if {"kc_down_unbroke", "kc_three_black_crows"} & ids:
        result.append(_event("kc_down_three_methods", 0.58, "跌势延续形态"))
        result.append(_event("kc_down_resistance", 0.54, "下跌抵抗"))
    if "kc_up_jump_gap" in ids:
        result.extend([_event("kc_up_jump_three_stars", 0.52, "跳空上涨星线族"), _event("kc_three_gap_up", 0.52, "三空阳线倾向")])
    if "kc_down_jump_gap" in ids:
        result.extend([_event("kc_down_jump_three_stars", 0.52, "跳空下跌星线族"), _event("kc_three_gap_down", 0.52, "三空阴线倾向")])
    if "kc_bullish_engulfing" in ids:
        result.extend([_event("kc_sunrise", 0.56, "阳线反包修复"), _event("kc_bullish_counterattack", 0.52, "好友反攻")])
    if "kc_bearish_engulfing" in ids:
        result.extend([_event("kc_down_pour", 0.56, "阴线反包走弱"), _event("kc_bearish_counterattack", 0.52, "淡友反攻")])
    if "kc_hammer" in ids:
        result.extend([_event("kc_bottom_end", 0.54, "底部尽头线"), _event("kc_bullish_inverted_hammer", 0.5, "低位倒锤头倾向")])
    if "kc_shooting_star" in ids:
        result.extend([_event("kc_top_end", 0.54, "顶部尽头线"), _event("kc_bearish_inverted_hammer", 0.5, "高位倒锤头倾向"), _event("kc_hanging_man", 0.5, "吊颈线倾向")])
    if "kc_bullish_harami" in ids:
        result.append(_event("kc_bullish_doji_harami", 0.5, "上涨孕线族"))
    if "kc_bearish_harami" in ids:
        result.append(_event("kc_bearish_doji_harami", 0.5, "下跌孕线族"))
    if low_zone and data.tail(2)["is_bull"].sum() == 2:
        result.extend([_event("kc_low_parallel_yang", 0.52, "低位并排阳线"), _event("kc_down_blocked", 0.52, "降势受阻"), _event("kc_down_pause", 0.5, "降势停顿"), _event("kc_midstream", 0.5, "中流砥柱")])
    if high_zone and data.tail(2)["is_bull"].sum() == 2:
        result.append(_event("kc_high_parallel_yang", 0.5, "高位并排阳线"))
    if high_zone and data.tail(2)["is_bear"].sum() == 2:
        result.extend([_event("kc_up_blocked", 0.52, "升势受阻"), _event("kc_up_pause", 0.5, "升势停顿"), _event("kc_two_crows", 0.5, "双飞乌鸦")])
    if "kc_morning_star" in ids:
        result.append(_event("kc_bullish_abandoned_baby", 0.52, "上涨孤独十字星倾向"))
    if "kc_evening_star" in ids:
        result.append(_event("kc_bearish_abandoned_baby", 0.52, "下跌孤独十字星倾向"))
    if data.iloc[-1]["is_bear"] and _num(data.iloc[-1]["close"]) > _num(data.iloc[-2]["low"]):
        result.append(_event("kc_down_insert", 0.48, "下降插入线"))
    if {"kc_up_pinbar", "kc_hammer"} & ids:
        result.append(_event("kc_up_stars", 0.5, "上涨星线族"))
    if {"kc_down_pinbar", "kc_shooting_star"} & ids:
        result.append(_event("kc_down_stars", 0.5, "下跌星线族"))
    return result


def _detect_graph_signals(data: pd.DataFrame) -> list[SignalEvent]:
    if len(data) < 30:
        return []
    events: list[SignalEvent] = []
    recent = data.tail(60).reset_index(drop=True)
    close = _num(recent.iloc[-1]["close"])
    prior_high = _num(recent["high"].iloc[:-1].tail(30).max())
    prior_low = _num(recent["low"].iloc[:-1].tail(30).min())
    width = (prior_high - prior_low) / prior_low if prior_low else 0
    pivots = _pivots(recent)
    lows = [p for p in pivots if p[2] == "low"][-5:]
    highs = [p for p in pivots if p[2] == "high"][-5:]
    if close > prior_high and width < 0.22:
        events.append(_event("rect_up_break", 0.66, "矩形整理后突破", {"prior_high": prior_high}))
    if close < prior_low and width < 0.22:
        events.append(_event("rect_down_target", 0.66, "矩形整理后下破", {"prior_low": prior_low}))
    if len(highs) >= 2 and len(lows) >= 2:
        high_slope = _slope(pd.Series([p[1] for p in highs]))
        low_slope = _slope(pd.Series([p[1] for p in lows]))
        if abs(high_slope) < 8 and low_slope > 2 and close > max(p[1] for p in highs[-2:]):
            events.append(_event("triangle_up_break", 0.7, "上升三角形突破"))
        if high_slope < -2 and abs(low_slope) < 8 and close < min(p[1] for p in lows[-2:]):
            events.append(_event("triangle_down_break", 0.68, "下降三角形下破"))
        if high_slope < -1 and low_slope > 1:
            events.append(_event("triangle_sym_break", 0.56, "对称三角形收敛突破观察"))
        if high_slope > 1 and low_slope < -1:
            events.append(_event("triangle_expand", 0.52, "扩散三角形震荡"))
    if _near_equal_lows(lows, tolerance=0.035) and close > _num(data["close"].tail(20).mean()):
        events.append(_event("double_bottom" if len(lows) < 3 else "triple_bottom", 0.66, "多次低点接近后修复"))
    if _near_equal_highs(highs, tolerance=0.035) and close < _num(data["close"].tail(20).mean()):
        events.append(_event("double_top" if len(highs) < 3 else "triple_top", 0.66, "多次高点接近后转弱"))
    if _head_shoulders(lows, bottom=True):
        events.append(_event("head_bottom", 0.62, "头肩底雏形"))
    if _head_shoulders(highs, bottom=False):
        events.append(_event("head_top_break", 0.62, "头肩顶破位风险"))
    if _falling_wedge(recent) and close > _num(recent["high"].tail(10).max()) * 0.98:
        events.append(_event("wedge_down_break", 0.64, "降楔整理末端向上"))
    if _rising_wedge(recent) and close < _num(recent["low"].tail(10).min()) * 1.02:
        events.append(_event("wedge_up_target", 0.64, "升楔整理末端转弱"))
    if _flag_like(recent, bullish=True):
        events.append(_event("flag_down_break", 0.58, "旗形回调后向上突破"))
    if _flag_like(recent, bullish=False):
        events.append(_event("flag_up_target", 0.58, "旗形反弹后向下回落"))
    return _dedupe(events)


def _detect_trendline_signals(data: pd.DataFrame) -> list[SignalEvent]:
    if len(data) < 25:
        return []
    events: list[SignalEvent] = []
    closes = data["close"].tail(30).reset_index(drop=True)
    slope = _slope(closes)
    close = _num(closes.iloc[-1])
    expected = _line_value(closes)
    deviation = (close / expected - 1) if expected else 0
    if slope < -2 and deviation > 0.02:
        events.append(_event("trend_breakthrough_chance", 0.66, "下降趋势线被向上突破", {"deviation": deviation}))
    if slope > 2 and deviation < -0.02:
        events.append(_event("trend_breakthrough_risk", 0.66, "上升趋势线被向下跌破", {"deviation": deviation}))
    if slope < -1 and abs(deviation) < 0.02 and data.iloc[-1]["is_bear"]:
        events.append(_event("trend_resistance_pullback", 0.56, "下降趋势线附近承压"))
    if slope > 1 and abs(deviation) < 0.02 and data.iloc[-1]["is_bull"]:
        events.append(_event("trend_support_rebound", 0.56, "上升趋势线附近反弹"))
    return events


def _detect_wave_signals(data: pd.DataFrame) -> list[SignalEvent]:
    if len(data) < 35:
        return []
    events: list[SignalEvent] = []
    pivots = _pivots(data.tail(90).reset_index(drop=True), order=3)
    if len(pivots) < 5:
        return events
    last = pivots[-5:]
    prices = [point[1] for point in last]
    close = _num(data.iloc[-1]["close"])
    if prices[-1] > prices[-2] and prices[-2] < prices[-4]:
        events.append(_event("elliott_c_reversal", 0.58, "回调浪后价格修复"))
    if prices[-1] > prices[-3] > prices[-5]:
        events.append(_event("elliott_c_up_continuation", 0.56, "上升波段延续"))
    if prices[-1] < prices[-3] < prices[-5]:
        events.append(_event("elliott_c_down_continuation", 0.56, "下降波段延续"))
    ma20 = _num(data.iloc[-1].get("ma20"))
    if ma20 and abs(close / ma20 - 1) < 0.02 and data.iloc[-1]["is_bull"]:
        events.append(_event("elliott_b_pullback", 0.5, "回调 b 浪中继观察"))
    return events


def _detect_harmonic_signals(data: pd.DataFrame) -> list[SignalEvent]:
    pivots = _pivots(data.tail(120).reset_index(drop=True), order=3)
    if len(pivots) < 5:
        return []
    points = pivots[-5:]
    prices = [point[1] for point in points]
    x, a, b, c, d = prices
    xa = a - x
    ab = b - a
    bc = c - b
    cd = d - c
    events: list[SignalEvent] = []
    if xa == 0 or ab == 0 or bc == 0:
        return events
    ab_ratio = abs(ab / xa)
    bc_ratio = abs(bc / ab)
    cd_ratio = abs(cd / bc)
    latest_bull = data.iloc[-1]["is_bull"]
    if latest_bull and 0.35 <= ab_ratio <= 0.75 and 1.1 <= cd_ratio <= 2.8:
        events.append(_event("gartley_bullish", 0.54, "XABCD 比例接近 Gartley/Bat 反转区", {"ab": ab_ratio, "cd": cd_ratio}))
    if latest_bull and 0.35 <= ab_ratio <= 0.6 and 1.6 <= cd_ratio <= 3.2:
        events.append(_event("bat_bullish", 0.54, "XABCD 比例接近 Bat 反转区", {"ab": ab_ratio, "cd": cd_ratio}))
    if latest_bull and 0.7 <= bc_ratio <= 1.4 and 1.2 <= cd_ratio <= 2.5:
        events.append(_event("cypher_bullish", 0.52, "XABCD 比例接近 Cypher 完成区", {"bc": bc_ratio, "cd": cd_ratio}))
    if latest_bull and 2.0 <= cd_ratio <= 4.0:
        events.append(_event("crab_bullish", 0.5, "XABCD 比例接近 Crab 扩展区", {"cd": cd_ratio}))
    if events and _num(data.iloc[-1]["close"]) < max(prices):
        events.append(_event("cypher_predict", 0.48, "C 点后向 D 点反弹预测"))
        events.append(_event("bat_third_target", 0.48, "蝙蝠形态反弹目标观察"))
    return _dedupe(events)


def _detect_chan_signals(item: SignalInput, data: pd.DataFrame) -> list[SignalEvent]:
    try:
        screening_input = ScreeningInput(
            ts_code=item.ts_code,
            trade_date=item.trade_date,
            daily=data,
            daily_basic=pd.DataFrame(),
            stock_basic=item.stock_basic,
            metadata=item.metadata,
        )
        chan_events = evaluate_chan_signals(screening_input)
    except Exception:
        return []
    events = []
    for signal in chan_events:
        if not signal.passed:
            continue
        signal_id = signal.signal_name
        if signal_id not in SIGNAL_DEFINITIONS:
            signal_id = {
                "chan_second_third_overlap": "chan_second_third_overlap",
            }.get(signal_id, signal_id)
        if signal_id not in SIGNAL_DEFINITIONS:
            continue
        confidence = max(0.35, min(float(signal.score) / 100.0, 0.95))
        events.append(
            _event(
                signal_id,
                confidence,
                signal.label,
                evidence={"watch_levels": signal.watch_levels, "metrics": signal.metrics},
                risks=signal.risk_flags,
            )
        )
    return events


def _score_decision(events: list[SignalEvent], data: pd.DataFrame) -> tuple[float, str, str]:
    buy = sum(event.score for event in events if event.side == "buy")
    sell = sum(event.score for event in events if event.side == "sell")
    hold = sum(event.score for event in events if event.side in {"hold", "cash"})
    net = buy - sell
    score = max(0.0, min(100.0, 50.0 + net / 6.0 + hold / 20.0))
    if score >= 72:
        decision = "买入观察"
    elif score >= 58:
        decision = "持有"
    elif score >= 45:
        decision = "观望"
    elif sell > buy:
        decision = "减仓"
    else:
        decision = "观望"
    ma20_slope = _slope(data["ma20"].tail(12))
    ma60_slope = _slope(data["ma60"].tail(20))
    if ma20_slope > 2 and ma60_slope >= 0:
        trend = "看多"
    elif ma20_slope < -2 and ma60_slope <= 0:
        trend = "看空"
    else:
        trend = "震荡"
    return round(score, 2), decision, trend


def _key_levels(data: pd.DataFrame) -> dict[str, Any]:
    recent = data.tail(60)
    support = _num(recent["low"].tail(20).min())
    resistance = _num(recent["high"].tail(20).max())
    return {"support": _fmt(support), "resistance": _fmt(resistance)}


def _llm_review(
    ts_code: str,
    name: str,
    trade_date: str,
    close: float,
    events: list[SignalEvent],
    *,
    data_sources: dict[str, Any],
) -> str:
    payload = {
        "ts_code": ts_code,
        "name": name,
        "trade_date": trade_date,
        "data_sources": data_sources,
        "missing_fields": [] if data_sources else ["data_sources"],
        "close": close,
        "signals": [event.to_dict() for event in events[:12]],
    }
    prompt = (
        "请只基于以下真实结构化数据和本地技术信号，用两句话给出分析摘要；"
        "不得编造价格、涨跌幅、成交量、新闻、公告、题材或未提供的数据，不要给确定性投资承诺：\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )
    response = ChatLLM(timeout_seconds=45).chat([{"role": "user", "content": prompt}])
    return str(response.content or "").strip()


def _signal_data_sources(item: SignalInput) -> dict[str, Any]:
    sources = {}
    nested = item.metadata.get("data_sources") if isinstance(item.metadata, dict) else None
    if isinstance(nested, dict):
        sources.update({str(key): value for key, value in nested.items() if value})
    if isinstance(item.metadata, dict):
        for key in ("data_source", "daily_source", "minute_30m_source", "daily_basic_source"):
            value = item.metadata.get(key)
            if value:
                sources[key] = value
    return sources


def _dedupe(events: list[SignalEvent]) -> list[SignalEvent]:
    best: dict[str, SignalEvent] = {}
    for event in events:
        old = best.get(event.signal_id)
        if old is None or event.score > old.score:
            best[event.signal_id] = event
    return list(best.values())


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _slope(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype=float)
    if len(values) < 2:
        return 0.0
    x = np.arange(len(values), dtype=float)
    base = np.nanmean(values)
    if base == 0 or np.isnan(base):
        return 0.0
    return float(np.polyfit(x, values / base * 100.0, 1)[0])


def _line_value(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype=float)
    if len(values) < 2:
        return _num(values[-1] if len(values) else 0)
    x = np.arange(len(values), dtype=float)
    coef = np.polyfit(x, values, 1)
    return float(coef[0] * x[-1] + coef[1])


def _cross_up(prev_a: float, prev_b: float, curr_a: float, curr_b: float) -> bool:
    return prev_a <= prev_b and curr_a > curr_b and min(prev_a, prev_b, curr_a, curr_b) > 0


def _cross_down(prev_a: float, prev_b: float, curr_a: float, curr_b: float) -> bool:
    return prev_a >= prev_b and curr_a < curr_b and min(prev_a, prev_b, curr_a, curr_b) > 0


def _recent_gain(data: pd.DataFrame, periods: int) -> float:
    if len(data) <= periods:
        return 0.0
    start = _num(data.iloc[-periods - 1]["close"])
    end = _num(data.iloc[-1]["close"])
    return end / start - 1.0 if start else 0.0


def _price_rank(data: pd.DataFrame, price: float, lookback: int = 80) -> float:
    recent = data.tail(lookback)
    low = _num(recent["low"].min())
    high = _num(recent["high"].max())
    if high <= low:
        return 0.5
    return max(0.0, min(1.0, (price - low) / (high - low)))


def _pivots(data: pd.DataFrame, order: int = 2) -> list[tuple[int, float, str]]:
    if len(data) < order * 2 + 1:
        return []
    highs = data["high"].astype(float).to_numpy()
    lows = data["low"].astype(float).to_numpy()
    pivots: list[tuple[int, float, str]] = []
    for idx in range(order, len(data) - order):
        high_window = highs[idx - order: idx + order + 1]
        low_window = lows[idx - order: idx + order + 1]
        if highs[idx] == np.max(high_window):
            pivots.append((idx, float(highs[idx]), "high"))
        if lows[idx] == np.min(low_window):
            pivots.append((idx, float(lows[idx]), "low"))
    return pivots


def _near_equal_lows(points: list[tuple[int, float, str]], *, tolerance: float) -> bool:
    if len(points) < 2:
        return False
    values = [point[1] for point in points[-3:]]
    avg = float(np.mean(values))
    return avg > 0 and (max(values) - min(values)) / avg <= tolerance


def _near_equal_highs(points: list[tuple[int, float, str]], *, tolerance: float) -> bool:
    return _near_equal_lows(points, tolerance=tolerance)


def _head_shoulders(points: list[tuple[int, float, str]], *, bottom: bool) -> bool:
    if len(points) < 3:
        return False
    values = [point[1] for point in points[-3:]]
    left, head, right = values
    if bottom:
        return head < left and head < right and abs(left - right) / max(left, right, 1) < 0.08
    return head > left and head > right and abs(left - right) / max(left, right, 1) < 0.08


def _falling_wedge(data: pd.DataFrame) -> bool:
    highs = data["high"].tail(30).reset_index(drop=True)
    lows = data["low"].tail(30).reset_index(drop=True)
    return _slope(highs) < -1 and _slope(lows) < 0 and abs(_slope(highs)) > abs(_slope(lows))


def _rising_wedge(data: pd.DataFrame) -> bool:
    highs = data["high"].tail(30).reset_index(drop=True)
    lows = data["low"].tail(30).reset_index(drop=True)
    return _slope(lows) > 1 and _slope(highs) > 0 and _slope(lows) > _slope(highs)


def _flag_like(data: pd.DataFrame, *, bullish: bool) -> bool:
    if len(data) < 25:
        return False
    pole = _recent_gain(data.iloc[:-10], 8)
    flag = _recent_gain(data, 10)
    if bullish:
        return pole > 0.08 and -0.08 < flag < 0.03 and _num(data.iloc[-1]["close"]) > _num(data["close"].tail(5).mean())
    return pole < -0.08 and -0.03 < flag < 0.08 and _num(data.iloc[-1]["close"]) < _num(data["close"].tail(5).mean())


def _bullish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
    return bool(prev["is_bear"] and curr["is_bull"] and _num(curr["open"]) <= _num(prev["close"]) and _num(curr["close"]) >= _num(prev["open"]))


def _bearish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
    return bool(prev["is_bull"] and curr["is_bear"] and _num(curr["open"]) >= _num(prev["close"]) and _num(curr["close"]) <= _num(prev["open"]))


def _morning_star(a: pd.Series, b: pd.Series, c: pd.Series) -> bool:
    return bool(a["is_bear"] and _num(b["body"]) < _num(a["body"]) * 0.6 and c["is_bull"] and _num(c["close"]) > (_num(a["open"]) + _num(a["close"])) / 2)


def _evening_star(a: pd.Series, b: pd.Series, c: pd.Series) -> bool:
    return bool(a["is_bull"] and _num(b["body"]) < _num(a["body"]) * 0.6 and c["is_bear"] and _num(c["close"]) < (_num(a["open"]) + _num(a["close"])) / 2)


def _piercing(prev: pd.Series, curr: pd.Series) -> bool:
    midpoint = (_num(prev["open"]) + _num(prev["close"])) / 2
    return bool(prev["is_bear"] and curr["is_bull"] and _num(curr["open"]) < _num(prev["low"]) and _num(curr["close"]) > midpoint)


def _dark_cloud(prev: pd.Series, curr: pd.Series) -> bool:
    midpoint = (_num(prev["open"]) + _num(prev["close"])) / 2
    return bool(prev["is_bull"] and curr["is_bear"] and _num(curr["open"]) > _num(prev["high"]) and _num(curr["close"]) < midpoint)


def _harami(prev: pd.Series, curr: pd.Series, *, bullish: bool) -> bool:
    inside = _num(curr["open"]) > min(_num(prev["open"]), _num(prev["close"])) and _num(curr["close"]) < max(_num(prev["open"]), _num(prev["close"]))
    return bool(inside and ((prev["is_bear"] and curr["is_bull"]) if bullish else (prev["is_bull"] and curr["is_bear"])))


def _tweezer_bottom(prev: pd.Series, curr: pd.Series) -> bool:
    low = max(_num(prev["low"]), _num(curr["low"]))
    return low > 0 and abs(_num(prev["low"]) - _num(curr["low"])) / low < 0.01 and curr["is_bull"]


def _tweezer_top(prev: pd.Series, curr: pd.Series) -> bool:
    high = max(_num(prev["high"]), _num(curr["high"]))
    return high > 0 and abs(_num(prev["high"]) - _num(curr["high"])) / high < 0.01 and curr["is_bear"]


def _double_needle(frame: pd.DataFrame) -> bool:
    needles = frame[(frame["lower_shadow"] > frame["body"] * 2.0) & (frame["is_bull"] | (frame["body"] / frame["range"] < 0.2))]
    return len(needles) >= 2
