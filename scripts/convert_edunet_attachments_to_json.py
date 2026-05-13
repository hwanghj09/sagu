#!/usr/bin/env python3
"""Convert downloaded Edunet PDF/HWP/HWPX attachments into JSON.

The script reads the downloader manifest, converts every successfully downloaded
attachment, and writes one JSON file per source document plus an index.json.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import fitz
import olefile


TAG_NAMES = {
    16: "HWPTAG_DOCUMENT_PROPERTIES",
    17: "HWPTAG_ID_MAPPINGS",
    18: "HWPTAG_BIN_DATA",
    19: "HWPTAG_FACE_NAME",
    20: "HWPTAG_BORDER_FILL",
    21: "HWPTAG_CHAR_SHAPE",
    22: "HWPTAG_TAB_DEF",
    23: "HWPTAG_NUMBERING",
    24: "HWPTAG_BULLET",
    25: "HWPTAG_PARA_SHAPE",
    26: "HWPTAG_STYLE",
    27: "HWPTAG_DOC_DATA",
    28: "HWPTAG_DISTRIBUTE_DOC_DATA",
    30: "HWPTAG_COMPATIBLE_DOCUMENT",
    31: "HWPTAG_LAYOUT_COMPATIBILITY",
    32: "HWPTAG_TRACKCHANGE",
    33: "HWPTAG_MEMO_SHAPE",
    66: "HWPTAG_PARA_HEADER",
    67: "HWPTAG_PARA_TEXT",
    68: "HWPTAG_PARA_CHAR_SHAPE",
    69: "HWPTAG_PARA_LINE_SEG",
    70: "HWPTAG_PARA_RANGE_TAG",
    71: "HWPTAG_CTRL_HEADER",
    72: "HWPTAG_LIST_HEADER",
    73: "HWPTAG_PAGE_DEF",
    74: "HWPTAG_FOOTNOTE_SHAPE",
    75: "HWPTAG_PAGE_BORDER_FILL",
    76: "HWPTAG_SHAPE_COMPONENT",
    77: "HWPTAG_TABLE",
    78: "HWPTAG_SHAPE_COMPONENT_LINE",
    79: "HWPTAG_SHAPE_COMPONENT_RECTANGLE",
    80: "HWPTAG_SHAPE_COMPONENT_ELLIPSE",
    81: "HWPTAG_SHAPE_COMPONENT_ARC",
    82: "HWPTAG_SHAPE_COMPONENT_POLYGON",
    83: "HWPTAG_SHAPE_COMPONENT_CURVE",
    84: "HWPTAG_SHAPE_COMPONENT_OLE",
    85: "HWPTAG_SHAPE_COMPONENT_PICTURE",
    86: "HWPTAG_SHAPE_COMPONENT_CONTAINER",
    87: "HWPTAG_CTRL_DATA",
    88: "HWPTAG_EQEDIT",
    89: "HWPTAG_SHAPE_COMPONENT_TEXTART",
    90: "HWPTAG_FORM_OBJECT",
}

CONTROL_NAMES = {
    0x00: "NULL",
    0x01: "CTLCHR01",
    0x02: "SECTION_COLUMN_DEF",
    0x03: "FIELD_START",
    0x04: "FIELD_END",
    0x05: "CTLCHR05",
    0x06: "CTLCHR06",
    0x07: "CTLCHR07",
    0x08: "TITLE_MARK",
    0x09: "TAB",
    0x0A: "LINE_BREAK",
    0x0B: "DRAWING_TABLE_OBJECT",
    0x0C: "CTLCHR0C",
    0x0D: "PARAGRAPH_BREAK",
    0x0E: "CTLCHR0E",
    0x0F: "HIDDEN_EXPLANATION",
    0x10: "HEADER_FOOTER",
    0x11: "FOOT_END_NOTE",
    0x12: "AUTO_NUMBER",
    0x13: "CTLCHR13",
    0x14: "CTLCHR14",
    0x15: "PAGE_CTLCHR",
    0x16: "BOOKMARK",
    0x17: "CTLCHR17",
    0x18: "HYPHEN",
    0x1E: "NONBREAK_SPACE",
    0x1F: "FIXWIDTH_SPACE",
}

CONTROL_UNIT_SIZES = {
    0x00: 1,
    0x01: 8,
    0x02: 8,
    0x03: 8,
    0x04: 8,
    0x05: 8,
    0x06: 8,
    0x07: 8,
    0x08: 8,
    0x09: 8,
    0x0A: 1,
    0x0B: 8,
    0x0C: 8,
    0x0D: 1,
    0x0E: 8,
    0x0F: 8,
    0x10: 8,
    0x11: 8,
    0x12: 8,
    0x13: 8,
    0x14: 8,
    0x15: 8,
    0x16: 8,
    0x17: 8,
    0x18: 1,
    0x1E: 1,
    0x1F: 1,
}

SUBJECT_PATTERNS = [
    "진로와 직업",
    "생활외국어",
    "기술가정",
    "기술·가정",
    "제2외국어",
    "문예 창작",
    "러시아어",
    "베트남어",
    "스페인어",
    "프랑스어",
    "중국어",
    "일본어",
    "독일어",
    "아랍어",
    "국제 계열",
    "외국어 계열",
    "외국어·국제계열",
    "외국어 국제 계열",
    "과학 계열",
    "예술 계열",
    "체육 계열",
    "교양 교과",
    "교양과",
    "국어",
    "수학",
    "영어",
    "사회",
    "역사",
    "도덕",
    "과학",
    "체육",
    "음악",
    "미술",
    "정보",
    "한문",
    "보건",
    "환경",
    "무용",
    "연극",
    "영화",
    "사진",
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_uint16(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        return 0
    return data[offset] | (data[offset + 1] << 8)


def read_uint32(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        return 0
    return (
        data[offset]
        | (data[offset + 1] << 8)
        | (data[offset + 2] << 16)
        | (data[offset + 3] << 24)
    ) & 0xFFFFFFFF


def bytes_to_hex(data: bytes) -> str:
    return data.hex()


def decode_utf16(data: bytes) -> str:
    return data.decode("utf-16le", errors="replace")


def decode_latin1(data: bytes) -> str:
    return data.decode("latin-1", errors="replace")


def hwp_control_to_text(code: int) -> str:
    if code in (0x0A, 0x0D):
        return "\n"
    if code == 0x09:
        return "\t"
    if code == 0x18:
        return "-"
    if code in (0x1E, 0x1F):
        return " "
    return ""


def hwp_find_control_pos(data: bytes, start: int) -> int:
    index = start
    while index + 1 < len(data):
        code = read_uint16(data, index)
        if 0 <= code <= 0x1F and code in CONTROL_UNIT_SIZES:
            return index
        index += 2
    return len(data)


def hwp_parse_para_text(payload: bytes) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    rendered_parts: list[str] = []
    index = 0

    while index < len(payload):
        ctrl_pos = hwp_find_control_pos(payload, index)
        if index < ctrl_pos:
            text = decode_utf16(payload[index:ctrl_pos])
            chunks.append(
                {
                    "type": "text",
                    "start_char": index // 2,
                    "end_char": ctrl_pos // 2,
                    "value": text,
                }
            )
            text_parts.append(text)
            rendered_parts.append(text)

        if ctrl_pos < len(payload):
            code = read_uint16(payload, ctrl_pos)
            unit_size = CONTROL_UNIT_SIZES.get(code, 1)
            end = min(len(payload), ctrl_pos + unit_size * 2)
            control: dict[str, Any] = {
                "type": "control",
                "start_char": ctrl_pos // 2,
                "end_char": end // 2,
                "code": code,
                "name": CONTROL_NAMES.get(code, f"CTLCHR{code:02X}"),
            }
            if unit_size == 8 and end - ctrl_pos >= 16:
                if code == 0x09:
                    control["param"] = {
                        "width": read_uint32(payload, ctrl_pos + 2),
                        "unknown0": payload[ctrl_pos + 6],
                        "unknown1": payload[ctrl_pos + 7],
                        "unknown2_hex": bytes_to_hex(payload[ctrl_pos + 8 : ctrl_pos + 16]),
                    }
                else:
                    control["chid"] = decode_latin1(payload[ctrl_pos + 2 : ctrl_pos + 6])
                    control["param_hex"] = bytes_to_hex(payload[ctrl_pos + 6 : ctrl_pos + 16])
            chunks.append(control)
            rendered_parts.append(hwp_control_to_text(code))
            index = end
        else:
            index = len(payload)

    rendered = "".join(rendered_parts)
    if rendered.endswith("\n"):
        rendered = rendered[:-1]
    return {
        "chunks": chunks,
        "text": "".join(text_parts),
        "rendered_text": rendered,
    }


def hwp_parse_para_header(payload: bytes) -> dict[str, Any]:
    if len(payload) < 22:
        return {"raw_hex": bytes_to_hex(payload)}
    text_field = read_uint32(payload, 0)
    return {
        "text_field_raw": text_field,
        "char_count": text_field & 0x7FFFFFFF,
        "unknown_high_bit": bool(text_field & 0x80000000),
        "controlmask": read_uint32(payload, 4),
        "parashape_id": read_uint16(payload, 8),
        "style_id": payload[10],
        "split": payload[11],
        "charshapes_count": read_uint16(payload, 12),
        "rangetags_count": read_uint16(payload, 14),
        "linesegs_count": read_uint16(payload, 16),
        "instance_id": read_uint32(payload, 18),
    }


def hwp_read_record(data: bytes, position: int) -> tuple[dict[str, Any], int]:
    start = position
    header_value = read_uint32(data, position)
    position += 4
    tag_id = header_value & 0x3FF
    level = (header_value >> 10) & 0x3FF
    size = (header_value >> 20) & 0xFFF
    header_size = 4
    if size == 0xFFF:
        size = read_uint32(data, position)
        position += 4
        header_size = 8
    payload = data[position : position + size]
    position += size
    return (
        {
            "start_offset": start,
            "end_offset": position,
            "header_size": header_size,
            "tag_id": tag_id,
            "tag_name": TAG_NAMES.get(tag_id, f"UNKNOWN_TAG_{tag_id}"),
            "level": level,
            "size": size,
            "payload": payload,
            "header_value_hex": f"{header_value:08x}",
        },
        position,
    )


def convert_hwp(path: Path) -> dict[str, Any]:
    with olefile.OleFileIO(str(path)) as ole:
        file_header = ole.openstream("FileHeader").read()
        signature = decode_latin1(file_header[:32]).rstrip("\x00")
        if not signature.startswith("HWP Document File"):
            raise ValueError("HWP 5.x signature not found")
        version = list(file_header[32:36])
        flags = read_uint32(file_header, 36)
        compressed = bool(flags & 1)
        password = bool(flags & 2)
        distributable = bool(flags & 4)
        if password:
            raise ValueError("password-protected HWP is not supported")

        section_paths = [
            item
            for item in ole.listdir(streams=True, storages=False)
            if len(item) >= 2 and item[-2].lower() == "bodytext" and re.match(r"section\d+$", item[-1], re.I)
        ]
        section_paths.sort(key=lambda item: int(re.search(r"\d+$", item[-1]).group(0)))

        sections: list[dict[str, Any]] = []
        all_paragraphs: list[dict[str, Any]] = []
        rendered_paragraphs: list[str] = []
        record_counts: dict[str, int] = {}
        para_text_record_count = 0
        text_chunk_count = 0
        control_chunk_count = 0
        text_character_count = 0

        for section_index, section_path in enumerate(section_paths):
            raw = ole.openstream(section_path).read()
            decompressed = zlib.decompress(raw, -15) if compressed else raw
            section_record_counts: dict[str, int] = {}
            section = {
                "section_index": section_index,
                "stream_path": "/".join(section_path),
                "compressed_size_bytes": len(raw),
                "decompressed_size_bytes": len(decompressed),
                "paragraphs": [],
                "record_counts": section_record_counts,
            }

            position = 0
            seqno = 0
            current: dict[str, Any] | None = None
            while position + 4 <= len(decompressed):
                record, position = hwp_read_record(decompressed, position)
                tag_name = record["tag_name"]
                section_record_counts[tag_name] = section_record_counts.get(tag_name, 0) + 1
                record_counts[tag_name] = record_counts.get(tag_name, 0) + 1

                if record["tag_id"] == 66:
                    if current is not None:
                        section["paragraphs"].append(current)
                        all_paragraphs.append(current)
                        rendered_paragraphs.append(current.get("rendered_text") or "")
                    current = {
                        "global_paragraph_index": len(all_paragraphs),
                        "section_index": section_index,
                        "section_paragraph_index": len(section["paragraphs"]),
                        "para_header_record_seqno": seqno,
                        "para_header_level": record["level"],
                        "para_header_size": record["size"],
                        "para_header": hwp_parse_para_header(record["payload"]),
                        "para_text_record_seqno": None,
                        "para_text_level": None,
                        "para_text_size": None,
                        "chunks": [],
                        "text": "",
                        "rendered_text": "",
                    }
                elif record["tag_id"] == 67:
                    para_text_record_count += 1
                    parsed = hwp_parse_para_text(record["payload"])
                    text_chunk_count += sum(1 for chunk in parsed["chunks"] if chunk["type"] == "text")
                    control_chunk_count += sum(1 for chunk in parsed["chunks"] if chunk["type"] == "control")
                    text_character_count += len(parsed["text"])

                    if current is None:
                        current = {
                            "global_paragraph_index": len(all_paragraphs),
                            "section_index": section_index,
                            "section_paragraph_index": len(section["paragraphs"]),
                            "para_header_record_seqno": None,
                            "para_header_level": None,
                            "para_header_size": None,
                            "para_header": None,
                        }
                    current["para_text_record_seqno"] = seqno
                    current["para_text_level"] = record["level"]
                    current["para_text_size"] = record["size"]
                    current["chunks"] = parsed["chunks"]
                    current["text"] = parsed["text"]
                    current["rendered_text"] = parsed["rendered_text"]
                seqno += 1

            if current is not None:
                section["paragraphs"].append(current)
                all_paragraphs.append(current)
                rendered_paragraphs.append(current.get("rendered_text") or "")

            sections.append(section)

    plain_text = "\n".join(rendered_paragraphs)
    nonempty_lines = nonempty_text_lines(plain_text)
    return {
        "parser": "hwp5_ole_bodytext_parser",
        "hwp": {
            "signature": signature,
            "version_tuple": version,
            "flags": {
                "raw": flags,
                "compressed": compressed,
                "password": password,
                "distributable": distributable,
            },
        },
        "counts": {
            "section_count": len(sections),
            "paragraph_count": len(all_paragraphs),
            "para_text_record_count": para_text_record_count,
            "text_chunk_count": text_chunk_count,
            "control_chunk_count": control_chunk_count,
            "text_character_count": text_character_count,
            "plain_text_character_count": len(plain_text),
            "nonempty_plain_text_line_count": len(nonempty_lines),
            "record_counts": record_counts,
        },
        "plain_text": plain_text,
        "plain_text_nonempty_lines": nonempty_lines,
        "paragraphs": slim_paragraphs(all_paragraphs),
        "bodytext_sections": sections,
    }


def slim_paragraphs(paragraphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "global_paragraph_index": item.get("global_paragraph_index"),
            "section_index": item.get("section_index"),
            "section_paragraph_index": item.get("section_paragraph_index"),
            "text": item.get("text", ""),
            "rendered_text": item.get("rendered_text", ""),
        }
        for item in paragraphs
    ]


def convert_hwpx(path: Path) -> dict[str, Any]:
    paragraphs: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name
            for name in archive.namelist()
            if re.search(r"(^|/)section\d+\.xml$", name, re.I)
        )
        for section_index, name in enumerate(names):
            root = ElementTree.fromstring(archive.read(name))
            section_paragraph_index = 0
            for elem in root.iter():
                if not str(elem.tag).endswith("}p") and elem.tag != "p":
                    continue
                texts = [
                    child.text or ""
                    for child in elem.iter()
                    if str(child.tag).endswith("}t") or child.tag == "t"
                ]
                rendered = "".join(texts).strip()
                if rendered:
                    paragraphs.append(
                        {
                            "global_paragraph_index": len(paragraphs),
                            "section_index": section_index,
                            "section_paragraph_index": section_paragraph_index,
                            "text": rendered,
                            "rendered_text": rendered,
                        }
                    )
                    section_paragraph_index += 1

    plain_text = "\n".join(item["rendered_text"] for item in paragraphs)
    lines = nonempty_text_lines(plain_text)
    return {
        "parser": "hwpx_zip_xml_parser",
        "counts": {
            "section_count": len({item["section_index"] for item in paragraphs}),
            "paragraph_count": len(paragraphs),
            "plain_text_character_count": len(plain_text),
            "nonempty_plain_text_line_count": len(lines),
        },
        "plain_text": plain_text,
        "plain_text_nonempty_lines": lines,
        "paragraphs": paragraphs,
    }


def convert_pdf(path: Path) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    with fitz.open(path) as document:
        metadata = dict(document.metadata or {})
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            text = page.get_text("text", sort=True)
            pages.append(
                {
                    "page_number": page_index + 1,
                    "text": text.rstrip(),
                    "character_count": len(text),
                }
            )
        try:
            toc = document.get_toc(simple=True)
        except Exception:
            toc = []

    plain_text = "\n\n".join(page["text"] for page in pages)
    lines = nonempty_text_lines(plain_text)
    return {
        "parser": "pymupdf_text_extractor",
        "pdf": {
            "metadata": metadata,
            "toc": [{"level": row[0], "title": row[1], "page": row[2]} for row in toc],
        },
        "counts": {
            "page_count": len(pages),
            "plain_text_character_count": len(plain_text),
            "nonempty_plain_text_line_count": len(lines),
        },
        "plain_text": plain_text,
        "plain_text_nonempty_lines": lines,
        "pages": pages,
    }


def nonempty_text_lines(text: str) -> list[str]:
    return [line.strip() for line in re.split(r"\r?\n", text) if line.strip()]


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extension_from_path(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def board_value_maps(manifest: dict[str, Any]) -> dict[int, dict[tuple[str, str], str]]:
    maps: dict[int, dict[tuple[str, str], str]] = {}
    for board_entry in manifest.get("boards", []):
        menu_id = int(board_entry["menu_id"])
        attrs = board_entry.get("board", {}).get("bbsClsfBscAtrbVlInfoList") or []
        value_map: dict[tuple[str, str], str] = {}
        for attr in attrs:
            value_map[(str(attr.get("bbsAtrbId")), str(attr.get("bbsAtrbVlId")))] = str(
                attr.get("stngVl") or ""
            )
        maps[menu_id] = value_map
    return maps


def decode_field_values(plan: dict[str, Any], value_maps: dict[int, dict[tuple[str, str], str]]) -> dict[str, str]:
    decoded: dict[str, str] = {}
    field_value = (plan.get("post") or {}).get("fieldVal") or ""
    menu_id = int(plan.get("menu_id"))
    value_map = value_maps.get(menu_id, {})
    for raw in str(field_value).split(","):
        parts = raw.split(":")
        if len(parts) < 4:
            continue
        attr_id, value_id, field_name, _visible = parts[:4]
        decoded[field_name] = value_map.get((attr_id, value_id), value_id)
    return decoded


def infer_document_tags(plan: dict[str, Any], decoded_fields: dict[str, str]) -> dict[str, Any]:
    board_name = str(plan.get("board_name") or "")
    title = str((plan.get("post") or {}).get("title") or "")
    file_name = str((plan.get("file") or {}).get("fileLgcNm") or "")
    haystack = f"{board_name} {title} {file_name}"

    curriculum = ""
    curriculum_match = re.search(r"(20\d{2})\s*개정", haystack)
    if curriculum_match:
        curriculum = f"{curriculum_match.group(1)} 개정"

    school_level = decoded_fields.get("schoolGradeSe", "")
    if not school_level:
        if re.search(r"\(초\)|초등|초\)", haystack):
            school_level = "초등학교"
        elif re.search(r"\(중\)|중등|중학교|중\)", haystack):
            school_level = "중학교"
        elif re.search(r"\(고\)|고등|고등학교|고\)", haystack):
            school_level = "고등학교"

    subjects = []
    normalized = haystack.replace(" ", "")
    for subject in SUBJECT_PATTERNS:
        if subject in haystack or subject.replace(" ", "") in normalized:
            subjects.append(subject)

    return {
        "curriculum": curriculum,
        "school_level": school_level,
        "subjects": subjects,
        "primary_subject": subjects[0] if subjects else "",
        "decoded_fields": decoded_fields,
    }


def output_path_for(source_path: Path, input_root: Path, output_root: Path) -> Path:
    relative = source_path.relative_to(input_root)
    return output_root / relative.with_suffix(".json")


def convert_one(
    plan: dict[str, Any],
    *,
    input_root: Path,
    output_root: Path,
    value_maps: dict[int, dict[tuple[str, str], str]],
    force: bool,
) -> dict[str, Any]:
    source_path = Path(plan["target_path"])
    if not source_path.exists():
        return {
            "status": "failed",
            "source_path": str(source_path),
            "error": "source file does not exist",
        }

    json_path = output_path_for(source_path, input_root, output_root)
    if json_path.exists() and not force:
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
            counts = existing.get("content", {}).get("counts", {})
        except Exception:
            counts = {}
        return {
            "status": "skipped_existing",
            "source_path": str(source_path),
            "json_path": str(json_path),
            "counts": counts,
        }

    ext = extension_from_path(source_path)
    try:
        if ext == "pdf":
            content = convert_pdf(source_path)
        elif ext == "hwp":
            content = convert_hwp(source_path)
        elif ext == "hwpx":
            content = convert_hwpx(source_path)
        else:
            raise ValueError(f"unsupported extension: {ext}")

        decoded_fields = decode_field_values(plan, value_maps)
        derived = infer_document_tags(plan, decoded_fields)
        document = {
            "schema": "edunet_attachment_json_v1",
            "source": {
                "path": str(source_path),
                "filename": source_path.name,
                "extension": ext,
                "size_bytes": source_path.stat().st_size,
                "sha256": sha256_file(source_path),
            },
            "edunet": {
                "menu_id": plan.get("menu_id"),
                "board_name": plan.get("board_name"),
                "bbs_id": plan.get("bbs_id"),
                "post": plan.get("post"),
                "file": plan.get("file"),
                "selection": plan.get("selection"),
                "source_page": plan.get("source_page"),
            },
            "derived": derived,
            "extraction": {
                "created_at": now_iso(),
                "status": "ok",
                "parser": content.get("parser"),
            },
            "content": content,
        }

        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "status": "converted",
            "source_path": str(source_path),
            "json_path": str(json_path),
            "extension": ext,
            "menu_id": plan.get("menu_id"),
            "title": (plan.get("post") or {}).get("title"),
            "file_name": (plan.get("file") or {}).get("fileLgcNm"),
            "derived": derived,
            "counts": content.get("counts", {}),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "source_path": str(source_path),
            "json_path": str(json_path),
            "extension": ext,
            "menu_id": plan.get("menu_id"),
            "title": (plan.get("post") or {}).get("title"),
            "file_name": (plan.get("file") or {}).get("fileLgcNm"),
            "error": str(exc),
        }


def manifest_plans(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    successful_paths = {
        item.get("target_path")
        for item in manifest.get("results", [])
        if item.get("status") in {"downloaded", "skipped_existing"}
    }
    plans = []
    for plan in manifest.get("planned_downloads", []):
        if plan.get("target_path") in successful_paths:
            plans.append(plan)
    return plans


def write_index(output_root: Path, manifest_path: Path, results: list[dict[str, Any]]) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    summary = {
        "planned": len(results),
        "converted": sum(1 for item in results if item["status"] == "converted"),
        "skipped_existing": sum(1 for item in results if item["status"] == "skipped_existing"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
    }
    index = {
        "schema": "edunet_attachment_json_index_v1",
        "generated_at": now_iso(),
        "source_manifest": str(manifest_path),
        "summary": summary,
        "documents": results,
    }
    index_path = output_root / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="downloads/edunet_attachments/manifest.json",
        help="Downloader manifest path.",
    )
    parser.add_argument(
        "--input-root",
        default="downloads/edunet_attachments",
        help="Root directory of downloaded source files.",
    )
    parser.add_argument(
        "--output-root",
        default="json/edunet_attachments",
        help="Root directory for generated JSON files.",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    manifest = load_manifest(manifest_path)
    plans = manifest_plans(manifest)
    value_maps = board_value_maps(manifest)

    if not plans:
        print("No successful downloaded files found in manifest.", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                convert_one,
                plan,
                input_root=input_root,
                output_root=output_root,
                value_maps=value_maps,
                force=args.force,
            )
            for plan in plans
        ]
        total = len(futures)
        for idx, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            label = Path(result.get("source_path", "")).name
            print(f"[{idx}/{total}] {result['status']}: {label}", flush=True)

    results.sort(key=lambda item: item.get("source_path", ""))
    index_path = write_index(output_root, manifest_path, results)
    failed = [item for item in results if item["status"] == "failed"]
    print(f"[done] wrote index: {index_path}", flush=True)
    if failed:
        print(f"[done] failed: {len(failed)}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
