from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.rag.knowledge import KnowledgeStore, load_document_chunks
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
                ("高股息", "fundamental"): "skills/high-dividend-strategy/SKILL.md",
                ("ESG", "fundamental"): "skills/esg-screener/SKILL.md",
                ("组合压力测试", "risk"): "skills/portfolio-health-check/SKILL.md",
                ("董监高增持", "sentiment"): "skills/insider-trading-analyzer/SKILL.md",
            }

            results = {
                expected_path: store.search(query, knowledge=knowledge, limit=5)
                for (query, knowledge), expected_path in cases.items()
            }

        self.assertGreater(count, 0)
        for expected_path, rows in results.items():
            with self.subTest(expected_path=expected_path):
                self.assertTrue(any(row.source_path == expected_path for row in rows))


if __name__ == "__main__":
    unittest.main()
