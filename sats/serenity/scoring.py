from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

import pandas as pd

from sats.serenity.models import SerenityCandidateResult, SerenityEvidence, SerenityFactorResult


# Adapted from muxuuu/serenity-skill (MIT) scorecard and UZI-Skill's
# Serenity feature engineering (MIT). SATS keeps the scoring deterministic.
FACTOR_WEIGHTS: dict[str, float] = {
    "demand_inflection": 15.0,
    "architecture_coupling": 10.0,
    "chokepoint_severity": 15.0,
    "supplier_concentration": 12.0,
    "expansion_difficulty": 12.0,
    "evidence_quality": 15.0,
    "valuation_disconnect": 11.0,
    "catalyst_timing": 10.0,
}

FACTOR_LABELS = {
    "demand_inflection": "需求拐点",
    "architecture_coupling": "架构耦合",
    "chokepoint_severity": "瓶颈强度",
    "supplier_concentration": "供应商集中度",
    "expansion_difficulty": "扩产难度",
    "evidence_quality": "证据质量",
    "valuation_disconnect": "估值错位",
    "catalyst_timing": "催化时点",
}

PENALTY_LABELS = {
    "dilution_financing": "融资稀释",
    "governance": "治理风险",
    "geopolitics": "地缘风险",
    "liquidity": "流动性风险",
    "hype_risk": "题材炒作",
    "accounting_quality": "会计质量",
    "cyclicality": "周期性",
    "alternative_design_risk": "替代设计风险",
}

AI_CHAIN_KEYWORDS = (
    "ai芯片",
    "ai 芯片",
    "gpu",
    "asic",
    "risc-v",
    "hbm",
    "ddr",
    "存储",
    "光模块",
    "光芯片",
    "cpo",
    "硅光",
    "光通信",
    "光器件",
    "激光器",
    "eml",
    "vcsel",
    "inp",
    "磷化铟",
    "砷化镓",
    "化合物半导体",
    "衬底",
    "外延",
    "cowos",
    "先进封装",
    "封装基板",
    "abf",
    "载板",
    "光刻",
    "刻蚀",
    "量测",
    "测试机",
    "pcb",
    "高速铜",
    "铜连接",
    "连接器",
    "液冷",
    "散热",
    "服务器电源",
    "pdu",
    "数据中心",
    "算力",
    "交换机",
    "ai服务器",
    "ai 服务器",
    "人形机器人",
    "具身智能",
    "谐波减速器",
    "rv减速器",
    "行星滚柱丝杠",
    "灵巧手",
    "空心杯电机",
    "六维力",
    "力传感器",
    "触觉传感器",
)

TIER_MAP: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    (
        "材料耗材",
        1.00,
        (
            "inp",
            "磷化铟",
            "砷化镓",
            "化合物半导体",
            "衬底",
            "外延",
            "晶体生长",
            "abf",
            "载板",
            "封装基板",
            "空芯光纤",
            "电子特气",
            "光刻胶",
        ),
    ),
    ("制程/封装", 0.92, ("cowos", "先进封装", "硅光", "键合", "晶圆级")),
    ("设备/测试", 0.85, ("光刻", "刻蚀", "量测", "测试机", "分选机", "设备")),
    (
        "芯片/器件",
        0.78,
        (
            "光芯片",
            "eml",
            "vcsel",
            "激光器",
            "hbm",
            "ddr",
            "asic",
            "gpu",
            "六维力",
            "力传感器",
            "谐波减速器",
            "rv减速器",
            "行星滚柱丝杠",
            "空心杯电机",
        ),
    ),
    ("基础设施", 0.70, ("数据中心", "算力", "电网", "变压器", "核电")),
    (
        "模块/子系统",
        0.62,
        ("光模块", "光引擎", "连接器", "电源", "液冷", "散热", "灵巧手", "执行器"),
    ),
    ("系统集成", 0.50, ("交换机", "ai服务器", "ai 服务器", "服务器", "机械臂", "整机")),
    ("下游需求", 0.40, ("人形机器人", "机器人", "头显", "ar眼镜", "近眼显示")),
)

