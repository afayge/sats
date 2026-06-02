from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class DependencySpec:
    package: str
    import_name: str


@dataclass
class OptionalDependencyStatus:
    group: str
    available: bool
    present: list[str]
    missing: list[str]
    packages: list[str]
    python_executable: str
    project_root: str
    in_project_venv: bool
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))
    installed: list[str] = field(default_factory=list)
    files_updated: list[str] = field(default_factory=list)
    pip_returncode: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "group": self.group,
            "available": self.available,
            "present": list(self.present),
            "missing": list(self.missing),
            "packages": list(self.packages),
            "python_executable": self.python_executable,
            "project_root": self.project_root,
            "in_project_venv": self.in_project_venv,
            "checked_at": self.checked_at,
            "installed": list(self.installed),
            "files_updated": list(self.files_updated),
            "pip_returncode": self.pip_returncode,
            "error": self.error,
        }


class OptionalDependencyError(RuntimeError):
    def __init__(self, message: str, *, status: OptionalDependencyStatus | None = None) -> None:
        super().__init__(message)
        self.status = status


OPTIONAL_DEPENDENCY_GROUPS: dict[str, list[DependencySpec]] = {
    "qlib_ml": [
        DependencySpec("pyqlib", "qlib"),
        DependencySpec("lightgbm", "lightgbm"),
        DependencySpec("xgboost", "xgboost"),
        DependencySpec("scikit-learn", "sklearn"),
    ],
    "deep": [
        DependencySpec("torch", "torch"),
    ],
}

OPTIONAL_DEPENDENCY_EXTRAS: dict[str, list[str]] = {
    "ml": ["pyqlib", "lightgbm", "xgboost", "scikit-learn"],
    "deep": ["torch"],
}


def check_optional_dependencies(group: str, *, project_root: Path | None = None) -> OptionalDependencyStatus:
    specs = _dependency_specs(group)
    root = _project_root(project_root)
    present: list[str] = []
    missing: list[str] = []
    for spec in specs:
        if importlib.util.find_spec(spec.import_name) is None:
            missing.append(spec.package)
        else:
            present.append(spec.package)
    return OptionalDependencyStatus(
        group=group,
        available=not missing,
        present=present,
        missing=missing,
        packages=[spec.package for spec in specs],
        python_executable=sys.executable,
        project_root=str(root),
        in_project_venv=_is_project_venv_python(sys.executable, root),
    )


def ensure_optional_dependencies(
    group: str,
    *,
    auto_install: bool = True,
    project_root: Path | None = None,
) -> OptionalDependencyStatus:
    root = _project_root(project_root)
    status = check_optional_dependencies(group, project_root=root)
    if status.available:
        files_updated = sync_optional_dependency_files(root, groups=("qlib_ml", "deep"))
        status.files_updated = files_updated
        return status
    if not auto_install:
        return status
    if not status.in_project_venv:
        status.error = (
            "Optional dependency auto-install is only allowed when the current Python "
            "is inside the project .venv. Activate .venv and rerun the command."
        )
        raise OptionalDependencyError(status.error, status=status)

    completed = subprocess.run(
        [sys.executable, "-m", "pip", "install", *status.missing],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        status.pip_returncode = completed.returncode
        status.error = _pip_error_message(completed)
        raise OptionalDependencyError(status.error, status=status)

    final_status = check_optional_dependencies(group, project_root=root)
    final_status.installed = list(status.missing)
    final_status.pip_returncode = completed.returncode
    if not final_status.available:
        final_status.error = "pip install completed but imports are still missing: " + ", ".join(final_status.missing)
        raise OptionalDependencyError(final_status.error, status=final_status)
    final_status.files_updated = sync_optional_dependency_files(root, groups=("qlib_ml", "deep"))
    return final_status


def sync_optional_dependency_files(project_root: Path, *, groups: Iterable[str]) -> list[str]:
    root = _project_root(project_root)
    packages: list[str] = []
    for group in groups:
        if group == "deep":
            continue
        packages.extend(spec.package for spec in _dependency_specs(group))
    updated: list[str] = []
    if _sync_requirements(root / "requirements.txt", packages):
        updated.append("requirements.txt")
    if _sync_pyproject_optional_dependencies(root / "pyproject.toml"):
        updated.append("pyproject.toml")
    return updated


def _dependency_specs(group: str) -> list[DependencySpec]:
    try:
        return OPTIONAL_DEPENDENCY_GROUPS[group]
    except KeyError as exc:
        known = ", ".join(sorted(OPTIONAL_DEPENDENCY_GROUPS))
        raise ValueError(f"unknown optional dependency group: {group}; known groups: {known}") from exc


def _project_root(project_root: Path | None) -> Path:
    if project_root is not None:
        return Path(project_root).expanduser().absolute()
    return Path(__file__).resolve().parents[1]


def _is_project_venv_python(executable: str, project_root: Path) -> bool:
    venv_dir = Path(project_root).expanduser().absolute() / ".venv"
    paths = [Path(executable).expanduser(), Path(sys.prefix).expanduser()]
    for raw_path in paths:
        path = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
        path = path.absolute()
        if path == venv_dir or venv_dir in path.parents:
            return True
    return False


def _pip_error_message(completed: subprocess.CompletedProcess[str]) -> str:
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    detail = stderr or stdout or f"pip exited with code {completed.returncode}"
    lines = detail.splitlines()
    return "\n".join(lines[-12:])


def _sync_requirements(path: Path, packages: Iterable[str]) -> bool:
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    existing_names = {_normalize_requirement_name(line) for line in existing_text.splitlines()}
    existing_names.discard("")
    missing = [package for package in packages if _normalize_package_name(package) not in existing_names]
    if not missing:
        return False
    lines = existing_text.splitlines()
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# Optional factor ML/Qlib engine:")
    lines.extend(missing)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return True


def _sync_pyproject_optional_dependencies(path: Path) -> bool:
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text = _set_optional_dependency_section(existing_text)
    if new_text == existing_text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def _set_optional_dependency_section(text: str) -> str:
    lines = text.splitlines()
    section = "[project.optional-dependencies]"
    entries = {
        key: f'{key} = [{", ".join(f'"{value}"' for value in values)}]'
        for key, values in OPTIONAL_DEPENDENCY_EXTRAS.items()
    }
    if section not in [line.strip() for line in lines]:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(section)
        lines.extend(entries.values())
        return "\n".join(lines).rstrip() + "\n"

    start = next(index for index, line in enumerate(lines) if line.strip() == section)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    section_lines = lines[start + 1 : end]
    for key, entry in entries.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for offset, line in enumerate(section_lines):
            if pattern.match(line):
                section_lines[offset] = entry
                break
        else:
            section_lines.append(entry)
    new_lines = [*lines[: start + 1], *section_lines, *lines[end:]]
    return "\n".join(new_lines).rstrip() + "\n"


def _normalize_requirement_name(line: str) -> str:
    stripped = line.split("#", 1)[0].strip()
    if not stripped:
        return ""
    match = re.match(r"([A-Za-z0-9_.-]+)", stripped)
    if not match:
        return ""
    return _normalize_package_name(match.group(1))


def _normalize_package_name(name: str) -> str:
    return name.replace("_", "-").lower()
