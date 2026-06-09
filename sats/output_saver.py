from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from sats.natural_output import OutputSemanticLexicon, build_output_semantic_lexicon, extract_output_metadata, tokenize_semantic_text


OutputFormat = Literal["md", "pdf"]
OutputSource = Literal["output", "report"]


@dataclass(slots=True)
class CapturedOutput:
    content: str
    request: str = ""
    source: str = "output"
    report_path: Path | None = None


@dataclass(slots=True)
class SaveRequest:
    format: OutputFormat = "md"
    source: OutputSource = "output"
    path: Path | None = None
    is_pure: bool = True
    cleaned_text: str = ""


@dataclass(slots=True)
class SavedOutputResult:
    path: Path
    format: OutputFormat
    source_used: OutputSource
    warning: str = ""


_SAVE_TERMS = ("保存", "导出", "存为")
_REFERENCE_TERMS = (
    "上面对话",
    "刚才对话",
    "上一个",
    "上一次",
    "上一轮",
    "上面",
    "刚才",
    "上一条",
    "前面",
    "之前",
    "本次",
    "这次",
    "当前",
    "结果",
    "输出",
    "答案",
    "内容",
    "报告",
    "对话",
    "回复",
    "回答",
)
_REFERENCE_PATTERN = r"(?:上面对话|刚才对话|上一个|上一次|上一轮|上面|刚才|上一条|前面|之前|本次|这次|当前)?"
_OUTPUT_PATTERN = r"(?:结果|输出|答案|内容|报告|对话|回复|回答)?"
_FORMAT_PATTERN = r"(?:MD|Markdown|PDF|md|markdown|pdf)"
_CONNECTOR_PATTERN = r"(?:到出到|出到|为|成|到|至)?"
_SAVE_PATTERNS = (
    re.compile(
        r"(?:[，,。；;]\s*)?(?:并(?:且)?|然后|同时)?\s*(?:保存|导出|存为)\s*"
        rf"{_REFERENCE_PATTERN}{_OUTPUT_PATTERN}\s*"
        rf"{_CONNECTOR_PATTERN}\s*{_FORMAT_PATTERN}?\s*(?:格式|文件)?"
    ),
    re.compile(
        r"(?:[，,。；;]\s*)?(?:并(?:且)?|然后|同时)?\s*(?:把|将)\s*"
        rf"{_REFERENCE_PATTERN}{_OUTPUT_PATTERN}\s*"
        rf"(?:保存|导出|存为)\s*{_CONNECTOR_PATTERN}\s*{_FORMAT_PATTERN}?\s*(?:格式|文件)?"
    ),
    re.compile(
        r"(?:[，,。；;]\s*)?(?:并(?:且)?|然后|同时)?\s*(?:把|将)?\s*"
        rf"{_REFERENCE_PATTERN}{_OUTPUT_PATTERN}\s*"
        rf"(?:输出|转|转换)\s*{_CONNECTOR_PATTERN}\s*{_FORMAT_PATTERN}\s*(?:格式|文件)?"
    ),
)


def parse_save_request(text: str) -> SaveRequest | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    fmt = _detect_format(raw)
    has_format = fmt is not None
    has_save_term = any(term in raw for term in _SAVE_TERMS)
    has_conversion_pattern = _looks_like_output_conversion_request(raw)
    if not has_save_term and not has_conversion_pattern:
        return None
    if not has_format and not any(term in raw for term in _REFERENCE_TERMS):
        return None
    output_format: OutputFormat = fmt or "md"
    source: OutputSource = "report" if "报告" in raw else "output"
    cleaned = _strip_save_phrase(raw)
    is_pure = _is_pure_save_request(raw, cleaned)
    return SaveRequest(format=output_format, source=source, is_pure=is_pure, cleaned_text=cleaned)


def extract_report_path(text: str) -> Path | None:
    report_path = None
    for line in str(text or "").splitlines():
        match = re.search(r"报告:\s*(.+)$", line.strip())
        if match:
            candidate = match.group(1).strip()
            if candidate:
                report_path = Path(candidate)
    return report_path


