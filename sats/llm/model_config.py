from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


LEGACY_LLM_ENV_NAMES = {
    "LANGCHAIN_PROVIDER",
    "LANGCHAIN_MODEL_NAME",
    "LANGCHAIN_LIGHT_PROVIDER",
    "LANGCHAIN_LIGHT_MODEL_NAME",
    "LANGCHAIN_TEMPERATURE",
    "LANGCHAIN_REASONING_EFFORT",
    "TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "LLM_PROVIDER",
    "OPENAI_MODEL",
}


@dataclass(frozen=True, slots=True)
class ModelProfile:
    name: str
    provider: str
    api_key: str
    base_url: str
    model: str
    light_model: str


@dataclass(frozen=True, slots=True)
class ModelSelection:
    profile_name: str
    provider: str
    api_key: str
    base_url: str
    model: str
    has_api_key: bool
    has_base_url: bool


BUILTIN_MODEL_PROFILES: dict[str, ModelProfile] = {
    "DEEPSEEK": ModelProfile(
        name="DEEPSEEK",
        provider="deepseek",
        api_key="",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        light_model="deepseek-chat",
    ),
    "XIAOMIMIMO": ModelProfile(
        name="XIAOMIMIMO",
        provider="mimo",
        api_key="",
        base_url="https://api.xiaomimimo.com/v1",
        model="MiMo-72B-A27B",
        light_model="MiMo-72B-A27B",
    ),
}

_PROFILE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)_PROVIDER$")


def discover_model_profiles(env: Mapping[str, str] | None = None) -> dict[str, ModelProfile]:
    values = env or os.environ
    profiles = dict(BUILTIN_MODEL_PROFILES)
    for key, provider in values.items():
        match = _PROFILE_RE.match(str(key))
        if not match:
            continue
        name = match.group(1).upper()
        model = str(values.get(f"{name}_MODEL", "") or "").strip()
        if not model:
            continue
        profiles[name] = ModelProfile(
            name=name,
            provider=str(provider or "").strip().lower(),
            api_key=str(values.get(f"{name}_API_KEY", "") or "").strip(),
            base_url=str(values.get(f"{name}_BASE_URL", "") or "").strip(),
            model=model,
            light_model=str(values.get(f"{name}_LIGHT_MODEL", "") or "").strip(),
        )
    return profiles


def resolve_model_selection(
    *,
    profile: str = "default",
    env: Mapping[str, str] | None = None,
) -> ModelSelection:
    values = env or os.environ
    profiles = discover_model_profiles(values)
    _raise_for_legacy_only_config(values, profiles)
    selected_name = _selected_profile_name(profile=profile, env=values)
    selected = profiles.get(selected_name)
    if selected is None:
        available = ", ".join(sorted(profiles)) or "none"
        raise ValueError(f"模型配置组不存在: {selected_name}。可用配置组: {available}")
    model = selected.light_model or selected.model if profile == "light" else selected.model
    return ModelSelection(
        profile_name=selected.name,
        provider=selected.provider,
        api_key=selected.api_key,
        base_url=selected.base_url,
        model=model,
        has_api_key=bool(selected.api_key),
        has_base_url=bool(selected.base_url),
    )


def current_model_status(env: Mapping[str, str] | None = None) -> dict[str, ModelSelection]:
    values = env or os.environ
    return {
        "main": resolve_model_selection(profile="default", env=values),
        "light": resolve_model_selection(profile="light", env=values),
    }


def update_default_model_selection(env_path: Path, profile_name: str, *, target: str) -> None:
    name = str(profile_name or "").strip().upper()
    if not name:
        raise ValueError("model profile name is required")
    if target not in {"main", "light", "both"}:
        raise ValueError("--target must be main, light or both")
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_values = _parse_env_text(env_text)
    profiles = discover_model_profiles({**os.environ, **env_values})
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "none"
        raise ValueError(f"模型配置组不存在: {name}。可用配置组: {available}")
    updates: dict[str, str] = {}
    if target in {"main", "both"}:
        updates["DEFAULT_MODEL"] = name
    if target in {"light", "both"}:
        updates["DEFAULT_LIGHT_MODEL"] = name
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_update_env_text(env_text, updates), encoding="utf-8")


def _selected_profile_name(*, profile: str, env: Mapping[str, str]) -> str:
    default_name = str(env.get("DEFAULT_MODEL", "") or "DEEPSEEK").strip().upper()
    if profile == "light":
        return str(env.get("DEFAULT_LIGHT_MODEL", "") or default_name).strip().upper()
    return default_name


def _raise_for_legacy_only_config(env: Mapping[str, str], profiles: Mapping[str, ModelProfile]) -> None:
    has_explicit_profile = any(
        _PROFILE_RE.match(str(key)) and str(env.get(f"{_PROFILE_RE.match(str(key)).group(1)}_MODEL", "") or "").strip()
        for key in env
    )
    if has_explicit_profile or "DEFAULT_MODEL" in env or "DEFAULT_LIGHT_MODEL" in env:
        return
    if any(str(env.get(name, "") or "").strip() for name in LEGACY_LLM_ENV_NAMES):
        raise ValueError(
            "旧 LLM 配置已移除，请迁移到 <PROFILE>_PROVIDER/<PROFILE>_MODEL 和 DEFAULT_MODEL/DEFAULT_LIGHT_MODEL。"
        )


def _parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _update_env_text(text: str, updates: Mapping[str, str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        stripped = raw_line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            lines.append(raw_line)
    missing = [(key, value) for key, value in updates.items() if key not in seen]
    if missing and lines and lines[-1].strip():
        lines.append("")
    lines.extend(f"{key}={value}" for key, value in missing)
    return "\n".join(lines).rstrip() + "\n"
