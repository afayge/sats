from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from sats.llm import ChatLLM, build_standard_llm, extract_json_object
from sats.memory import ChatMemoryStore


SOURCE_REPAIR_ACTION_TYPE = "source_repair"
_ALLOWED_SOURCE = re.compile(r"^sats/(?:[^/]+/)*[^/]+\.py$")
_ALLOWED_TEST = re.compile(r"^tests/test_[^/]+\.py$")
_DANGEROUS_ADDED = re.compile(
    r"^\+.*(?:\bimport\s+(?:os|subprocess|socket|requests|urllib|shutil|importlib|ctypes|builtins)\b|"
    r"\bfrom\s+(?:os|subprocess|socket|requests|urllib|shutil|importlib|ctypes|builtins)\b|"
    r"\b(?:exec|eval|compile|__import__|os\.system|subprocess\.)\s*\(|"
    r"\bgetattr\s*\([^\n]*(?:exec|eval|compile|__import__))"
)
_CREDENTIAL_ADDED = re.compile(
    r"(?i)^\+.*(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret)\s*[:=]|"
    r"^\+.*\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
)


def propose_source_repair_for_turn(
    turn_id: str,
    *,
    settings: Any,
    store: ChatMemoryStore | None = None,
    llm_factory: Callable[..., Any] | None = ChatLLM,
) -> dict[str, Any]:
    if str(getattr(settings, "self_repair_mode", "propose") or "propose").lower() != "propose":
        raise ValueError("source repair proposals require SATS_SELF_REPAIR_MODE=propose")
    store = store or ChatMemoryStore(settings.db_path)
    failures = store.list_agent_failures(turn_id=str(turn_id or "").strip(), limit=50)
    failure = next(
        (
            item
            for item in failures
            if item.get("category") == "local_code_defect"
            and item.get("repair_level") == "source_proposal"
            and item.get("status") == "exhausted"
            and item.get("frames")
        ),
        None,
    )
    if failure is None:
        raise ValueError(f"turn {turn_id} has no exhausted local source failure eligible for repair")
    existing = store.find_open_repair_by_fingerprint(str(failure.get("fingerprint") or ""))
    if existing is not None:
        return existing
    if llm_factory is None:
        raise ValueError("source repair proposal requires an LLM")

    repair_id = f"repair_{uuid.uuid4().hex[:16]}"
    store.add_agent_repair(
        {
            "repair_id": repair_id,
            "failure_id": failure["failure_id"],
            "turn_id": str(turn_id or ""),
            "status": "running",
        }
    )
    try:
        return _complete_source_repair_proposal(
            repair_id=repair_id,
            turn_id=str(turn_id or ""),
            failure=failure,
            settings=settings,
            store=store,
            llm_factory=llm_factory,
        )
    except Exception as exc:
        store.update_agent_repair(
            repair_id,
            status="failed",
            diagnosis={"error_type": exc.__class__.__name__, "error": str(exc)},
        )
        store.append_agent_event(
            str(turn_id or ""),
            "repair_failed",
            status="error",
            content=str(exc),
            payload={"repair_id": repair_id, "error_type": exc.__class__.__name__, "error": str(exc)},
            item_name=repair_id,
        )
        raise


