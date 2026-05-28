from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


BUY_ADVICES = {"强烈买入", "买入"}
HOLD_ADVICES = {"持有", "观望"}
SELL_ADVICES = {"减仓", "卖出", "强烈卖出"}
ALLOWED_ADVICES = BUY_ADVICES | HOLD_ADVICES | SELL_ADVICES

BIAS_THRESHOLD = 5.0
VOLUME_SHRINK_RATIO = 0.7
VOLUME_HEAVY_RATIO = 1.5
MA_SUPPORT_TOLERANCE = 0.02


@dataclass(frozen=True, slots=True)
class DsaLocalDecision:
    score: float
    operation_advice: str
    decision_type: str
    trend_prediction: str
    confidence_level: str
    trend_status: str
    trend_strength: float = 0.0
    signal_reasons: list[str] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)
    raw_score: float = 0.0
    raw_operation_advice: str = ""
    adjustment_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "operation_advice": self.operation_advice,
            "decision_type": self.decision_type,
            "trend_prediction": self.trend_prediction,
            "confidence_level": self.confidence_level,
            "trend_status": self.trend_status,
            "trend_strength": self.trend_strength,
            "signal_reasons": list(self.signal_reasons),
            "risk_factors": list(self.risk_factors),
            "raw_score": self.raw_score,
            "raw_operation_advice": self.raw_operation_advice,
            "adjustment_reasons": list(self.adjustment_reasons),
        }


