from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.analysis.daily_stock_analysis import (
    AnalysisRanking,
    AnalysisRunResult,
    parse_report_rankings,
    run_daily_stock_analysis_for_symbols,
    run_screened_stock_analysis,
    to_daily_stock_analysis_code,
)
from sats.cli import _format_analysis_rankings, _resolve_analysis_trade_date, build_parser, cmd_analyze_dsa, cmd_dsa
from sats.screening.base import ScreeningResult
from sats.storage.duckdb import DuckDBStorage


def make_screening_result(ts_code: str, *, score: float = 80.0) -> ScreeningResult:
    return ScreeningResult(
        trade_date="20260513",
        ts_code=ts_code,
        rule_name="ma_volume_relative_strength",
        passed=True,
        score=score,
        matched_conditions=["ok"],
        failed_conditions=[],
        metrics={},
    )


class DailyStockAnalysisBridgeTest(unittest.TestCase):
    def test_to_daily_stock_analysis_code_excludes_bj_and_688(self) -> None:
        self.assertEqual(to_daily_stock_analysis_code("000001.SZ"), "000001")
        self.assertEqual(to_daily_stock_analysis_code("600519.SH"), "600519")
        self.assertIsNone(to_daily_stock_analysis_code("430047.BJ"))
        self.assertIsNone(to_daily_stock_analysis_code("920748.BJ"))
        self.assertIsNone(to_daily_stock_analysis_code("430047"))
        self.assertIsNone(to_daily_stock_analysis_code("830000"))
        self.assertIsNone(to_daily_stock_analysis_code("688001.SH"))

    def test_parse_report_rankings_sorts_by_score(self) -> None:
        report = """
## 分析结果摘要

🟡 **平安银行(000001)**: 持有 | 评分 68 | 震荡
🟢 **贵州茅台(600519)**: 买入 | 评分 82 | 看多
"""

        rows = parse_report_rankings(report)

        self.assertEqual([row.code for row in rows], ["600519", "000001"])
        self.assertEqual(rows[0].name, "贵州茅台")
        self.assertEqual(rows[0].score, 82)

    def test_run_screened_stock_analysis_uses_external_daily_stock_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sats_env = root / ".env"
            sats_env.write_text("SATS_DB_PATH=sats.duckdb\n", encoding="utf-8")
            analysis_dir = root / "daily_stock_analysis"
            analysis_dir.mkdir()
            (analysis_dir / "main.py").write_text("# fake\n", encoding="utf-8")
            storage = DuckDBStorage(root / "sats.duckdb")
            storage.upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "name": "平安银行"},
                        {"ts_code": "600519.SH", "name": "贵州茅台"},
                        {"ts_code": "688001.SH", "name": "科创样本"},
                        {"ts_code": "430047.BJ", "name": "北交样本"},
                    ]
                )
            )
            storage.upsert_screening_results(
                [
                    make_screening_result("000001.SZ", score=70),
                    make_screening_result("600519.SH", score=90),
                    make_screening_result("688001.SH", score=95),
                    make_screening_result("430047.BJ", score=96),
                ]
            )
            captured: dict[str, object] = {}

            def fake_runner(cmd, **kwargs):
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs
                report = analysis_dir / "reports" / "report_20260513.md"
                report.parent.mkdir()
                report.write_text(
                    "🟢 **贵州茅台(600519)**: 买入 | 评分 82 | 看多\n"
                    "🟡 **平安银行(000001)**: 持有 | 评分 68 | 震荡\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(cmd, 0, stdout="hidden stdout", stderr="hidden stderr")

            result = run_screened_stock_analysis(
                storage=storage,
                trade_date="20260513",
                rule_name="ma_volume_relative_strength",
                reports_dir=root / "reports",
                analysis_dir=analysis_dir,
                sats_env_path=sats_env,
                python_executable="/usr/bin/python3",
                runner=fake_runner,
            )

            self.assertEqual(captured["cmd"][:4], ["/usr/bin/python3", str(analysis_dir / "main.py"), "--stocks", "600519,000001"])
            self.assertIn("--no-notify", captured["cmd"])
            self.assertIn("--no-market-review", captured["cmd"])
            self.assertEqual(captured["kwargs"]["cwd"], str(analysis_dir))
            self.assertEqual(result.analyzed_codes, ["600519", "000001"])
            self.assertEqual(result.skipped_codes, ["430047.BJ", "688001.SH"])
            self.assertEqual([row.code for row in result.rankings], ["600519", "000001"])
            self.assertTrue(str(result.archived_report).endswith(".md"))

    def test_run_screened_stock_analysis_does_not_call_native_when_no_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = DuckDBStorage(root / "sats.duckdb")

            result = run_screened_stock_analysis(
                storage=storage,
                trade_date="20260513",
                rule_name="ma_volume_relative_strength",
                reports_dir=root / "reports",
                runner=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
            )

            self.assertEqual(result.message, "无通过筛选股票")
            self.assertEqual(result.analyzed_codes, [])

    def test_cmd_analyze_dsa_prints_progress_rankings_and_report(self) -> None:
        fake_result = AnalysisRunResult(
            analyzed_codes=["600519"],
            skipped_codes=[],
            rankings=[AnalysisRanking("600519", "贵州茅台", 82, "买入", "看多")],
            source_report=Path("/tmp/source.md"),
            archived_report=Path("/tmp/report.md"),
        )
        args = SimpleNamespace(
            trade_date="20260513",
            rule="ma-volume-relative-strength",
            stocks=None,
            db=Path("/tmp/sats.duckdb"),
        )
        stdout = io.StringIO()

        with patch("sats.cli.DuckDBStorage"), patch(
            "sats.cli.run_screened_stock_analysis",
            return_value=fake_result,
        ), redirect_stdout(stdout):
            exit_code = cmd_analyze_dsa(args)

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertTrue(output.startswith("analyzing...\n"))
        self.assertIn("1. 600519 贵州茅台 评分 82 买入 看多", output)
        self.assertIn("报告: /tmp/report.md", output)

    def test_cmd_analyze_dsa_stocks_uses_external_bridge(self) -> None:
        fake_result = AnalysisRunResult(
            analyzed_codes=["000001"],
            skipped_codes=[],
            rankings=[AnalysisRanking("000001", "平安银行", 76, "关注", "偏多")],
            source_report=Path("/tmp/source.md"),
            archived_report=Path("/tmp/report.md"),
        )
        args = SimpleNamespace(
            trade_date="20260513",
            rule=None,
            stocks="000001,600519",
            db=Path("/tmp/sats.duckdb"),
        )
        stdout = io.StringIO()
        captured = {}

        def fake_run(symbols, **kwargs):
            captured["symbols"] = symbols
            captured["kwargs"] = kwargs
            return fake_result

        with patch("sats.cli.DuckDBStorage"), patch("sats.cli.run_daily_stock_analysis_for_symbols", side_effect=fake_run), redirect_stdout(stdout):
            exit_code = cmd_analyze_dsa(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["symbols"], ["000001.SZ", "600519.SH"])
        self.assertEqual(captured["kwargs"]["trade_date"], "20260513")
        self.assertIn("1. 000001 平安银行 评分 76 关注 偏多", stdout.getvalue())

    def test_cmd_analyze_dsa_stocks_rejects_rule(self) -> None:
        args = SimpleNamespace(
            trade_date="20260513",
            rule="chan-composite",
            stocks="000001",
            db=Path("/tmp/sats.duckdb"),
        )

        with self.assertRaisesRegex(SystemExit, "--rule only supports screened"):
            cmd_analyze_dsa(args)

    def test_run_daily_stock_analysis_reports_no_supported_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            result = run_daily_stock_analysis_for_symbols(
                ["688001.SH", "430047.BJ"],
                trade_date="20260513",
                reports_dir=root / "reports",
                analysis_dir=root / "missing_daily_stock_analysis",
                runner=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
            )

            self.assertEqual(result.message, "无可供 daily_stock_analysis 分析股票")
            self.assertEqual(result.skipped_codes, ["688001.SH", "430047.BJ"])

    def test_cmd_dsa_prints_progress_rankings_and_report(self) -> None:
        fake_result = AnalysisRunResult(
            analyzed_codes=["000001.SZ"],
            skipped_codes=[],
            rankings=[AnalysisRanking("000001.SZ", "平安银行", 76, "买入", "看多")],
            source_report=Path("/tmp/source.md"),
            archived_report=Path("/tmp/report.md"),
            llm_unavailable=True,
        )
        args = SimpleNamespace(
            stocks="000001,600519",
            from_screened=False,
            rule=None,
            trade_date="20260513",
            lookback_days=120,
            db=Path("/tmp/sats.duckdb"),
        )
        stdout = io.StringIO()
        captured = {}

        def fake_run(symbols, **kwargs):
            captured["symbols"] = symbols
            captured["kwargs"] = kwargs
            return fake_result

        with patch("sats.cli.DuckDBStorage"), patch("sats.cli.run_dsa_analysis", side_effect=fake_run), redirect_stdout(stdout):
            exit_code = cmd_dsa(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["symbols"], ["000001.SZ", "600519.SH"])
        self.assertEqual(captured["kwargs"]["trade_date"], "20260513")
        self.assertEqual(captured["kwargs"]["lookback_days"], 120)
        self.assertEqual(captured["kwargs"]["llm_timeout_seconds"], 20)
        self.assertTrue(captured["kwargs"]["llm_enabled"])
        output = stdout.getvalue()
        self.assertTrue(output.startswith("analyzing...\n"))
        self.assertIn("提示: 大模型不可用，已使用本地规则评级。", output)
        self.assertIn("1. 000001.SZ 平安银行 评分 76 买入 看多", output)
        self.assertIn("报告: /tmp/report.md", output)

    def test_cmd_dsa_from_screened_uses_screened_bridge(self) -> None:
        fake_result = AnalysisRunResult(
            analyzed_codes=["600519.SH"],
            skipped_codes=[],
            rankings=[AnalysisRanking("600519.SH", "贵州茅台", 88, "买入", "看多")],
            source_report=Path("/tmp/source.md"),
            archived_report=Path("/tmp/report.md"),
        )
        args = SimpleNamespace(
            stocks=None,
            from_screened=True,
            rule="chan-composite",
            trade_date="20260518",
            lookback_days=90,
            db=Path("/tmp/sats.duckdb"),
        )
        stdout = io.StringIO()
        captured = {}

        def fake_run(symbols, **kwargs):
            captured["symbols"] = symbols
            captured.update(kwargs)
            return fake_result

        fake_storage = SimpleNamespace(
            list_screening_stocks=lambda **kwargs: [{"ts_code": "600519.SH"}],
        )

        with patch("sats.cli.DuckDBStorage", return_value=fake_storage), patch("sats.cli.run_dsa_analysis", side_effect=fake_run), redirect_stdout(stdout):
            exit_code = cmd_dsa(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["trade_date"], "20260518")
        self.assertEqual(captured["lookback_days"], 90)
        self.assertEqual(captured["source_label"], "chan_composite")
        self.assertEqual(captured["llm_timeout_seconds"], 20)
        self.assertTrue(captured["llm_enabled"])
        self.assertEqual(captured["symbols"], ["600519.SH"])
        output = stdout.getvalue()
        self.assertTrue(output.startswith("analyzing...\n"))
        self.assertIn("1. 600519.SH 贵州茅台 评分 88 买入 看多", output)
        self.assertIn("报告: /tmp/report.md", output)

    def test_cmd_dsa_from_screened_requires_trade_date(self) -> None:
        args = SimpleNamespace(
            stocks=None,
            from_screened=True,
            rule="chan-composite",
            trade_date=None,
            lookback_days=90,
            db=Path("/tmp/sats.duckdb"),
        )

        with self.assertRaisesRegex(SystemExit, "requires --trade-date"):
            cmd_dsa(args)

    def test_cmd_dsa_rule_requires_from_screened(self) -> None:
        args = SimpleNamespace(
            stocks="000001",
            from_screened=False,
            rule="chan-composite",
            trade_date="20260518",
            lookback_days=90,
            db=Path("/tmp/sats.duckdb"),
        )

        with self.assertRaisesRegex(SystemExit, "--rule only supports --from-screened"):
            cmd_dsa(args)

    def test_dsa_parser_supports_from_screened_source(self) -> None:
        args = build_parser().parse_args(
            ["dsa", "--from-screened", "--trade-date", "20260518", "--rule", "chan-composite", "--explain-rating"]
        )

        self.assertTrue(args.from_screened)
        self.assertIsNone(args.stocks)
        self.assertEqual(args.trade_date, "20260518")
        self.assertEqual(args.rule, "chan-composite")
        self.assertTrue(args.explain_rating)
        self.assertEqual(args.llm_timeout, 20)
        self.assertFalse(args.no_llm)

    def test_dsa_parser_supports_llm_timeout_and_no_llm(self) -> None:
        args = build_parser().parse_args(["dsa", "--stocks", "000001", "--llm-timeout", "5", "--no-llm"])

        self.assertEqual(args.llm_timeout, 5)
        self.assertTrue(args.no_llm)

    def test_cmd_dsa_passes_llm_timeout_and_no_llm(self) -> None:
        fake_result = AnalysisRunResult(
            analyzed_codes=["000001.SZ"],
            skipped_codes=[],
            rankings=[AnalysisRanking("000001.SZ", "平安银行", 76, "买入", "看多")],
            source_report=Path("/tmp/source.md"),
            archived_report=Path("/tmp/report.md"),
        )
        args = SimpleNamespace(
            stocks="000001",
            from_screened=False,
            rule=None,
            trade_date="20260513",
            lookback_days=120,
            llm_timeout=5,
            no_llm=True,
            db=Path("/tmp/sats.duckdb"),
        )
        stdout = io.StringIO()
        captured = {}

        def fake_run(symbols, **kwargs):
            captured["symbols"] = symbols
            captured["kwargs"] = kwargs
            return fake_result

        with patch("sats.cli.DuckDBStorage"), patch("sats.cli.run_dsa_analysis", side_effect=fake_run), redirect_stdout(stdout):
            exit_code = cmd_dsa(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["kwargs"]["llm_timeout_seconds"], 5)
        self.assertFalse(captured["kwargs"]["llm_enabled"])

    def test_format_analysis_rankings_groups_native_extra_and_explains_rating(self) -> None:
        rows = [
            AnalysisRanking("600519.SH", "贵州茅台", 65, "持有", "看多", raw_advice="买入", rating_adjustment="RSI 超买"),
            AnalysisRanking(
                "688001.SH",
                "华兴源创",
                70,
                "持有",
                "看多",
                external_supported=False,
                external_skip_reason="daily_stock_analysis 不支持",
            ),
        ]

        text = _format_analysis_rankings(rows, explain_rating=True)

        self.assertIn("可比股票", text)
        self.assertIn("1. 600519.SH 贵州茅台 评分 65 持有 看多", text)
        self.assertIn("调整: 原始评级 买入，RSI 超买", text)
        self.assertIn("原生额外股票", text)
        self.assertIn("1. 688001.SH 华兴源创 评分 70 持有 看多 daily_stock_analysis 不支持", text)

    def test_resolve_analysis_trade_date_uses_provider_latest_open_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            class Provider:
                def _recent_trade_dates(self, trade_date, *, count):
                    return ["20260518"]

            self.assertEqual(
                _resolve_analysis_trade_date(None, storage=storage, provider=Provider()),
                "20260518",
            )

    def test_resolve_analysis_trade_date_uses_explicit_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            self.assertEqual(
                _resolve_analysis_trade_date("20260517", storage=storage, provider=None),
                "20260517",
            )

    def test_dsa_parser_requires_one_source(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            build_parser().parse_args(["dsa"])

    def test_dsa_parser_rejects_multiple_sources(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            build_parser().parse_args(["dsa", "--stocks", "000001", "--from-screened"])

    def test_parser_rejects_old_analyze_screened_command(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            build_parser().parse_args(["analyze-screened", "--trade-date", "20260513"])


if __name__ == "__main__":
    unittest.main()
