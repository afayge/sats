from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.utils import get_cwidth

from sats.analysis.market_llm_context import DEFAULT_MARKET_INDICES, _INDEX_ALIASES

TITLE_STYLE = "fg:#7dd3fc bold"
SECTION_STYLE = "fg:#93c5fd bold"
CALLOUT_STYLE = "bg:#0f172a #f8fafc bold"
WARNING_CALLOUT_STYLE = "bg:#3f2d16 #fde68a bold"
META_STYLE = "fg:#94a3b8"
BODY_STYLE = ""
POSITIVE_STYLE = "fg:#22c55e"
RISK_STYLE = "fg:#f59e0b"
MUTED_STYLE = "fg:#94a3b8"
TABLE_HEADER_STYLE = "fg:#93c5fd bold"
TABLE_BORDER_STYLE = "fg:#475569"
CODE_STYLE = "bg:#1f2937 #e2e8f0"
CHART_POSITIVE_STYLE = "fg:#22c55e"
CHART_NEUTRAL_STYLE = "fg:#94a3b8"
CHART_RISK_STYLE = "fg:#f59e0b"
SYMBOL_HIGHLIGHT_STYLE = "fg:#1d4ed8"
PERCENT_POSITIVE_HIGHLIGHT_STYLE = "fg:#ef4444"
PERCENT_NEGATIVE_HIGHLIGHT_STYLE = "fg:#22c55e"

SUMMARY_ALIASES = ("结论摘要", "核心结论")
EVIDENCE_ALIASES = ("关键证据", "已获取证据", "今日/近期盘面", "候选排序表", "核心指数表")
CHART_ALIASES = ("文字图表",)
RISK_ALIASES = ("风险与限制", "数据限制或失败项", "限制")
NEXT_ALIASES = ("下一步", "下一步观察")

DATE_PATTERNS = (
    re.compile(r"\d{4}[-/.]\d{2}[-/.]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?"),
    re.compile(r"\d{8}(?:\s+\d{2}:\d{2}(?::\d{2})?)?"),
)
CODE_PATTERN = re.compile(r"\b\d{6}\.(?:SZ|SH|BJ)\b", re.IGNORECASE)
PERCENT_PATTERN = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?%")
NUMBER_PATTERN = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class OutputDecorations:
    title: str = ""
    callout: str = ""
    badges: tuple[str, ...] = ()
    section_titles: tuple[str, ...] = ()
    table_count: int = 0
    has_risk_section: bool = False


@dataclass(frozen=True, slots=True)
class OutputSemanticLexicon:
    symbol_codes: tuple[str, ...] = ()
    symbol_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticToken:
    kind: str
    text: str


def format_meter(value: int, *, total: int = 5, filled: str = "█", empty: str = "░") -> str:
    capped = max(0, min(total, int(value)))
    return f"{filled * capped}{empty * max(0, total - capped)} {capped}/{total}"


def format_sparkline(values: list[int] | tuple[int, ...]) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    numbers = [max(0, int(item)) for item in values]
    if not numbers:
        return "▁▁▁"
    upper = max(numbers) or 1
    chars = []
    for item in numbers:
        index = min(len(blocks) - 1, round((item / upper) * (len(blocks) - 1)))
        chars.append(blocks[index])
    return "".join(chars)


def extract_output_metadata(markdown_text: str) -> OutputDecorations:
    text = str(markdown_text or "").strip()
    lines = text.splitlines()
    title = ""
    callout = ""
    badges: list[str] = []
    section_titles: list[str] = []
    table_count = 0
    has_risk_section = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not title and line.startswith("# "):
            title = line[2:].strip()
            continue
        if not callout and line.startswith("> "):
            callout = line[2:].strip()
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            section_titles.append(heading)
            if heading in RISK_ALIASES:
                has_risk_section = True
            continue
        if line.startswith("|"):
            table_count += 1
        badges.extend(match.group(1).strip() for match in re.finditer(r"`([^`]+)`", line))
    return OutputDecorations(
        title=title,
        callout=callout,
        badges=tuple(badges),
        section_titles=tuple(section_titles),
        table_count=table_count,
        has_risk_section=has_risk_section,
    )


