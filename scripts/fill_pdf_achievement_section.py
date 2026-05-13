#!/usr/bin/env python3
"""Fill achievement standards into the template PDF and save a new PDF.

This script keeps the original PDF pages, clears the placeholder area for
"3. 성취기준 및 성취수준", then redraws that section with dynamic table
heights so long Korean text remains readable.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any

import fitz


SECTION_TITLE = "성취기준 및 성취수준"
SECTION_B_TITLE = "학기 단위 성취수준"
DEFAULT_FONT_URL = (
    "https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Korean/"
    "NotoSansCJKkr-Regular.otf"
)

PAGE_SIZE = fitz.Rect(0, 0, 595, 842)
TABLE_LEFT = 72.2
STANDARD_RIGHT = 187.0
LABEL_RIGHT = 211.2
TABLE_RIGHT = 523.0
FIRST_PAGE_CONTENT_TOP = 628.0
CONT_PAGE_CONTENT_TOP = 80.0
CONTENT_BOTTOM = 760.0
FOOTER_RECT = fitz.Rect(248, 780, 347, 803)
ERASE_FIRST_PAGE_RECT = fitz.Rect(65, 625, 530, 777)
ERASE_CONT_PAGE_RECT = fitz.Rect(65, 78, 530, 760)
ERASE_SUMMARY_PAGE_RECT = fitz.Rect(65, 360, 530, 760)
HEADER_FILL = (0.803922, 0.94902, 0.894118)
LINE_COLOR = (0, 0, 0)

AREA_TITLE_SIZE = 12
HEADER_TEXT_SIZE = 9
STANDARD_TEXT_SIZE = 7.2
LEVEL_TEXT_SIZE = 6.6
FOOTER_TEXT_SIZE = 9
TITLE_HEIGHT = 18
HEADER_HEIGHT = 18
AREA_GAP = 12
TITLE_GAP = 6
CELL_PAD_X = 4
CELL_PAD_Y = 3
MIN_LEVEL_HEIGHT = 16
LINE_WIDTH = 0.24
LEVELS = ("A", "B", "C", "D", "E")
SUMMARY_CONTENT_TOP = 365.3
SUMMARY_LEFT = 72.2
SUMMARY_LABEL_RIGHT = 102.2
SUMMARY_RIGHT = 524.2
SUMMARY_HEADER_HEIGHT = 23.5
SUMMARY_TEXT_SIZE = 7.0
SUMMARY_CONT_TITLE = "나. 학기 단위 성취수준 (계속)"


def normalize_space(text: str) -> str:
    return " ".join(str(text).replace("\u00a0", " ").split())


def normalize_key(text: str) -> str:
    return normalize_space(text).replace(" ", "")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template_pdf", help="Template PDF path.")
    parser.add_argument("achievement_json", help="Achievement standards JSON path.")
    parser.add_argument("--course", help="Course name inside the JSON.")
    parser.add_argument("--font-path", help="Korean font path for PDF text rendering.")
    parser.add_argument("--font-url", default=DEFAULT_FONT_URL, help="Fallback font download URL.")
    parser.add_argument(
        "--output",
        help="Output PDF path. Defaults to '<template stem>_성취기준입력.pdf'.",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def infer_course_name(template_path: Path, payload: dict[str, Any]) -> str:
    courses = sorted(
        {item.get("course", "").strip() for item in payload.get("achievement_standards", []) if item.get("course")}
    )
    haystack = normalize_key(template_path.stem)
    for course in sorted(courses, key=len, reverse=True):
        if normalize_key(course) in haystack:
            return course
    raise ValueError("과목명을 자동으로 추론하지 못했습니다. --course 옵션을 지정해 주세요.")


def collect_course_areas(payload: dict[str, Any], course: str) -> list[dict[str, Any]]:
    wanted = normalize_key(course)
    grouped: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()

    for item in payload.get("achievement_standards", []):
        if normalize_key(item.get("course", "")) != wanted:
            continue
        key = (str(item.get("area_number", "")).strip(), str(item.get("area", "")).strip())
        grouped.setdefault(key, []).append(item)

    if not grouped:
        raise ValueError(f"JSON 안에서 과목 '{course}' 데이터를 찾지 못했습니다.")

    areas: list[dict[str, Any]] = []
    for area_number, area_title in grouped:
        areas.append(
            {
                "area_number": area_number,
                "title": area_title,
                "standards": grouped[(area_number, area_title)],
            }
        )
    return areas


def find_section_b_start_page(source: fitz.Document) -> int:
    for page_index, page in enumerate(source):
        text = page.get_text("text")
        if SECTION_B_TITLE in text:
            return page_index
    raise ValueError(f"템플릿 PDF에서 '{SECTION_B_TITLE}' 페이지를 찾지 못했습니다.")


def ensure_font(font_path: str | None, font_url: str) -> Path:
    candidates = []
    if font_path:
        candidates.append(Path(font_path))
    candidates.extend(
        [
            Path("/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
            Path("/usr/share/fonts/truetype/noto/NotoSansKR-Regular.otf"),
            Path.home() / ".cache" / "sagu" / "NotoSansCJKkr-Regular.otf",
        ]
    )

    for candidate in candidates:
        if candidate.exists():
            return candidate

    cache_path = Path.home() / ".cache" / "sagu" / "NotoSansCJKkr-Regular.otf"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(font_url, cache_path)
    return cache_path


def wrap_text(text: str, font: fitz.Font, fontsize: float, max_width: float) -> list[str]:
    paragraphs = text.splitlines() or [text]
    wrapped: list[str] = []

    for paragraph in paragraphs:
        paragraph = normalize_space(paragraph)
        if not paragraph:
            wrapped.append("")
            continue

        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if font.text_length(candidate, fontsize) <= max_width:
                current = candidate
                continue

            if current:
                wrapped.append(current)
                current = ""

            fragment = ""
            for char in word:
                candidate = fragment + char
                if not fragment or font.text_length(candidate, fontsize) <= max_width:
                    fragment = candidate
                else:
                    wrapped.append(fragment)
                    fragment = char
            current = fragment

        if current:
            wrapped.append(current)

    return wrapped or [""]


def text_height(line_count: int, fontsize: float) -> float:
    line_height = fontsize * 1.35
    return line_count * line_height + (CELL_PAD_Y * 2)


def measure_standard_row(standard: dict[str, Any], font: fitz.Font) -> dict[str, Any]:
    standard_text = normalize_space(standard["statement"])
    standard_lines = wrap_text(
        standard_text,
        font,
        STANDARD_TEXT_SIZE,
        (STANDARD_RIGHT - TABLE_LEFT) - (CELL_PAD_X * 2),
    )
    left_height = text_height(len(standard_lines), STANDARD_TEXT_SIZE)

    level_entries: list[dict[str, Any]] = []
    total_level_height = 0.0
    for level in LEVELS:
        level_text = normalize_space(standard.get("achievement_levels", {}).get(level, ""))
        lines = wrap_text(
            level_text,
            font,
            LEVEL_TEXT_SIZE,
            (TABLE_RIGHT - LABEL_RIGHT) - (CELL_PAD_X * 2),
        )
        height = max(MIN_LEVEL_HEIGHT, text_height(len(lines), LEVEL_TEXT_SIZE))
        total_level_height += height
        level_entries.append({"level": level, "text": level_text, "lines": lines, "height": height})

    row_height = max(left_height, total_level_height)
    if total_level_height < row_height:
        level_entries[-1]["height"] += row_height - total_level_height

    return {
        "code": standard["code"],
        "standard_text": standard_text,
        "standard_lines": standard_lines,
        "levels": level_entries,
        "row_height": row_height,
    }


def create_text_page(source: fitz.Document, target: fitz.Document, source_page_index: int, erase_rect: fitz.Rect) -> fitz.Page:
    start_at = target.page_count
    target.insert_pdf(source, from_page=source_page_index, to_page=source_page_index, start_at=start_at)
    page = target[start_at]
    page.draw_rect(erase_rect, fill=(1, 1, 1), color=None, overlay=True)
    return page


def create_blank_page(target: fitz.Document) -> fitz.Page:
    return target.new_page(width=PAGE_SIZE.width, height=PAGE_SIZE.height)


def register_font(page: fitz.Page, font_path: Path) -> str:
    font_name = "noto_kr"
    page.insert_font(fontname=font_name, fontfile=str(font_path))
    return font_name


def draw_multiline_text(
    page: fitz.Page,
    rect: fitz.Rect,
    lines: list[str],
    *,
    font_name: str,
    fontsize: float,
    color: tuple[float, float, float] = LINE_COLOR,
) -> None:
    line_height = fontsize * 1.35
    baseline = rect.y0 + fontsize
    for line in lines:
        page.insert_text(
            fitz.Point(rect.x0, baseline),
            line,
            fontname=font_name,
            fontsize=fontsize,
            color=color,
            overlay=True,
        )
        baseline += line_height


def draw_area_title(page: fitz.Page, y: float, title: str, font_name: str) -> float:
    page.insert_text(
        fitz.Point(TABLE_LEFT, y + AREA_TITLE_SIZE),
        title,
        fontname=font_name,
        fontsize=AREA_TITLE_SIZE,
        color=LINE_COLOR,
        overlay=True,
    )
    return y + TITLE_HEIGHT + TITLE_GAP


def draw_table_header(page: fitz.Page, y: float, font_name: str) -> float:
    left_rect = fitz.Rect(TABLE_LEFT, y, STANDARD_RIGHT, y + HEADER_HEIGHT)
    right_rect = fitz.Rect(STANDARD_RIGHT, y, TABLE_RIGHT, y + HEADER_HEIGHT)

    page.draw_rect(left_rect, fill=HEADER_FILL, color=LINE_COLOR, width=LINE_WIDTH, overlay=True)
    page.draw_rect(right_rect, fill=HEADER_FILL, color=LINE_COLOR, width=LINE_WIDTH, overlay=True)

    page.insert_textbox(
        left_rect,
        "성취기준",
        fontname=font_name,
        fontsize=HEADER_TEXT_SIZE,
        color=LINE_COLOR,
        align=1,
        overlay=True,
    )
    page.insert_textbox(
        right_rect,
        "성취기준별 성취수준",
        fontname=font_name,
        fontsize=HEADER_TEXT_SIZE,
        color=LINE_COLOR,
        align=1,
        overlay=True,
    )
    return y + HEADER_HEIGHT


def draw_standard_row(page: fitz.Page, y: float, row: dict[str, Any], font_name: str) -> float:
    row_bottom = y + row["row_height"]
    page.draw_rect(
        fitz.Rect(TABLE_LEFT, y, STANDARD_RIGHT, row_bottom),
        fill=None,
        color=LINE_COLOR,
        width=LINE_WIDTH,
        overlay=True,
    )

    left_text_rect = fitz.Rect(
        TABLE_LEFT + CELL_PAD_X,
        y + CELL_PAD_Y,
        STANDARD_RIGHT - CELL_PAD_X,
        row_bottom - CELL_PAD_Y,
    )
    draw_multiline_text(
        page,
        left_text_rect,
        row["standard_lines"],
        font_name=font_name,
        fontsize=STANDARD_TEXT_SIZE,
    )

    cursor_y = y
    for level_entry in row["levels"]:
        next_y = cursor_y + level_entry["height"]
        label_rect = fitz.Rect(STANDARD_RIGHT, cursor_y, LABEL_RIGHT, next_y)
        text_rect = fitz.Rect(LABEL_RIGHT, cursor_y, TABLE_RIGHT, next_y)

        page.draw_rect(label_rect, fill=None, color=LINE_COLOR, width=LINE_WIDTH, overlay=True)
        page.draw_rect(text_rect, fill=None, color=LINE_COLOR, width=LINE_WIDTH, overlay=True)

        page.insert_textbox(
            label_rect,
            level_entry["level"],
            fontname=font_name,
            fontsize=HEADER_TEXT_SIZE,
            color=LINE_COLOR,
            align=1,
            overlay=True,
        )
        draw_multiline_text(
            page,
            fitz.Rect(
                text_rect.x0 + CELL_PAD_X,
                text_rect.y0 + CELL_PAD_Y,
                text_rect.x1 - CELL_PAD_X,
                text_rect.y1 - CELL_PAD_Y,
            ),
            level_entry["lines"],
            font_name=font_name,
            fontsize=LEVEL_TEXT_SIZE,
        )
        cursor_y = next_y

    return row_bottom


def draw_footer_page_number(page: fitz.Page, page_number: int, font_name: str) -> None:
    page.draw_rect(FOOTER_RECT, fill=(1, 1, 1), color=None, overlay=True)
    page.insert_textbox(
        FOOTER_RECT,
        f"- {page_number} -",
        fontname=font_name,
        fontsize=FOOTER_TEXT_SIZE,
        color=LINE_COLOR,
        align=1,
        overlay=True,
    )


def build_summary_by_level(areas: list[dict[str, Any]]) -> dict[str, str]:
    summary: dict[str, list[str]] = {level: [] for level in LEVELS}
    for area in areas:
        by_level: dict[str, list[str]] = {level: [] for level in LEVELS}
        for standard in area["standards"]:
            for level in LEVELS:
                text = normalize_space(standard.get("achievement_levels", {}).get(level, ""))
                if text:
                    by_level[level].append(text)
        for level in LEVELS:
            if by_level[level]:
                summary[level].extend(by_level[level])
    return {level: "\n".join(lines).strip() for level, lines in summary.items()}


def draw_summary_header(page: fitz.Page, y: float, font_name: str) -> float:
    left_rect = fitz.Rect(SUMMARY_LEFT, y, SUMMARY_LABEL_RIGHT, y + SUMMARY_HEADER_HEIGHT)
    right_rect = fitz.Rect(SUMMARY_LABEL_RIGHT, y, SUMMARY_RIGHT, y + SUMMARY_HEADER_HEIGHT)
    page.draw_rect(left_rect, fill=HEADER_FILL, color=LINE_COLOR, width=LINE_WIDTH, overlay=True)
    page.draw_rect(right_rect, fill=HEADER_FILL, color=LINE_COLOR, width=LINE_WIDTH, overlay=True)
    page.insert_textbox(
        left_rect,
        "성취\n수준",
        fontname=font_name,
        fontsize=HEADER_TEXT_SIZE - 0.5,
        color=LINE_COLOR,
        align=1,
        overlay=True,
    )
    page.insert_textbox(
        right_rect,
        "학기 단위 성취수준",
        fontname=font_name,
        fontsize=HEADER_TEXT_SIZE,
        color=LINE_COLOR,
        align=1,
        overlay=True,
    )
    return y + SUMMARY_HEADER_HEIGHT


def render_summary_section(
    source: fitz.Document,
    target: fitz.Document,
    areas: list[dict[str, Any]],
    font_path: Path,
    summary_source_page_index: int,
) -> None:
    summary_texts = build_summary_by_level(areas)
    font = fitz.Font(fontfile=str(font_path))

    current_page = create_text_page(source, target, summary_source_page_index, ERASE_SUMMARY_PAGE_RECT)
    font_name = register_font(current_page, font_path)
    current_y = draw_summary_header(current_page, SUMMARY_CONTENT_TOP, font_name)

    def new_summary_continuation_page() -> tuple[fitz.Page, float, str]:
        page = create_blank_page(target)
        local_font_name = register_font(page, font_path)
        page.insert_text(
            fitz.Point(SUMMARY_LEFT, 52),
            SUMMARY_CONT_TITLE,
            fontname=local_font_name,
            fontsize=AREA_TITLE_SIZE,
            color=LINE_COLOR,
            overlay=True,
        )
        start_y = draw_summary_header(page, 68, local_font_name)
        return page, start_y, local_font_name

    for level in LEVELS:
        wrapped_lines = wrap_text(
            summary_texts[level],
            font,
            SUMMARY_TEXT_SIZE,
            (SUMMARY_RIGHT - SUMMARY_LABEL_RIGHT) - (CELL_PAD_X * 2),
        )
        if not wrapped_lines:
            wrapped_lines = [""]

        line_height = SUMMARY_TEXT_SIZE * 1.35
        while wrapped_lines:
            available_height = CONTENT_BOTTOM - current_y
            max_lines = max(1, int((available_height - (CELL_PAD_Y * 2)) // line_height))
            chunk = wrapped_lines[:max_lines]
            chunk_height = max(MIN_LEVEL_HEIGHT, text_height(len(chunk), SUMMARY_TEXT_SIZE))
            if current_y + chunk_height > CONTENT_BOTTOM:
                current_page, current_y, font_name = new_summary_continuation_page()
                continue

            next_y = current_y + chunk_height
            label_rect = fitz.Rect(SUMMARY_LEFT, current_y, SUMMARY_LABEL_RIGHT, next_y)
            text_rect = fitz.Rect(SUMMARY_LABEL_RIGHT, current_y, SUMMARY_RIGHT, next_y)
            current_page.draw_rect(label_rect, fill=None, color=LINE_COLOR, width=LINE_WIDTH, overlay=True)
            current_page.draw_rect(text_rect, fill=None, color=LINE_COLOR, width=LINE_WIDTH, overlay=True)
            current_page.insert_textbox(
                label_rect,
                level,
                fontname=font_name,
                fontsize=HEADER_TEXT_SIZE,
                color=LINE_COLOR,
                align=1,
                overlay=True,
            )
            draw_multiline_text(
                current_page,
                fitz.Rect(
                    text_rect.x0 + CELL_PAD_X,
                    text_rect.y0 + CELL_PAD_Y,
                    text_rect.x1 - CELL_PAD_X,
                    text_rect.y1 - CELL_PAD_Y,
                ),
                chunk,
                font_name=font_name,
                fontsize=SUMMARY_TEXT_SIZE,
            )
            wrapped_lines = wrapped_lines[max_lines:]
            current_y = next_y


def render_section(
    source: fitz.Document,
    target: fitz.Document,
    areas: list[dict[str, Any]],
    font_path: Path,
) -> None:
    summary_source_page_index = find_section_b_start_page(source)

    target.insert_pdf(source, from_page=0, to_page=2)

    pages: list[fitz.Page] = []
    first_page = create_text_page(source, target, 3, ERASE_FIRST_PAGE_RECT)
    pages.append(first_page)
    current_page = first_page
    current_y = FIRST_PAGE_CONTENT_TOP
    continuation_source_page = 4

    def new_continuation_page() -> tuple[fitz.Page, float]:
        page = create_text_page(source, target, continuation_source_page, ERASE_CONT_PAGE_RECT)
        pages.append(page)
        return page, CONT_PAGE_CONTENT_TOP

    font_name = register_font(first_page, font_path)

    for area in areas:
        title = f"({area['area_number']}) {area['title']}"
        continuation = False
        pending_rows = [measure_standard_row(item, fitz.Font(fontfile=str(font_path))) for item in area["standards"]]

        while pending_rows:
            needed = TITLE_HEIGHT + TITLE_GAP + HEADER_HEIGHT + pending_rows[0]["row_height"]
            if current_y + needed > CONTENT_BOTTOM:
                current_page, current_y = new_continuation_page()
                font_name = register_font(current_page, font_path)

            shown_title = title if not continuation else f"{title} (계속)"
            current_y = draw_area_title(current_page, current_y, shown_title, font_name)
            current_y = draw_table_header(current_page, current_y, font_name)

            while pending_rows:
                row = pending_rows[0]
                if current_y + row["row_height"] > CONTENT_BOTTOM:
                    current_page, current_y = new_continuation_page()
                    font_name = register_font(current_page, font_path)
                    continuation = True
                    break

                current_y = draw_standard_row(current_page, current_y, row, font_name)
                pending_rows.pop(0)

            if not pending_rows:
                current_y += AREA_GAP

    render_summary_section(source, target, areas, font_path, summary_source_page_index)

    target.insert_pdf(source, from_page=summary_source_page_index + 1, to_page=source.page_count - 1)

    for page_number, page in enumerate(target, start=1):
        try:
            register_font(page, font_path)
        except Exception:
            pass
        draw_footer_page_number(page, page_number, "noto_kr")


def default_output_path(template_pdf: Path) -> Path:
    return template_pdf.with_name(f"{template_pdf.stem}_성취기준입력.pdf")


def main() -> int:
    args = parse_args()
    template_pdf = Path(args.template_pdf)
    achievement_json = Path(args.achievement_json)

    if not template_pdf.exists():
        print(f"[error] template PDF not found: {template_pdf}", file=sys.stderr)
        return 1
    if not achievement_json.exists():
        print(f"[error] achievement JSON not found: {achievement_json}", file=sys.stderr)
        return 1

    payload = load_json(achievement_json)
    course = args.course or infer_course_name(template_pdf, payload)
    font_path = ensure_font(args.font_path, args.font_url)
    output_pdf = Path(args.output) if args.output else default_output_path(template_pdf)

    try:
        areas = collect_course_areas(payload, course)
        with fitz.open(template_pdf) as source:
            target = fitz.open()
            render_section(source, target, areas, font_path)
            output_pdf.parent.mkdir(parents=True, exist_ok=True)
            if output_pdf.exists():
                output_pdf.unlink()
            target.save(output_pdf, garbage=4, deflate=True)
            target.close()
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    print(f"[ok] wrote output PDF: {output_pdf}")
    print(f"[ok] font: {font_path}")
    print(f"[ok] course: {course}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
