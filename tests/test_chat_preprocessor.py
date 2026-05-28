from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.chat_preprocessor import preprocess_chat_message
from sats.chat_reference import ChatReferenceContext
from sats.llm import LLMResponse
from sats.storage.duckdb import DuckDBStorage


class _JSONLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"intent":"stock_analysis","stock_names":["紫光股份"],'
                '"needs_stock_context":true,"needs_market_context":true,'
                '"needs_indicators":true,"skill_hints":["technical-basic"],"confidence":0.91}'
            )
        )


class _BadJSONLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(content="不是 JSON")


class _RaisingLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        raise TimeoutError("timeout")


class _ProfileJSONLLM:
    instances: list["_ProfileJSONLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        _ProfileJSONLLM.instances.append(self)

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"intent":"stock_analysis","stock_names":["紫光股份"],'
                '"needs_stock_context":true,"needs_market_context":true,'
                '"needs_indicators":true,"confidence":0.9}'
            )
        )


class _MarketPlanningLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"intent":"market_analysis","needs_market_context":true,'
                '"market_indices":["上证指数","创业板指","沪深300"],'
                '"market_dimensions":["core_indices","limit_sentiment"],'
                '"market_horizons":["tomorrow","day_after_tomorrow"],'
                '"missing_questions":["您想预测哪个大盘指数？","请您提供具体的预测日期。"],'
                '"confidence":0.88}'
            )
        )


class _ReferenceClarificationLLM:
    instances: list["_ReferenceClarificationLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.messages = []
        _ReferenceClarificationLLM.instances.append(self)

    def chat(self, messages, timeout=None):
        self.messages = messages
        return LLMResponse(
            content=(
                '{"intent":"stock_analysis","reference_needed":true,'
                '"needs_stock_context":true,"needs_market_context":true,'
                '"needs_indicators":true,'
                '"missing_questions":["请提供具体股票名称或代码。"],'
                '"confidence":0.7}'
            )
        )


class _Provider:
    def __init__(self, settings) -> None:
        self.settings = settings

    def load_stock_basic(self, *, storage=None):
        frame = _stock_basic()
        if storage is not None:
            storage.upsert_stock_basic(frame)
        return frame


def _stock_basic() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000938.SZ",
                "symbol": "000938",
                "name": "紫光股份",
                "industry": "计算机",
                "market": "主板",
                "exchange": "SZSE",
                "list_date": "19991104",
            },
            {
                "ts_code": "600519.SH",
                "symbol": "600519",
                "name": "贵州茅台",
                "industry": "白酒",
                "market": "主板",
                "exchange": "SSE",
                "list_date": "20010827",
            },
            {
                "ts_code": "600000.SH",
                "symbol": "600000",
                "name": "浦发银行",
                "industry": "银行",
                "market": "主板",
                "exchange": "SSE",
                "list_date": "19991110",
            },
            {
                "ts_code": "000001.SZ",
                "symbol": "000001",
                "name": "平安银行",
                "industry": "银行",
                "market": "主板",
                "exchange": "SZSE",
                "list_date": "19910403",
            },
        ]
    )