def normalize_natural_markdown(
    markdown_text: str,
    *,
    data_names: tuple[str, ...] = (),
    skill_names: tuple[str, ...] = (),
    artifacts: tuple[dict[str, Any], ...] = (),
    requires_confirmation: bool = False,
    pending_action_id: str | None = None,
) -> str:
    text = str(markdown_text or "").strip()
    content = text or "无响应"
    title = _infer_title(content, data_names=data_names, requires_confirmation=requires_confirmation)
    callout = _derive_callout(content, title=title)
    badges = _badge_tokens(
        data_names=data_names,
        skill_names=skill_names,
        artifacts=artifacts,
        requires_confirmation=requires_confirmation,
        pending_action_id=pending_action_id,
    )
    structured = _looks_structured(content)
    if not structured:
        return _build_wrapped_markdown(
            content,
            title=title,
            callout=callout,
            badges=badges,
            artifacts=artifacts,
            requires_confirmation=requires_confirmation,
            pending_action_id=pending_action_id,
            data_names=data_names,
            skill_names=skill_names,
        )
    return _augment_structured_markdown(
        content,
        title=title,
        callout=callout,
        badges=badges,
        artifacts=artifacts,
        requires_confirmation=requires_confirmation,
        pending_action_id=pending_action_id,
        data_names=data_names,
        skill_names=skill_names,
    )


def build_output_semantic_lexicon(
    markdown_text: str,
    *,
    db_path: Path | str | None = None,
) -> OutputSemanticLexicon:
    text = str(markdown_text or "")
    symbol_codes = {
        str(match.group(0) or "").upper()
        for match in CODE_PATTERN.finditer(text)
        if str(match.group(0) or "").strip()
    }
    symbol_names = set(_matching_index_names(text))
    symbol_names.update(_matching_stock_names(text, db_path=db_path))
    return OutputSemanticLexicon(
        symbol_codes=tuple(sorted(symbol_codes, key=lambda item: (-len(item), item))),
        symbol_names=tuple(sorted(symbol_names, key=lambda item: (-len(item), item))),
    )


def tokenize_semantic_text(text: str, semantic_lexicon: OutputSemanticLexicon | None) -> tuple[SemanticToken, ...]:
    raw = str(text or "")
    if not raw:
        return ()
    codes = tuple(sorted((item for item in (semantic_lexicon.symbol_codes if semantic_lexicon is not None else ()) if item), key=len, reverse=True))
    names = tuple(sorted((item for item in (semantic_lexicon.symbol_names if semantic_lexicon is not None else ()) if item), key=len, reverse=True))
    tokens: list[SemanticToken] = []
    index = 0
    while index < len(raw):
        date_value = _match_date(raw, index)
        if date_value is not None:
            _append_semantic_token(tokens, "date", date_value)
            index += len(date_value)
            continue
        code_value = _match_candidate(raw, index, codes)
        if code_value is not None:
            _append_semantic_token(tokens, "symbol_code", code_value)
            index += len(code_value)
            continue
        name_value = _match_candidate(raw, index, names)
        if name_value is not None:
            _append_semantic_token(tokens, "symbol_name", name_value)
            index += len(name_value)
            continue
        percent_match = PERCENT_PATTERN.match(raw, index)
        if percent_match is not None:
            token_text = str(percent_match.group(0) or "")
            kind = "percent_negative" if token_text.startswith("-") else "percent_positive"
            _append_semantic_token(tokens, kind, token_text)
            index = percent_match.end()
            continue
        number_match = NUMBER_PATTERN.match(raw, index)
        if number_match is not None:
            token_text = str(number_match.group(0) or "")
            _append_semantic_token(tokens, "number", token_text)
            index = number_match.end()
            continue
        _append_semantic_token(tokens, "text", raw[index])
        index += 1
    return tuple(tokens)


