from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from sats.chat_components import (
    CHAT_ROUTE_CHAN,
    CHAT_ROUTE_MARKET,
    CHAT_ROUTE_OPPORTUNITY,
    CHAT_ROUTE_RULE,
    CHAT_ROUTE_STOCK,
    ChatEvidenceBundle,
    ChatRequestRoute,
    COMPONENT_CHAN_CONTEXT,
    COMPONENT_INDICATORS,
    COMPONENT_KNOWLEDGE_CONTEXT,
    COMPONENT_MARKET_CONTEXT,
    COMPONENT_OPPORTUNITY,
    COMPONENT_RULE_GENERATION,
    COMPONENT_STOCK_CONTEXT,
    build_chat_request_route,
    resolve_stock_question_from_preprocess,
    _chat_evidence_digest,
    _build_synthesis_messages,
)
from sats.chat_planner import build_chat_plan
from sats.chat_preprocessor import preprocess_chat_message
from sats.chat_reference import ChatReferenceContext
from sats.skills import load_skills


class ChatComponentsRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(project_root=Path("."), openai_model="m", light_model_name="m")
        self.skills = load_skills(Path("skills"))

    def _route_for(self, message: str):
        preprocess = preprocess_chat_message(message, settings=self.settings, llm_enabled=False)
        stock_question = resolve_stock_question_from_preprocess(
            message,
            preprocess,
            reference_context=None,
            last_stock_question=None,
        )
        plan = build_chat_plan(message, skills=self.skills, stock_question=stock_question, preprocess=preprocess)
        return build_chat_request_route(
            message,
            skills=self.skills,
            preprocess=preprocess,
            plan=plan,
            stock_question=stock_question,
        )

    def test_stock_route_maps_to_stock_context_indicators_and_market(self) -> None:
        route = self._route_for("分析 000938 下周走势")

        self.assertEqual(route.route_kind, CHAT_ROUTE_STOCK)
        self.assertEqual(
            route.required_components,
            (
                COMPONENT_STOCK_CONTEXT,
                COMPONENT_INDICATORS,
                COMPONENT_MARKET_CONTEXT,
                COMPONENT_KNOWLEDGE_CONTEXT,
            ),
        )

    def test_market_route_maps_to_market_context(self) -> None:
        route = self._route_for("分析今天大盘走势，预测明天走势")

        self.assertEqual(route.route_kind, CHAT_ROUTE_MARKET)
        self.assertEqual(route.required_components, (COMPONENT_MARKET_CONTEXT, COMPONENT_KNOWLEDGE_CONTEXT))

    def test_opportunity_route_maps_to_discovery_component(self) -> None:
        route = self._route_for("列出10支明天大概率上涨的股票")

        self.assertEqual(route.route_kind, CHAT_ROUTE_OPPORTUNITY)
        self.assertIn(COMPONENT_OPPORTUNITY, route.required_components)
        self.assertNotIn(COMPONENT_STOCK_CONTEXT, route.required_components)

    def test_chan_route_maps_to_chan_context_and_stock_components(self) -> None:
        route = self._route_for("用缠论分析 002436")

        self.assertEqual(route.route_kind, CHAT_ROUTE_CHAN)
        self.assertEqual(
            route.required_components,
            (
                COMPONENT_CHAN_CONTEXT,
                COMPONENT_STOCK_CONTEXT,
                COMPONENT_INDICATORS,
                COMPONENT_KNOWLEDGE_CONTEXT,
            ),
        )

    def test_rule_generation_route_maps_to_rule_component(self) -> None:
        route = self._route_for("新增一个低位放量突破筛选规则")

        self.assertEqual(route.route_kind, CHAT_ROUTE_RULE)
        self.assertEqual(route.required_components, (COMPONENT_RULE_GENERATION,))

    def test_indicator_digest_keeps_all_explicit_symbols(self) -> None:
        symbols = [f"300{i:03d}.SZ" for i in range(12)]
        route = ChatRequestRoute(
            route_kind=CHAT_ROUTE_STOCK,
            intent="stock_analysis",
            symbols=tuple(symbols),
            required_components=(COMPONENT_STOCK_CONTEXT, COMPONENT_INDICATORS),
        )
        rows = [
            {
                "ts_code": symbol,
                "name": f"股票{i}",
                "trade_date": "20260609",
                "indicator_result": {"close": 10 + i, "ma5": 9 + i, "rsi6": 30 + i},
                "period_returns": {"6m": {"start_trade_date": "20251222", "end_trade_date": "20260609", "pct_change": 12.3 + i}},
                "missing_fields": [],
            }
            for i, symbol in enumerate(symbols)
        ]
        evidence = ChatEvidenceBundle(route=route, indicators={"kind": "indicators", "results": rows})

        digest = _chat_evidence_digest(evidence)

        self.assertEqual([item["ts_code"] for item in digest["indicators"]], symbols)
        self.assertEqual(digest["indicator_coverage"]["requested_count"], 12)
        self.assertEqual(digest["indicator_coverage"]["included_count"], 12)
        self.assertEqual(digest["indicator_coverage"]["omitted_count"], 0)
        self.assertEqual(digest["indicators"][-1]["indicator_result"]["close"], 21)
        self.assertEqual(digest["indicators"][0]["period_returns"]["6m"]["pct_change"], 12.3)

    def test_stock_context_digest_keeps_period_returns(self) -> None:
        route = ChatRequestRoute(
            route_kind=CHAT_ROUTE_STOCK,
            intent="stock_analysis",
            symbols=("002436.SZ",),
            required_components=(COMPONENT_STOCK_CONTEXT, COMPONENT_INDICATORS),
        )
        context = SimpleNamespace(
            payload={
                "stocks": [
                    {
                        "ts_code": "002436.SZ",
                        "name": "兴森科技",
                        "requested_trade_date": "20260620",
                        "trade_date": "20260619",
                        "price_context": {"close": 34.5},
                        "indicator_result": {"close": 34.5, "ma5": 33.8},
                        "period_returns": {
                            "6m": {
                                "calendar_start": "20251220",
                                "calendar_end": "20260620",
                                "start_trade_date": "20251222",
                                "end_trade_date": "20260619",
                                "pct_change": 18.5,
                            }
                        },
                    }
                ]
            }
        )

        digest = _chat_evidence_digest(ChatEvidenceBundle(route=route, stock_context=context))

        self.assertEqual(digest["stock_context"][0]["requested_trade_date"], "20260620")
        self.assertEqual(digest["stock_context"][0]["period_returns"]["6m"]["end_trade_date"], "20260619")

    def test_synthesis_messages_include_reference_symbol_policy(self) -> None:
        route = ChatRequestRoute(
            route_kind=CHAT_ROUTE_STOCK,
            intent="stock_analysis",
            symbols=("000001.SZ", "600519.SH"),
            required_components=(COMPONENT_STOCK_CONTEXT, COMPONENT_INDICATORS),
        )
        reference_context = ChatReferenceContext(
            system_message="上一条输出包含【强势追击】和 000001.SZ",
            symbols=["000001.SZ", "600519.SH"],
            data_name="上条输出",
        )
        evidence = ChatEvidenceBundle(route=route, reference_context=reference_context)

        digest = _chat_evidence_digest(evidence)
        messages = _build_synthesis_messages(
            "分析上面10个股票",
            route=route,
            evidence=evidence,
            skills=[],
            history=[],
            memories=[],
            session_summary="",
        )
        combined = "\n".join(str(message.get("content") or "") for message in messages)

        self.assertEqual(digest["reference_symbol_policy"]["allowed_symbols"], ["000001.SZ", "600519.SH"])
        self.assertIn("逐股票条目只能来自 reference_symbol_policy.allowed_symbols", combined)
        self.assertIn("必须同时有有效股票代码和名称", combined)
        self.assertIn("【...】、章节标题、策略标签、风险标签、分组名不是股票", combined)


if __name__ == "__main__":
    unittest.main()