STRONG_EVIDENCE_TERMS = (
    "定点",
    "量产",
    "批量交付",
    "订单",
    "中标",
    "长协",
    "认证",
    "合格供应商",
    "独供",
    "专利",
    "公告",
    "财报",
    "年报",
    "半年报",
    "问询函",
)
MEDIUM_EVIDENCE_TERMS = (
    "研报",
    "研究报告",
    "行业协会",
    "权威媒体",
    "互动易",
    "e互动",
    "机构调研",
    "送样",
    "小批量",
)
DEMAND_TERMS = (
    "订单",
    "中标",
    "长协",
    "供货",
    "定点",
    "量产",
    "扩产",
    "产能",
    "放量",
    "提价",
    "缺货",
    "满产",
    "认证",
    "backlog",
)
SCARCITY_TERMS = (
    "独供",
    "唯一",
    "仅有",
    "少数",
    "两家",
    "三家",
    "寡头",
    "满产",
    "排产",
    "缺货",
    "供不应求",
    "长交期",
)
EXPANSION_TERMS = (
    "认证周期",
    "验证周期",
    "扩产周期",
    "良率",
    "纯度",
    "工艺壁垒",
    "专利",
    "客户认证",
    "资本开支",
    "在建工程",
    "长交期",
)
IRREPLACEABLE_TERMS = (
    "不可替代",
    "切换成本",
    "独供",
    "唯一供应",
    "核心供应商",
    "认证周期",
    "专利",
    "技术壁垒",
    "客户粘性",
)


def preliminary_serenity_score(candidate: dict[str, Any]) -> float:
    blob = _text_blob(candidate)
    hits = sum(1 for keyword in AI_CHAIN_KEYWORDS if keyword in blob)
    tier_name, tier_weight = infer_chain_tier(blob)
    market_cap = _num(candidate.get("market_cap_yi"))
    amount = _num(candidate.get("amount"))
    theme_score = min(hits, 5) * 12.0
    tier_score = tier_weight * 20.0 if tier_name != "未分层" else 0.0
    size_score = 12.0 if market_cap and market_cap < 300 else 7.0 if market_cap and market_cap < 800 else 2.0
    liquidity_score = 6.0 if amount and amount > 100_000 else 2.0 if amount else 0.0
    return round(min(theme_score + tier_score + size_score + liquidity_score, 100.0), 2)