def render_natural_output(
    markdown_text: str,
    *,
    channel: str,
    tty: bool,
    width: int,
    semantic_lexicon: OutputSemanticLexicon | None = None,
) -> str | FormattedText:
    text = str(markdown_text or "")
    if not tty:
        return text
    lines = text.splitlines()
    fragments: list[tuple[str, str]] = []
    current_section = ""
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            fragments.append(("", "\n"))
            index += 1
            continue
        if stripped.startswith("|"):
            table_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            fragments.extend(_render_table_block(table_lines, width=width, section=current_section, semantic_lexicon=semantic_lexicon))
            fragments.append(("", "\n"))
            continue
        if stripped.startswith("# "):
            current_section = stripped[2:].strip()
            fragments.extend(_render_text_fragments(stripped[2:].strip(), base_style=TITLE_STYLE, semantic_lexicon=semantic_lexicon))
            fragments.extend([("", "\n\n")])
            index += 1
            continue
        if stripped.startswith("## "):
            current_section = stripped[3:].strip()
            fragments.extend([(SECTION_STYLE, "## ")])
            fragments.extend(_render_text_fragments(current_section, base_style=SECTION_STYLE, semantic_lexicon=semantic_lexicon))
            fragments.extend([("", "\n")])
            index += 1
            continue
        if stripped.startswith("> "):
            style = WARNING_CALLOUT_STYLE if ("待确认动作" in stripped or "风险" in stripped) else CALLOUT_STYLE
            fragments.extend([(style, "> ")])
            fragments.extend(
                _render_text_fragments(
                    stripped[2:].strip(),
                    base_style=style,
                    semantic_lexicon=semantic_lexicon,
                    semantic_enabled=not _should_skip_semantic_highlight(stripped),
                )
            )
            fragments.extend([("", "\n")])
            index += 1
            continue
        if _is_badge_line(stripped):
            fragments.extend(_render_badge_line(stripped))
            fragments.append(("", "\n"))
            index += 1
            continue
        style = _line_style(current_section, stripped)
        fragments.extend(_render_inline_code(stripped, default_style=style, semantic_lexicon=semantic_lexicon))
        fragments.append(("", "\n"))
        index += 1
    return FormattedText(fragments)


def _build_wrapped_markdown(
    content: str,
    *,
    title: str,
    callout: str,
    badges: list[str],
    artifacts: tuple[dict[str, Any], ...],
    requires_confirmation: bool,
    pending_action_id: str | None,
    data_names: tuple[str, ...],
    skill_names: tuple[str, ...],
) -> str:
    bullet_lines = _summary_bullets(content)
    evidence_lines = _evidence_lines(content, data_names=data_names, skill_names=skill_names)
    risk_lines = _risk_lines(data_names=data_names, requires_confirmation=requires_confirmation)
    next_lines = _next_lines(artifacts=artifacts, pending_action_id=pending_action_id)
    lines = [
        f"# {title}",
        "",
        f"> {callout}",
        "",
    ]
    if badges:
        lines.extend([" ".join(badges), ""])
    if pending_action_id:
        lines.extend([_confirmation_callout(pending_action_id), ""])
    lines.extend(["## 结论摘要", ""])
    lines.extend(f"- {item}" for item in bullet_lines)
    lines.extend(["", "## 关键证据", ""])
    lines.extend(evidence_lines)
    lines.extend(["", "## 文字图表", ""])
    lines.extend(_chart_lines(data_names=data_names, skill_names=skill_names, artifacts=artifacts, pending_action_id=pending_action_id))
    lines.extend(["", "## 风险与限制", ""])
    lines.extend(f"- {item}" for item in risk_lines)
    lines.extend(["", "## 下一步", ""])
    lines.extend(f"- {item}" for item in next_lines)
    return "\n".join(lines).strip()


def _augment_structured_markdown(
    content: str,
    *,
    title: str,
    callout: str,
    badges: list[str],
    artifacts: tuple[dict[str, Any], ...],
    requires_confirmation: bool,
    pending_action_id: str | None,
    data_names: tuple[str, ...],
    skill_names: tuple[str, ...],
) -> str:
    lines = content.splitlines()
    output: list[str] = []
    rest = lines[:]
    if rest and rest[0].strip().startswith("# "):
        output.append(rest[0].rstrip())
        rest = rest[1:]
    else:
        output.append(f"# {title}")
    output.append("")
    if not _has_blockquote(content):
        output.append(f"> {callout}")
        output.append("")
    if badges and not _has_badges(content):
        output.append(" ".join(badges))
        output.append("")
    if pending_action_id and pending_action_id not in content:
        output.append(_confirmation_callout(pending_action_id))
        output.append("")
    output.extend(rest)
    augmented = "\n".join(output).strip()
    if not _has_section_body(augmented, SUMMARY_ALIASES):
        augmented = _ensure_section_body(
            augmented,
            SUMMARY_ALIASES,
            "结论摘要",
            [f"- {item}" for item in _summary_bullets(content)],
        )
    if not _has_heading(augmented, CHART_ALIASES):
        augmented += "\n\n## 文字图表\n\n" + "\n".join(
            f"- {item}" for item in _chart_lines(data_names=data_names, skill_names=skill_names, artifacts=artifacts, pending_action_id=pending_action_id)
        )
    if not _has_heading(augmented, RISK_ALIASES):
        augmented += "\n\n## 风险与限制\n\n" + "\n".join(
            f"- {item}" for item in _risk_lines(data_names=data_names, requires_confirmation=requires_confirmation)
        )
    if not _has_section_body(augmented, NEXT_ALIASES):
        augmented = _ensure_section_body(
            augmented,
            NEXT_ALIASES,
            "下一步",
            [f"- {item}" for item in _next_lines(artifacts=artifacts, pending_action_id=pending_action_id)],
        )
    elif artifacts and not any(str(item.get("path") or "").strip() and str(item.get("path") or "").strip() in augmented for item in artifacts):
        artifact_lines = [f"- {item}" for item in _artifact_lines(artifacts)]
        if artifact_lines:
            augmented += "\n" + "\n".join(artifact_lines)
    return augmented.strip()


