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
        if len(argv) < 2 or argv[0] != "qmt":
            return argv
        action = argv[1]
        if action in {"buy", "sell"}:
            if not self.policy.allows_trade(action):
                return CommandRunResult(tuple(argv), 2, stderr=f"agent auto-trade does not allow {action}", status="error")
            if self.policy.broker != "qmt" or not self.policy.live_trading:
                return argv if "--dry-run" in argv else [*argv, "--dry-run"]
            return argv
        if action == "cancel" and not self.policy.live_trading:
            return CommandRunResult(tuple(argv), 2, stderr="agent qmt cancel requires --live-trading", status="error")
        return argv


def _blocked_agent_recursion(argv: list[str]) -> str:
    if argv[0] == "agent":
        return "agent command cannot recursively run sats agent"
    if argv[0] == "chat":
        return "agent command cannot recursively run sats chat; use chat.answer tool"
    return ""