def _complete_source_repair_proposal(
    *,
    repair_id: str,
    turn_id: str,
    failure: dict[str, Any],
    settings: Any,
    store: ChatMemoryStore,
    llm_factory: Callable[..., Any],
) -> dict[str, Any]:
    root = Path(getattr(settings, "project_root", ".")).resolve()
    contexts = _bounded_source_context(root, failure)
    diagnosis = _request_patch(failure=failure, contexts=contexts, settings=settings, llm_factory=llm_factory)
    patch = str(diagnosis.get("diff") or diagnosis.get("patch") or "").strip()
    changed_paths = validate_source_patch(patch)
    tests = _target_tests(root, diagnosis.get("tests"), changed_paths)
    target_hashes = {path: _file_hash(root / path) for path in changed_paths}
    repair_dir = root / "reports" / "repairs"
    repair_dir.mkdir(parents=True, exist_ok=True)
    patch_path = repair_dir / f"{repair_id}.patch"
    diagnosis_path = repair_dir / f"{repair_id}.json"
    _validate_in_shadow(root, patch, changed_paths, tests, timeout=int(getattr(settings, "self_repair_test_timeout_seconds", 300) or 300))
    patch_path.write_text(patch.rstrip() + "\n", encoding="utf-8")
    diagnostic_payload = {
        "repair_id": repair_id,
        "failure": failure,
        "root_cause": str(diagnosis.get("root_cause") or diagnosis.get("cause") or ""),
        "risk": str(diagnosis.get("risk") or ""),
        "changed_paths": changed_paths,
        "tests": tests,
        "target_hashes": target_hashes,
        "patch_path": str(patch_path),
    }
    diagnosis_path.write_text(json.dumps(diagnostic_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    store.update_agent_repair(
        repair_id,
        status="proposed",
        diagnosis=diagnostic_payload,
        patch_path=str(patch_path),
        target_hashes=target_hashes,
        tests=tests,
    )
    action_id = store.create_pending_action(
        session_id=str(failure.get("session_id") or "repair"),
        turn_id=turn_id,
        action_type=SOURCE_REPAIR_ACTION_TYPE,
        title=f"SATS source repair {repair_id}",
        payload={
            "repair_id": repair_id,
            "failure_id": failure["failure_id"],
            "patch_path": str(patch_path),
            "diagnosis_path": str(diagnosis_path),
            "changed_paths": changed_paths,
            "target_hashes": target_hashes,
            "tests": tests,
        },
    )
    store.update_agent_repair(repair_id, status="pending", pending_action_id=action_id)
    result = store.get_agent_repair(repair_id) or {"repair_id": repair_id, "pending_action_id": action_id}
    store.append_agent_event(
        turn_id,
        "repair_proposed",
        payload={"repair_id": repair_id, "pending_action_id": action_id, "patch_path": str(patch_path)},
        item_name=repair_id,
    )
    return result


def confirm_source_repair(action_id: str, *, settings: Any, store: ChatMemoryStore | None = None) -> dict[str, Any]:
    store = store or ChatMemoryStore(settings.db_path)
    action = store.get_pending_action(action_id)
    if action is None:
        raise ValueError(f"未找到待确认动作 {action_id}")
    if action.get("action_type") != SOURCE_REPAIR_ACTION_TYPE:
        raise ValueError(f"not a source repair action: {action.get('action_type')}")
    if action.get("status") != "pending":
        raise ValueError(f"动作 {action_id} 当前状态为 {action.get('status')}，不能确认执行")
    payload = dict(action.get("payload") or {})
    root = Path(getattr(settings, "project_root", ".")).resolve()
    repair_id = str(payload.get("repair_id") or "")
    patch_path = Path(str(payload.get("patch_path") or ""))
    if not patch_path.is_file() or root not in patch_path.resolve().parents:
        raise ValueError("source repair patch artifact is missing or outside the project")
    patch = patch_path.read_text(encoding="utf-8")
    changed_paths = validate_source_patch(patch)
    hashes = payload.get("target_hashes") if isinstance(payload.get("target_hashes"), dict) else {}
    mismatches = [path for path in changed_paths if str(hashes.get(path) or "") != _file_hash(root / path)]
    if mismatches:
        result = {"status": "hash_mismatch", "files": mismatches}
        store.update_pending_action(action_id, status="error", result=result)
        store.update_agent_repair(repair_id, status="failed")
        store.append_agent_event(
            str(action.get("turn_id") or ""),
            "repair_failed",
            status="error",
            content="source repair target hash mismatch",
            payload={"repair_id": repair_id, **result},
            item_name=repair_id,
        )
        raise ValueError(f"目标文件已变化，停止应用补丁: {', '.join(mismatches)}")
    tests = [str(item) for item in payload.get("tests", []) if str(item).strip()]
    _run(["git", "apply", "--check", str(patch_path)], cwd=root, timeout=60)
    _run(["git", "apply", str(patch_path)], cwd=root, timeout=60)
    try:
        _validate_applied_tree(
            root,
            changed_paths,
            tests,
            timeout=int(getattr(settings, "self_repair_test_timeout_seconds", 300) or 300),
        )
    except Exception as exc:
        try:
            _run(["git", "apply", "-R", str(patch_path)], cwd=root, timeout=60)
        except Exception as rollback_exc:
            result = {"status": "rollback_failed", "error": str(exc), "rollback_error": str(rollback_exc)}
            store.update_pending_action(action_id, status="error", result=result)
            store.update_agent_repair(repair_id, status="failed")
            store.append_agent_event(
                str(action.get("turn_id") or ""),
                "repair_failed",
                status="error",
                content="source repair rollback failed",
                payload={"repair_id": repair_id, **result},
                item_name=repair_id,
            )
            raise RuntimeError("source repair validation failed and rollback also failed; manual recovery required") from rollback_exc
        result = {"status": "rolled_back", "error": str(exc)}
        store.update_pending_action(action_id, status="error", result=result)
        store.update_agent_repair(repair_id, status="failed")
        store.append_agent_event(
            str(action.get("turn_id") or ""),
            "repair_failed",
            status="error",
            content="source repair validation failed and patch was rolled back",
            payload={"repair_id": repair_id, **result},
            item_name=repair_id,
        )
        raise RuntimeError(f"source repair validation failed and was rolled back: {exc}") from exc
    result = {"status": "applied", "repair_id": repair_id, "changed_paths": changed_paths, "tests": tests}
    store.update_pending_action(action_id, status="done", result=result)
    store.update_agent_repair(repair_id, status="applied")
    store.append_agent_event(
        str(action.get("turn_id") or ""),
        "repair_applied",
        payload=result,
        item_name=repair_id,
    )
    return result


def reject_source_repair(action_id: str, *, settings: Any, store: ChatMemoryStore | None = None) -> dict[str, Any]:
    store = store or ChatMemoryStore(settings.db_path)
    action = store.get_pending_action(action_id)
    if action is None or action.get("action_type") != SOURCE_REPAIR_ACTION_TYPE:
        raise ValueError(f"not a source repair action: {action_id}")
    if action.get("status") != "pending":
        raise ValueError(f"动作 {action_id} 当前状态为 {action.get('status')}，不能拒绝")
    repair_id = str((action.get("payload") or {}).get("repair_id") or "")
    result = {"status": "rejected", "repair_id": repair_id}
    store.update_pending_action(action_id, status="rejected", result=result)
    if repair_id:
        store.update_agent_repair(repair_id, status="rejected")
    return result


def validate_source_patch(patch: str) -> list[str]:
    text = str(patch or "")
    if not text.strip() or "diff --git " not in text:
        raise ValueError("repair response does not contain a unified git diff")
    forbidden = ("GIT binary patch", "Binary files ", "rename from ", "rename to ", "deleted file mode", "new mode ", "old mode ")
    if any(token in text for token in forbidden):
        raise ValueError("repair patch contains a forbidden binary, rename, delete, or mode change")
    paths: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) != 4:
                raise ValueError("repair patch has an invalid diff header")
            for raw_header_path in parts[2:]:
                header_path = raw_header_path[2:] if raw_header_path.startswith(("a/", "b/")) else raw_header_path
                if not (_ALLOWED_SOURCE.fullmatch(header_path) or _ALLOWED_TEST.fullmatch(header_path)):
                    raise ValueError(f"repair patch path is outside the allowlist: {header_path}")
        if line.startswith("+++ "):
            raw = line[4:].strip()
            if raw == "/dev/null":
                raise ValueError("repair patch may not delete files")
            path = raw[2:] if raw.startswith("b/") else raw
            if not (_ALLOWED_SOURCE.fullmatch(path) or _ALLOWED_TEST.fullmatch(path)):
                raise ValueError(f"repair patch path is outside the allowlist: {path}")
            if path not in paths:
                paths.append(path)
    if not paths or len(paths) > 3:
        raise ValueError("repair patch must change one to three allowed Python files")
    changed_lines = sum(
        1
        for line in text.splitlines()
        if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++", "---"))
    )
    if changed_lines > 300:
        raise ValueError("repair patch exceeds the 300 changed-line limit")
    if any(_DANGEROUS_ADDED.search(line) for line in text.splitlines()):
        raise ValueError("repair patch adds a dangerous import or dynamic execution")
    if any(_CREDENTIAL_ADDED.search(line) for line in text.splitlines()):
        raise ValueError("repair patch adds credential-like material")
    return paths


def _bounded_source_context(root: Path, failure: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for frame in reversed(failure.get("frames") or []):
        if len(seen) >= 3:
            break
        path = str(frame.get("path") or "") if isinstance(frame, dict) else ""
        if path in seen or not _ALLOWED_SOURCE.fullmatch(path):
            continue
        target = root / path
        if not target.is_file():
            continue
        seen.add(path)
        lines = target.read_text(encoding="utf-8").splitlines()
        line = max(1, int(frame.get("line") or 1))
        start = max(0, line - 121)
        end = min(len(lines), line + 120)
        rows.append({"path": path, "focus_line": line, "start_line": start + 1, "content": "\n".join(lines[start:end])})
        test_path = root / "tests" / f"test_{Path(path).stem}.py"
        if test_path.is_file():
            test_lines = test_path.read_text(encoding="utf-8").splitlines()
            rows.append(
                {
                    "path": test_path.relative_to(root).as_posix(),
                    "focus_line": 1,
                    "start_line": 1,
                    "content": "\n".join(test_lines[:240]),
                }
            )
    if not rows:
        raise ValueError("failure has no readable project source frame")
    return rows


def _request_patch(
    *,
    failure: dict[str, Any],
    contexts: list[dict[str, Any]],
    settings: Any,
    llm_factory: Callable[..., Any],
) -> dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": (
                "你是 SATS 的受控源码修复提案器。仅根据给定失败和源码上下文生成最小修复。"
                "严格输出 JSON 对象：root_cause 字符串、diff 完整 unified git diff、tests 字符串数组、risk 字符串。"
                "只能修改 sats/**/*.py 或 tests/test_*.py，最多 3 个文件、300 个增删行；不得删除、重命名、"
                "安装依赖、修改权限、增加网络/进程/动态执行、交易权限或凭据处理。测试使用 unittest 模块名。"
            ),
        },
        {"role": "user", "content": json.dumps({"failure": failure, "sources": contexts}, ensure_ascii=False, default=str)},
    ]
    llm = build_standard_llm(
        llm_factory,
        model_name=str(getattr(settings, "openai_model", "") or ""),
        timeout_seconds=int(getattr(settings, "self_repair_timeout_seconds", 120) or 120),
    )
    try:
        response = llm.chat(messages, timeout=int(getattr(settings, "self_repair_timeout_seconds", 120) or 120))
    except TypeError:
        response = llm.chat(messages)
    parsed = extract_json_object(str(getattr(response, "content", "") or ""))
    if not isinstance(parsed, dict) or not str(parsed.get("root_cause") or "").strip():
        raise ValueError("repair model did not return the required strict JSON diagnosis")
    return parsed