def save_captured_output(
    captured: CapturedOutput,
    request: SaveRequest,
    *,
    output_dir: Path,
    db_path: Path | str | None = None,
) -> SavedOutputResult:
    content, source_used, warning = _resolve_content(captured, request.source)
    semantic_lexicon = build_output_semantic_lexicon(content, db_path=db_path)
    if request.format == "pdf":
        path = save_output_as_pdf(
            captured,
            content,
            output_dir=output_dir,
            path=request.path,
            semantic_lexicon=semantic_lexicon,
        )
    else:
        path = save_output_as_markdown(captured, content, output_dir=output_dir, path=request.path)
    return SavedOutputResult(path=path, format=request.format, source_used=source_used, warning=warning)


def save_output_as_markdown(
    captured: CapturedOutput,
    content: str,
    *,
    output_dir: Path,
    path: Path | None = None,
) -> Path:
    target = _target_path(output_dir=output_dir, path=path, suffix=".md")
    body = _markdown_document(captured, content)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def save_output_as_pdf(
    captured: CapturedOutput,
    content: str,
    *,
    output_dir: Path,
    path: Path | None = None,
    semantic_lexicon: OutputSemanticLexicon | None = None,
) -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    except ImportError as exc:  # pragma: no cover - exercised when dependency is absent
        raise RuntimeError("PDF 保存需要安装 reportlab：pip install reportlab") from exc

    target = _target_path(output_dir=output_dir, path=path, suffix=".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    font_name = "STSong-Light"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    except Exception:
        font_name = "Helvetica"

    document = _markdown_document(captured, content)
    styles = _pdf_styles(font_name, colors=colors, ParagraphStyle=ParagraphStyle, getSampleStyleSheet=getSampleStyleSheet)
    story = _pdf_story_from_markdown(
        document,
        styles=styles,
        colors=colors,
        Paragraph=Paragraph,
        Spacer=Spacer,
        Table=Table,
        TableStyle=TableStyle,
        HRFlowable=HRFlowable,
        semantic_lexicon=semantic_lexicon,
    )
    pdf = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=42,
        rightMargin=42,
        topMargin=42,
        bottomMargin=42,
        title="SATS Saved Output",
    )
    pdf.build(story)
    return target


def _detect_format(text: str) -> OutputFormat | None:
    lower = text.lower()
    if "pdf" in lower:
        return "pdf"
    if "markdown" in lower or re.search(r"(?<![a-z])md(?![a-z])", lower):
        return "md"
    return None


def _strip_save_phrase(text: str) -> str:
    cleaned = text
    for pattern in _SAVE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip(" \t，,。；;")


def _looks_like_output_conversion_request(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw or _detect_format(raw) is None:
        return False
    return any(pattern.search(raw) for pattern in _SAVE_PATTERNS[2:])


def _is_pure_save_request(raw: str, cleaned: str) -> bool:
    compact = cleaned.strip(" \t，,。；;")
    if not compact or compact in {"把", "将"}:
        return True
    stripped = raw.strip()
    return stripped.startswith(_SAVE_TERMS) or (
        stripped.startswith(("把", "将"))
        and any(term in stripped for term in _REFERENCE_TERMS)
    ) or (
        compact in _REFERENCE_TERMS
        or any(compact == f"{reference}{output}" for reference in _REFERENCE_TERMS for output in _REFERENCE_TERMS)
    )


def _resolve_content(captured: CapturedOutput, source: OutputSource) -> tuple[str, OutputSource, str]:
    if source == "report":
        if captured.report_path is not None and captured.report_path.exists():
            return captured.report_path.read_text(encoding="utf-8"), "report", ""
        if captured.report_path is not None:
            return captured.content, "output", f"报告文件不可读，已保存终端输出：{captured.report_path}"
        return captured.content, "output", "上一条输出没有报告路径，已保存终端输出。"
    return captured.content, "output", ""


def _target_path(*, output_dir: Path, path: Path | None, suffix: str) -> Path:
    if path is None:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir / f"saved_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"
    target = Path(path)
    if target.suffix.lower() != suffix:
        target = target.with_suffix(suffix)
    return target


def _markdown_document(captured: CapturedOutput, content: str) -> str:
    if _looks_like_markdown_output(content):
        return str(content or "").rstrip() + "\n"
    lines = [
        "# SATS Saved Output",
        "",
        f"- Saved at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Source: {captured.source}",
    ]
    if captured.request:
        lines.append(f"- Request: {captured.request}")
    if captured.report_path is not None:
        lines.append(f"- Report: {captured.report_path}")
    lines.extend(["", "---", "", str(content or "").rstrip(), ""])
    return "\n".join(lines)


def _looks_like_markdown_output(content: str) -> bool:
    text = str(content or "").strip()
    metadata = extract_output_metadata(text)
    if metadata.title:
        return True
    return text.startswith("# ") or "\n## " in text or "\n> " in text or "\n|---" in text


def _pdf_styles(font_name: str, *, colors, ParagraphStyle, getSampleStyleSheet) -> dict[str, object]:
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "sats-title",
            parent=sample["Heading1"],
            fontName=font_name,
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#0f172a"),
            spaceAfter=10,
        ),
        "section": ParagraphStyle(
            "sats-section",
            parent=sample["Heading2"],
            fontName=font_name,
            fontSize=13,
            leading=17,
            textColor=colors.HexColor("#0f4c81"),
            spaceBefore=8,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "sats-body",
            parent=sample["BodyText"],
            fontName=font_name,
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#111827"),
            spaceAfter=4,
        ),
        "muted": ParagraphStyle(
            "sats-muted",
            parent=sample["BodyText"],
            fontName=font_name,
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#64748b"),
            spaceAfter=4,
        ),
        "callout": ParagraphStyle(
            "sats-callout",
            parent=sample["BodyText"],
            fontName=font_name,
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#0f172a"),
        ),
        "warning": ParagraphStyle(
            "sats-warning",
            parent=sample["BodyText"],
            fontName=font_name,
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#7c2d12"),
        ),
        "badge": ParagraphStyle(
            "sats-badge",
            parent=sample["BodyText"],
            fontName=font_name,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#334155"),
        ),
    }