def score_serenity_candidate(payload: dict[str, Any]) -> SerenityCandidateResult:
    blob = _text_blob(payload)
    ai_hits = [keyword for keyword in AI_CHAIN_KEYWORDS if keyword in blob]
    ai_chain_hit = bool(ai_hits)
    chain_tier, chain_tier_weight = infer_chain_tier(blob)
    evidence = extract_evidence(payload, blob=blob)
    evidence_grade = strongest_evidence_grade(evidence)
    indicator = payload.get("indicator") if isinstance(payload.get("indicator"), dict) else {}
    fundamentals = indicator.get("fundamentals") if isinstance(indicator.get("fundamentals"), dict) else {}
    market_cap = _first_number(
        fundamentals.get("total_mv"),
        payload.get("market_cap_yi"),
        payload.get("total_mv"),
    )
    if market_cap is not None and market_cap > 100_000:
        market_cap = market_cap / 10_000.0
    amount = _num(payload.get("amount"))
    factors = (
        _factor_demand(payload, blob, fundamentals),
        _factor_architecture(ai_hits, chain_tier, chain_tier_weight),
        _factor_chokepoint(blob, ai_chain_hit, chain_tier_weight),
        _factor_supplier_concentration(blob),
        _factor_expansion_difficulty(blob),
        _factor_evidence_quality(evidence),
        _factor_valuation(fundamentals, payload, market_cap),
        _factor_catalyst(payload, blob),
    )
    raw_points = round(sum(item.points for item in factors), 2)
    penalties = calculate_penalties(
        payload,
        blob=blob,
        market_cap_yi=market_cap,
        amount=amount,
        evidence_grade=evidence_grade,
        ai_chain_hit=ai_chain_hit,
    )
    penalty_points = round(sum(penalties.values()), 2)
    final_score = round(max(0.0, min(100.0, raw_points - penalty_points)), 2)
    available_count = sum(1 for item in factors if item.available)
    coverage_pct = round(available_count / len(FACTOR_WEIGHTS) * 100.0, 2)
    has_credible_evidence = evidence_grade in {"strong", "medium"}
    passed = final_score >= 55 and coverage_pct >= 60 and ai_chain_hit and has_credible_evidence
    missing_fields = tuple(item.key for item in factors if not item.available)
    constrained_link = _constrained_link(ai_hits, chain_tier)
    scarce_layer = _scarce_layer(chain_tier, blob)
    bear_case, kill_switches = _risk_statements(penalties, blob)
    next_checks = _next_checks(factors, evidence_grade)
    return SerenityCandidateResult(
        rank=0,
        ts_code=str(payload.get("ts_code") or ""),
        name=str(payload.get("name") or ""),
        trade_date=str(payload.get("trade_date") or ""),
        final_score=final_score,
        raw_factor_points=raw_points,
        penalty_points=penalty_points,
        verdict=score_verdict(final_score),
        passed=passed,
        coverage_pct=coverage_pct,
        ai_chain_hit=ai_chain_hit,
        chain_tier=chain_tier,
        chain_tier_weight=chain_tier_weight,
        scarce_layer=scarce_layer,
        constrained_link=constrained_link,
        factors=factors,
        penalties=penalties,
        evidence=evidence,
        missing_fields=missing_fields,
        bear_case=bear_case,
        kill_switches=kill_switches,
        next_checks=next_checks,
        data_sources=dict(payload.get("data_sources") or {}),
        preliminary_score=float(payload.get("preliminary_score") or 0),
    )


def rank_serenity_candidates(
    candidates: list[SerenityCandidateResult],
    *,
    limit: int,
) -> tuple[SerenityCandidateResult, ...]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            not item.passed,
            -item.final_score,
            -item.coverage_pct,
            -item.preliminary_score,
            item.ts_code,
        ),
    )
    return tuple(replace(item, rank=index) for index, item in enumerate(ordered[:limit], start=1))


def infer_chain_tier(blob: str) -> tuple[str, float]:
    lowered = str(blob or "").lower()
    for name, weight, keywords in TIER_MAP:
        if any(keyword in lowered for keyword in keywords):
            return name, weight
    return "未分层", 0.0


def strongest_evidence_grade(evidence: tuple[SerenityEvidence, ...]) -> str:
    strengths = {item.strength for item in evidence}
    if "strong" in strengths:
        return "strong"
    if "medium" in strengths:
        return "medium"
    if "weak" in strengths:
        return "weak"
    return "missing"


def score_verdict(score: float) -> str:
    if score >= 85:
        return "最高研究优先级"
    if score >= 70:
        return "高研究优先级"
    if score >= 55:
        return "值得跟踪"
    return "早期线索或低优先级"


def extract_evidence(payload: dict[str, Any], *, blob: str | None = None) -> tuple[SerenityEvidence, ...]:
    source_blob = blob or _text_blob(payload)
    rows: list[SerenityEvidence] = []
    seen: set[tuple[str, str]] = set()
    contexts = (
        ("statements", payload.get("statements")),
        ("news", payload.get("news")),
        ("events", payload.get("events")),
        ("holder_activity", payload.get("holder_activity")),
        ("fundamental_extra", payload.get("fundamental_extra")),
        ("hot_sectors", payload.get("hot_sectors")),
    )
    for context_name, context in contexts:
        for item in _context_items(context):
            text = _row_text(item)
            if not text:
                continue
            strength = _evidence_strength(item, text, context_name)
            if strength == "weak" and not _has_any_term(text, (*AI_CHAIN_KEYWORDS, *DEMAND_TERMS)):
                continue
            claim = _evidence_claim(item, text)
            source = str(
                item.get("source")
                or item.get("data_source")
                or item.get("dataset")
                or context_name
            )
            key = (claim, source)
            if not claim or key in seen:
                continue
            seen.add(key)
            rows.append(
                SerenityEvidence(
                    claim=claim,
                    source=source,
                    strength=strength,
                    dataset=str(item.get("dataset") or ""),
                    url=str(item.get("url") or item.get("source_url") or ""),
                )
            )
    if not rows and _has_any_term(source_blob, AI_CHAIN_KEYWORDS):
        relation = str(payload.get("relation_reason") or payload.get("industry") or "").strip()
        if relation:
            rows.append(
                SerenityEvidence(
                    claim=f"主题/行业线索：{relation[:160]}",
                    source=str(payload.get("candidate_source") or "theme_universe"),
                    strength="weak",
                )
            )
    order = {"strong": 0, "medium": 1, "weak": 2}
    rows.sort(key=lambda item: (order.get(item.strength, 3), item.source, item.claim))
    return tuple(rows[:12])


