from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd
from prompt_toolkit.formatted_text.utils import fragment_list_to_text

from sats.natural_output import (
    CALLOUT_STYLE,
    PERCENT_NEGATIVE_HIGHLIGHT_STYLE,
    PERCENT_POSITIVE_HIGHLIGHT_STYLE,
    SYMBOL_HIGHLIGHT_STYLE,
    TITLE_STYLE,
    OutputSemanticLexicon,
    build_output_semantic_lexicon,
    extract_output_metadata,
    normalize_natural_markdown,
    render_text_output_for_tty,
    render_natural_output,
    tokenize_semantic_text,
)
from sats.storage.duckdb import DuckDBStorage


class NaturalOutputTest(unittest.TestCase):
    def test_normalize_wraps_plain_text_into_markdown_skeleton(self) -> None:
        markdown = normalize_natural_markdown(
            "回答",
            data_names=("Agent",),
            skill_names=("chan-theory",),
        )

        self.assertIn("# SATS 自然对话输出", markdown)
        self.assertIn("> 回答", markdown)
        self.assertIn("`数据: Agent`", markdown)
        self.assertIn("`skill: chan-theory`", markdown)
        self.assertIn("## 结论摘要", markdown)
        self.assertIn("## 关键证据", markdown)
        self.assertIn("## 文字图表", markdown)
        self.assertIn("## 风险与限制", markdown)
        self.assertIn("## 下一步", markdown)

    def test_normalize_fills_empty_standard_sections_without_duplicate_headings(self) -> None:
        markdown = normalize_natural_markdown(
            "# 大盘分析\n\n## 结论摘要\n\n## 下一步\n",
            data_names=("Conversation", "market_context"),
        )

        self.assertEqual(markdown.count("## 结论摘要"), 1)
        self.assertEqual(markdown.count("## 下一步"), 1)
        summary = markdown.split("## 结论摘要", 1)[1].split("## 下一步", 1)[0]
        next_section = markdown.split("## 下一步", 1)[1]
        self.assertIn("- ", summary)
        self.assertIn("- ", next_section)

    def test_render_non_tty_returns_plain_markdown(self) -> None:
        markdown = "# 标题\n\n> 结论\n"

        rendered = render_natural_output(markdown, channel="cli", tty=False, width=80)

        self.assertEqual(rendered, markdown)

    def test_render_text_output_for_tty_styles_plain_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            DuckDBStorage(db_path).upsert_stock_basic(
                pd.DataFrame([{"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"}])
            )
            rendered = render_text_output_for_tty(
                "1. 000001.SZ 平安银行 涨幅 +1.33%\n状态: 已保存关注股票",
                db_path=db_path,
                width=80,
            )

        text = fragment_list_to_text(rendered)
        self.assertIn("1. 000001.SZ 平安银行 涨幅 +1.33%", text)
        self.assertIn("状态: 已保存关注股票", text)
        self.assertTrue(any(SYMBOL_HIGHLIGHT_STYLE in style and chunk == "000001.SZ" for style, chunk in rendered))
        self.assertTrue(any(SYMBOL_HIGHLIGHT_STYLE in style and chunk == "平安银行" for style, chunk in rendered))
        self.assertTrue(any(PERCENT_POSITIVE_HIGHLIGHT_STYLE in style and chunk == "+1.33%" for style, chunk in rendered))

    def test_render_text_output_for_tty_keeps_json_plain(self) -> None:
        payload = '{"ts_code":"000001.SZ","pct_chg":1.33}\n'

        rendered = render_text_output_for_tty(payload, width=80)

        self.assertEqual(rendered, payload)

    def test_render_tty_collapses_wide_table_on_narrow_terminal(self) -> None:
        markdown = "\n".join(
            [
                "# 标题",
                "",
                "> 核心结论",
                "",
                "## 关键证据",
                "",
                "| 维度 | 结论 | 备注 |",
                "|---|---|---|",
                "| 趋势 | 均线维持多头 | 量能未明显放大 |",
            ]
        )

        rendered = render_natural_output(markdown, channel="repl", tty=True, width=28)

        text = fragment_list_to_text(rendered)
        self.assertTrue(any(style == TITLE_STYLE and "标题" in chunk for style, chunk in rendered))
        self.assertIn("维度: 趋势", text)
        self.assertIn("结论: 均线维持多头", text)
        self.assertIn("备注: 量能未明显放大", text)

    def test_extract_output_metadata_reads_badges_and_sections(self) -> None:
        markdown = "\n".join(
            [
                "# 标题",
                "",
                "> 核心结论",
                "",
                "`数据: 个股` `风格: 研究输出`",
                "",
                "## 风险与限制",
                "",
                "- 仅供研究",
            ]
        )

        metadata = extract_output_metadata(markdown)

        self.assertEqual(metadata.title, "标题")
        self.assertEqual(metadata.callout, "核心结论")
        self.assertIn("数据: 个股", metadata.badges)
        self.assertIn("风险与限制", metadata.section_titles)
        self.assertTrue(metadata.has_risk_section)

    def test_tokenize_semantic_text_handles_dates_codes_names_numbers_and_percent(self) -> None:
        lexicon = OutputSemanticLexicon(
            symbol_codes=("000001.SZ",),
            symbol_names=("上证指数", "沪深300", "平安银行"),
        )

        tokens = tokenize_semantic_text(
            "上证指数 2026-06-08 涨 1.33%，沪深300 跌 -1.70%，000001.SZ 平安银行 -12.5",
            lexicon,
        )

        self.assertIn(("symbol_name", "上证指数"), [(item.kind, item.text) for item in tokens])
        self.assertIn(("date", "2026-06-08"), [(item.kind, item.text) for item in tokens])
        self.assertIn(("percent_positive", "1.33%"), [(item.kind, item.text) for item in tokens])
        self.assertIn(("percent_negative", "-1.70%"), [(item.kind, item.text) for item in tokens])
        self.assertIn(("symbol_code", "000001.SZ"), [(item.kind, item.text) for item in tokens])
        self.assertIn(("symbol_name", "平安银行"), [(item.kind, item.text) for item in tokens])
        self.assertIn(("number", "-12.5"), [(item.kind, item.text) for item in tokens])

    def test_render_tty_applies_semantic_highlight_styles(self) -> None:
        markdown = "# 标题\n\n> 上证指数 2026-06-08 涨 1.33%，沪深300 跌 -1.70%，000001.SZ 平安银行 12.5\n"
        lexicon = OutputSemanticLexicon(
            symbol_codes=("000001.SZ",),
            symbol_names=("上证指数", "沪深300", "平安银行"),
        )

        rendered = render_natural_output(
            markdown,
            channel="repl",
            tty=True,
            width=80,
            semantic_lexicon=lexicon,
        )

        self.assertTrue(any(SYMBOL_HIGHLIGHT_STYLE in style and text == "上证指数" for style, text in rendered))
        self.assertTrue(any(SYMBOL_HIGHLIGHT_STYLE in style and text == "000001.SZ" for style, text in rendered))
        self.assertTrue(any(PERCENT_POSITIVE_HIGHLIGHT_STYLE in style and text == "1.33%" for style, text in rendered))
        self.assertTrue(any(PERCENT_NEGATIVE_HIGHLIGHT_STYLE in style and text == "-1.70%" for style, text in rendered))
        self.assertTrue(any(text == "12.5" and style.strip() == CALLOUT_STYLE for style, text in rendered))
        self.assertTrue(any(text == "2026-06-08" and style.strip() == CALLOUT_STYLE for style, text in rendered))

    def test_build_output_semantic_lexicon_loads_stock_names_from_local_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
                    ]
                )
            )

            lexicon = build_output_semantic_lexicon("平安银行 上证指数 000001.SZ", db_path=db_path)

            self.assertIn("平安银行", lexicon.symbol_names)
            self.assertIn("上证指数", lexicon.symbol_names)
            self.assertIn("000001.SZ", lexicon.symbol_codes)


if __name__ == "__main__":
    unittest.main()
