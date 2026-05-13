#!/usr/bin/env python3
"""Fill section 3 achievement standards into a Hancom HWP template.

The script reads the extracted achievement-standards JSON, groups the
standards so they fit the available row slots in the template, and then uses
Hancom HWP COM automation on Windows to write the text into the template.

The COM fill step only runs on Windows with Hancom HWP and pywin32 installed.
On other platforms, use --preview-only to generate the fill plan JSON first.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

import fitz


LEVELS = ("A", "B", "C", "D", "E")
SECTION_TITLE = "성취기준 및 성취수준"
TABLE_HEADER_RIGHT = "성취기준별 성취수준"
AREA_TITLE_PLACEHOLDER = "단원명"
SECTION_B_TITLE = "학기 단위 성취수준"


def normalize_space(text: str) -> str:
    return " ".join(str(text).replace("\u00a0", " ").split())


def normalize_key(text: str) -> str:
    return normalize_space(text).replace(" ", "")


def block_lines(block: dict[str, Any]) -> list[str]:
    return [
        "".join(span.get("text", "") for span in line.get("spans", []))
        for line in block.get("lines", [])
    ]


def block_text(block: dict[str, Any]) -> str:
    return "\n".join(line.rstrip() for line in block_lines(block)).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template_hwp", help="Target HWP template path.")
    parser.add_argument("achievement_json", help="Extracted achievement standards JSON path.")
    parser.add_argument(
        "--course",
        help="Course name inside the achievement JSON. If omitted, inferred from the HWP filename.",
    )
    parser.add_argument(
        "--template-pdf",
        help="Optional PDF version of the template used to detect row capacities automatically.",
    )
    parser.add_argument(
        "--area-row-capacities",
        help="Comma-separated row capacities per area, e.g. 4,4,2. Overrides PDF detection.",
    )
    parser.add_argument(
        "--output",
        help="Output HWP path. Defaults to '<template stem>_성취기준삽입.hwp'.",
    )
    parser.add_argument(
        "--plan-json",
        help="Optional path to write the generated fill plan JSON.",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Only build and write the fill plan JSON. Do not automate Hancom HWP.",
    )
    return parser.parse_args()


def load_achievement_json(path: Path) -> dict[str, Any]:
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
    for index, ((area_number, area_title), standards) in enumerate(grouped.items(), start=1):
        areas.append(
            {
                "index": index,
                "area_number": area_number,
                "title": area_title,
                "standards": standards,
            }
        )
    return areas


def detect_template_pdf(template_hwp: Path, explicit_pdf: str | None) -> Path | None:
    if explicit_pdf:
        path = Path(explicit_pdf)
        return path if path.exists() else None

    sibling_pdf = template_hwp.with_suffix(".pdf")
    if sibling_pdf.exists():
        return sibling_pdf
    return None


def detect_area_row_capacities_from_pdf(path: Path) -> list[int]:
    with fitz.open(path) as document:
        start_page = None
        end_page = None

        for page_index, page in enumerate(document):
            text = page.get_text("text")
            if start_page is None and SECTION_TITLE in text:
                start_page = page_index
            if start_page is not None and SECTION_B_TITLE in text:
                end_page = page_index
                break

        if start_page is None:
            raise ValueError(f"템플릿 PDF에서 '{SECTION_TITLE}' 섹션을 찾지 못했습니다: {path}")
        if end_page is None:
            end_page = document.page_count

        events: list[tuple[int, float, str, int]] = []
        for page_index in range(start_page, end_page):
            page = document[page_index]
            blocks = [
                block
                for block in page.get_text("dict", sort=True).get("blocks", [])
                if block.get("type") == 0
            ]

            for block in blocks:
                text = block_text(block)
                if not text:
                    continue

                y0 = float(block.get("bbox", (0, 0, 0, 0))[1])

                if AREA_TITLE_PLACEHOLDER in text:
                    if "(1)" in text:
                        events.append((page_index, y0, "area", 1))
                    elif "(2)" in text:
                        events.append((page_index, y0, "area", 2))
                    elif "(3)" in text:
                        events.append((page_index, y0, "area", 3))
                    continue

                lines = [normalize_space(line) for line in text.splitlines() if normalize_space(line)]
                if lines and all(line in LEVELS for line in lines):
                    label_count = sum(1 for line in lines if line in LEVELS)
                    if label_count and label_count % len(LEVELS) == 0:
                        events.append((page_index, y0, "rows", label_count // len(LEVELS)))

        events.sort(key=lambda item: (item[0], item[1]))
        capacities: list[int] = []
        current_area_index = -1

        for _page_index, _y0, kind, value in events:
            if kind == "area":
                current_area_index = value - 1
                while len(capacities) <= current_area_index:
                    capacities.append(0)
                continue

            if kind == "rows" and current_area_index >= 0:
                capacities[current_area_index] += value

        capacities = [count for count in capacities if count > 0]
        if not capacities:
            raise ValueError(f"템플릿 PDF에서 단원별 행 수를 읽지 못했습니다: {path}")
        return capacities


def parse_row_capacities(raw: str | None, template_pdf: Path | None) -> list[int]:
    if raw:
        values = [int(part.strip()) for part in raw.split(",") if part.strip()]
        if not values or any(value <= 0 for value in values):
            raise ValueError("--area-row-capacities 값이 올바르지 않습니다.")
        return values

    if template_pdf:
        return detect_area_row_capacities_from_pdf(template_pdf)

    raise ValueError("--area-row-capacities 또는 --template-pdf(또는 템플릿과 같은 이름의 PDF)가 필요합니다.")


def split_evenly(items: list[dict[str, Any]], slot_count: int) -> list[list[dict[str, Any]]]:
    if slot_count <= 0:
        raise ValueError("slot_count must be positive")

    total = len(items)
    base, extra = divmod(total, slot_count)
    sizes = [base + (1 if index < extra else 0) for index in range(slot_count)]

    groups: list[list[dict[str, Any]]] = []
    cursor = 0
    for size in sizes:
        groups.append(items[cursor : cursor + size])
        cursor += size
    return groups


def format_standard_group(group: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"[{item['code']}] {item['statement']}" for item in group).strip()


def format_level_group(group: list[dict[str, Any]], level: str) -> str:
    parts = []
    for item in group:
        level_text = normalize_space(item.get("achievement_levels", {}).get(level, ""))
        if level_text:
            parts.append(f"[{item['code']}] {level_text}")
    return "\n\n".join(parts).strip()


def build_fill_plan(
    payload: dict[str, Any],
    *,
    template_hwp: Path,
    template_pdf: Path | None,
    course: str,
    row_capacities: list[int],
) -> dict[str, Any]:
    areas = collect_course_areas(payload, course)
    if len(row_capacities) < len(areas):
        raise ValueError(
            f"템플릿 행 수 정보가 부족합니다. 단원 {len(areas)}개, 감지된 행 수 {len(row_capacities)}개입니다."
        )

    plan_areas: list[dict[str, Any]] = []
    for area, row_capacity in zip(areas, row_capacities):
        groups = split_evenly(area["standards"], row_capacity)
        rows: list[dict[str, Any]] = []
        for row_index, group in enumerate(groups, start=1):
            rows.append(
                {
                    "row_index": row_index,
                    "codes": [item["code"] for item in group],
                    "standard_text": format_standard_group(group),
                    "levels": {
                        level: format_level_group(group, level)
                        for level in LEVELS
                    },
                }
            )

        plan_areas.append(
            {
                "index": area["index"],
                "title": area["title"],
                "row_capacity": row_capacity,
                "standard_count": len(area["standards"]),
                "rows": rows,
            }
        )

    return {
        "schema": "achievement_section_fill_plan_v1",
        "template": {
            "hwp_path": str(template_hwp),
            "pdf_path": str(template_pdf) if template_pdf else "",
            "row_capacities": row_capacities,
        },
        "course": course,
        "areas": plan_areas,
    }


def write_plan_json(path: Path, plan: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def insert_text(hwp: Any, text: str) -> None:
    hwp.HAction.GetDefault("InsertText", hwp.HParameterSet.HInsertText.HSet)
    hwp.HParameterSet.HInsertText.Text = text
    hwp.HAction.Execute("InsertText", hwp.HParameterSet.HInsertText.HSet)


def repeat_find(hwp: Any, text: str, *, from_top: bool = False) -> bool:
    if from_top:
        hwp.HAction.Run("MoveDocBegin")
    hwp.HAction.GetDefault("RepeatFind", hwp.HParameterSet.HFindReplace.HSet)
    find_set = hwp.HParameterSet.HFindReplace
    find_set.FindString = text
    find_set.Direction = hwp.FindDir("Forward")
    find_set.IgnoreMessage = 1
    find_set.MatchCase = 0
    find_set.AllWordForms = 0
    find_set.SeveralWords = 0
    find_set.UseWildCards = 0
    find_set.WholeWordOnly = 0
    find_set.AutoSpell = 0
    find_set.FindJaso = 0
    find_set.FindRegExp = 0
    find_set.FindType = 1
    return bool(hwp.HAction.Execute("RepeatFind", find_set.HSet))


def replace_next_text(hwp: Any, find_text: str, replacement: str) -> None:
    if not repeat_find(hwp, find_text):
        raise RuntimeError(f"문서에서 '{find_text}'를 찾지 못했습니다.")
    hwp.HAction.Run("Delete")
    if replacement:
        insert_text(hwp, replacement)


def move_to_next_area_table(hwp: Any) -> None:
    if not repeat_find(hwp, TABLE_HEADER_RIGHT):
        raise RuntimeError(f"문서에서 '{TABLE_HEADER_RIGHT}'를 찾지 못했습니다.")
    hwp.HAction.Run("Cancel")
    hwp.HAction.Run("TableLeftCell")
    hwp.HAction.Run("TableLowerCell")


def move_to_next_row_standard_cell(hwp: Any) -> None:
    hwp.HAction.Run("TableLowerCell")
    hwp.HAction.Run("TableLeftCell")
    hwp.HAction.Run("TableLeftCell")


def fill_area_rows(hwp: Any, rows: list[dict[str, Any]]) -> None:
    for row_index, row in enumerate(rows):
        if row["standard_text"]:
            insert_text(hwp, row["standard_text"])

        hwp.HAction.Run("TableRightCell")
        hwp.HAction.Run("TableRightCell")

        for level_index, level in enumerate(LEVELS):
            text = row["levels"].get(level, "")
            if text:
                insert_text(hwp, text)
            if level_index < len(LEVELS) - 1:
                hwp.HAction.Run("TableLowerCell")

        if row_index < len(rows) - 1:
            move_to_next_row_standard_cell(hwp)


def build_default_output_path(template_hwp: Path) -> Path:
    return template_hwp.with_name(f"{template_hwp.stem}_성취기준삽입{template_hwp.suffix}")


def build_default_plan_path(output_hwp: Path) -> Path:
    return output_hwp.with_suffix(".achievement_fill_plan.json")


def automate_hwp_fill(template_hwp: Path, output_hwp: Path, plan: dict[str, Any]) -> None:
    if sys.platform != "win32":
        raise RuntimeError("한글 COM 자동화는 Windows에서만 실행할 수 있습니다. --preview-only로 계획 JSON을 먼저 확인하세요.")

    try:
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pywin32가 설치되어 있지 않습니다. 'pip install pywin32' 후 다시 실행해 주세요.") from exc

    hwp = win32com.client.gencache.EnsureDispatch("HWPFrame.HwpObject")
    try:
        try:
            hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
        except Exception:
            pass

        try:
            hwp.XHwpWindows.Item(0).Visible = True
        except Exception:
            try:
                hwp.XHwpWindows.Active_XHwpWindow.Visible = True
            except Exception:
                pass

        if not hwp.Open(str(template_hwp)):
            raise RuntimeError(f"템플릿 열기에 실패했습니다: {template_hwp}")

        if not repeat_find(hwp, SECTION_TITLE, from_top=True):
            raise RuntimeError(f"문서에서 '{SECTION_TITLE}' 섹션을 찾지 못했습니다.")
        hwp.HAction.Run("Cancel")

        for area in plan["areas"]:
            replace_next_text(hwp, AREA_TITLE_PLACEHOLDER, area["title"])
            move_to_next_area_table(hwp)
            fill_area_rows(hwp, area["rows"])

        output_hwp.parent.mkdir(parents=True, exist_ok=True)
        hwp.SaveAs(str(output_hwp))
    finally:
        try:
            hwp.Quit()
        except Exception:
            pass


def print_plan_summary(plan: dict[str, Any]) -> None:
    print(f"course: {plan['course']}")
    print(f"row_capacities: {plan['template']['row_capacities']}")
    for area in plan["areas"]:
        row_sizes = [len(row["codes"]) for row in area["rows"]]
        print(
            f"  ({area['index']}) {area['title']}: "
            f"{area['standard_count']}개 -> {area['row_capacity']}행 {row_sizes}"
        )


def main() -> int:
    args = parse_args()
    template_hwp = Path(args.template_hwp)
    achievement_json = Path(args.achievement_json)

    if not achievement_json.exists():
        print(f"[error] achievement JSON not found: {achievement_json}", file=sys.stderr)
        return 1
    if not args.preview_only and not template_hwp.exists():
        print(f"[error] template HWP not found: {template_hwp}", file=sys.stderr)
        return 1

    payload = load_achievement_json(achievement_json)
    course = args.course or infer_course_name(template_hwp, payload)
    template_pdf = detect_template_pdf(template_hwp, args.template_pdf)

    try:
        row_capacities = parse_row_capacities(args.area_row_capacities, template_pdf)
        output_hwp = Path(args.output) if args.output else build_default_output_path(template_hwp)
        plan = build_fill_plan(
            payload,
            template_hwp=template_hwp,
            template_pdf=template_pdf,
            course=course,
            row_capacities=row_capacities,
        )
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    plan_path = Path(args.plan_json) if args.plan_json else build_default_plan_path(output_hwp)
    write_plan_json(plan_path, plan)
    print(f"[ok] wrote fill plan: {plan_path}")
    print_plan_summary(plan)

    if args.preview_only:
        return 0

    try:
        automate_hwp_fill(template_hwp, output_hwp, plan)
    except Exception as exc:
        print(f"[error] HWP fill failed: {exc}", file=sys.stderr)
        return 1

    print(f"[ok] wrote output HWP: {output_hwp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