def _target_tests(root: Path, raw: Any, changed_paths: list[str]) -> list[str]:
    tests: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        value = str(item or "").strip().replace("/", ".")
        if value.endswith(".py"):
            value = value[:-3]
        if re.fullmatch(r"tests\.test_[A-Za-z0-9_.]+", value) and value not in tests:
            tests.append(value)
    for path in changed_paths:
        if path.startswith("tests/test_"):
            module = path[:-3].replace("/", ".")
        else:
            name = Path(path).stem
            candidate = root / "tests" / f"test_{name}.py"
            module = f"tests.test_{name}" if candidate.is_file() else ""
        if module and module not in tests:
            tests.append(module)
    if not tests:
        raise ValueError("repair proposal must name at least one existing targeted unittest module")
    for module in tests[:3]:
        if not (root / (module.replace(".", "/") + ".py")).is_file() and module.replace(".", "/") + ".py" not in changed_paths:
            raise ValueError(f"repair test module does not exist: {module}")
    return tests[:3]


def _validate_in_shadow(root: Path, patch: str, changed_paths: list[str], tests: list[str], *, timeout: int) -> None:
    with tempfile.TemporaryDirectory(prefix="sats-repair-") as temp:
        shadow = Path(temp)
        tracked = _run(["git", "ls-files", "-z"], cwd=root, timeout=60, text=False).stdout.split(b"\0")
        for raw in tracked:
            if not raw:
                continue
            relative = raw.decode("utf-8", errors="strict")
            source = root / relative
            if source.is_file() and not source.is_symlink():
                target = shadow / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        patch_path = shadow / "repair.patch"
        patch_path.write_text(patch.rstrip() + "\n", encoding="utf-8")
        _run(["git", "init", "-q"], cwd=shadow, timeout=60)
        _run(["git", "add", "--all"], cwd=shadow, timeout=60)
        _run(["git", "apply", "--check", "--whitespace=error-all", str(patch_path)], cwd=shadow, timeout=60)
        _run(["git", "apply", str(patch_path)], cwd=shadow, timeout=60)
        _validate_applied_tree(shadow, changed_paths, tests, timeout=timeout)


