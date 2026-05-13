#!/usr/bin/env python3
"""Extract curriculum achievement standards from text-based PDF files.

The target text is the left "성취기준" column in "성취기준별 성취수준"
tables. The script keeps only the achievement code and standard statement,
then writes a compact JSON file that can be used by later plan-generation
steps.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import fitz


CODE_RE = re.compile(r"\[([^\]\n]{2,50})\]")
BROKEN_PREFIX_CODE_RE = re.compile(r"(?<![가-힣])(?P<prefix>[가-힣]{1,4})\[(?P<body>\d[\d\s-]{4,24})\]")
STANDARD_CODE_PARTS_RE = re.compile(
    r"^(?P<grade>\d{1,2})(?P<subject>[가-힣]{1,6})(?P<area>\d{2})-(?P<sequence>\d{2})$"
)
AREA_RE = re.compile(r"^\((\d+)\)\s+(.{1,80})$")
COURSE_RE = re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+\s*\.?\s*(.+?)(?:\s+성취수준)?$")
GENERIC_COURSE_TITLES = {
    "교과별",
    "성취수준",
    "성취수준 활용",
    "성취수준 개발의 이해",
}
LEVEL_LABELS = ("A", "B", "C", "D", "E")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_space(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_code(code: str) -> str:
    return re.sub(r"\s+", "", code).strip()


def looks_like_standard_code(code: str) -> bool:
    normalized = normalize_code(code)
    return bool(re.search(r"\d", normalized) and re.search(r"\d{2}-\d{2}", normalized))


def split_standard_code(code: str) -> dict[str, str] | None:
    match = STANDARD_CODE_PARTS_RE.fullmatch(normalize_code(code))
    if not match:
        return None
    return match.groupdict()


def align_code_with_context(
    code: str,
    *,
    last_standard: dict[str, Any] | None,
    current_area_number: str,
) -> str:
    if not last_standard or not current_area_number:
        return code

    current_parts = split_standard_code(code)
    previous_parts = split_standard_code(last_standard.get("code", ""))
    if not current_parts or not previous_parts:
        return code

    if current_parts["area"] != current_area_number.zfill(2):
        return code
    if previous_parts["area"] != current_parts["area"]:
        return code
    if previous_parts["grade"] != current_parts["grade"]:
        return code
    if previous_parts["subject"] == current_parts["subject"]:
        return code

    previous_sequence = int(previous_parts["sequence"])
    current_sequence = int(current_parts["sequence"])
    if current_sequence != previous_sequence + 1:
        return code

    return (
        f"{current_parts['grade']}"
        f"{previous_parts['subject']}"
        f"{current_parts['area']}"
        f"-{current_parts['sequence']}"
    )


def block_lines(block: dict[str, Any]) -> list[str]:
    return [
        "".join(span.get("text", "") for span in line.get("spans", []))
        for line in block.get("lines", [])
    ]


def block_text(block: dict[str, Any], *, preserve_wrap_spaces: bool = False) -> str:
    lines = block_lines(block)
    if preserve_wrap_spaces:
        return "".join(lines)
    return "\n".join(line.rstrip() for line in lines)


def normalized_block_text(block: dict[str, Any]) -> str:
    return normalize_space(block_text(block))


def page_text_blocks(page: fitz.Page) -> list[dict[str, Any]]:
    return [
        block
        for block in page.get_text("dict", sort=True).get("blocks", [])
        if block.get("type") == 0
    ]


def exact_heading(text: str, kind: str) -> bool:
    text = normalize_space(text)
    if "·" in text or "." * 3 in text:
        return False
    compact = re.sub(r"\s+", "", text)
    if kind == "start":
        return bool(re.fullmatch(r"(?:1|가)\.?\s*성취기준별\s*성취수준", text)) or bool(
            re.fullmatch(r"(?:1|가)\.?성취기준별성취수준", compact)
        )
    return bool(
        re.fullmatch(r"[23]\s*\.?\s*(영역별\s*성취수준|예시\s*평가\s*도구|평가\s*도구)", text)
        or re.fullmatch(r"(?:[23]|[나다])\.?(?:영역별성취수준|예시평가도구|평가도구)", compact)
    )


def has_standard_table_header(blocks: list[dict[str, Any]]) -> bool:
    for block in blocks:
        text = normalized_block_text(block)
        if re.search(r"(교육과정\s*)?성취기준\s+성취기준별\s+성취수준", text):
            return True
        compact = re.sub(r"\s+", "", text)
        if "성취기준성취기준별성취수준" in compact:
            return True
    return False


def find_area_heading(blocks: list[dict[str, Any]]) -> tuple[str, str] | None:
    for block in blocks:
        for line in block_lines(block):
            text = normalize_space(line)
            match = AREA_RE.fullmatch(text)
            if not match:
                continue
            title = match.group(2).strip()
            if any(skip in title for skip in ("성취수준", "평가", "개발")):
                continue
            return match.group(1), title
    return None


def find_course_heading(blocks: list[dict[str, Any]]) -> str | None:
    for block in blocks:
        lines = [normalize_space(line) for line in block_lines(block) if normalize_space(line)]
        if len(lines) >= 2 and re.fullmatch(r"\d+", lines[0]):
            title = clean_course_title(lines[1])
            if title:
                return title

        for line in lines:
            text = normalize_space(line)
            if not text or any(skip in text for skip in ("교육과정", "성취기준", "목차")):
                continue
            match = COURSE_RE.fullmatch(text)
            if not match:
                continue
            title = clean_course_title(match.group(1))
            if not title:
                continue
            return title
    return None


def clean_course_title(title: str) -> str:
    title = normalize_space(title)
    title = re.sub(r"\s*성취수준$", "", title)
    title = re.sub(r"성취수준$", "", title)
    title = re.sub(r"^(초등학교|중학교|고등학교)", "", title)
    title = normalize_space(title)
    if not title or len(title) > 40 or title in GENERIC_COURSE_TITLES:
        return ""
    return title


def looks_like_course_heading_text(text: str) -> bool:
    match = COURSE_RE.fullmatch(normalize_space(text))
    if not match:
        return False
    return bool(clean_course_title(match.group(1)))


def infer_course_from_path(path: Path) -> str:
    haystack = " ".join([path.stem, *[part for part in path.parts[-3:-1]]])
    for pattern in (
        r"성취수준\(([^)]+)\)",
        r"평가기준\(([^)]+)\)",
        r"평가\s*기준\(([^)]+)\)",
    ):
        match = re.search(pattern, haystack)
        if match:
            value = normalize_space(match.group(1))
            if len(value) > 1 and value.endswith("과"):
                value = value[:-1]
            return value
    return ""


def code_prefix_for_course(course: str) -> str:
    compact = re.sub(r"\s+", "", course)
    if "한문" in compact:
        return "한"
    if "진로" in compact:
        return "진로"
    if "한국사" in compact or "역사" in compact:
        return "한사"
    return ""


def repair_broken_codes(text: str, default_prefix: str = "") -> str:
    def replace_prefixed(match: re.Match[str]) -> str:
        prefix = normalize_code(match.group("prefix"))
        body = normalize_code(match.group("body"))
        if not re.fullmatch(r"\d[\d-]+", body):
            return match.group(0)
        if body.startswith(("10", "11", "12")) and len(body) > 2:
            code = f"{body[:2]}{prefix}{body[2:]}"
        else:
            code = f"{body[0]}{prefix}{body[1:]}"
        return f"[{code}]"

    text = BROKEN_PREFIX_CODE_RE.sub(replace_prefixed, text)

    if default_prefix:
        text = re.sub(
            r"\[(?P<grade>[1-9])(?P<rest>\d{2}-\d{2})\]",
            lambda match: f"[{match.group('grade')}{default_prefix}{match.group('rest')}]",
            text,
        )
        text = re.sub(
            r"\[(?P<grade>1[0-2])(?P<rest>\d-\d{2}-\d{2})\]",
            lambda match: f"[{match.group('grade')}{default_prefix}{match.group('rest')}]",
            text,
        )
    return text


def is_left_column_candidate(block: dict[str, Any], page_width: float) -> bool:
    x0, _y0, x1, _y1 = block.get("bbox", (0, 0, page_width, 0))
    if x0 > page_width * 0.30:
        return False
    if x1 > page_width * 0.55:
        return False
    if (x1 - x0) > page_width * 0.42:
        return False

    text = repair_broken_codes(block_text(block, preserve_wrap_spaces=True))
    return any(looks_like_standard_code(match.group(1)) for match in CODE_RE.finditer(text))


def is_left_column_text_block(block: dict[str, Any], page_width: float) -> bool:
    x0, _y0, x1, _y1 = block.get("bbox", (0, 0, page_width, 0))
    if x0 > page_width * 0.30:
        return False
    if x1 > page_width * 0.55:
        return False
    if (x1 - x0) > page_width * 0.42:
        return False

    text = normalize_space(block_text(block, preserve_wrap_spaces=True))
    if not text or CODE_RE.search(text):
        return False
    if re.fullmatch(r"\d+|[A-E](?:\s+[A-E])*", text):
        return False
    if exact_heading(text, "start") or exact_heading(text, "stop"):
        return False
    if has_standard_table_header([block]):
        return False
    if AREA_RE.fullmatch(text):
        return False
    return True


def right_column_min_x(page_width: float) -> float:
    return page_width * 0.30


def block_first_line(block: dict[str, Any]) -> str:
    lines = block_lines(block)
    for line in lines:
        text = normalize_space(line)
        if text:
            return text
    return ""


def text_first_line(text: str) -> str:
    for line in text.splitlines():
        normalized = normalize_space(line)
        if normalized:
            return normalized
    return ""


def text_contains_level_label(text: str) -> bool:
    return any(normalize_space(line) in LEVEL_LABELS for line in text.splitlines())


def level_label_for_block(block: dict[str, Any], page_width: float) -> str:
    x0, _y0, _x1, _y1 = block.get("bbox", (0, 0, page_width, 0))
    if x0 < right_column_min_x(page_width):
        return ""

    first_line = block_first_line(block)
    if first_line in LEVEL_LABELS:
        return first_line
    return ""


def strip_level_prefix(text: str, label: str) -> str:
    text = text.lstrip()
    text = re.sub(rf"^{re.escape(label)}(?:\s+|\n+)?", "", text, count=1)
    return text.strip()


def collect_band_text(
    blocks: list[dict[str, Any]],
    *,
    label: str,
    start_y: float,
    end_y: float,
    min_x: float,
) -> str:
    parts: list[tuple[float, float, str]] = []
    for block in blocks:
        x0, y0, _x1, y1 = block.get("bbox", (0, 0, 0, 0))
        center_y = (y0 + y1) / 2
        if x0 < min_x or center_y <= start_y or center_y >= end_y:
            continue

        raw_text = block_text(block)
        normalized = normalize_space(raw_text)
        if not normalized:
            continue
        if re.fullmatch(r"\d+", normalized):
            continue
        if looks_like_course_heading_text(normalized):
            continue

        first_line = block_first_line(block)
        if first_line == label and normalized == label:
            continue
        if first_line in LEVEL_LABELS and first_line != label:
            continue

        cleaned = strip_level_prefix(raw_text, label) if first_line == label else raw_text.strip()
        if cleaned:
            parts.append((y0, x0, cleaned))

    if not parts:
        return ""

    parts.sort(key=lambda item: (item[0], item[1]))
    return normalize_space(" ".join(text for _y, _x, text in parts))


def extract_page_level_clusters(blocks: list[dict[str, Any]], page_width: float, page_height: float) -> list[dict[str, Any]]:
    label_blocks: list[tuple[str, dict[str, Any]]] = []
    for block in blocks:
        label = level_label_for_block(block, page_width)
        if label:
            label_blocks.append((label, block))

    clusters: list[list[tuple[str, dict[str, Any]]]] = []
    current: list[tuple[str, dict[str, Any]]] = []
    expected_index = 0

    for label, block in label_blocks:
        expected_label = LEVEL_LABELS[expected_index]
        if label == "A":
            if current and len(current) == len(LEVEL_LABELS):
                clusters.append(current)
            current = [(label, block)]
            expected_index = 1
            continue

        if not current:
            continue
        if label != expected_label:
            current = []
            expected_index = 0
            continue

        current.append((label, block))
        expected_index += 1
        if expected_index == len(LEVEL_LABELS):
            expected_index = len(LEVEL_LABELS) - 1

    if current and len(current) == len(LEVEL_LABELS):
        clusters.append(current)

    extracted: list[dict[str, Any]] = []
    for index, cluster in enumerate(clusters):
        label_map = {label: block for label, block in cluster}
        next_cluster = clusters[index + 1] if index + 1 < len(clusters) else None
        next_cluster_start_y = next_cluster[0][1]["bbox"][1] if next_cluster else page_height

        levels: dict[str, str] = {}
        for label_index, label in enumerate(LEVEL_LABELS):
            block = label_map[label]
            current_y0 = block["bbox"][1]
            prev_y1 = cluster[label_index - 1][1]["bbox"][3] if label_index else max(0.0, current_y0 - 30.0)
            next_y0 = cluster[label_index + 1][1]["bbox"][1] if label_index + 1 < len(LEVEL_LABELS) else next_cluster_start_y
            text = collect_band_text(
                blocks,
                label=label,
                start_y=prev_y1,
                end_y=next_y0,
                min_x=right_column_min_x(page_width),
            )
            levels[label] = text

        extracted.append(
            {
                "bbox": [
                    round(min(label_map[label]["bbox"][0] for label in LEVEL_LABELS), 2),
                    round(min(label_map[label]["bbox"][1] for label in LEVEL_LABELS), 2),
                    round(max(label_map[label]["bbox"][2] for label in LEVEL_LABELS), 2),
                    round(max(label_map[label]["bbox"][3] for label in LEVEL_LABELS), 2),
                ],
                "achievement_levels": levels,
            }
        )

    return extracted


def collect_active_right_blocks(
    page_index: int,
    blocks: list[dict[str, Any]],
    page_width: float,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    min_x = right_column_min_x(page_width)
    for block in blocks:
        x0, y0, x1, y1 = block.get("bbox", (0, 0, 0, 0))
        if x0 < min_x:
            continue

        raw_text = block_text(block)
        normalized = normalize_space(raw_text)
        if not normalized or re.fullmatch(r"\d+", normalized):
            continue
        if looks_like_course_heading_text(normalized):
            continue

        entries.append(
            {
                "page_index": page_index,
                "bbox": (x0, y0, x1, y1),
                "raw_text": raw_text,
                "first_line": text_first_line(raw_text),
            }
        )
    return entries


def parse_levels_from_entries(entries: list[dict[str, Any]]) -> dict[str, str]:
    lines: list[str] = []
    for entry in entries:
        for line in entry["raw_text"].splitlines():
            normalized = normalize_space(line)
            if not normalized or re.fullmatch(r"\d+", normalized):
                continue
            lines.append(normalized)

    buckets: dict[str, list[str]] = {label: [] for label in LEVEL_LABELS}
    prefix: list[str] = []
    current_label: str | None = None

    for line in lines:
        exact_match = re.fullmatch(r"([A-E])(?:\s+(.*))?", line)
        if exact_match:
            current_label = exact_match.group(1)
            remainder = normalize_space(exact_match.group(2) or "")
            if remainder:
                buckets[current_label].append(remainder)
            continue

        if current_label is None:
            prefix.append(line)
        else:
            buckets[current_label].append(line)

    if prefix:
        buckets["A"] = prefix + buckets["A"]

    return {label: normalize_space(" ".join(buckets[label])) for label in LEVEL_LABELS}


def extract_document_level_clusters(active_right_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    a_start_indices = [
        index
        for index, entry in enumerate(active_right_blocks)
        if entry["first_line"] == "A"
    ]

    clusters: list[dict[str, Any]] = []
    for position, start_index in enumerate(a_start_indices):
        end_index = a_start_indices[position + 1] if position + 1 < len(a_start_indices) else len(active_right_blocks)
        region_entries = active_right_blocks[start_index:end_index]

        if start_index > 0:
            previous = active_right_blocks[start_index - 1]
            current = active_right_blocks[start_index]
            if (
                previous["page_index"] == current["page_index"]
                and not text_contains_level_label(previous["raw_text"])
                and current["bbox"][1] - previous["bbox"][3] <= 32
            ):
                region_entries = [previous] + region_entries

        levels = parse_levels_from_entries(region_entries)
        if any(levels.values()):
            clusters.append({"achievement_levels": levels})

    return clusters


def strip_non_standard_suffix(text: str) -> str:
    text = re.split(r"\s*<\s*탐구\s*활동\s*>", text, maxsplit=1)[0]
    text = re.split(r"\s*성취기준\s*해설", text, maxsplit=1)[0]
    text = re.split(r"\s*성취기준\s*적용\s*시\s*고려\s*사항", text, maxsplit=1)[0]
    return text


def split_standards_from_block(text: str, *, default_code_prefix: str = "") -> list[dict[str, str]]:
    text = repair_broken_codes(text, default_code_prefix)
    text = strip_non_standard_suffix(text)
    matches = [match for match in CODE_RE.finditer(text) if looks_like_standard_code(match.group(1))]
    standards: list[dict[str, str]] = []

    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end() : next_start]
        code = normalize_code(match.group(1))
        statement = normalize_space(segment)
        statement = re.sub(r"^[\s:：,，ㆍ·-]+", "", statement).strip()
        if not statement:
            continue
        standards.append(
            {
                "code": code,
                "statement": statement,
                "text": f"[{code}] {statement}",
            }
        )

    return standards


def append_continuation(standard: dict[str, Any], raw_text: str) -> bool:
    if standard["statement"].rstrip().endswith("다."):
        return False

    continuation = normalize_space(strip_non_standard_suffix(raw_text))
    continuation = re.sub(r"^[\s:：,，ㆍ·-]+", "", continuation).strip()
    if not continuation:
        return False
    if continuation.startswith(("성취기준", "교육과정", "영역", "평가")):
        return False

    statement = normalize_space(f"{standard['statement']} {continuation}")
    standard["statement"] = statement
    standard["text"] = f"[{standard['code']}] {statement}"
    return True


def extract_from_pdf(path: Path, *, loose: bool = False) -> dict[str, Any]:
    standards: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    last_standard: dict[str, Any] | None = None
    current_course = infer_course_from_path(path)
    current_area_number = ""
    current_area = ""
    active = loose
    pending_start_pages = 0
    parser_mode = "loose_left_column" if loose else "strict_standard_section"
    active_right_blocks: list[dict[str, Any]] = []

    with fitz.open(path) as document:
        metadata = dict(document.metadata or {})
        page_count = document.page_count
        for page_index, page in enumerate(document):
            blocks = page_text_blocks(page)
            page_level_clusters = extract_page_level_clusters(blocks, page.rect.width, page.rect.height)
            page_level_index = 0
            block_texts = [normalized_block_text(block) for block in blocks]
            has_start = any(exact_heading(text, "start") for text in block_texts)
            has_stop = any(exact_heading(text, "stop") for text in block_texts)
            has_table = has_standard_table_header(blocks)

            course = find_course_heading(blocks)
            if course:
                current_course = course

            area = find_area_heading(blocks)
            if area:
                current_area_number, current_area = area
                last_standard = None

            if not loose:
                if has_start and not has_table:
                    pending_start_pages = 3
                elif pending_start_pages:
                    pending_start_pages -= 1

                if has_table and (has_start or active or pending_start_pages):
                    active = True
                    pending_start_pages = 0

                if has_stop and not has_start and not has_table:
                    active = False
                    pending_start_pages = 0
                    last_standard = None

            if not active:
                continue

            active_right_blocks.extend(collect_active_right_blocks(page_index, blocks, page.rect.width))

            for block in blocks:
                if last_standard is not None and is_left_column_text_block(block, page.rect.width):
                    raw_text = block_text(block, preserve_wrap_spaces=True)
                    if append_continuation(last_standard, raw_text):
                        continue

                if not is_left_column_candidate(block, page.rect.width):
                    continue

                raw_text = block_text(block, preserve_wrap_spaces=True)
                prefix = code_prefix_for_course(current_course)
                for item in split_standards_from_block(raw_text, default_code_prefix=prefix):
                    code = align_code_with_context(
                        item["code"],
                        last_standard=last_standard,
                        current_area_number=current_area_number,
                    )
                    item["code"] = code
                    item["text"] = f"[{code}] {item['statement']}"
                    if code in seen_codes:
                        continue
                    seen_codes.add(code)
                    standard = {
                        **item,
                        "course": current_course,
                        "area_number": current_area_number,
                        "area": current_area,
                        "page_number": page_index + 1,
                        "bbox": [round(value, 2) for value in block.get("bbox", ())],
                        "achievement_levels": {label: "" for label in LEVEL_LABELS},
                    }
                    if page_level_index < len(page_level_clusters):
                        standard["achievement_levels"] = page_level_clusters[page_level_index]["achievement_levels"]
                        standard["achievement_levels_bbox"] = page_level_clusters[page_level_index]["bbox"]
                    page_level_index += 1
                    standards.append(standard)
                    last_standard = standard

    document_level_clusters = extract_document_level_clusters(active_right_blocks)
    if len(document_level_clusters) == len(standards):
        for standard, cluster in zip(standards, document_level_clusters):
            standard["achievement_levels"] = cluster["achievement_levels"]

    return {
        "schema": "achievement_standards_v2",
        "source": {
            "path": str(path),
            "filename": path.name,
            "extension": path.suffix.lower().lstrip("."),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        },
        "extraction": {
            "created_at": now_iso(),
            "parser": "pymupdf_left_column_with_levels_extractor",
            "mode": parser_mode,
            "page_count": page_count,
            "standard_count": len(standards),
            "pdf_metadata": metadata,
        },
        "achievement_standards": standards,
    }


def output_path_for(input_path: Path, output_root: Path, base_root: Path | None = None) -> Path:
    try:
        relative = input_path.resolve().relative_to((base_root or Path.cwd()).resolve())
    except ValueError:
        relative = Path(input_path.name)

    if relative.parts and relative.parts[0] == "pdf":
        relative = Path(*relative.parts[1:])
    return output_root / relative.with_suffix(".achievement_standards.json")


def collect_inputs(paths: list[Path]) -> list[Path]:
    pdfs: list[Path] = []
    for path in paths:
        if path.is_dir():
            pdfs.extend(sorted(item for item in path.rglob("*.pdf") if item.is_file()))
        elif path.is_file() and path.suffix.lower() == ".pdf":
            pdfs.append(path)
        else:
            raise FileNotFoundError(f"PDF file or directory not found: {path}")
    return pdfs


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="PDF file(s) or directory path(s) to scan recursively.")
    parser.add_argument(
        "--output-root",
        default="json/achievement_standards",
        help="Directory where extracted JSON files are written.",
    )
    parser.add_argument(
        "--base-root",
        default=".",
        help="Base path used to mirror input directories under output-root.",
    )
    parser.add_argument(
        "--loose",
        action="store_true",
        help="Scan every left-column code block, even outside strict 성취기준별 성취수준 sections.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_paths = [Path(value) for value in args.inputs]
    output_root = Path(args.output_root)
    base_root = Path(args.base_root)

    try:
        pdfs = collect_inputs(input_paths)
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    if not pdfs:
        print("[error] no PDF files found", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = []
    for index, pdf in enumerate(pdfs, start=1):
        try:
            payload = extract_from_pdf(pdf, loose=args.loose)
            output_path = output_path_for(pdf, output_root, base_root)
            write_json(output_path, payload)
            count = payload["extraction"]["standard_count"]
            print(f"[{index}/{len(pdfs)}] wrote {count:4d} standards: {output_path}", flush=True)
            results.append({"status": "ok", "source": str(pdf), "output": str(output_path), "count": count})
        except Exception as exc:
            print(f"[{index}/{len(pdfs)}] failed: {pdf} ({exc})", file=sys.stderr, flush=True)
            results.append({"status": "failed", "source": str(pdf), "error": str(exc)})

    if len(pdfs) > 1:
        index_path = output_root / "index.json"
        write_json(
            index_path,
            {
                "schema": "achievement_standards_index_v1",
                "generated_at": now_iso(),
                "summary": {
                    "pdf_count": len(pdfs),
                    "ok": sum(1 for item in results if item["status"] == "ok"),
                    "failed": sum(1 for item in results if item["status"] == "failed"),
                    "standard_count": sum(item.get("count", 0) for item in results),
                },
                "documents": results,
            },
        )
        print(f"[done] wrote index: {index_path}", flush=True)

    return 1 if any(item["status"] == "failed" for item in results) else 0


if __name__ == "__main__":
    sys.exit(main())