def _chart_lines(
    *,
    data_names: tuple[str, ...],
    skill_names: tuple[str, ...],
    artifacts: tuple[dict[str, Any], ...],
    pending_action_id: str | None,
) -> list[str]:
    coverage = min(5, max(1, len(data_names)))
    completeness = min(5, max(1, len(skill_names) + len(artifacts) + (1 if coverage >= 2 else 0)))
    risk = 4 if pending_action_id else (2 if data_names else 3)
    density = format_sparkline([len(data_names), len(skill_names), len(artifacts), max(0, 5 - risk), completeness])
    return [
        f"数据覆盖: {format_meter(coverage)}",
        f"风险等级: {format_meter(risk)}",
        f"信息密度: {density}",
    ]


def _risk_lines(*, data_names: tuple[str, ...], requires_confirmation: bool) -> list[str]:
    lines = []
    if data_names:
        lines.append(f"当前输出只基于已注入的数据与证据：{', '.join(data_names)}。")
    else:
        lines.append("当前输出未附带真实行情或结构化证据，若要进一步判断需要继续注入数据。")
    if requires_confirmation:
        lines.append("输出中包含待确认动作，执行前仍需要显式确认。")
    lines.append("以上内容用于研究交流，不构成投资建议。")
    return lines


def _next_lines(*, artifacts: tuple[dict[str, Any], ...], pending_action_id: str | None) -> list[str]:
    lines = []
    if pending_action_id:
        lines.append(f"确认执行: /confirm {pending_action_id}")
        lines.append(f"取消执行: /reject {pending_action_id}")
    else:
        lines.append("继续追问同一主题，或补充股票代码、日期、指标、筛选约束。")
    lines.extend(_artifact_lines(artifacts))
    return lines


def _artifact_lines(artifacts: tuple[dict[str, Any], ...]) -> list[str]:
    lines: list[str] = []
    for artifact in artifacts:
        path = str(artifact.get("path") or "").strip()
        if not path:
            continue
        label = "报告" if "report" in path.lower() or path.lower().endswith(".md") else "产物"
        lines.append(f"{label}: {path}")
    return lines


def _evidence_lines(content: str, *, data_names: tuple[str, ...], skill_names: tuple[str, ...]) -> list[str]:
    remainder = [line for line in _summary_bullets(content) if line]
    lines = []
    if data_names:
        lines.append(f"- 已注入数据: {', '.join(data_names)}")
    if skill_names:
        lines.append(f"- 已匹配方法: {', '.join(skill_names)}")
    for item in remainder[1:3]:
        lines.append(f"- {item}")
    if not lines:
        lines.append("- 当前没有更多结构化证据，建议继续补充真实行情、指标或知识库上下文。")
    return lines


