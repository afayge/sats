from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.rag.knowledge import KnowledgeStore, infer_stock_collections, load_document_chunks
from sats.storage.duckdb import DuckDBStorage


class KnowledgeRagTest(unittest.TestCase):
    def test_load_document_chunks_splits_markdown_with_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.md"
            path.write_text("# 三买\n第三类买点需要离开中枢后回试不跌回。\n", encoding="utf-8")

            chunks = load_document_chunks(path, tags=("chan",), project_root=Path(tmp))

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].title, "三买")
        self.assertEqual(chunks[0].source_path, "notes.md")
        self.assertIn("第三类买点", chunks[0].content)

    def test_knowledge_store_crud_ingest_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "sats.duckdb"
            doc = root / "chan.md"
            doc.write_text("# 三买\n第三类买点 回试 中枢 ZG 风险确认。\n", encoding="utf-8")
            store = KnowledgeStore(db_path)

            kb = store.add_knowledge_base(name="chan", description="缠论", tags=("chan",))
            count = store.ingest_path(kb.name, doc, project_root=root)
            rows = store.search("三买 回试 中枢", knowledge="chan", limit=3)

        self.assertEqual(count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].knowledge_name, "chan")
        self.assertEqual(rows[0].collection_name, "chan")
        self.assertIn("第三类买点", rows[0].content)

    def test_reingest_replaces_duplicate_file_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "sats.duckdb"
            doc = root / "technical.txt"
            doc.write_text("MACD 金叉 技术指标", encoding="utf-8")
            store = KnowledgeStore(db_path)
            store.add_knowledge_base(name="technical", description="技术")

            self.assertEqual(store.ingest_path("technical", doc, project_root=root), 1)
            self.assertEqual(store.ingest_path("technical", doc, project_root=root), 1)

            rows = store.search("MACD", knowledge="technical", limit=10)

        self.assertEqual(len(rows), 1)

    def test_sync_stock_basic_ingests_cached_stock_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_basic(
                pd.DataFrame(
                    [
                        {
                            "ts_code": "000938.SZ",
                            "symbol": "000938",
                            "name": "紫光股份",
                            "industry": "计算机",
                            "market": "主板",
                            "exchange": "SZSE",
                        }
                    ]
                )
            )
            store = KnowledgeStore(db_path)
            settings = SimpleNamespace(project_root=root, db_path=db_path)

            count = store.sync_stock_basic(settings=settings)
            rows = store.search("紫光股份 股票代码", knowledge="stock-basic", limit=3)

        self.assertEqual(count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].collection_name, "stock-basic")
        self.assertIn("股票代码: 000938.SZ", rows[0].content)

    def test_default_knowledge_ingests_china_market_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            store = KnowledgeStore(db_path)
            settings = SimpleNamespace(project_root=Path.cwd(), db_path=db_path)

            count = store.ensure_default_knowledge(settings=settings)
            cases = {
                ("factor 多因子", "signals"): "skills/quant-factor-screener/SKILL.md",
                ("均线金叉", "technical"): "skills/ma-golden-cross/SKILL.md",
                ("开盘溢价率 负溢价", "price-action"): "knowledge/price_action/opening_premium_retention.md",
                ("缩量阴线 止损", "price-action"): "knowledge/price_action/retail_candlestick_discipline.md",
                ("量价齐升 成交量放大", "price-action"): "knowledge/price_action/volume_price_relationship_patterns.md",
                ("放量下跌 空方力量", "price-action"): "knowledge/price_action/volume_price_relationship_patterns.md",
                ("60日线不穿 看空", "price-action"): "knowledge/price_action/moving_average_signal_patterns.md",
                ("线上缩量阴 主力洗盘", "price-action"): "knowledge/price_action/moving_average_signal_patterns.md",
                ("RSI低于20 高于80", "price-action"): "knowledge/price_action/rsi_extreme_reversal_discipline.md",
                ("RSI战法 顶背离 底背离", "price-action"): "knowledge/price_action/rsi_extreme_reversal_discipline.md",
                ("主升浪首次分歧 缩量回踩", "price-action"): "knowledge/price_action/trend_execution_five_disciplines.md",
                ("量比低于1.5 RPS强度", "price-action"): "knowledge/price_action/trend_execution_five_disciplines.md",
                ("左倍量抄底 右倍量逃顶", "price-action"): "knowledge/price_action/left_right_double_volume_discipline.md",
                ("阳柱左倍量 黄金坑 倍量低点", "price-action"): "knowledge/price_action/left_right_double_volume_discipline.md",
                ("520均线战法 20日线向上 金叉买点", "price-action"): "knowledge/price_action/moving_average_520_discipline.md",
                ("回踩买点 缩量回踩20日线 死叉清仓", "price-action"): "knowledge/price_action/moving_average_520_discipline.md",
                ("回踩均线洗盘 缩量企稳 放量突破", "price-action"): "knowledge/price_action/main_force_washout_patterns.md",
                ("假跌破支撑洗盘 快速收回支撑", "price-action"): "knowledge/price_action/main_force_washout_patterns.md",
                ("放量突破", "signals"): "skills/volume-breakout/SKILL.md",
                ("热点题材", "market"): "skills/hot-theme/SKILL.md",
                ("情绪周期", "sentiment"): "skills/emotion-cycle/SKILL.md",
                ("高股息", "fundamental"): "skills/high-dividend-strategy/SKILL.md",
                ("成长质量", "fundamental"): "skills/growth-quality/SKILL.md",
                ("ESG", "fundamental"): "skills/esg-screener/SKILL.md",
                ("组合压力测试", "risk"): "skills/portfolio-health-check/SKILL.md",
                ("董监高增持", "sentiment"): "skills/insider-trading-analyzer/SKILL.md",
            }

            results = {
                expected_path: store.search(query, knowledge=knowledge, limit=5)
                for (query, knowledge), expected_path in cases.items()
            }
            natural_rows = store.search(
                "量价背离和放量下跌怎么判断",
                collections=("technical", "price-action"),
                limit=5,
            )

        self.assertGreater(count, 0)
        for expected_path, rows in results.items():
            with self.subTest(expected_path=expected_path):
                self.assertTrue(any(row.source_path == expected_path for row in rows))
        self.assertTrue(
            any(row.source_path == "knowledge/price_action/volume_price_relationship_patterns.md" for row in natural_rows)
        )

    def test_price_action_queries_infer_price_action_collection(self) -> None:
        self.assertIn("price-action", infer_stock_collections("开盘溢价率为负，今天该走还是该留"))
        self.assertIn("price-action", infer_stock_collections("缩量阴线踩支撑后如何设置止损"))
        self.assertIn("price-action", infer_stock_collections("量价背离和放量下跌怎么判断"))
        self.assertIn("price-action", infer_stock_collections("60日均线不穿还能看多吗"))
        self.assertIn("price-action", infer_stock_collections("线上缩量阴是不是主力洗盘"))
        self.assertIn("price-action", infer_stock_collections("RSI低于20能不能满仓买"))
        self.assertIn("price-action", infer_stock_collections("RSI 低于 20 后出现底背离"))
        self.assertIn("price-action", infer_stock_collections("主升浪首次分歧后缩量回踩怎么处理"))
        self.assertIn("price-action", infer_stock_collections("量比低于1.5但RPS强度高能买吗"))
        self.assertIn("price-action", infer_stock_collections("左倍量抄底右倍量逃顶怎么判断"))
        self.assertIn("price-action", infer_stock_collections("阳柱左倍量后黄金坑有没有跌破倍量低点"))
        self.assertIn("price-action", infer_stock_collections("520均线战法里20日线向上怎么找金叉买点"))
        self.assertIn("price-action", infer_stock_collections("缩量回踩20日线后跌破5日线要不要死叉清仓"))
        self.assertIn("price-action", infer_stock_collections("大阴线洗盘后快速收回支撑是不是洗盘"))
        self.assertIn("price-action", infer_stock_collections("三角形洗盘旗形整理放量突破上轨怎么判断"))


if __name__ == "__main__":
    unittest.main()