def _pdf_story_from_markdown(
    markdown_text: str,
    *,
    styles,
    colors,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
    semantic_lexicon: OutputSemanticLexicon | None,
) -> list[object]:
    lines = str(markdown_text or "").splitlines()
    story: list[object] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            story.append(Spacer(1, 4))
            index += 1
            continue
        if stripped.startswith("|"):
            table_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            story.extend(
                _pdf_table_story(
                    table_lines,
                    styles=styles,
                    colors=colors,
                    Paragraph=Paragraph,
                    Table=Table,
                    TableStyle=TableStyle,
                    Spacer=Spacer,
                    semantic_lexicon=semantic_lexicon,
                )
            )
            continue
        if stripped == "---":
            story.append(HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#cbd5e1")))
            story.append(Spacer(1, 6))
            index += 1
            continue
        if stripped.startswith("# "):
            story.append(Paragraph(_semantic_pdf_markup(stripped[2:].strip(), semantic_lexicon=semantic_lexicon), styles["title"]))
            story.append(Spacer(1, 6))
            index += 1
            continue
        if stripped.startswith("## "):
            story.append(Paragraph(_semantic_pdf_markup(stripped[3:].strip(), semantic_lexicon=semantic_lexicon), styles["section"]))
            index += 1
            continue
        if stripped.startswith("> "):
            warning = "待确认动作" in stripped or "风险" in stripped
            style = styles["warning"] if warning else styles["callout"]
            background = colors.HexColor("#fef3c7") if warning else colors.HexColor("#e0f2fe")
            border = colors.HexColor("#f59e0b") if warning else colors.HexColor("#38bdf8")
            story.append(
                Table(
                    [[Paragraph(_semantic_pdf_markup(stripped[2:].strip(), semantic_lexicon=semantic_lexicon, semantic_enabled=not _should_skip_semantic_highlight(stripped)), style)]],
                    style=TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), background),
                            ("BOX", (0, 0), (-1, -1), 0.8, border),
                            ("LEFTPADDING", (0, 0), (-1, -1), 8),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                            ("TOPPADDING", (0, 0), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ]
                    ),
                )
            )
            story.append(Spacer(1, 6))
            index += 1
            continue
        if _is_badge_line(stripped):
            story.extend(
                _pdf_badge_story(
                    stripped,
                    styles=styles,
                    colors=colors,
                    Paragraph=Paragraph,
                    Table=Table,
                    TableStyle=TableStyle,
                    Spacer=Spacer,
                )
            )
            index += 1
            continue
        if stripped.startswith("- "):
            story.append(
                Paragraph(
                    "• " + _semantic_pdf_markup(
                        stripped[2:].strip(),
                        semantic_lexicon=semantic_lexicon,
                        semantic_enabled=not _should_skip_semantic_highlight(stripped),
                    ),
                    styles["body"],
                )
            )
            index += 1
            continue
        story.append(
            Paragraph(
                _semantic_pdf_markup(
                    stripped,
                    semantic_lexicon=semantic_lexicon,
                    semantic_enabled=not _should_skip_semantic_highlight(stripped),
                ),
                styles["body"],
            )
        )
        index += 1
    return story