class ChatPreprocessorTest(unittest.TestCase):
    def test_resolves_stock_name_with_llm_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", openai_model="m")

            result = preprocess_chat_message(
                "分析紫光股份技术面",
                settings=settings,
                llm_factory=_JSONLLM,
                provider_factory=_Provider,
            )

        self.assertEqual(result.symbols, ("000938.SZ",))
        self.assertEqual(result.stock_names, ("紫光股份",))
        self.assertTrue(result.needs_stock_context)
        self.assertTrue(result.needs_market_context)
        self.assertTrue(result.needs_indicators)
        self.assertIn("technical-basic", result.skill_hints)
        self.assertEqual(result.missing_questions, ())

    def test_explicit_symbol_is_kept_and_name_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_stock_basic(_stock_basic())

            result = preprocess_chat_message(
                "分析000938和贵州茅台",
                settings=settings,
                llm_factory=_BadJSONLLM,
                provider_factory=_Provider,
            )

        self.assertEqual(result.symbols, ("000938.SZ", "600519.SH"))
        self.assertTrue(result.needs_stock_context)
        self.assertTrue(result.needs_market_context)

    def test_reference_question_uses_reference_symbols_without_guessing(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), openai_model="m")
        reference = ChatReferenceContext(
            system_message="上一条筛选结果",
            symbols=["000001.SZ", "600519.SH"],
            trade_date="20260522",
            source="/results",
            data_name="筛选结果",
        )

        result = preprocess_chat_message(
            "分析上面16支股票列表",
            settings=settings,
            reference_context=reference,
            llm_factory=_BadJSONLLM,
            provider_factory=_Provider,
        )

        self.assertTrue(result.reference_needed)
        self.assertEqual(result.symbols, ("000001.SZ", "600519.SH"))
        self.assertEqual(result.trade_date, None)
        self.assertTrue(result.needs_stock_context)

    def test_reference_symbols_override_llm_missing_stock_code_question(self) -> None:
        _ReferenceClarificationLLM.instances = []
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), openai_model="m")
        reference = ChatReferenceContext(
            system_message="上一条筛选结果",
            symbols=["000001.SZ", "600519.SH"],
            trade_date="20260527",
            source="/screen",
            data_name="筛选结果",
        )

        result = preprocess_chat_message(
            "分析上面股票，预测短期走势",
            settings=settings,
            reference_context=reference,
            llm_factory=_ReferenceClarificationLLM,
            provider_factory=_Provider,
        )

        self.assertEqual(result.intent, "stock_analysis")
        self.assertTrue(result.reference_needed)
        self.assertEqual(result.symbols, ("000001.SZ", "600519.SH"))
        self.assertEqual(result.missing_questions, ())
        self.assertTrue(result.needs_stock_context)
        self.assertTrue(result.needs_market_context)
        self.assertTrue(result.needs_indicators)
        prompt = "\n".join(message["content"] for message in _ReferenceClarificationLLM.instances[0].messages)
        self.assertIn("symbols=000001.SZ, 600519.SH", prompt)
        self.assertIn("不得要求用户再次提供股票代码或名称", prompt)

    def test_reference_quote_question_uses_symbols_without_full_stock_context(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), openai_model="m")
        reference = ChatReferenceContext(
            system_message="上一条筛选结果",
            symbols=["000001.SZ", "600519.SH"],
            trade_date="20260522",
            source="/results",
            data_name="筛选结果",
        )

        result = preprocess_chat_message(
            "查看上面股票实时报价",
            settings=settings,
            reference_context=reference,
            llm_factory=_BadJSONLLM,
            provider_factory=_Provider,
        )

        self.assertEqual(result.symbols, ("000001.SZ", "600519.SH"))
        self.assertTrue(result.reference_needed)
        self.assertTrue(result.needs_realtime_quote_context)
        self.assertFalse(result.needs_stock_context)
        self.assertFalse(result.needs_market_context)

    def test_llm_invalid_json_falls_back_to_rules(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), openai_model="m")

        result = preprocess_chat_message(
            "今天A股大盘分析，明天走势预测",
            settings=settings,
            llm_factory=_BadJSONLLM,
            provider_factory=_Provider,
        )

        self.assertEqual(result.intent, "market_analysis")
        self.assertTrue(result.needs_market_context)
        self.assertFalse(result.needs_stock_context)
        self.assertEqual(result.market_horizons, ("today", "tomorrow"))

    def test_market_question_does_not_block_on_llm_clarification_questions(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), openai_model="m")

        result = preprocess_chat_message(
            "预测明后的大盘走势",
            settings=settings,
            llm_factory=_MarketPlanningLLM,
            provider_factory=_Provider,
        )

        self.assertEqual(result.intent, "market_analysis")
        self.assertEqual(result.missing_questions, ())
        self.assertEqual(result.market_horizons, ("tomorrow", "day_after_tomorrow"))
        self.assertEqual(result.market_dimensions, ("core_indices", "limit_sentiment"))
        self.assertEqual(result.market_indices, ("000001.SH", "399006.SZ", "000300.SH"))

    def test_market_question_keeps_explicit_indices_and_can_merge_core_basket_hints(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), openai_model="m")

        result = preprocess_chat_message(
            "分析上证、创业板和沪深300",
            settings=settings,
            llm_factory=_BadJSONLLM,
            provider_factory=_Provider,
        )

        self.assertEqual(result.intent, "market_analysis")
        self.assertEqual(result.market_indices, ("000001.SH", "399006.SZ", "000300.SH"))
        self.assertEqual(result.market_horizons, ("today",))

    def test_llm_timeout_falls_back_to_opportunity_discovery(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), openai_model="m")

        result = preprocess_chat_message(
            "预测未来几天大概率上涨的股票",
            settings=settings,
            llm_factory=_RaisingLLM,
            provider_factory=_Provider,
        )

        self.assertEqual(result.intent, "opportunity_discovery")
        self.assertTrue(result.needs_opportunity_discovery)
        self.assertFalse(result.needs_stock_context)

    def test_ambiguous_stock_name_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_stock_basic(_stock_basic())

            result = preprocess_chat_message(
                "分析银行技术面",
                settings=settings,
                llm_factory=_BadJSONLLM,
                provider_factory=_Provider,
            )

        self.assertEqual(result.symbols, ())
        self.assertTrue(result.missing_questions)
        self.assertIn("匹配到多个结果", result.missing_questions[0])

    def test_preprocess_uses_light_profile_and_light_model_name(self) -> None:
        _ProfileJSONLLM.instances = []
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                db_path=Path(tmp) / "sats.duckdb",
                openai_model="main-model",
                light_model_name="light-model",
            )

            result = preprocess_chat_message(
                "分析紫光股份技术面",
                settings=settings,
                llm_factory=_ProfileJSONLLM,
                provider_factory=_Provider,
            )

        self.assertEqual(result.symbols, ("000938.SZ",))
        self.assertEqual(_ProfileJSONLLM.instances[0].kwargs["model_name"], "light-model")
        self.assertEqual(_ProfileJSONLLM.instances[0].kwargs["profile"], "light")


if __name__ == "__main__":
    unittest.main()
