from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sats.cli import main
from sats.dependencies import (
    OptionalDependencyError,
    ensure_optional_dependencies,
    sync_optional_dependency_files,
)


class OptionalDependencyTest(unittest.TestCase):
    def test_factor_ml_status_does_not_install_or_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "requirements.txt"
            pyproject = root / "pyproject.toml"
            requirements.write_text("pandas>=2.2\n", encoding="utf-8")
            pyproject.write_text(
                "[project]\nname = \"sats-test\"\n\n[project.optional-dependencies]\nakshare = [\"akshare>=1.16\"]\n",
                encoding="utf-8",
            )
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.dependencies.importlib.util.find_spec", return_value=None),
                patch("sats.dependencies.subprocess.run") as pip_run,
            ):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(main(["factor", "ml", "status", "--json"]), 0)

            pip_run.assert_not_called()
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["available"])
            self.assertEqual(payload["missing"], ["pyqlib", "lightgbm", "xgboost", "scikit-learn"])
            self.assertEqual(requirements.read_text(encoding="utf-8"), "pandas>=2.2\n")
            self.assertNotIn("ml =", pyproject.read_text(encoding="utf-8"))

    def test_setup_installs_missing_dependencies_and_syncs_files_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "requirements.txt"
            pyproject = root / "pyproject.toml"
            requirements.write_text("pandas>=2.2\n", encoding="utf-8")
            pyproject.write_text(
                "[project]\nname = \"sats-test\"\n\n[project.optional-dependencies]\nakshare = [\"akshare>=1.16\"]\n",
                encoding="utf-8",
            )
            fake_python = root / ".venv" / "bin" / "python"
            installed = {"value": False}

            def fake_find_spec(import_name: str):
                if installed["value"]:
                    return object()
                return None

            def fake_pip_run(cmd, **kwargs):
                installed["value"] = True
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                patch("sats.dependencies.sys.executable", str(fake_python)),
                patch("sats.dependencies.sys.prefix", str(root / ".venv")),
                patch("sats.dependencies.importlib.util.find_spec", side_effect=fake_find_spec),
                patch("sats.dependencies.subprocess.run", side_effect=fake_pip_run) as pip_run,
            ):
                status = ensure_optional_dependencies("qlib_ml", project_root=root)
                sync_optional_dependency_files(root, groups=("qlib_ml", "deep"))

            self.assertTrue(status.available)
            pip_run.assert_called_once()
            self.assertEqual(
                pip_run.call_args.args[0],
                [str(fake_python), "-m", "pip", "install", "pyqlib", "lightgbm", "xgboost", "scikit-learn"],
            )
            requirements_text = requirements.read_text(encoding="utf-8")
            for package in ("pyqlib", "lightgbm", "xgboost", "scikit-learn"):
                self.assertEqual(requirements_text.splitlines().count(package), 1)
            pyproject_text = pyproject.read_text(encoding="utf-8")
            self.assertEqual(pyproject_text.count("ml = "), 1)
            self.assertEqual(pyproject_text.count("deep = "), 1)

    def test_auto_install_requires_project_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("sats.dependencies.sys.executable", "/usr/bin/python3"),
                patch("sats.dependencies.sys.prefix", "/usr"),
                patch("sats.dependencies.importlib.util.find_spec", return_value=None),
                patch("sats.dependencies.subprocess.run") as pip_run,
            ):
                with self.assertRaises(OptionalDependencyError):
                    ensure_optional_dependencies("qlib_ml", project_root=root)
            pip_run.assert_not_called()

    def test_factor_ml_train_enters_dependency_gate_then_calls_train_engine(self) -> None:
        status = SimpleNamespace(
            available=True,
            present=["pyqlib", "lightgbm", "xgboost", "scikit-learn"],
            missing=[],
            installed=[],
            files_updated=[],
            error="",
            to_dict=lambda: {"available": True},
        )
        fake_result = SimpleNamespace(
            run_id="factor_ml_test",
            model_type="lightgbm",
            profile="balanced",
            factor_ids=["barra_style_value"],
            horizon=1,
            model_path="models/factors/factor_ml_test/model.pkl",
            metrics={"train_rows": 10},
            to_dict=lambda: {
                "run_id": "factor_ml_test",
                "model_type": "lightgbm",
                "profile": "balanced",
                "factor_ids": ["barra_style_value"],
                "horizon": 1,
                "model_path": "models/factors/factor_ml_test/model.pkl",
                "metrics": {"train_rows": 10},
            },
        )
        with (
            patch("sats.cli.load_settings", return_value=SimpleNamespace(project_root=Path("."), db_path=Path("x.duckdb"))),
            patch("sats.cli.ensure_optional_dependencies", return_value=status) as ensure,
            patch("sats.cli.AStockDataProvider"),
            patch("sats.cli.train_factor_ml_model", return_value=fake_result) as train,
        ):
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    main(
                        [
                            "factor",
                            "ml",
                            "train",
                            "--profile",
                            "balanced",
                            "--model",
                            "lightgbm",
                            "--train-end",
                            "20260514",
                        ]
                    ),
                    0,
                )
        ensure.assert_called_once()
        train.assert_called_once()
        self.assertIn("factor_ml_test", stdout.getvalue())

    def test_ordinary_factor_list_does_not_touch_ml_dependency_gate(self) -> None:
        with patch("sats.cli.ensure_optional_dependencies") as ensure:
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["factor", "list", "--zoo", "barra_style"]), 0)
        ensure.assert_not_called()
        self.assertIn("barra_style_value", stdout.getvalue())
