from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sats.agent.models import AgentExecutionPolicy
from sats.agent.recovery import AgentFailure, FailureFrame, capture_exception, failure_from_message, redact_text
from sats.agent.source_repair import confirm_source_repair, propose_source_repair_for_turn, validate_source_patch
from sats.agent.tools.analysis_tools import _python_program
from sats.agent.tools.base import AgentToolContext, AgentToolRegistry, AgentToolResult, AgentToolSpec
from sats.cli import build_parser
from sats.memory import ChatMemoryStore
from sats.repl import BUILTIN_COMMANDS, CLI_COMMANDS


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _RepairLLM:
    def chat(self, messages, **kwargs):
        return _Response(
            json.dumps(
                {"code": 'def run(context):\n    return {"result": sum([1])}', "reason": "fix iterable"},
                ensure_ascii=False,
            )
        )


class _PatchLLM:
    def __init__(self, patch: str) -> None:
        self.patch = patch

    def chat(self, messages, **kwargs):
        return _Response(
            json.dumps(
                {
                    "root_cause": "demo returns the wrong value",
                    "diff": self.patch,
                    "tests": ["tests.test_demo"],
                    "risk": "low",
                }
            )
        )


class AgentRecoveryTest(unittest.TestCase):
    def test_traceback_exception_redacts_chain_group_and_has_stable_fingerprint(self) -> None:
        root = Path(__file__).resolve().parents[1]

        def fail() -> None:
            try:
                raise ValueError("token=very-secret-value")
            except ValueError as exc:
                raise RuntimeError("request failed with api_key=abc123") from exc

        failures = []
        for _ in range(2):
            try:
                fail()
            except RuntimeError as exc:
                failures.append(capture_exception(exc, project_root=root, stage="executor", tool="demo.read"))
        self.assertEqual(failures[0].fingerprint, failures[1].fingerprint)
        self.assertNotIn("abc123", failures[0].message)
        self.assertTrue(any(frame.path == "tests/test_agent_recovery.py" for frame in failures[0].frames))

        try:
            raise ExceptionGroup("many", [SyntaxError("bad syntax"), ValueError("password=hunter2")])
        except ExceptionGroup as exc:
            grouped = capture_exception(exc, project_root=root, stage="executor", tool="demo.read")
        self.assertEqual(grouped.exception_type, "ExceptionGroup")
        self.assertNotIn("hunter2", redact_text("password=hunter2", project_root=root))
        syntax = failure_from_message(
            "SyntaxError: invalid syntax",
            project_root=root,
            stage="python_validation",
            tool="analysis.python_program",
            exception_type="SyntaxError",
        )
        self.assertEqual(syntax.category, "python_code_error")

    def test_registry_retries_only_readonly_transient_and_returns_failure_envelope(self) -> None:
        settings = SimpleNamespace(
            project_root=Path(__file__).resolve().parents[1],
            self_repair_mode="runtime",
            self_repair_max_attempts=2,
            self_repair_timeout_seconds=5,
        )
        context = self._context(settings)
        readonly_calls = 0

        def readonly_executor(context, arguments):
            nonlocal readonly_calls
            readonly_calls += 1
            if readonly_calls < 3:
                raise TimeoutError("temporary timeout")
            return AgentToolResult(status="done", content="ok")

        registry = AgentToolRegistry([AgentToolSpec(name="demo.read", description="read", executor=readonly_executor)])
        result = registry.execute("demo.read", {}, context)
        self.assertEqual(result.status, "done")
        self.assertEqual(readonly_calls, 3)
        self.assertIsNone(result.payload["failure"])
        self.assertEqual(len(result.payload["recovery_attempts"]), 2)

        command_calls = 0

        def command_executor(context, arguments):
            nonlocal command_calls
            command_calls += 1
            raise TimeoutError("command timeout")

        command_registry = AgentToolRegistry(
            [AgentToolSpec(name="demo.command", description="command", side_effect="command", executor=command_executor)]
        )
        failed = command_registry.execute("demo.command", {}, context)
        self.assertEqual(command_calls, 1)
        self.assertEqual(failed.status, "error")
        self.assertEqual(failed.payload["failure"]["category"], "timeout")
        self.assertEqual(failed.payload["recovery_attempts"], [])

    def test_python_program_revises_code_and_runs_through_same_guard(self) -> None:
        settings = SimpleNamespace(
            project_root=Path(__file__).resolve().parents[1],
            self_repair_mode="runtime",
            self_repair_max_attempts=2,
            self_repair_timeout_seconds=5,
            openai_model="test-model",
        )
        context = self._context(settings, llm_factory=lambda **kwargs: _RepairLLM())
        result = _python_program(
            context,
            {
                "task": "sum values",
                "code": "def run(context):\n    return sum(1)",
                "row_limit": 10,
            },
        )
        self.assertEqual(result.status, "done")
        program = result.payload["python_program"]
        self.assertEqual(program["result"], 1)
        self.assertEqual(program["recovery_attempts"][0]["status"], "done")

        preflight = _python_program(
            context,
            {"task": "fix top-level context access", "code": 'RESULT = context["value"]', "row_limit": 10},
        )
        self.assertEqual(preflight.status, "done")
        self.assertEqual(preflight.payload["python_program"]["result"], 1)

    def test_python_program_limits_failed_code_revision_to_one_attempt(self) -> None:
        class BadRepairLLM:
            calls = 0
            timeouts: list[int] = []

            def chat(self, messages, **kwargs):
                BadRepairLLM.calls += 1
                BadRepairLLM.timeouts.append(int(kwargs.get("timeout") or 0))
                return _Response(json.dumps({"code": "def run(context):\n    return missing_again", "reason": "still bad"}))

        settings = SimpleNamespace(
            project_root=Path(__file__).resolve().parents[1],
            self_repair_mode="runtime",
            self_repair_max_attempts=5,
            self_repair_timeout_seconds=120,
            openai_model="test-model",
        )
        context = self._context(settings, llm_factory=lambda **kwargs: BadRepairLLM())

        result = _python_program(
            context,
            {"task": "bad code", "code": "def run(context):\n    return missing_name", "timeout_seconds": 120},
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(BadRepairLLM.calls, 1)
        self.assertGreater(BadRepairLLM.timeouts[0], 0)
        self.assertLessEqual(BadRepairLLM.timeouts[0], 30)
        self.assertEqual(len(result.payload["python_program"]["recovery_attempts"]), 1)
        self.assertEqual(result.payload["failure"]["category"], "python_code_error")

    def test_source_patch_allowlist_and_confirm_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._make_repo(root, expected=2)
            patch = self._demo_patch(2)
            self.assertEqual(validate_source_patch(patch), ["sats/demo.py"])
            settings = SimpleNamespace(
                project_root=root,
                db_path=root / "data.duckdb",
                self_repair_test_timeout_seconds=30,
            )
            store = ChatMemoryStore(settings.db_path)
            action_id = self._pending_repair(store, root, patch)
            result = confirm_source_repair(action_id, settings=settings, store=store)
            self.assertEqual(result["status"], "applied")
            self.assertIn("return 2", (root / "sats/demo.py").read_text(encoding="utf-8"))

            with self.assertRaises(ValueError):
                validate_source_patch(
                    "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
                )

    def test_source_proposal_isolated_validation_and_fingerprint_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._make_repo(root, expected=2)
            patch = self._demo_patch(2)
            settings = SimpleNamespace(
                project_root=root,
                db_path=root / "data.duckdb",
                self_repair_mode="propose",
                self_repair_timeout_seconds=10,
                self_repair_test_timeout_seconds=30,
                openai_model="test-model",
            )
            store = ChatMemoryStore(settings.db_path)
            failure = AgentFailure(
                failure_id="failure_proposal",
                category="local_code_defect",
                stage="executor",
                tool="demo.read",
                exception_type="RuntimeError",
                message="wrong value",
                frames=(FailureFrame(path="sats/demo.py", line=2, function="value"),),
                fingerprint="stable-demo-fingerprint",
                retryable=False,
                repair_level="source_proposal",
            )
            store.add_agent_failure(failure, turn_id="turn_proposal", session_id="session_1", status="exhausted")
            factory = lambda **kwargs: _PatchLLM(patch)
            first = propose_source_repair_for_turn(
                "turn_proposal", settings=settings, store=store, llm_factory=factory
            )
            second = propose_source_repair_for_turn(
                "turn_proposal", settings=settings, store=store, llm_factory=factory
            )
            self.assertEqual(first["repair_id"], second["repair_id"])
            self.assertEqual(first["status"], "pending")
            self.assertTrue(Path(first["patch_path"]).is_file())
            self.assertTrue(first["pending_action_id"].startswith("act_"))
            with self.assertRaises(ValueError):
                validate_source_patch(
                    "diff --git a/sats/x.py b/sats/x.py\n--- a/sats/x.py\n+++ b/sats/x.py\n@@ -0,0 +1 @@\n+import subprocess\n"
                )
            with self.assertRaises(ValueError):
                validate_source_patch(
                    "diff --git a/sats/x.py b/sats/x.py\n--- a/sats/x.py\n+++ b/sats/x.py\n@@ -0,0 +1 @@\n+API_KEY = 'secret'\n"
                )

    def test_source_repair_hash_change_stops_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._make_repo(root, expected=2)
            patch = self._demo_patch(2)
            settings = SimpleNamespace(
                project_root=root,
                db_path=root / "data.duckdb",
                self_repair_test_timeout_seconds=30,
            )
            store = ChatMemoryStore(settings.db_path)
            action_id = self._pending_repair(store, root, patch)
            (root / "sats/demo.py").write_text("def value():\n    return 9\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                confirm_source_repair(action_id, settings=settings, store=store)
            self.assertIn("return 9", (root / "sats/demo.py").read_text(encoding="utf-8"))

    def test_source_repair_failed_test_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._make_repo(root, expected=3)
            patch = self._demo_patch(2)
            settings = SimpleNamespace(
                project_root=root,
                db_path=root / "data.duckdb",
                self_repair_test_timeout_seconds=30,
            )
            store = ChatMemoryStore(settings.db_path)
            action_id = self._pending_repair(store, root, patch)
            with self.assertRaises(RuntimeError):
                confirm_source_repair(action_id, settings=settings, store=store)
            self.assertIn("return 1", (root / "sats/demo.py").read_text(encoding="utf-8"))

    def test_failure_and_repair_storage_and_command_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "recovery.duckdb"
            store = ChatMemoryStore(db_path)
            failure = failure_from_message(
                "local defect",
                project_root=Path(__file__).resolve().parents[1],
                stage="runtime",
                tool="demo.read",
                category="local_code_defect",
            )
            store.add_agent_failure(failure, turn_id="turn_1", session_id="session_1", status="exhausted")
            stored = store.get_agent_failure(failure.failure_id)
            self.assertEqual(stored["fingerprint"], failure.fingerprint)
            store.add_agent_repair(
                {
                    "repair_id": "repair_1",
                    "failure_id": failure.failure_id,
                    "turn_id": "turn_1",
                    "status": "pending",
                }
            )
            reused = store.find_open_repair_by_fingerprint(failure.fingerprint)
            self.assertEqual(reused["repair_id"], "repair_1")

        parser = build_parser()
        parsed = parser.parse_args(["repair", "run", "--turn", "turn_1"])
        self.assertEqual((parsed.command, parsed.repair_command, parsed.turn), ("repair", "run", "turn_1"))
        self.assertIn("repair", CLI_COMMANDS)
        self.assertIn("repairs", BUILTIN_COMMANDS)

    @staticmethod
    def _context(settings, *, llm_factory=None) -> AgentToolContext:
        return AgentToolContext(
            settings=settings,
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            llm_factory=llm_factory,
            session_id="test",
            turn_id="turn_test",
            message="test",
        )

    @staticmethod
    def _make_repo(root: Path, *, expected: int) -> None:
        (root / "sats").mkdir()
        (root / "tests").mkdir()
        (root / "sats/__init__.py").write_text("", encoding="utf-8")
        (root / "tests/__init__.py").write_text("", encoding="utf-8")
        (root / "sats/demo.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (root / "tests/test_demo.py").write_text(
            "import unittest\nfrom sats.demo import value\n\n"
            f"class DemoTest(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value(), {expected})\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "--all"], cwd=root, check=True)

    @staticmethod
    def _demo_patch(value: int) -> str:
        return (
            "diff --git a/sats/demo.py b/sats/demo.py\n"
            "--- a/sats/demo.py\n"
            "+++ b/sats/demo.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def value():\n"
            "-    return 1\n"
            f"+    return {value}\n"
        )

    @staticmethod
    def _pending_repair(store: ChatMemoryStore, root: Path, patch: str) -> str:
        repair_dir = root / "reports/repairs"
        repair_dir.mkdir(parents=True)
        patch_path = repair_dir / "repair_test.patch"
        patch_path.write_text(patch, encoding="utf-8")
        digest = hashlib.sha256((root / "sats/demo.py").read_bytes()).hexdigest()
        store.add_agent_repair(
            {
                "repair_id": "repair_test",
                "failure_id": "failure_test",
                "turn_id": "turn_test",
                "status": "pending",
                "patch_path": str(patch_path),
                "target_hashes": {"sats/demo.py": digest},
                "tests": ["tests.test_demo"],
            }
        )
        return store.create_pending_action(
            session_id="repair-test",
            turn_id="turn_test",
            action_type="source_repair",
            payload={
                "repair_id": "repair_test",
                "patch_path": str(patch_path),
                "target_hashes": {"sats/demo.py": digest},
                "tests": ["tests.test_demo"],
            },
        )


if __name__ == "__main__":
    unittest.main()