def build_local_dsa_decision(indicator: Any, *, chip: dict[str, Any] | None = None) -> DsaLocalDecision:
    technical = getattr(indicator, "technical", {}) or {}
    volume = getattr(indicator, "volume", {}) or {}
    moneyflow = getattr(indicator, "moneyflow", {}) or {}
    fundamentals = getattr(indicator, "fundamentals", {}) or {}
    support_resistance = getattr(indicator, "support_resistance", {}) or {}
    close = _num(getattr(indicator, "close", None))
    ma = technical.get("ma") if isinstance(technical.get("ma"), dict) else {}
    ma5 = _optional_float(ma.get("ma5"))
    ma10 = _optional_float(ma.get("ma10"))
    ma20 = _optional_float(ma.get("ma20"))
    ma60 = _optional_float(ma.get("ma60"))

    trend_status, trend_strength, ma_text = _trend_status(ma5, ma10, ma20, ma60)
    score = _trend_score(trend_status)
    reasons: list[str] = []
    risks: list[str] = []
    if trend_status in {"强势多头", "多头排列"}:
        reasons.append(f"{ma_text}，顺势做多")
    elif trend_status in {"空头排列", "强势空头"}:
        risks.append(f"{ma_text}，不宜做多")

    bias = _bias_ma5(technical, close, ma5)
    bias10 = _bias_value(technical, close, "ma10", ma10)
    bias20 = _bias_value(technical, close, "ma20", ma20)
    strong_trend = trend_status == "强势多头" and trend_strength >= 70
    effective_threshold = BIAS_THRESHOLD * 1.5 if strong_trend else BIAS_THRESHOLD
    if bias is None:
        score += 4
        risks.append("MA5 乖离率数据不足")
    elif bias < 0:
        if bias > -3:
            score += 20
            reasons.append(f"价格略低于 MA5({bias:.1f}%)，回踩买点")
        elif bias > -5:
            score += 16
            reasons.append(f"价格回踩 MA5({bias:.1f}%)，观察支撑")
        else:
            score += 8
            risks.append(f"乖离率过大({bias:.1f}%)，可能破位")
    elif bias < 2:
        score += 18
        reasons.append(f"价格贴近 MA5({bias:.1f}%)，介入位置较好")
    elif bias < BIAS_THRESHOLD:
        score += 14
        reasons.append(f"价格略高于 MA5({bias:.1f}%)，可小仓跟踪")
    elif bias > effective_threshold:
        score += 4
        risks.append(f"乖离率过高({bias:.1f}%>{effective_threshold:.1f}%)，严禁追高")
    elif bias > BIAS_THRESHOLD and strong_trend:
        score += 10
        reasons.append(f"强势趋势中乖离率偏高({bias:.1f}%)，可轻仓追踪")
    else:
        score += 4
        risks.append(f"乖离率过高({bias:.1f}%>{BIAS_THRESHOLD:.1f}%)，严禁追高")

    volume_status = str(volume.get("status") or "量能正常")
    volume_ratio = _optional_float(volume.get("volume_ratio_5d"))
    score += _volume_score(volume_status)
    if volume_status == "缩量回调":
        reasons.append("缩量回调，抛压减轻")
    elif volume_status == "放量下跌":
        risks.append("放量下跌，注意风险")
    if volume_ratio is not None:
        if volume_ratio >= VOLUME_HEAVY_RATIO and volume_status == "放量上涨":
            reasons.append(f"量比5日 {volume_ratio:.2f}，量价配合")
        elif volume_ratio >= VOLUME_HEAVY_RATIO and volume_status == "放量下跌":
            risks.append(f"量比5日 {volume_ratio:.2f}，放量回落")
        elif volume_ratio <= VOLUME_SHRINK_RATIO and bias is not None and bias <= 2:
            reasons.append(f"量比5日 {volume_ratio:.2f}，低量回踩更接近理想买点")

    turnover = _optional_float(fundamentals.get("turnover_rate") or fundamentals.get("turnover_rate_f"))
    if turnover is not None:
        if turnover > 12:
            risks.append(f"换手率偏高({turnover:.1f}%)，短线分歧较大")
        elif 1 <= turnover <= 8:
            reasons.append(f"换手率 {turnover:.1f}%，流动性处于可观察区间")

    if _is_ma_support(close, ma5):
        score += 5
        reasons.append("MA5 支撑有效")
    if _is_ma_support(close, ma10):
        score += 5
        reasons.append("MA10 支撑有效")

    macd_signal = str((technical.get("macd") or {}).get("signal") or "")
    score += _macd_score(macd_signal)
    if "金叉" in macd_signal or "上穿" in macd_signal:
        reasons.append(macd_signal)
    elif "死叉" in macd_signal or "下穿" in macd_signal:
        risks.append(macd_signal)
    elif macd_signal:
        reasons.append(f"MACD {macd_signal}")

    rsi12 = _optional_float((technical.get("rsi") or {}).get("rsi12"))
    rsi_status, rsi_text = _rsi_status(rsi12)
    score += _rsi_score(rsi_status)
    if rsi_status in {"超卖", "强势买入"}:
        reasons.append(rsi_text)
    elif rsi_status == "超买":
        risks.append(rsi_text)
    elif rsi_text:
        reasons.append(rsi_text)

    score = max(0.0, min(100.0, score))
    raw_score = score
    advice = _operation_advice(score, trend_status)
    raw_advice = advice
    decision_type = decision_type_for_advice(advice)
    trend_prediction = _trend_prediction(trend_status)

    advice, decision_type, score, trend_prediction, adjustments = _stabilize_with_structure(
        advice,
        decision_type,
        score,
        trend_prediction,
        close=close,
        bias=bias,
        rsi12=rsi12,
        kdj=technical.get("kdj") if isinstance(technical.get("kdj"), dict) else {},
        boll=technical.get("boll") if isinstance(technical.get("boll"), dict) else {},
        volume_status=volume_status,
        support=support_resistance.get("support"),
        resistance=support_resistance.get("resistance"),
        moneyflow=moneyflow,
        fundamentals=fundamentals,
        chip=chip or {},
        trend_status=trend_status,
        trend_strength=trend_strength,
        bias10=bias10,
        bias20=bias20,
        reasons=reasons,
        risks=risks,
    )
    confidence_level = _confidence_level(score)

    if not reasons:
        reasons.append("技术信号中性，等待更清晰结构")
    if not risks:
        risks.append("未发现显著结构风险，仍需控制仓位")

    return DsaLocalDecision(
        score=round(score, 2),
        operation_advice=advice,
        decision_type=decision_type,
        trend_prediction=trend_prediction,
        confidence_level=confidence_level,
        trend_status=trend_status,
        trend_strength=round(trend_strength, 2),
        signal_reasons=reasons,
        risk_factors=risks,
        raw_score=round(raw_score, 2),
        raw_operation_advice=raw_advice,
        adjustment_reasons=adjustments,
    )