def _validate_applied_tree(root: Path, changed_paths: list[str], tests: list[str], *, timeout: int) -> None:
    for path in changed_paths:
        target = root / path
        if not target.is_file() or target.is_symlink():
            raise ValueError(f"repair target is missing or a symlink: {path}")
        ast.parse(target.read_text(encoding="utf-8"), filename=path)
    _run([sys.executable, "-m", "py_compile", *changed_paths], cwd=root, timeout=min(timeout, 120))
    _run([sys.executable, "-m", "unittest", *tests], cwd=root, timeout=timeout)
    _run(["git", "diff", "--check", "--", *changed_paths], cwd=root, timeout=60)


def _file_hash(path: Path) -> str:
    if not path.exists():
        return "__missing__"
    if not path.is_file() or path.is_symlink():
        return "__invalid__"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(argv: list[str], *, cwd: Path, timeout: int, text: bool = True) -> subprocess.CompletedProcess[Any]:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=text, timeout=max(1, int(timeout)), check=False)
    if result.returncode != 0:
        stdout = result.stdout if isinstance(result.stdout, str) else result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode("utf-8", errors="replace")
        detail = (stderr or stdout or "command failed").strip()[-4000:]
        raise RuntimeError(f"{' '.join(argv[:4])} failed: {detail}")
    return result