def calculate_penalties(
    payload: dict[str, Any],
    *,
    blob: str,
    market_cap_yi: float | None,
    amount: float | None,
    evidence_grade: str,
    ai_chain_hit: bool,
) -> dict[str, float]:
    ratings: dict[str, float] = {}
    if _has_any_term(blob, ("定增", "增发", "配股", "再融资", "可转债", "解禁", "摊薄")):
        ratings["dilution_financing"] = 3.0
    if _has_any_term(blob, ("高比例质押", "质押爆仓", "实控人风险", "违规担保", "监管处罚")):
        ratings["governance"] = 3.0
    if _has_any_term(blob, ("出口管制", "制裁", "实体清单", "断供")) and not _has_any_term(
        blob,
        ("国产替代", "自主可控", "进口替代"),
    ):
        ratings["geopolitics"] = 2.5
    if market_cap_yi is not None and market_cap_yi < 30:
        ratings["liquidity"] = 4.0
    elif market_cap_yi is not None and market_cap_yi < 50:
        ratings["liquidity"] = 2.0
    elif amount is not None and amount < 50_000:
        ratings["liquidity"] = 2.0
    if ai_chain_hit and evidence_grade in {"weak", "missing"} and _has_any_term(
        blob,
        ("概念", "题材", "热搜", "大v", "传闻", "爆炒"),
    ):
        ratings["hype_risk"] = 4.0
    fundamentals = _indicator_fundamentals(payload)
    debt = _num(fundamentals.get("debt_to_assets"))
    roe = _num(fundamentals.get("roe"))
    if _has_any_term(blob, ("审计保留", "财务造假", "非标审计", "资金占用")):
        ratings["accounting_quality"] = 5.0
    elif debt is not None and debt > 75:
        ratings["accounting_quality"] = 2.0
    elif roe is not None and roe < 0:
        ratings["accounting_quality"] = 1.5
    if _has_any_term(blob, ("钢铁", "煤炭", "有色冶炼", "化工原料", "航运", "水泥", "养殖", "周期")):
        ratings["cyclicality"] = 2.5
    if _has_any_term(blob, ("技术路线之争", "被替代", "替代风险", "路线分歧", "新技术冲击", "替代方案")):
        ratings["alternative_design_risk"] = 3.0
    return {
        PENALTY_LABELS[key]: round(min(max(rating, 0.0), 5.0) * 2.0, 2)
        for key, rating in ratings.items()
    }


def _factor_demand(
    payload: dict[str, Any],
    blob: str,
    fundamentals: dict[str, Any],
) -> SerenityFactorResult:
    growth = _growth_from_payload(payload)
    hits = [term for term in DEMAND_TERMS if term in blob]
    hot = bool(payload.get("hot_sectors"))
    available = growth is not None or bool(hits) or hot
    rating = 0.0
    if growth is not None:
        rating += 2.5 if growth >= 30 else 2.0 if growth >= 15 else 1.0 if growth > 0 else 0.0
    rating += min(len(hits), 3) * 0.7
    if hot:
        rating += 0.5
    summary = (
        f"增长 {_fmt_pct(growth)}；需求证据 {', '.join(hits[:3]) or '无'}"
        if available
        else "缺营收增长、订单、产能或行业景气证据"
    )
    return _factor("demand_inflection", rating, available, summary, {"growth": growth, "hits": hits[:5]})