def _summary_bullets(content: str) -> list[str]:
    bullet_lines = []
    for raw in str(content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(">") or line.startswith("|"):
            continue
        if line.startswith("- "):
            bullet_lines.append(line[2:].strip())
            continue
        if re.match(r"\d+\.\s+", line):
            bullet_lines.append(re.sub(r"^\d+\.\s+", "", line))
            continue
        bullet_lines.append(line)
    cleaned = []
    seen: set[str] = set()
    for item in bullet_lines:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(_trim_sentence(normalized))
    return cleaned[:3] or ["已生成结构化研究输出。"]


def _derive_callout(content: str, *, title: str) -> str:
    metadata = extract_output_metadata(content)
    if metadata.callout:
        return metadata.callout
    for item in _summary_bullets(content):
        if item and item != title:
            return item
    return "已生成结构化研究输出。"


def _infer_title(content: str, *, data_names: tuple[str, ...], requires_confirmation: bool) -> str:
    metadata = extract_output_metadata(content)
    if metadata.title:
        return metadata.title
    joined = " ".join(data_names)
    if requires_confirmation:
        return "SATS 待确认动作"
    if "选股Agent" in joined or "热点板块" in joined:
        return "SATS 机会发现"
    if "大盘" in joined:
        return "SATS 大盘研究输出"
    if "个股" in joined or "实时报价" in joined:
        return "SATS 个股研究输出"
    if "缠论" in joined:
        return "SATS 缠论研究输出"
    if "Runtime" in joined:
        return "SATS 执行结果"
    return "SATS 自然对话输出"


def _badge_tokens(
    *,
    data_names: tuple[str, ...],
    skill_names: tuple[str, ...],
    artifacts: tuple[dict[str, Any], ...],
    requires_confirmation: bool,
    pending_action_id: str | None,
) -> list[str]:
    badges = []
    if data_names:
        badges.append(f"`数据: {' · '.join(data_names)}`")
    if skill_names:
        badges.append(f"`skill: {' · '.join(skill_names)}`")
    if artifacts:
        labels = []
        for artifact in artifacts[:3]:
            path = str(artifact.get("path") or "").strip()
            if path:
                labels.append(path)
        if labels:
            badges.append(f"`产物: {' · '.join(labels)}`")
    if requires_confirmation and pending_action_id:
        badges.append(f"`待确认动作: {pending_action_id}`")
    badges.append("`风格: 研究输出`")
    return badges


def _confirmation_callout(action_id: str) -> str:
    return f"> 待确认动作 {action_id}。确认: /confirm {action_id}；取消: /reject {action_id}"


def _looks_structured(content: str) -> bool:
    text = str(content or "")
    heading_count = sum(1 for line in text.splitlines() if line.strip().startswith("## "))
    return text.lstrip().startswith("#") or heading_count >= 2 or "|---" in text


def _has_heading(content: str, aliases: tuple[str, ...]) -> bool:
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("## "):
            continue
        heading = stripped[3:].strip()
        if heading in aliases:
            return True
    return False


def _has_section_body(content: str, aliases: tuple[str, ...]) -> bool:
    lines = str(content or "").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("## "):
            continue
        if stripped[3:].strip() not in aliases:
            continue
        for body in lines[index + 1 :]:
            body_text = body.strip()
            if body_text.startswith("## "):
                break
            if body_text:
                return True
    return False


def _ensure_section_body(content: str, aliases: tuple[str, ...], heading: str, body_lines: list[str]) -> str:
    body = [line for line in body_lines if str(line or "").strip()]
    if not body:
        return content
    lines = str(content or "").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("## ") or stripped[3:].strip() not in aliases:
            continue
        insert_at = index + 1
        while insert_at < len(lines) and not lines[insert_at].strip():
            insert_at += 1
        if insert_at < len(lines) and not lines[insert_at].strip().startswith("## "):
            return content
        replacement = lines[: index + 1] + ["", *body, ""] + lines[insert_at:]
        return "\n".join(replacement).strip()
    suffix = "\n\n" if str(content or "").strip() else ""
    return (str(content or "").strip() + f"{suffix}## {heading}\n\n" + "\n".join(body)).strip()


def _has_blockquote(content: str) -> bool:
    return any(line.strip().startswith("> ") for line in str(content or "").splitlines())


def _has_badges(content: str) -> bool:
    return any("`" in line for line in str(content or "").splitlines())


def _trim_sentence(text: str, *, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _is_badge_line(text: str) -> bool:
    stripped = str(text or "").strip()
    return bool(stripped) and stripped.count("`") >= 2 and stripped.replace("`", "").strip() != stripped


def _render_badge_line(text: str) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    position = 0
    for match in re.finditer(r"`([^`]+)`", text):
        prefix = text[position : match.start()]
        if prefix:
            fragments.append((MUTED_STYLE, prefix))
        fragments.append((CODE_STYLE, f" {match.group(1).strip()} "))
        if match.end() < len(text):
            fragments.append((MUTED_STYLE, " "))
        position = match.end()
    suffix = text[position:]
    if suffix:
        fragments.append((MUTED_STYLE, suffix))
    return fragments


def _render_inline_code(
    text: str,
    *,
    default_style: str,
    semantic_lexicon: OutputSemanticLexicon | None,
) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    position = 0
    semantic_enabled = not _should_skip_semantic_highlight(text)
    for match in re.finditer(r"`([^`]+)`", text):
        prefix = text[position : match.start()]
        if prefix:
            fragments.extend(
                _render_text_fragments(
                    prefix,
                    base_style=default_style,
                    semantic_lexicon=semantic_lexicon,
                    semantic_enabled=semantic_enabled,
                )
            )
        fragments.append((CODE_STYLE, f" {match.group(1).strip()} "))
        position = match.end()
    suffix = text[position:]
    if suffix:
        fragments.extend(
            _render_text_fragments(
                suffix,
                base_style=default_style,
                semantic_lexicon=semantic_lexicon,
                semantic_enabled=semantic_enabled,
            )
        )
    if fragments:
        return fragments
    return _render_text_fragments(text, base_style=default_style, semantic_lexicon=semantic_lexicon, semantic_enabled=semantic_enabled)


def _line_style(section: str, text: str) -> str:
    if any(alias == section for alias in RISK_ALIASES) or "风险" in text or "警告" in text:
        return RISK_STYLE
    if any(alias == section for alias in CHART_ALIASES):
        if "风险" in text:
            return CHART_RISK_STYLE
        if "覆盖" in text or "密度" in text:
            return CHART_POSITIVE_STYLE
        return CHART_NEUTRAL_STYLE
    if text.startswith("- ") and any(word in text for word in ("触发", "改善", "机会", "偏强", "修复")):
        return POSITIVE_STYLE
    return BODY_STYLE


def _render_table_block(
    lines: list[str],
    *,
    width: int,
    section: str,
    semantic_lexicon: OutputSemanticLexicon | None = None,
) -> list[tuple[str, str]]:
    rows = [_parse_table_row(line) for line in lines if line.startswith("|")]
    rows = [row for row in rows if row]
    if len(rows) < 2:
        return [(BODY_STYLE, "\n".join(lines))]
    header = rows[0]
    body = rows[2:] if len(rows) >= 3 and _is_separator_row(rows[1]) else rows[1:]
    if not body:
        return [(BODY_STYLE, "\n".join(lines))]
    if _should_collapse_table(header, body, width):
        return _render_collapsed_table(header, body, section=section, semantic_lexicon=semantic_lexicon)
    return _render_aligned_table(header, body, semantic_lexicon=semantic_lexicon)


def _parse_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(row: list[str]) -> bool:
    return all(set(cell) <= {"-", ":"} for cell in row if cell)


def _should_collapse_table(header: list[str], body: list[list[str]], width: int) -> bool:
    widths = [max(get_cwidth(header[index]), max((get_cwidth(row[index]) for row in body if index < len(row)), default=0)) for index in range(len(header))]
    total = sum(widths) + max(0, len(widths) - 1) * 3
    return width < 72 or total > max(24, width - 4)


def _render_aligned_table(
    header: list[str],
    body: list[list[str]],
    *,
    semantic_lexicon: OutputSemanticLexicon | None,
) -> list[tuple[str, str]]:
    widths = [get_cwidth(item) for item in header]
    for row in body:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], get_cwidth(cell))
    header_line = " | ".join(_pad_display(header[index], widths[index]) for index in range(len(header)))
    separator = "-+-".join("-" * widths[index] for index in range(len(widths)))
    fragments: list[tuple[str, str]] = [
        (TABLE_HEADER_STYLE, header_line),
        ("", "\n"),
        (TABLE_BORDER_STYLE, separator),
        ("", "\n"),
    ]
    for row in body:
        for index, width in enumerate(widths):
            if index > 0:
                fragments.append((BODY_STYLE, " | "))
            value = row[index] if index < len(row) else ""
            padded = _pad_display(value, width)
            fragments.extend(
                _render_text_fragments(
                    padded,
                    base_style=BODY_STYLE,
                    semantic_lexicon=semantic_lexicon,
                    semantic_enabled=not _should_skip_semantic_highlight(value),
                )
            )
        fragments.extend([("", "\n")])
    return fragments


