from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


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
_REFERENCE_TERMS = ("上面", "刚才", "上一条", "前面", "之前", "本次", "这次", "当前", "结果", "输出", "答案", "内容", "报告")
_SAVE_PATTERNS = (
    re.compile(
        r"(?:[，,。；;]\s*)?(?:并(?:且)?|然后|同时)?\s*(?:保存|导出|存为)\s*"
        r"(?:上面|刚才|上一条|前面|之前|本次|这次|当前)?(?:结果|输出|答案|内容|报告)?\s*"
        r"(?:为|成|到|至)?\s*(?:MD|Markdown|PDF|md|markdown|pdf)?\s*(?:格式|文件)?"
    ),
    re.compile(
        r"(?:[，,。；;]\s*)?(?:并(?:且)?|然后|同时)?\s*(?:把|将)\s*"
        r"(?:上面|刚才|上一条|前面|之前|本次|这次|当前)?(?:结果|输出|答案|内容|报告)?\s*"
        r"(?:保存|导出|存为)\s*(?:为|成|到|至)?\s*(?:MD|Markdown|PDF|md|markdown|pdf)?\s*(?:格式|文件)?"
    ),
    re.compile(
        r"(?:[，,。；;]\s*)?(?:并(?:且)?|然后|同时)?\s*(?:把|将)?\s*"
        r"(?:上面|刚才|上一条|前面|之前|本次|这次|当前)?(?:结果|输出|答案|内容|报告)?\s*"
        r"(?:输出|转|转换)\s*(?:为|成|到|至)?\s*(?:MD|Markdown|PDF|md|markdown|pdf)\s*(?:格式|文件)?"
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
) -> SavedOutputResult:
    content, source_used, warning = _resolve_content(captured, request.source)
    if request.format == "pdf":
        path = save_output_as_pdf(captured, content, output_dir=output_dir, path=request.path)
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
) -> Path:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas
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
    page_width, page_height = A4
    margin = 42
    line_height = 14
    y = page_height - margin
    pdf = canvas.Canvas(str(target), pagesize=A4)
    pdf.setTitle("SATS Saved Output")
    pdf.setFont(font_name, 11)
    for raw_line in document.splitlines() or [""]:
        for line in _wrap_pdf_line(raw_line, max_width=78):
            if y < margin:
                pdf.showPage()
                pdf.setFont(font_name, 11)
                y = page_height - margin
            pdf.drawString(margin, y, line)
            y -= line_height
    pdf.save()
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
        and any(term in stripped for term in ("上面", "刚才", "上一条", "前面", "之前", "报告", "结果", "输出"))
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