def _factor_architecture(
    ai_hits: list[str],
    chain_tier: str,
    tier_weight: float,
) -> SerenityFactorResult:
    available = bool(ai_hits)
    rating = min(5.0, min(len(ai_hits), 4) * 0.8 + tier_weight * 1.8) if available else 0.0
    summary = (
        f"命中 {', '.join(ai_hits[:4])}，位于{chain_tier}"
        if available
        else "未确认公司处于 AI/科技关键链条"
    )
    return _factor(
        "architecture_coupling",
        rating,
        available,
        summary,
        {"keywords": ai_hits[:8], "chain_tier": chain_tier},
    )


def _factor_chokepoint(
    blob: str,
    ai_chain_hit: bool,
    tier_weight: float,
) -> SerenityFactorResult:
    hits = [term for term in (*IRREPLACEABLE_TERMS, *SCARCITY_TERMS) if term in blob]
    available = ai_chain_hit and bool(hits)
    rating = min(5.0, tier_weight * 2.0 + min(len(hits), 4) * 0.75) if available else 0.0
    summary = (
        f"不可替代/稀缺证据 {', '.join(hits[:4])}"
        if available
        else "缺不可替代性、切换成本或供给紧张证据"
    )
    return _factor("chokepoint_severity", rating, available, summary, {"hits": hits[:8]})


def _factor_supplier_concentration(blob: str) -> SerenityFactorResult:
    hits = [term for term in SCARCITY_TERMS if term in blob]
    available = bool(hits)
    rating = min(5.0, 2.0 + len(hits) * 0.75) if available else 0.0
    summary = f"供应集中/紧缺线索 {', '.join(hits[:4])}" if available else "缺供应商数量或集中度证据"
    return _factor("supplier_concentration", rating, available, summary, {"hits": hits[:8]})


def _factor_expansion_difficulty(blob: str) -> SerenityFactorResult:
    hits = [term for term in EXPANSION_TERMS if term in blob]
    available = bool(hits)
    rating = min(5.0, 1.5 + len(hits) * 0.8) if available else 0.0
    summary = f"扩产难点 {', '.join(hits[:4])}" if available else "缺认证、良率、纯度或扩产周期证据"
    return _factor("expansion_difficulty", rating, available, summary, {"hits": hits[:8]})


def _factor_evidence_quality(evidence: tuple[SerenityEvidence, ...]) -> SerenityFactorResult:
    grade = strongest_evidence_grade(evidence)
    rating = {"strong": 5.0, "medium": 3.5, "weak": 1.5}.get(grade, 0.0)
    available = bool(evidence)
    summary = f"最高证据等级 {grade}，共 {len(evidence)} 条" if available else "缺可验证公开证据"
    return _factor("evidence_quality", rating, available, summary, {"grade": grade, "count": len(evidence)})


def _factor_valuation(
    fundamentals: dict[str, Any],
    payload: dict[str, Any],
    market_cap_yi: float | None,
) -> SerenityFactorResult:
    pe = _first_number(fundamentals.get("pe"), payload.get("pe"))
    pb = _first_number(fundamentals.get("pb"), payload.get("pb"))
    roe = _num(fundamentals.get("roe"))
    growth = _growth_from_payload(payload)
    available = pe is not None or pb is not None
    rating = 0.0
    if pe is not None and pe > 0:
        if growth is not None and growth > 0:
            peg = pe / growth
            rating += 3.0 if peg <= 1 else 2.0 if peg <= 1.5 else 1.0 if peg <= 2.5 else 0.3
        else:
            rating += 2.0 if pe <= 25 else 1.0 if pe <= 45 else 0.3
    if pb is not None and pb > 0:
        rating += 1.0 if pb <= 3 else 0.5 if pb <= 6 else 0.1
    if roe is not None and roe >= 12:
        rating += 0.8
    if market_cap_yi is not None and 30 <= market_cap_yi < 500:
        rating += 0.4
    summary = (
        f"PE {_fmt(pe)}，PB {_fmt(pb)}，增长 {_fmt_pct(growth)}"
        if available
        else "缺估值字段，不能判断市场是否仍按旧叙事定价"
    )
    return _factor(
        "valuation_disconnect",
        rating,
        available,
        summary,
        {"pe": pe, "pb": pb, "roe": roe, "growth": growth, "market_cap_yi": market_cap_yi},
    )