def _render_collapsed_table(
    header: list[str],
    body: list[list[str]],
    *,
    section: str,
    semantic_lexicon: OutputSemanticLexicon | None,
) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    label_style = RISK_STYLE if section in RISK_ALIASES else TABLE_HEADER_STYLE
    for row in body:
        for index, title in enumerate(header):
            value = row[index] if index < len(row) else "数据缺失"
            prefix = "- " if index == 0 else "  "
            fragments.extend([(label_style, f"{prefix}{title}: ")])
            fragments.extend(
                _render_text_fragments(
                    value,
                    base_style=BODY_STYLE,
                    semantic_lexicon=semantic_lexicon,
                    semantic_enabled=not _should_skip_semantic_highlight(value),
                )
            )
            fragments.extend([("", "\n")])
        fragments.append(("", "\n"))
    return fragments


def _pad_display(text: str, width: int) -> str:
    value = str(text or "")
    padding = max(0, width - get_cwidth(value))
    return value + (" " * padding)


def _render_text_fragments(
    text: str,
    *,
    base_style: str,
    semantic_lexicon: OutputSemanticLexicon | None,
    semantic_enabled: bool = True,
) -> list[tuple[str, str]]:
    if not text:
        return []
    if not semantic_enabled or semantic_lexicon is None:
        return [(base_style, text)]
    fragments: list[tuple[str, str]] = []
    for token in tokenize_semantic_text(text, semantic_lexicon):
        fragments.append((_semantic_style(token.kind, base_style), token.text))
    return fragments