def _pdf_table_story(
    table_lines: list[str],
    *,
    styles,
    colors,
    Paragraph,
    Table,
    TableStyle,
    Spacer,
    semantic_lexicon: OutputSemanticLexicon | None,
) -> list[object]:
    rows = [_parse_table_row(line) for line in table_lines if line.strip().startswith("|")]
    rows = [row for row in rows if row]
    if len(rows) < 2:
        return [Paragraph(_semantic_pdf_markup("\n".join(table_lines), semantic_lexicon=semantic_lexicon), styles["body"])]
    header = rows[0]
    body = rows[2:] if len(rows) >= 3 and _is_separator_row(rows[1]) else rows[1:]
    if not body:
        body = rows[1:]
    cells = [[Paragraph(_semantic_pdf_markup(cell, semantic_lexicon=semantic_lexicon, semantic_enabled=False), styles["badge"]) for cell in header]]
    for row in body:
        cells.append(
            [
                Paragraph(
                    _semantic_pdf_markup(
                        cell,
                        semantic_lexicon=semantic_lexicon,
                        semantic_enabled=not _should_skip_semantic_highlight(cell),
                    ),
                    styles["body"],
                )
                for cell in row
            ]
        )
    table = Table(cells, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbeafe")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f4c81")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#cbd5e1")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return [table, Spacer(1, 6)]


def _pdf_badge_story(text: str, *, styles, colors, Paragraph, Table, TableStyle, Spacer) -> list[object]:
    tokens = re.findall(r"`([^`]+)`", text)
    if not tokens:
        return [Paragraph(_escape_inline_markdown(text), styles["muted"])]
    table = Table([[Paragraph(escape(token.strip()), styles["badge"]) for token in tokens]], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e2e8f0")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return [table, Spacer(1, 6)]


def _is_badge_line(text: str) -> bool:
    stripped = str(text or "").strip()
    return bool(stripped) and stripped.count("`") >= 2 and stripped.replace("`", "").strip() != stripped


def _parse_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(row: list[str]) -> bool:
    return all(set(cell) <= {"-", ":"} for cell in row if cell)


def _escape_inline_markdown(text: str) -> str:
    value = escape(str(text or ""))
    value = re.sub(r"`([^`]+)`", lambda match: f"<font color='#334155'>{escape(match.group(1))}</font>", value)
    return value


def _semantic_pdf_markup(
    text: str,
    *,
    semantic_lexicon: OutputSemanticLexicon | None,
    semantic_enabled: bool = True,
) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    parts = re.split(r"(`[^`]+`)", raw)
    blocks: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("`") and part.endswith("`"):
            blocks.append(f"<font color='#334155'>{escape(part[1:-1])}</font>")
            continue
        if not semantic_enabled or semantic_lexicon is None:
            blocks.append(escape(part))
            continue
        for token in tokenize_semantic_text(part, semantic_lexicon):
            blocks.append(_semantic_token_markup(token.kind, token.text))
    return "".join(blocks)


def _semantic_token_markup(kind: str, text: str) -> str:
    color = None
    if kind in {"symbol_code", "symbol_name"}:
        color = "#1d4ed8"
    elif kind == "percent_positive":
        color = "#ef4444"
    elif kind == "percent_negative":
        color = "#22c55e"
    if color is None:
        return escape(text)
    return f"<font color='{color}'>{escape(text)}</font>"


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


def _wrap_pdf_line(line: str, *, max_width: int) -> list[str]:
    if not line:
        return [""]
    output: list[str] = []
    current = ""
    current_width = 0
    for char in line:
        char_width = 2 if ord(char) > 127 else 1
        if current and current_width + char_width > max_width:
            output.append(current)
            current = char
            current_width = char_width
        else:
            current += char
            current_width += char_width
    output.append(current)
    return output
