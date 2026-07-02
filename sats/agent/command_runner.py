from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Callable

from sats.agent.models import AgentExecutionPolicy


@dataclass(frozen=True, slots=True)
class CommandRunResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    status: str = "done"

    @property
    def output(self) -> str:
        return "\n".join(part for part in (self.stdout.rstrip(), self.stderr.rstrip()) if part)


class AgentCommandRunner:
    def __init__(self, *, policy: AgentExecutionPolicy, cli_main: Callable[[list[str]], int] | None = None) -> None:
        self.policy = policy
        self.cli_main = cli_main

    def run(self, argv: list[str] | tuple[str, ...], *, timeout: int | None = None) -> CommandRunResult:
        clean = [str(item).strip() for item in argv if str(item).strip()]
        if not clean:
            return CommandRunResult(tuple(), 2, stderr="empty SATS command", status="error")
        blocked = _blocked_agent_recursion(clean)
        if blocked:
            return CommandRunResult(tuple(clean), 2, stderr=blocked, status="error")
        report_guarded = _guard_report_writing_command(clean)
        if isinstance(report_guarded, CommandRunResult):
            return report_guarded
        clean = report_guarded
        guarded = self._guard_trading_command(clean)
        if isinstance(guarded, CommandRunResult):
            return guarded
        clean = guarded
        timeout = int(timeout if timeout is not None else self.policy.command_timeout)
        if self.cli_main is not None:
            return self._run_in_process(clean)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "sats", *clean],
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1, timeout),
            )
            return CommandRunResult(
                tuple(clean),
                int(completed.returncode),
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                status="done" if completed.returncode == 0 else "error",
            )
        except subprocess.TimeoutExpired as exc:
            return CommandRunResult(
                tuple(clean),
                124,
                stdout=exc.stdout or "",
                stderr=f"command timed out after {timeout}s",
                status="timeout",
            )

    def _run_in_process(self, argv: list[str]) -> CommandRunResult:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = self.cli_main(argv)
            except SystemExit as exc:
                code = int(exc.code or 0) if isinstance(exc.code, int) else 1
                if exc.code not in (None, 0):
                    print(str(exc.code), file=stderr)
        return CommandRunResult(
            tuple(argv),
            int(code or 0),
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            status="done" if code in (None, 0) else "error",
        )

    def _guard_trading_command(self, argv: list[str]) -> list[str] | CommandRunResult:
        if len(argv) >= 3 and argv[:3] == ["portfolio", "orders", "approve"]:
            return CommandRunResult(
                tuple(argv),
                2,
                stderr="portfolio live intent approval requires direct human CLI/REPL confirmation",
                status="error",
            )
        if len(argv) < 2 or argv[0] != "qmt":
            return argv
        action = argv[1]
        if action in {"buy", "sell"}:
            if not self.policy.allows_trade(action):
                return CommandRunResult(tuple(argv), 2, stderr=f"agent auto-trade does not allow {action}", status="error")
            if bool(getattr(self.policy, "dry_run", False)) or self.policy.broker != "qmt" or not self.policy.live_trading:
                return argv if "--dry-run" in argv else [*argv, "--dry-run"]
            return argv
        if action == "cancel" and not self.policy.live_trading:
            return CommandRunResult(tuple(argv), 2, stderr="agent qmt cancel requires --live-trading", status="error")
        return argv


def _blocked_agent_recursion(argv: list[str]) -> str:
    if argv[0] == "agent":
        return "OBSOLETE: sats agent 已停用，请使用 sats chat ..."
    if argv[0] == "chat":
        return "agent command cannot recursively run sats chat; use chat.answer tool"
    return ""


def _guard_report_writing_command(argv: list[str]) -> list[str] | CommandRunResult:
    command = argv[0]
    if command in {"dsa", "analyze-dsa", "analyze-chan"}:
        return _blocked_report_command(argv, command)
    if command == "portfolio" and _portfolio_report_phase(argv):
        return _blocked_report_command(argv, "portfolio run --phase report")
    if command in {"analyze", "deep-analysis", "serenity-screen", "trading-committee"}:
        return _ensure_noreport(argv)
    if command == "discover":
        return _ensure_discover_noreport(argv)
    if len(argv) >= 2 and command == "factor" and argv[1] in {"analyze", "pick"}:
        return _ensure_noreport(argv)
    return argv


def _blocked_report_command(argv: list[str], command: str) -> CommandRunResult:
    return CommandRunResult(
        tuple(argv),
        2,
        stderr=(
            f"agent command cannot run report-writing CLI path: {command}; "
            "use SATS data tools for evidence and research.write_report only after final synthesis when a file is explicitly requested"
        ),
        status="error",
    )


def _ensure_noreport(argv: list[str]) -> list[str]:
    if "--noreport" in argv:
        return argv
    return [*argv, "--noreport"]


def _ensure_discover_noreport(argv: list[str]) -> list[str]:
    if "--noreport" in argv:
        return argv
    insert_at = _discover_query_start(argv)
    return [*argv[:insert_at], "--noreport", *argv[insert_at:]]


def _discover_query_start(argv: list[str]) -> int:
    value_options = {
        "--trade-date",
        "--signals",
        "--limit",
        "--candidate-limit",
        "--lookback-days",
        "--hot-sector-days",
        "--db",
    }
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in value_options:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in value_options):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    return index


def _portfolio_report_phase(argv: list[str]) -> bool:
    if len(argv) < 2 or argv[1] != "run":
        return False
    phase = "afternoon-buy"
    index = 2
    while index < len(argv):
        token = argv[index]
        if token == "--phase" and index + 1 < len(argv):
            phase = argv[index + 1]
            break
        if token.startswith("--phase="):
            phase = token.split("=", 1)[1]
            break
        index += 1
    return str(phase or "").strip().lower() in {"report", "close"}