def _semantic_style(kind: str, base_style: str) -> str:
    semantic_style = ""
    if kind in {"symbol_code", "symbol_name"}:
        semantic_style = SYMBOL_HIGHLIGHT_STYLE
    elif kind == "percent_positive":
        semantic_style = PERCENT_POSITIVE_HIGHLIGHT_STYLE
    elif kind == "percent_negative":
        semantic_style = PERCENT_NEGATIVE_HIGHLIGHT_STYLE
    if not semantic_style:
        return base_style
    return f"{base_style} {semantic_style}".strip()


def _match_date(text: str, start: int) -> str | None:
    for pattern in DATE_PATTERNS:
        match = pattern.match(text, start)
        if match is not None:
            return str(match.group(0) or "")
    return None


def _match_candidate(text: str, start: int, candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate and text.startswith(candidate, start):
            return candidate
    return None


def _append_semantic_token(tokens: list[SemanticToken], kind: str, text: str) -> None:
    if not text:
        return
    if tokens and tokens[-1].kind == kind:
        previous = tokens[-1]
        tokens[-1] = SemanticToken(kind=kind, text=previous.text + text)
        return
    tokens.append(SemanticToken(kind=kind, text=text))


def _matching_index_names(text: str) -> tuple[str, ...]:
    names = {str(name).strip() for _, name in DEFAULT_MARKET_INDICES}
    names.update(str(name).strip() for name in _INDEX_ALIASES)
    matched = [name for name in names if name and name in text]
    return tuple(sorted(set(matched), key=lambda item: (-len(item), item)))


def _matching_stock_names(text: str, *, db_path: Path | str | None) -> tuple[str, ...]:
    if db_path is None:
        return ()
    try:
        from sats.stock_basic_lookup import names_from_stock_basic
        from sats.storage.duckdb import DuckDBStorage

        frame = DuckDBStorage(Path(db_path)).get_stock_basic()
    except Exception:
        return ()
    if getattr(frame, "empty", True):
        return ()
    try:
        matched = names_from_stock_basic(text, frame)
    except Exception:
        return ()
    return tuple(sorted({str(item).strip() for item in matched if len(str(item).strip()) >= 2}, key=lambda item: (-len(item), item)))


def _should_skip_semantic_highlight(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if raw.startswith("- "):
        raw = raw[2:].strip()
    if raw.startswith(("报告:", "产物:", "确认执行:", "取消执行:")):
        return True
    if "/confirm" in raw or "/reject" in raw:
        return True
    if raw.startswith("/"):
        return True
    return False
