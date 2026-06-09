from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

from sats.natural_output import OutputSemanticLexicon
from sats.output_saver import (
    CapturedOutput,
    SaveRequest,
    _semantic_pdf_markup,
    extract_report_path,
    parse_save_request,
    save_captured_output,
)


class OutputSaverTest(unittest.TestCase):
    def test_parse_pure_and_compound_save_requests(self) -> None:
        pure = parse_save_request("保存上面结果为PDF")
        compound = parse_save_request("分析 000938 技术面，并保存结果为 MD")
        pure_output = parse_save_request("上面内容输出为PDF")
        pure_previous_output = parse_save_request("上一个输出到markdown文件")
        pure_typo_output = parse_save_request("上一个输出到出到markdown文件")
        pure_dialog = parse_save_request("保存刚才对话为markdown文件")
        compound_output = parse_save_request("分析 000938 技术面，输出为MD")
        not_save = parse_save_request("PDF输出和Markdown输出有什么区别")

        self.assertIsNotNone(pure)
        self.assertEqual(pure.format, "pdf")
        self.assertTrue(pure.is_pure)
        self.assertIsNotNone(compound)
        self.assertEqual(compound.format, "md")
        self.assertFalse(compound.is_pure)
        self.assertEqual(compound.cleaned_text, "分析 000938 技术面")
        self.assertIsNotNone(pure_output)
        self.assertEqual(pure_output.format, "pdf")
        self.assertTrue(pure_output.is_pure)
        self.assertEqual(pure_output.cleaned_text, "")
        self.assertIsNotNone(pure_previous_output)
        self.assertEqual(pure_previous_output.format, "md")
        self.assertTrue(pure_previous_output.is_pure)
        self.assertEqual(pure_previous_output.cleaned_text, "")
        self.assertIsNotNone(pure_typo_output)
        self.assertEqual(pure_typo_output.format, "md")
        self.assertTrue(pure_typo_output.is_pure)
        self.assertEqual(pure_typo_output.cleaned_text, "")
        self.assertIsNotNone(pure_dialog)
        self.assertEqual(pure_dialog.format, "md")
        self.assertTrue(pure_dialog.is_pure)
        self.assertEqual(pure_dialog.cleaned_text, "")
        self.assertIsNotNone(compound_output)
        self.assertEqual(compound_output.format, "md")
        self.assertFalse(compound_output.is_pure)
        self.assertEqual(compound_output.cleaned_text, "分析 000938 技术面")
        self.assertIsNone(not_save)

    def test_extract_report_path_uses_last_report_line(self) -> None:
        path = extract_report_path("报告: /tmp/old.md\n其他输出\n报告: /tmp/new.md")

        self.assertEqual(path, Path("/tmp/new.md"))

    def test_save_markdown_uses_report_source_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.md"
            report.write_text("# 报告\n\n正文", encoding="utf-8")
            captured = CapturedOutput(
                content="终端摘要",
                request="/discover",
                source="/discover",
                report_path=report,
            )

            result = save_captured_output(
                captured,
                SaveRequest(format="md", source="report"),
                output_dir=root / "saved",
            )

            self.assertEqual(result.source_used, "report")
            saved_text = result.path.read_text(encoding="utf-8")
            self.assertIn("# 报告", saved_text)
            self.assertNotIn("终端摘要", saved_text)

    def test_save_markdown_preserves_canonical_markdown_without_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = CapturedOutput(
                content="# 标题\n\n> 核心结论\n\n`数据: 个股`\n\n## 结论摘要\n\n- 结论",
                request="分析 000001",
                source="chat",
            )

            result = save_captured_output(
                captured,
                SaveRequest(format="md", source="output"),
                output_dir=root / "saved",
            )

            saved_text = result.path.read_text(encoding="utf-8")
            self.assertTrue(saved_text.startswith("# 标题"))
            self.assertNotIn("# SATS Saved Output", saved_text)
            self.assertIn("`数据: 个股`", saved_text)

    def test_save_pdf_renders_structured_markdown_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = CapturedOutput(
                content="\n".join(
                    [
                        "# 标题",
                        "",
                        "> 核心结论",
                        "",
                        "`数据: 个股` `风格: 研究输出`",
                        "",
                        "## 关键证据",
                        "",
                        "| 维度 | 结论 |",
                        "|---|---|",
                        "| 趋势 | 均线多头 |",
                    ]
                ),
                request="分析 000001",
                source="chat",
            )

            result = save_captured_output(
                captured,
                SaveRequest(format="pdf", source="output"),
                output_dir=root / "saved",
            )

            reader = PdfReader(str(result.path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertIn("标题", text)
            self.assertIn("核心结论", text)
            self.assertIn("关键证据", text)
            self.assertIn("均线多头", text)

    def test_semantic_pdf_markup_uses_expected_colors_and_skips_dates(self) -> None:
        lexicon = OutputSemanticLexicon(
            symbol_codes=("000001.SZ",),
            symbol_names=("上证指数", "沪深300", "平安银行"),
        )

        markup = _semantic_pdf_markup(
            "上证指数 2026-06-08 涨 1.33%，沪深300 跌 -1.70%，000001.SZ 平安银行 12.5",
            semantic_lexicon=lexicon,
        )

        self.assertIn("<font color='#1d4ed8'>上证指数</font>", markup)
        self.assertIn("<font color='#1d4ed8'>沪深300</font>", markup)
        self.assertIn("<font color='#1d4ed8'>000001.SZ</font>", markup)
        self.assertIn("<font color='#1d4ed8'>平安银行</font>", markup)
        self.assertIn("<font color='#ef4444'>1.33%</font>", markup)
        self.assertIn("<font color='#22c55e'>-1.70%</font>", markup)
        self.assertNotIn("<font color='#f472b6'>12.5</font>", markup)
        self.assertNotRegex(markup, r"<font color='#[0-9a-fA-F]{6}'>12\.5</font>")
        self.assertIn("12.5", markup)
        self.assertIn("2026-06-08", markup)
        self.assertNotIn("<font color='#f472b6'>2026-06-08</font>", markup)


if __name__ == "__main__":
    unittest.main()