def _factor_catalyst(payload: dict[str, Any], blob: str) -> SerenityFactorResult:
    hits = [term for term in DEMAND_TERMS if term in blob]
    event_count = len(_context_items(payload.get("events"))) + len(_context_items(payload.get("news")))
    hot = bool(payload.get("hot_sectors"))
    available = bool(hits) or event_count > 0 or hot
    rating = min(5.0, min(len(hits), 3) * 0.8 + min(event_count, 3) * 0.6 + (0.5 if hot else 0.0))
    summary = (
        f"近期事件 {event_count} 条；催化线索 {', '.join(hits[:3]) or '无'}"
        if available
        else "缺近期公告、订单、认证或产能节点"
    )
    return _factor(
        "catalyst_timing",
        rating,
        available,
        summary,
        {"hits": hits[:8], "event_count": event_count, "hot": hot},
    )


def _factor(
    key: str,
    rating: float,
    available: bool,
    summary: str,
    evidence: dict[str, Any],
) -> SerenityFactorResult:
    normalized = round(max(0.0, min(5.0, float(rating))), 2) if available else 0.0
    weight = FACTOR_WEIGHTS[key]
    return SerenityFactorResult(
        key=key,
        label=FACTOR_LABELS[key],
        rating=normalized,
        weight=weight,
        points=round(normalized / 5.0 * weight, 2),
        available=available,
        summary=summary,
        evidence=evidence,
    )


def _constrained_link(ai_hits: list[str], tier: str) -> str:
    if not ai_hits:
        return ""
    return f"{tier}中的 {', '.join(ai_hits[:3])}"


def _scarce_layer(tier: str, blob: str) -> str:
    if tier == "未分层":
        return ""
    scarcity = [term for term in SCARCITY_TERMS if term in blob]
    if scarcity:
        return f"{tier}，线索：{', '.join(scarcity[:3])}"
    return f"{tier}，稀缺性仍待硬证据确认"


