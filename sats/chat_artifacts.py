from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    kind: str
    title: str
    path: Path
    mime_type: str
    summary: str = ""
    meta: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "path": str(self.path),
            "mime_type": self.mime_type,
            "summary": self.summary,
            "meta": dict(self.meta or {}),
        }


def chat_report_dir(project_root: Path | str, session_id: str, turn_id: str) -> Path:
    return Path(project_root).resolve() / "reports" / "chat" / _safe_part(session_id) / _safe_part(turn_id)


def chat_artifact_dir(project_root: Path | str, session_id: str, turn_id: str) -> Path:
    return Path(project_root).resolve() / "artifacts" / "chat" / _safe_part(session_id) / _safe_part(turn_id)


def save_markdown_artifact(
    *,
    project_root: Path | str,
    session_id: str,
    turn_id: str,
    title: str,
    content: str,
    filename: str | None = None,
    summary: str = "",
    report: bool = True,
    meta: Mapping[str, Any] | None = None,
) -> ArtifactWriteResult:
    base = chat_report_dir(project_root, session_id, turn_id) if report else chat_artifact_dir(project_root, session_id, turn_id)
    path = _safe_target(base, filename or f"{_safe_part(title) or 'report'}.md", suffix=".md")
    body = str(content or "").strip() or "(空报告)"
    if not body.startswith("#"):
        body = f"# {title or 'SATS 研究报告'}\n\n{body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    return ArtifactWriteResult(
        kind="markdown_report" if report else "markdown",
        title=title or path.stem,
        path=path,
        mime_type="text/markdown",
        summary=summary or _summary(body),
        meta=dict(meta or {}),
    )


def save_json_artifact(
    *,
    project_root: Path | str,
    session_id: str,
    turn_id: str,
    title: str,
    payload: Mapping[str, Any],
    filename: str | None = None,
    summary: str = "",
    meta: Mapping[str, Any] | None = None,
) -> ArtifactWriteResult:
    base = chat_artifact_dir(project_root, session_id, turn_id)
    path = _safe_target(base, filename or f"{_safe_part(title) or 'artifact'}.json", suffix=".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload or {}), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return ArtifactWriteResult(
        kind="json",
        title=title or path.stem,
        path=path,
        mime_type="application/json",
        summary=summary or _summary(json.dumps(dict(payload or {}), ensure_ascii=False, default=str)),
        meta=dict(meta or {}),
    )


def save_csv_artifact(
    *,
    project_root: Path | str,
    session_id: str,
    turn_id: str,
    title: str,
    rows: Iterable[Mapping[str, Any]],
    filename: str | None = None,
    summary: str = "",
    meta: Mapping[str, Any] | None = None,
) -> ArtifactWriteResult:
    base = chat_artifact_dir(project_root, session_id, turn_id)
    path = _safe_target(base, filename or f"{_safe_part(title) or 'artifact'}.csv", suffix=".csv")
    row_list = [dict(row) for row in rows]
    fields: list[str] = []
    for row in row_list:
        for key in row:
            if key not in fields:
                fields.append(str(key))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(row_list)
    return ArtifactWriteResult(
        kind="csv",
        title=title or path.stem,
        path=path,
        mime_type="text/csv",
        summary=summary or f"{len(row_list)} rows",
        meta=dict(meta or {}),
    )


def validate_chat_artifact_path(project_root: Path | str, path: Path | str) -> Path:
    root = Path(project_root).resolve()
    target = Path(path).resolve()
    allowed_roots = [root / "reports" / "chat", root / "artifacts" / "chat"]
    if not any(_is_relative_to(target, allowed) for allowed in allowed_roots):
        raise ValueError("chat artifact path must stay under reports/chat or artifacts/chat")
    return target


def _safe_target(base: Path, filename: str, *, suffix: str) -> Path:
    name = _safe_filename(filename)
    if not name.endswith(suffix):
        name = f"{name}{suffix}"
    target = (base / name).resolve()
    if not _is_relative_to(target, base.resolve()):
        raise ValueError("artifact path escaped chat artifact directory")
    return target


def _safe_filename(value: str) -> str:
    text = Path(str(value or "artifact")).name.strip()
    if not text:
        text = "artifact"
    stem = text
    suffix = ""
    if "." in text:
        path = Path(text)
        stem = path.stem
        suffix = path.suffix
    stem = _safe_part(stem) or "artifact"
    return f"{stem}{suffix}"


def _safe_part(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", text)
    text = text.strip("._-")
    return text[:80]


def _summary(content: str) -> str:
    text = " ".join(str(content or "").split())
    return text[:160]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
