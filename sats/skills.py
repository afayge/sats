from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Skill:
    id: str
    name: str
    description: str
    triggers: tuple[str, ...]
    content: str
    path: Path
    category: str = "other"
    source: str = "local"
    requires_tools: tuple[str, ...] = ()
    applies_to: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    auto_load: str = "summary"
    priority: int = 0
    aliases: tuple[str, ...] = ()


_DEFAULT_MATCH_TERMS = {
    "tickflow": (
        "TickFlow",
        "实时行情",
        "分钟K",
        "分钟 K",
        "K线",
        "K 线",
        "quote",
        "quotes",
        "日内分时",
        "五档",
        "除权因子",
    ),
    "tushare-data": (
        "Tushare",
        "财报",
        "财务",
        "估值",
        "资金流",
        "北向资金",
        "板块",
        "宏观",
        "公告",
        "新闻",
        "龙虎榜",
        "ROE",
        "PE",
        "PB",
    ),
}


def default_skills_dir(project_root: Path) -> Path:
    return project_root / "skills"


def load_skills(skills_dir: Path) -> list[Skill]:
    if not skills_dir.exists():
        return []
    skills: list[Skill] = []
    for child in sorted(skills_dir.iterdir(), key=lambda item: item.name):
        skill_file = child / "SKILL.md"
        if not child.is_dir() or not skill_file.exists():
            continue
        skills.append(parse_skill_file(skill_file, skill_id=child.name))
    return skills


def parse_skill_file(path: Path, *, skill_id: str | None = None) -> Skill:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    identifier = skill_id or path.parent.name
    name = identifier
    description = ""
    triggers: tuple[str, ...] = ()
    category = "other"
    source = "local"
    requires_tools: tuple[str, ...] = ()
    applies_to: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    auto_load = "summary"
    priority = 0
    aliases: tuple[str, ...] = ()
    index = 0

    if lines and lines[0].strip() == "---":
        metadata, index = _parse_front_matter(lines)
        name = metadata.get("name") or identifier
        description = metadata.get("description", "")
        triggers = _parse_triggers(metadata.get("triggers", ""))
        category = metadata.get("category", "other") or "other"
        source = metadata.get("source", "local") or "local"
        requires_tools = _parse_triggers(metadata.get("requires_tools", ""))
        applies_to = _parse_triggers(metadata.get("applies_to", ""))
        evidence = _parse_triggers(metadata.get("evidence", ""))
        auto_load = _parse_auto_load(metadata.get("auto_load", "summary"))
        priority = _parse_int(metadata.get("priority", ""), default=0)
        aliases = _parse_triggers(metadata.get("aliases", ""))
    elif lines and lines[0].strip().startswith("#"):
        name = lines[0].strip().lstrip("#").strip() or identifier
        index = 1
        (
            index,
            description,
            triggers,
            category,
            source,
            requires_tools,
            applies_to,
            evidence,
            auto_load,
            priority,
            aliases,
        ) = _parse_legacy_metadata(lines, index)

    content = "\n".join(lines[index:]).strip() or text.strip()
    return Skill(
        id=identifier,
        name=name,
        description=description,
        triggers=triggers,
        content=content,
        path=path,
        category=category,
        source=source,
        requires_tools=requires_tools,
        applies_to=applies_to,
        evidence=evidence,
        auto_load=auto_load,
        priority=priority,
        aliases=aliases,
    )


def match_skills(message: str, skills: list[Skill], *, limit: int = 3) -> list[Skill]:
    text = _normalize(message)
    scored: list[tuple[int, Skill]] = []
    for skill in skills:
        score = 0
        for trigger in skill.triggers:
            normalized_trigger = _normalize(trigger)
            if normalized_trigger and normalized_trigger in text:
                score += 100 + len(normalized_trigger)
        normalized_name = _normalize(skill.name)
        normalized_id = _normalize(skill.id)
        if (len(normalized_name) >= 2 and normalized_name in text) or (
            len(normalized_id) >= 2 and normalized_id in text
        ):
            score += 50
        for alias in skill.aliases:
            normalized_alias = _normalize(alias)
            if len(normalized_alias) >= 2 and normalized_alias in text:
                score += 50 + len(normalized_alias)
        for token in _skill_match_terms(skill):
            if token in text or text in token:
                score += 5
        if score > 0:
            scored.append((score, skill))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    return [skill for _, skill in scored[:limit]]


def format_skill_list(skills: list[Skill]) -> str:
    if not skills:
        return "无可用 skill"
    lines = []
    index = 1
    current_category = None
    for skill in sorted(skills, key=lambda item: (_category_order(item.category), item.category, item.id)):
        if skill.category != current_category:
            if lines:
                lines.append("")
            current_category = skill.category
            lines.append(f"[{current_category or 'other'}]")
        description = f" - {skill.description}" if skill.description else ""
        triggers = f" 触发: {', '.join(skill.triggers)}" if skill.triggers else " 触发: 无"
        requires = f" 工具: {', '.join(skill.requires_tools)}" if skill.requires_tools else ""
        lines.append(f"{index}. {skill.name}{description}{triggers}")
        if requires:
            lines[-1] += requires
        index += 1
    return "\n".join(lines)


