from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sats.cli import main
from sats.scheduler import SchedulerService, ScheduledTaskRunner, compute_next_run
from sats.storage.duckdb import DuckDBStorage


class SchedulerStorageTest(unittest.TestCase):
    def test_scheduled_task_and_run_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.insert_scheduled_task(
                {
                    "name": "daily-discover",
                    "task_type": "chat",
                    "text": "预测未来几天大概率上涨的股票",
                    "schedule_kind": "daily",
                    "days": [],
                    "time_of_day": "08:45",
                    "next_run_at": "2026-05-25 08:45:00",
                }
            )

            task = storage.get_scheduled_task("daily-discover")
            self.assertEqual(task["task_type"], "chat")
            self.assertTrue(task["enabled"])
            self.assertEqual(storage.list_due_scheduled_tasks("2026-05-25 08:45:01")[0]["name"], "daily-discover")

            storage.insert_scheduled_task_run(
                {
                    "run_id": "run-1",
                    "task_name": "daily-discover",
                    "task_type": "chat",
                    "text": task["text"],
                    "scheduled_for": task["next_run_at"],
                    "started_at": "2026-05-25 08:45:01",
                    "finished_at": "2026-05-25 08:45:02",
                    "status": "success",
                    "duration_seconds": 1.2,
                    "output_text": "ok",
                }
            )
            self.assertEqual(storage.list_scheduled_task_runs()[0]["output_text"], "ok")
            self.assertTrue(storage.set_scheduled_task_enabled("daily-discover", False))
            self.assertFalse(storage.list_scheduled_tasks()[0]["enabled"])
            self.assertTrue(storage.delete_scheduled_task("daily-discover"))


class SchedulerTimingTest(unittest.TestCase):
    def test_compute_next_run_daily_and_weekly(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 5, 25, 8, 0, tzinfo=tz)

        self.assertEqual(
            compute_next_run(now, schedule_kind="daily", days=[], time_of_day="08:45"),
            "2026-05-25 08:45:00",
        )
        self.assertEqual(
            compute_next_run(now, schedule_kind="daily", days=[], time_of_day="07:45"),
            "2026-05-26 07:45:00",
        )
        self.assertEqual(
            compute_next_run(now, schedule_kind="weekly", days=["wed", "fri"], time_of_day="09:10"),
            "2026-05-27 09:10:00",
        )


class ScheduledTaskRunnerTest(unittest.TestCase):
    def test_cli_and_chat_runner_capture_output(self) -> None:
        def fake_main(argv: list[str]) -> int:
            print("cli:" + " ".join(argv))
            return 0

        runner = ScheduledTaskRunner(cli_main=fake_main, chat_runner=lambda message: "chat:" + message)
        cli_run = runner.run_task({"name": "cli-task", "task_type": "cli", "text": "results --passed"})
        chat_run = runner.run_task({"name": "chat-task", "task_type": "chat", "text": "推荐股票"})

        self.assertEqual(cli_run["status"], "success")
        self.assertIn("cli:results --passed", cli_run["output_text"])
        self.assertEqual(chat_run["output_text"], "chat:推荐股票")

    def test_blocks_recursive_and_long_running_cli_tasks(self) -> None:
        runner = ScheduledTaskRunner(cli_main=lambda _argv: 0)

        for text in ("schedule list", "serve", "monitor run", "monitor-display run"):
            run = runner.run_task({"name": "bad", "task_type": "cli", "text": text})
            self.assertEqual(run["status"], "failed")
            self.assertIn("cannot run", run["error"])


class SchedulerServiceTest(unittest.TestCase):
    def test_run_once_executes_due_task_and_writes_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.insert_scheduled_task(
                {
                    "name": "daily-cli",
                    "task_type": "cli",
                    "text": "skills",
                    "schedule_kind": "daily",
                    "days": [],
                    "time_of_day": "08:45",
                    "next_run_at": "2026-05-25 08:44:00",
                }
            )
            runner = ScheduledTaskRunner(cli_main=lambda _argv: print("done") or 0)
            service = SchedulerService(storage=storage, runner=runner)

            runs = service.run_once(now=datetime(2026, 5, 25, 8, 45, tzinfo=ZoneInfo("Asia/Shanghai")))

            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "success")
            self.assertIn("done", storage.list_scheduled_task_runs()[0]["output_text"])
            task = storage.get_scheduled_task("daily-cli")
            self.assertEqual(task["last_status"], "success")
            self.assertFalse(task["running"])

    def test_running_task_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.insert_scheduled_task(
                {
                    "name": "busy",
                    "task_type": "cli",
                    "text": "skills",
                    "schedule_kind": "daily",
                    "days": [],
                    "time_of_day": "08:45",
                    "next_run_at": "2026-05-25 08:44:00",
                }
            )
            self.assertTrue(storage.mark_scheduled_task_running("busy"))
            service = SchedulerService(storage=storage, runner=ScheduledTaskRunner(cli_main=lambda _argv: 0))

            runs = service.run_once(now=datetime(2026, 5, 25, 8, 45, tzinfo=ZoneInfo("Asia/Shanghai")))

            self.assertEqual(runs[0]["status"], "skipped")


class SchedulerCliTest(unittest.TestCase):
    def test_schedule_cli_add_list_start_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            with patch("builtins.print") as printer:
                self.assertEqual(
                    main(
                        [
                            "schedule",
                            "add",
                            "--name",
                            "daily-discover",
                            "--type",
                            "chat",
                            "--text",
                            "推荐股票",
                            "--daily",
                            "--time",
                            "08:45",
                            "--db",
                            str(db),
                        ]
                    ),
                    0,
                )
                self.assertEqual(main(["schedule", "list", "--db", str(db)]), 0)
            printed = "\n".join(str(call.args[0]) for call in printer.call_args_list if call.args)
            self.assertIn("daily-discover", printed)

            fake_process = SimpleNamespace(pid=2468)
            with patch("subprocess.Popen", return_value=fake_process) as popen, patch("builtins.print") as printer:
                self.assertEqual(main(["schedule", "start", "--db", str(db)]), 0)
            popen.assert_called_once()
            self.assertIn("PID 2468", printer.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