def _risk_statements(
    penalties: dict[str, float],
    blob: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    bear = [f"{key}已触发，扣 {value:.1f} 分" for key, value in penalties.items()]
    if not bear:
        bear.append("当前未触发结构化罚分，但供应链集中度和扩产难度仍可能被高估")
    kill = [
        "替代材料、替代工艺或竞争方案通过量产验证",
        "竞品扩产显著快于需求增长，供给瓶颈被填平",
        "订单、认证、毛利率或产能利用率没有按验证链兑现",
    ]
    if "出口管制" in blob or "制裁" in blob:
        kill.append("地缘限制导致客户流失，且国产替代无法抵消")
    return tuple(bear[:5]), tuple(kill[:5])


def _next_checks(
    factors: tuple[SerenityFactorResult, ...],
    evidence_grade: str,
) -> tuple[str, ...]:
    missing = {item.key for item in factors if not item.available}
    checks: list[str] = []
    if evidence_grade in {"missing", "weak"}:
        checks.append("查交易所公告、财报、问询函或客户认证，替代主题叙事")
    if "supplier_concentration" in missing:
        checks.append("核实全球/国内供应商数量、客户份额和是否存在独供")
    if "expansion_difficulty" in missing:
        checks.append("核实认证周期、扩产周期、良率、纯度和资本开支")
    if "valuation_disconnect" in missing:
        checks.append("补齐 PE/PB、增长和历史/同业估值，确认是否仍被旧标签定价")
    checks.append("跟踪订单、ASP、毛利率、在建产能和客户下一代 roadmap")
    return tuple(_dedupe(checks)[:5])


def _evidence_strength(item: dict[str, Any], text: str, context_name: str) -> str:
    dataset = str(item.get("dataset") or "").lower()
    source = str(item.get("source") or item.get("data_source") or "").lower()
    lowered = text.lower()
    if dataset in {
        "anns_d",
        "income",
        "balancesheet",
        "cashflow",
        "fina_indicator",
        "stk_holdertrade",
        "pledge_stat",
        "repurchase",
        "block_trade",
    }:
        return "strong"
    if _has_any_term(lowered, STRONG_EVIDENCE_TERMS):
        return "strong"
    if dataset in {"research_report", "irm_qa_sh", "irm_qa_sz"}:
        return "medium"
    if context_name == "hot_sectors" or "news" in dataset or "media" in source:
        return "medium" if _has_any_term(lowered, MEDIUM_EVIDENCE_TERMS) else "weak"
    if _has_any_term(lowered, MEDIUM_EVIDENCE_TERMS):
        return "medium"
    return "weak"


def _evidence_claim(item: dict[str, Any], text: str) -> str:
    for key in (
        "title",
        "ann_title",
        "news_title",
        "question",
        "answer",
        "content",
        "summary",
        "reason",
        "name",
    ):
        value = str(item.get(key) or "").strip()
        if value:
            return " ".join(value.split())[:220]
    return " ".join(text.split())[:220]


def _context_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return [dict(item) for item in items if isinstance(item, dict)]
        return [dict(value)] if value else []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    return []


def _text_blob(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "name",
        "industry",
        "theme",
        "relation_reason",
        "candidate_source",
        "stock_basic",
        "fundamental_extra",
        "hot_sectors",
        "statements",
        "news",
        "events",
        "holder_activity",
        "chip",
    ):
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        try:
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            parts.append(str(value))
    return " ".join(parts).lower()


def _row_text(item: dict[str, Any]) -> str:
    try:
        return json.dumps(item, ensure_ascii=False, default=str).lower()
    except (TypeError, ValueError):
        return str(item).lower()


def _indicator_fundamentals(payload: dict[str, Any]) -> dict[str, Any]:
    indicator = payload.get("indicator") if isinstance(payload.get("indicator"), dict) else {}
    return indicator.get("fundamentals") if isinstance(indicator.get("fundamentals"), dict) else {}


def _growth_from_payload(payload: dict[str, Any]) -> float | None:
    for key in ("revenue_growth", "profit_growth", "industry_growth"):
        value = _num(payload.get(key))
        if value is not None:
            return value
    statements = payload.get("statement_frames")
    if isinstance(statements, pd.DataFrame):
        return _frame_growth(statements)
    fundamentals = payload.get("fundamentals_frame")
    if isinstance(fundamentals, pd.DataFrame):
        return _frame_growth(fundamentals)
    return None


def _frame_growth(frame: pd.DataFrame) -> float | None:
    if frame is None or frame.empty:
        return None
    data = frame.copy()
    for date_key in ("end_date", "ann_date"):
        if date_key in data.columns:
            data = data.sort_values(date_key)
            break
    for column in ("revenue", "total_revenue", "profit", "net_profit"):
        if column not in data.columns:
            continue
        values = pd.to_numeric(data[column], errors="coerce").dropna()
        if len(values) < 2:
            continue
        previous = float(values.iloc[-2])
        latest = float(values.iloc[-1])
        if previous == 0:
            continue
        return round((latest / abs(previous) - 1.0) * 100.0, 2)
    return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _num(value)
        if number is not None:
            return number
    return None


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        if pd.isna(value):
            return None
        text = str(value).replace(",", "").replace("%", "").strip()
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    number = _num(value)
    return "N/A" if number is None else f"{number:.2f}"


def _fmt_pct(value: Any) -> str:
    number = _num(value)
    return "N/A" if number is None else f"{number:.2f}%"


def _has_any_term(text: str, terms: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(str(term).lower() in lowered for term in terms)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
