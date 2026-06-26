from __future__ import annotations

import os


def is_runtime_process_alive(pid: object) -> bool:
    if pid in (None, ""):
        return False
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def normalized_runtime_status(row: dict) -> str:
    raw_status = str(row.get("status") or "stopped").strip().lower()
    if raw_status != "running":
        return raw_status or "stopped"
    return "running" if is_runtime_process_alive(row.get("pid")) else "stale"


def runtime_is_running(row: dict) -> bool:
    return normalized_runtime_status(row) == "running"
