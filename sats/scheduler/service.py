from __future__ import annotations

import io
import os
import shlex
import time
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from sats.storage.duckdb import DuckDBStorage

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SCHEDULER_SERVICE_NAME = "scheduler"
WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
WEEKDAY_LABELS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FORBIDDEN_TOP_LEVEL_COMMANDS = {"schedule", "serve"}
FORBIDDEN_LONG_RUNNING = {
    ("monitor", "start"),
    ("monitor", "run"),
    ("monitor-display", "start"),
    ("monitor-display", "run"),
}


@dataclass(slots=True)
class SchedulerConfig:
    interval_seconds: int = 30
    max_cycles: int | None = None


class ScheduledTaskRunner:
    def __init__(
        self,
        *,
        settings=None,
        cli_main: Callable[[list[str]], int] | None = None,
        chat_runner: Callable[[str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.cli_main = cli_main
        self.chat_runner = chat_runner

    def run_task(self, task: dict) -> dict:
        started = _now()
        start_mono = time.monotonic()
        output = ""
        error = ""
        status = "success"
        report_path = ""
        try:
            if task.get("task_type") == "cli":
                output = self._run_cli(str(task.get("text") or ""))
            elif task.get("task_type") == "chat":
                output = self._run_chat(str(task.get("text") or ""))
            else:
                raise ValueError(f"unknown scheduled task type: {task.get('task_type')}")
        except SystemExit as exc:
            if exc.code in (None, 0):
                status = "success"
            else:
                status = "failed"
                error = str(exc.code)
        except _ScheduledTaskExecutionError as exc:
            status = "failed"
            output = exc.output
            error = str(exc)
        except Exception as exc:
            status = "failed"
            error = str(exc)
        duration = max(0.0, time.monotonic() - start_mono)
        if not output and error:
            output = error
        report_path = _extract_report_path(output)
        return {
            "run_id": uuid.uuid4().hex,
            "task_name": str(task.get("name") or ""),
            "task_type": str(task.get("task_type") or ""),
            "text": str(task.get("text") or ""),
            "scheduled_for": task.get("next_run_at") or "",
            "started_at": _format_dt(started),
            "finished_at": _format_dt(_now()),
            "status": status,
            "duration_seconds": duration,
            "output_text": output,
            "error": error,
            "report_path": report_path,
        }

    def skipped_run(self, task: dict, reason: str) -> dict:
        now = _format_dt(_now())
        return {
            "run_id": uuid.uuid4().hex,
            "task_name": str(task.get("name") or ""),
            "task_type": str(task.get("task_type") or ""),
            "text": str(task.get("text") or ""),
            "scheduled_for": task.get("next_run_at") or "",
            "started_at": now,
            "finished_at": now,
            "status": "skipped",
            "duration_seconds": 0.0,
            "output_text": reason,
            "error": reason,
            "report_path": "",
        }

    def _run_cli(self, text: str) -> str:
        argv = shlex.split(text)
        _validate_cli_argv(argv)
        stdout = io.StringIO()
        stderr = io.StringIO()
        cli_main = self.cli_main
        if cli_main is None:
            from sats.cli import main as cli_main
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli_main(argv)
            except SystemExit as exc:
                code = exc.code
        captured = _join_output(stdout.getvalue(), stderr.getvalue())
        if code not in (None, 0):
            raise _ScheduledTaskExecutionError(str(code), captured)
        return captured

    def _run_chat(self, text: str) -> str:
        message = str(text or "").strip()
        if not message:
            raise ValueError("chat task text is required")
        if self.chat_runner is not None:
            return str(self.chat_runner(message) or "")
        from sats.chat import format_chat_result, run_chat_once

        kwargs = {}
        if self.settings is not None:
            kwargs["settings"] = self.settings
        result = run_chat_once(message, **kwargs)
        return format_chat_result(result)


class SchedulerService:
    def __init__(
        self,
        *,
        storage: DuckDBStorage,
        runner: ScheduledTaskRunner | None = None,
    ) -> None:
        self.storage = storage
        self.runner = runner or ScheduledTaskRunner()

    def run_once(self, *, now: datetime | None = None) -> list[dict]:
        current = now or _now()
        due_tasks = self.storage.list_due_scheduled_tasks(_format_dt(current))
        runs = []
        for task in due_tasks:
            if not task.get("enabled", True):
                continue
            if str(task.get("schedule_kind") or "") == "trading_day" and not _is_trading_day(
                current.strftime("%Y%m%d"),
                settings=getattr(self.runner, "settings", None),
            ):
                run = self.runner.skipped_run(task, "非交易日，本轮跳过")
                self.storage.insert_scheduled_task_run(run)
                next_run_at = compute_next_run(
                    current,
                    schedule_kind="trading_day",
                    days=[],
                    time_of_day=str(task.get("time_of_day") or "09:00"),
                )
                self.storage.update_scheduled_task_after_run(
                    str(task["name"]),
                    last_status="skipped",
                    last_run_at=str(run["finished_at"]),
                    next_run_at=next_run_at,
                )
                runs.append(run)
                continue
            if not self.storage.mark_scheduled_task_running(str(task["name"])):
                run = self.runner.skipped_run(task, "上次运行未结束，本轮跳过")
                self.storage.insert_scheduled_task_run(run)
                runs.append(run)
                continue
            try:
                run = self.runner.run_task(task)
                self.storage.insert_scheduled_task_run(run)
                next_run_at = compute_next_run(
                    current,
                    schedule_kind=str(task.get("schedule_kind") or "daily"),
                    days=task.get("days") or [],
                    time_of_day=str(task.get("time_of_day") or "09:00"),
                )
                self.storage.update_scheduled_task_after_run(
                    str(task["name"]),
                    last_status=str(run["status"]),
                    last_run_at=str(run["finished_at"]),
                    next_run_at=next_run_at,
                )
                runs.append(run)
            finally:
                self.storage.mark_scheduled_task_running(str(task["name"]), running=False)
        return runs

    def run_forever(self, config: SchedulerConfig) -> None:
        interval = max(1, int(config.interval_seconds))
        self.storage.upsert_monitor_runtime(
            service_name=SCHEDULER_SERVICE_NAME,
            status="running",
            pid=os.getpid(),
            params={"interval": interval},
            heartbeat=True,
        )
        cycles = 0
        while True:
            try:
                self.storage.upsert_monitor_runtime(
                    service_name=SCHEDULER_SERVICE_NAME,
                    status="running",
                    pid=os.getpid(),
                    params={"interval": interval},
                    heartbeat=True,
                )
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.storage.upsert_monitor_runtime(
                    service_name=SCHEDULER_SERVICE_NAME,
                    status="running",
                    pid=os.getpid(),
                    params={"interval": interval},
                    last_error=str(exc),
                    heartbeat=True,
                )
            cycles += 1
            if config.max_cycles is not None and cycles >= config.max_cycles:
                break
            time.sleep(interval)

    def run_now(self, name: str) -> dict:
        task = self.storage.get_scheduled_task(name)
        if not task:
            raise ValueError(f"未找到定时任务: {name}")
        if not self.storage.mark_scheduled_task_running(str(task["name"])):
            run = self.runner.skipped_run(task, "上次运行未结束，本轮跳过")
            self.storage.insert_scheduled_task_run(run)
            return run
        else:
            try:
                run = self.runner.run_task(task)
            finally:
                self.storage.mark_scheduled_task_running(str(task["name"]), running=False)
        self.storage.insert_scheduled_task_run(run)
        self.storage.update_scheduled_task_last_status(
            str(task["name"]),
            last_status=str(run["status"]),
            last_run_at=str(run["finished_at"]),
        )
        return run


def compute_next_run(
    now: datetime | None = None,
    *,
    schedule_kind: str,
    days: list[str] | tuple[str, ...] | None,
    time_of_day: str,
) -> str:
    current = _ensure_tz(now or _now())
    hour, minute = _parse_time(time_of_day)
    schedule_kind = str(schedule_kind or "").strip().lower()
    if schedule_kind == "daily":
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
        return _format_dt(candidate)
    if schedule_kind == "weekly":
        wanted = [WEEKDAYS[item] for item in parse_schedule_days(days or [])]
        if not wanted:
            raise ValueError("weekly schedule requires --days")
        for offset in range(0, 8):
            candidate_date = current + timedelta(days=offset)
            if candidate_date.weekday() not in wanted:
                continue
            candidate = candidate_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > current:
                return _format_dt(candidate)
        raise ValueError("unable to compute next weekly run")
    if schedule_kind == "trading_day":
        for offset in range(0, 8):
            candidate_date = current + timedelta(days=offset)
            if candidate_date.weekday() >= 5:
                continue
            candidate = candidate_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > current:
                return _format_dt(candidate)
        raise ValueError("unable to compute next trading-day run")
    raise ValueError("schedule kind must be daily, weekly or trading_day")


def parse_schedule_days(value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_items = [item.strip().lower() for item in value.split(",") if item.strip()]
    else:
        raw_items = [str(item).strip().lower() for item in value if str(item).strip()]
    days = []
    for item in raw_items:
        if item not in WEEKDAYS:
            raise ValueError(f"unsupported weekday: {item}")
        label = WEEKDAY_LABELS[WEEKDAYS[item]]
        if label not in days:
            days.append(label)
    return tuple(days)


def validate_time_of_day(value: str) -> str:
    hour, minute = _parse_time(value)
    return f"{hour:02d}:{minute:02d}"


def format_task_schedule(task: dict) -> str:
    kind = str(task.get("schedule_kind") or "")
    time_of_day = str(task.get("time_of_day") or "")
    if kind == "daily":
        return f"daily {time_of_day}"
    if kind == "trading_day":
        return f"trading-day {time_of_day}"
    days = ",".join(task.get("days") or [])
    return f"weekly {days} {time_of_day}".strip()


def _validate_cli_argv(argv: list[str]) -> None:
    if not argv:
        raise ValueError("cli task text is required")
    command = argv[0]
    if command.startswith("-"):
        raise ValueError("cli task must start with a SATS command")
    if command in FORBIDDEN_TOP_LEVEL_COMMANDS:
        raise ValueError(f"scheduled cli task cannot run '{command}'")
    if len(argv) >= 2 and (argv[0], argv[1]) in FORBIDDEN_LONG_RUNNING:
        raise ValueError(f"scheduled cli task cannot run '{argv[0]} {argv[1]}'")
    if len(argv) >= 3 and argv[:3] == ["portfolio", "orders", "approve"]:
        raise ValueError("scheduled cli task cannot approve live portfolio intents")


class _ScheduledTaskExecutionError(Exception):
    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


def _extract_report_path(output: str) -> str:
    for line in str(output or "").splitlines():
        if line.strip().startswith("报告:"):
            return line.split(":", 1)[1].strip()
    return ""


def _is_trading_day(trade_date: str, *, settings=None) -> bool:
    if settings is not None:
        try:
            from sats.data.astock_provider import AStockDataProvider
            from sats.storage.duckdb import DuckDBStorage

            provider = AStockDataProvider(settings)
            payload = provider.fetch_data_operation(
                "tushare.dataset.fetch",
                {
                    "dataset": "trade_cal",
                    "params": {
                        "exchange": "SSE",
                        "start_date": trade_date,
                        "end_date": trade_date,
                    },
                },
                limit=10,
                storage=DuckDBStorage(settings.db_path),
            )
            rows = payload.get("data") or payload.get("rows") or []
            if rows:
                return any(
                    str(row.get("cal_date") or "") == trade_date
                    and int(row.get("is_open") or 0) == 1
                    for row in rows
                )
        except Exception:
            pass
    return datetime.strptime(trade_date, "%Y%m%d").weekday() < 5


def _join_output(stdout: str, stderr: str) -> str:
    parts = [part.rstrip() for part in (stdout, stderr) if part and part.rstrip()]
    return "\n".join(parts)


def _parse_time(value: str) -> tuple[int, int]:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("--time must be HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("--time must be HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("--time must be HH:MM")
    return hour, minute


def _now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def _ensure_tz(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def _format_dt(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _ensure_tz(value).strftime("%Y-%m-%d %H:%M:%S")
