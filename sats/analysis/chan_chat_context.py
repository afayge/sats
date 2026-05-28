from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sats.rag.chan_knowledge import load_rule_cards, search_chan_knowledge
from sats.skills import Skill, find_skill


CHAN_CHAT_TERMS = (
    "缠论",
    "缠中说禅",
    "一买",
    "二买",
    "三买",
    "一卖",
    "二卖",
    "三卖",
    "背驰",
    "中枢",
    "区间套",
    "分型",
    "买点",
    "卖点",
    "chan_signals",
    "chan-composite",
    "chan-third-buy",
)
CORE_RULE_IDS = (
    "chan_first_buy",
    "chan_second_buy",
    "chan_third_buy",
    "chan_first_sell",
    "chan_second_sell",
    "chan_third_sell",
    "chan_center_oscillation_low",
    "chan_center_oscillation_high",
)


@dataclass(frozen=True, slots=True)
class ChanChatContext:
    payload: dict[str, Any]
    system_message: str


def is_chan_chat_question(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    return any(term.lower() in text for term in CHAN_CHAT_TERMS)


def build_chan_chat_context(
    message: str,
    *,
    skills: list[Skill] | None = None,
    limit: int = 6,
) -> ChanChatContext | None:
    if not is_chan_chat_question(message):
        return None
    chan_skill = find_skill(skills or [], "chan-theory")
    evidence = search_chan_knowledge(message, limit=limit)
    if not evidence:
        evidence = _fallback_rule_cards(limit=limit)
    payload = {
        "user_intent": "chan_theory_chat",
        "skill": {
            "id": chan_skill.id if chan_skill else "chan-theory",
            "name": chan_skill.name if chan_skill else "chan-theory",
            "content": chan_skill.content if chan_skill else "",
        },
        "rag_evidence": evidence,
        "data_policy": (
            "SATS has loaded local chan-theory skill guidance and RAG rule cards for this answer. "
            "For stock-specific analysis, use only the structured stock and market data supplied in this prompt."
        ),
    }
    return ChanChatContext(payload=payload, system_message=_system_message(payload))


def _fallback_rule_cards(*, limit: int) -> list[dict[str, Any]]:
    wanted = set(CORE_RULE_IDS)
    rows = []
    for card in load_rule_cards():
        if card.rule_id not in wanted:
            continue
        rows.append(
            {
                **card.to_context(),
                "score": 0,
                "source": f"PDF pages {', '.join(str(page) for page in card.source_pages)}",
            }
        )
    rows.sort(key=lambda item: CORE_RULE_IDS.index(str(item.get("rule_id"))))
    return rows[:limit]


def _system_message(payload: dict[str, Any]) -> str:
    return (
        "以下是 SATS 为本轮缠论问题加载的本地 chan-theory skill 全文和缠论知识库 RAG 证据。"
        "回答缠论问题时必须优先使用这些规则依据；不得编造 PDF 原文、行情、新闻、题材或基本面。"
        "若同时提供了真实股票/大盘结构化数据，具体买卖点和风险判断只能基于这些真实数据。"
        "所有股票相关结论仅供研究，不构成投资建议。\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str)}"
    )