def skill_summaries(skills: list[Skill]) -> str:
    if not skills:
        return "无可用 skill"
    lines = []
    for skill in sorted(skills, key=lambda item: (_category_order(item.category), item.category, item.id)):
        source = f" source={skill.source}" if skill.source else ""
        tools = f" tools={','.join(skill.requires_tools)}" if skill.requires_tools else ""
        lines.append(f"- {skill.name} [{skill.category}]{source}{tools}: {skill.description}")
    return "\n".join(lines)


def find_skill(skills: list[Skill], name: str) -> Skill | None:
    key = _normalize(name)
    for skill in skills:
        if _normalize(skill.id) == key or _normalize(skill.name) == key:
            return skill
    return None


def _normalize(value: str) -> str:
    return str(value or "").strip().lower()


def _parse_front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    metadata: dict[str, str] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped == "---":
            return metadata, index + 1
        if line and not line[0].isspace() and not stripped.startswith("-") and ":" in stripped:
            key, value = stripped.split(":", 1)
            metadata[key.strip().lower()] = _strip_yaml_scalar(value.strip())
        index += 1
    return metadata, index


def _parse_legacy_metadata(
    lines: list[str], index: int
) -> tuple[
    int,
    str,
    tuple[str, ...],
    str,
    str,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str,
    int,
    tuple[str, ...],
]:
    description = ""
    triggers: tuple[str, ...] = ()
    category = "other"
    source = "local"
    requires_tools: tuple[str, ...] = ()
    applies_to: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    auto_load = "summary"
    priority = 0
    aliases: tuple[str, ...] = ()
    content_start = index
    while content_start < len(lines):
        stripped = lines[content_start].strip()
        if not stripped:
            content_start += 1
            break
        if ":" not in stripped:
            break
        key, value = stripped.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "description":
            description = value
        elif key == "triggers":
            triggers = _parse_triggers(value)
        elif key == "category":
            category = _strip_yaml_scalar(value) or "other"
        elif key == "source":
            source = _strip_yaml_scalar(value) or "local"
        elif key == "requires_tools":
            requires_tools = _parse_triggers(value)
        elif key == "applies_to":
            applies_to = _parse_triggers(value)
        elif key == "evidence":
            evidence = _parse_triggers(value)
        elif key == "auto_load":
            auto_load = _parse_auto_load(value)
        elif key == "priority":
            priority = _parse_int(value, default=0)
        elif key == "aliases":
            aliases = _parse_triggers(value)
        content_start += 1
    return content_start, description, triggers, category, source, requires_tools, applies_to, evidence, auto_load, priority, aliases


def _parse_triggers(value: str) -> tuple[str, ...]:
    cleaned = _strip_yaml_scalar(value).strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    return tuple(_strip_yaml_scalar(item.strip()) for item in re.split(r"[,，]", cleaned) if _strip_yaml_scalar(item.strip()))


def _strip_yaml_scalar(value: str) -> str:
    cleaned = str(value or "").strip()
    if len(cleaned) >= 2 and cleaned[0] in {"'", '"'} and cleaned[-1] == cleaned[0]:
        return cleaned[1:-1].strip()
    return cleaned


def _parse_auto_load(value: str) -> str:
    mode = _strip_yaml_scalar(value).strip().lower()
    return mode if mode in {"summary", "full", "never"} else "summary"


def _parse_int(value: str, *, default: int) -> int:
    try:
        return int(_strip_yaml_scalar(value))
    except (TypeError, ValueError):
        return default


def _category_order(category: str) -> int:
    order = {
        "data-source": 0,
        "strategy": 1,
        "analysis": 2,
        "risk-analysis": 3,
        "workflow": 4,
        "tool": 5,
        "flow": 6,
        "asset-class": 7,
        "other": 99,
    }
    return order.get(str(category or "other"), 90)


def _skill_match_terms(skill: Skill) -> list[str]:
    terms = [*_description_tokens(skill.description)]
    terms.extend(_normalize(term) for term in skill.aliases)
    terms.extend(_normalize(term) for term in _DEFAULT_MATCH_TERMS.get(skill.id, ()))
    terms.extend(_normalize(term) for term in _DEFAULT_MATCH_TERMS.get(skill.name, ()))
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        result.append(term)
    return result


def _description_tokens(description: str) -> list[str]:
    tokens = []
    for token in re.split(r"[\s,，、;；:：.。!！?？\"'“”‘’（）()【】\[\]<>《》/|]+", description):
        normalized = _normalize(token)
        if len(normalized) >= 2:
            tokens.append(normalized)
    return tokens
