from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sats.output_saver import CapturedOutput, SaveRequest, extract_report_path, parse_save_request, save_captured_output


class OutputSaverTest(unittest.TestCase):
    def test_parse_pure_and_compound_save_requests(self) -> None:
        pure = parse_save_request("保存上面结果为PDF")
        compound = parse_save_request("分析 000938 技术面，并保存结果为 MD")
        pure_output = parse_save_request("上面内容输出为PDF")
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


if __name__ == "__main__":
    unittest.main()