def normalize_operation_advice(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    aliases = {
        "strong buy": "强烈买入",
        "strong_buy": "强烈买入",
        "强买": "强烈买入",
        "buy": "买入",
        "加仓": "买入",
        "hold": "持有",
        "持有观察": "持有",
        "洗盘观察": "持有",
        "wait": "观望",
        "watch": "观望",
        "等待": "观望",
        "震荡观望": "观望",
        "reduce": "减仓",
        "trim": "减仓",
        "减仓/卖出": "减仓",
        "sell": "卖出",
        "strong sell": "强烈卖出",
        "strong_sell": "强烈卖出",
    }
    if text in ALLOWED_ADVICES:
        return text
    return aliases.get(text.lower()) or aliases.get(text)


def decision_type_for_advice(advice: str) -> str:
    if advice in BUY_ADVICES:
        return "buy"
    if advice in SELL_ADVICES:
        return "sell"
    return "hold"


def _trend_status(ma5: float | None, ma10: float | None, ma20: float | None, ma60: float | None = None) -> tuple[str, float, str]:
    if ma5 is None or ma10 is None or ma20 is None or ma5 <= 0 or ma10 <= 0 or ma20 <= 0:
        return "盘整", 50.0, "均线数据不足"
    if ma5 > ma10 > ma20:
        spread = (ma5 - ma20) / ma20 * 100
        long_confirm = ma60 is not None and ma60 > 0 and ma20 > ma60
        if spread > 5:
            strength = 92.0 if long_confirm else 88.0
            suffix = "，MA60 也确认中期趋势" if long_confirm else ""
            return "强势多头", strength, f"强势多头排列，均线发散上行{suffix}"
        strength = 78.0 if long_confirm else 75.0
        suffix = "，MA20>MA60" if long_confirm else ""
        return "多头排列", strength, f"多头排列 MA5>MA10>MA20{suffix}"
    if ma5 > ma10 and ma10 <= ma20:
        return "弱势多头", 55.0, "弱势多头，MA5>MA10 但 MA10<=MA20"
    if ma5 < ma10 < ma20:
        spread = (ma20 - ma5) / ma5 * 100
        if spread > 5:
            return "强势空头", 10.0, "强势空头排列，均线发散下行"
        return "空头排列", 25.0, "空头排列 MA5<MA10<MA20"
    if ma5 < ma10 and ma10 >= ma20:
        return "弱势空头", 40.0, "弱势空头，MA5<MA10 但 MA10>=MA20"
    return "盘整", 50.0, "均线缠绕，趋势不明"


def _trend_score(trend_status: str) -> int:
    return {
        "强势多头": 30,
        "多头排列": 26,
        "弱势多头": 18,
        "盘整": 12,
        "弱势空头": 8,
        "空头排列": 4,
        "强势空头": 0,
    }.get(trend_status, 12)


def _operation_advice(score: float, trend_status: str) -> str:
    if score >= 75 and trend_status in {"强势多头", "多头排列"}:
        return "强烈买入"
    if score >= 60 and trend_status in {"强势多头", "多头排列", "弱势多头"}:
        return "买入"
    if score >= 45:
        return "持有"
    if score >= 35 and trend_status in {"弱势空头", "空头排列", "强势空头"}:
        return "减仓"
    if score >= 30:
        return "观望"
    if trend_status in {"空头排列", "强势空头"}:
        return "强烈卖出"
    return "卖出"


def _trend_prediction(trend_status: str) -> str:
    if trend_status == "强势多头":
        return "强烈看多"
    if trend_status in {"多头排列", "弱势多头"}:
        return "看多"
    if trend_status == "强势空头":
        return "强烈看空"
    if trend_status in {"空头排列", "弱势空头"}:
        return "看空"
    return "震荡"


def _confidence_level(score: float) -> str:
    if score >= 75 or score < 30:
        return "高"
    if score >= 45:
        return "中"
    return "低"


def _bias_ma5(technical: dict[str, Any], close: float | None, ma5: float | None) -> float | None:
    return _bias_value(technical, close, "ma5", ma5)


def _bias_value(technical: dict[str, Any], close: float | None, key: str, ma_value: float | None) -> float | None:
    bias = technical.get("bias") if isinstance(technical.get("bias"), dict) else {}
    parsed = _optional_float(bias.get(key))
    if parsed is not None:
        return parsed
    if close is None or ma_value in (None, 0):
        return None
    return (close - ma_value) / ma_value * 100


def _volume_score(status: str) -> int:
    return {
        "缩量回调": 15,
        "放量上涨": 12,
        "量能正常": 10,
        "缩量上涨": 6,
        "放量下跌": 0,
    }.get(status, 8)


def _is_ma_support(close: float | None, ma_value: float | None) -> bool:
    if close is None or ma_value is None or ma_value <= 0:
        return False
    return close >= ma_value and abs(close - ma_value) / ma_value <= MA_SUPPORT_TOLERANCE


def _macd_score(signal: str) -> int:
    if "零轴上金叉" in signal:
        return 15
    if "金叉" in signal:
        return 12
    if "上穿零轴" in signal:
        return 10
    if "多头" in signal:
        return 8
    if "空头" in signal:
        return 2
    if "下穿零轴" in signal or "死叉" in signal:
        return 0
    return 5


def _rsi_status(rsi12: float | None) -> tuple[str, str]:
    if rsi12 is None:
        return "中性", "RSI 数据不足"
    if rsi12 > 70:
        return "超买", f"RSI 超买({rsi12:.1f}>70)，短期回调风险高"
    if rsi12 > 60:
        return "强势买入", f"RSI 强势({rsi12:.1f})，多头力量充足"
    if rsi12 >= 40:
        return "中性", f"RSI 中性({rsi12:.1f})，震荡整理中"
    if rsi12 >= 30:
        return "弱势", f"RSI 弱势({rsi12:.1f})，关注反弹"
    return "超卖", f"RSI 超卖({rsi12:.1f}<30)，反弹机会大"


def _rsi_score(status: str) -> int:
    return {
        "超卖": 10,
        "强势买入": 8,
        "中性": 5,
        "弱势": 3,
        "超买": 0,
    }.get(status, 5)


def _stabilize_with_structure(
    advice: str,
    decision_type: str,
    score: float,
    trend_prediction: str,
    *,
    close: float | None,
    bias: float | None,
    rsi12: float | None,
    kdj: dict[str, Any],
    boll: dict[str, Any],
    volume_status: str,
    support: Any,
    resistance: Any,
    moneyflow: dict[str, Any],
    fundamentals: dict[str, Any],
    chip: dict[str, Any],
    trend_status: str,
    trend_strength: float,
    bias10: float | None,
    bias20: float | None,
    reasons: list[str],
    risks: list[str],
) -> tuple[str, str, float, str, list[str]]:
    adjustments: list[str] = []
    main_flow = _first_number(
        moneyflow.get("main_net_amount"),
        moneyflow.get("main_net_amount_5d"),
        moneyflow.get("main_net_amount_10d"),
    )
    flow_values = [
        value for value in (
            _optional_float(moneyflow.get("main_net_amount")),
            _optional_float(moneyflow.get("main_net_amount_5d")),
            _optional_float(moneyflow.get("main_net_amount_10d")),
        )
        if value is not None
    ]
    negative_flow_count = sum(1 for value in flow_values if value < 0)
    support_price = _first_level(support)
    resistance_price = _first_level(resistance)
    near_resistance = close is not None and resistance_price is not None and close >= resistance_price * 0.97 and close <= resistance_price * 1.01
    near_support = close is not None and support_price is not None and close <= support_price * 1.03 and close >= support_price * 0.985
    broke_support = close is not None and support_price is not None and close < support_price * 0.985
    kdj_k = _optional_float(kdj.get("k"))
    kdj_d = _optional_float(kdj.get("d"))
    kdj_j = _optional_float(kdj.get("j"))
    boll_position = str(boll.get("position") or "")
    profit_ratio = _profit_ratio_pct(chip.get("profit_ratio"))
    pe = _optional_float(fundamentals.get("pe"))
    pb = _optional_float(fundamentals.get("pb"))
    roe = _optional_float(fundamentals.get("roe"))
    profit = _optional_float(fundamentals.get("profit"))
    debt = _optional_float(fundamentals.get("debt_to_assets"))

    high_bias = bias is not None and bias > BIAS_THRESHOLD
    medium_bias = bias10 is not None and bias10 > 8
    stretched_from_ma20 = bias20 is not None and bias20 > 15
    warning_bias = bias is not None and bias > 3.0
    rsi_overbought = rsi12 is not None and rsi12 > 70
    kdj_overbought = any(value is not None and value >= limit for value, limit in [(kdj_k, 85), (kdj_d, 80), (kdj_j, 100)])
    chip_overheated = profit_ratio is not None and profit_ratio >= 90
    weak_fundamentals = (
        (profit is not None and profit < 0)
        or (roe is not None and roe < 3)
        or (pe is not None and pe >= 80)
        or (pb is not None and pb >= 10)
        or (debt is not None and debt >= 75)
    )
    overheat = high_bias or medium_bias or stretched_from_ma20 or rsi_overbought or kdj_overbought or chip_overheated or "上轨上方" in boll_position
    buy_location_ok = (
        (bias is not None and bias <= 2.0)
        or near_support
        or volume_status == "缩量回调"
    )
    funds_confirmed = main_flow is not None and main_flow > 0 and negative_flow_count == 0

    if high_bias:
        risks.append(f"乖离率超过 {BIAS_THRESHOLD:.0f}% 安全线，严禁追高")
    if medium_bias:
        risks.append(f"MA10 乖离偏高({bias10:.1f}%)，短线性价比下降")
    if stretched_from_ma20:
        risks.append(f"MA20 乖离偏高({bias20:.1f}%)，趋势虽强但位置偏高")
    if chip_overheated:
        risks.append(f"获利盘偏高({profit_ratio:.1f}%)，需防获利回吐")
    if kdj_overbought:
        risks.append("KDJ 高位，短线追高性价比下降")
    if weak_fundamentals:
        risks.append("基本面或估值约束偏弱，买入结论需降级")
    if trend_strength >= 85 and advice in SELL_ADVICES and not broke_support:
        reasons.append("中短期趋势强度仍高，卖出结论需等待破位确认")

    if advice in BUY_ADVICES and overheat and not buy_location_ok:
        reason = "技术过热且不在理想买点，按 daily_stock_analysis 风格降为持有/观望"
        adjustments.append(reason)
        risks.append(reason)
        next_advice = "观望" if high_bias or chip_overheated or negative_flow_count >= 1 else "持有"
        next_score = min(score, 59.0 if next_advice == "观望" else 65.0)
        return next_advice, "hold", next_score, "震荡" if next_advice == "观望" else trend_prediction, adjustments
    if advice in BUY_ADVICES and rsi_overbought and (warning_bias or kdj_overbought or weak_fundamentals):
        reason = "RSI/KDJ 或基本面风险与买入结论冲突，先按持有处理"
        adjustments.append(reason)
        risks.append(reason)
        return "持有", "hold", min(score, 65.0), trend_prediction, adjustments
    if advice in BUY_ADVICES and not funds_confirmed and warning_bias:
        reason = "资金未充分确认且乖离偏高，不追高买入"
        adjustments.append(reason)
        risks.append(reason)
        return "持有", "hold", min(score, 62.0), trend_prediction, adjustments
    if advice in BUY_ADVICES and near_resistance and (main_flow is None or main_flow <= 0):
        reason = "价格接近压力位且资金流未确认，不追高"
        adjustments.append(reason)
        risks.append(reason)
        return "持有", "hold", min(score, 59.0), "震荡", adjustments
    if advice in BUY_ADVICES and main_flow is not None and main_flow < 0:
        reason = "主力资金流出与买入结论冲突，等待资金回流"
        adjustments.append(reason)
        risks.append(reason)
        return "观望", "hold", min(score, 59.0), "震荡", adjustments
    sell_has_confirmation = broke_support or volume_status == "放量下跌" or negative_flow_count >= 2
    if advice in SELL_ADVICES and trend_status in {"强势多头", "多头排列", "弱势多头"} and overheat and not sell_has_confirmation:
        reason = "趋势仍偏多但高位过热，先降为观望而不是直接减仓"
        adjustments.append(reason)
        reasons.append(reason)
        return "观望", "hold", max(45.0, min(score, 59.0)), "震荡", adjustments
    if advice in SELL_ADVICES and near_support and not broke_support and (main_flow is None or main_flow >= 0):
        reason = "价格贴近支撑且资金未确认流出，先按持有观察处理"
        adjustments.append(reason)
        reasons.append(reason)
        return "持有", "hold", max(45.0, min(score, 59.0)), "震荡", adjustments
    return advice, decision_type, score, trend_prediction, adjustments


def _first_level(value: Any) -> float | None:
    if isinstance(value, (list, tuple)) and value:
        return _optional_float(value[0])
    return _optional_float(value)


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _optional_float(value)
        if parsed is not None:
            return parsed
    return None


def _profit_ratio_pct(value: Any) -> float | None:
    parsed = _optional_float(value)
    if parsed is None:
        return None
    return parsed * 100 if 0 <= parsed <= 1 else parsed


def _optional_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> float | None:
    parsed = _optional_float(value)
    return parsed
