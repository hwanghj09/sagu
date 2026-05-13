#!/usr/bin/env python3
"""Download Edunet board attachments.

Policy: for each post, download PDF attachments when present; otherwise download
HWP/HWPX attachments. A manifest is written next to the downloaded files so the
downloaded corpus can be traced back to board/post/file metadata later.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests


API_BASE = "https://api.edunet.net/main"
WEB_BASE = "https://www.edunet.net"
DEFAULT_MENU_IDS = [58, 57]
PDF_EXTENSIONS = {"pdf"}
FALLBACK_EXTENSIONS = {"hwp", "hwpx"}
HEADERS = {
    "Origin": WEB_BASE,
    "Referer": f"{WEB_BASE}/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
}


print_lock = threading.Lock()


def log(message: str) -> None:
    with print_lock:
        print(message, flush=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def sanitize_segment(value: Any, fallback: str = "untitled", max_len: int = 120) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    if not text:
        text = fallback
    return text[:max_len].rstrip(" ._")


def file_extension(file_meta: dict[str, Any]) -> str:
    ext = str(file_meta.get("extn") or "").strip().lower().lstrip(".")
    if ext:
        return ext
    name = str(file_meta.get("fileLgcNm") or "")
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def request_json(
    session: requests.Session,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    for attempt in range(1, 4):
        try:
            response = session.request(
                method,
                url,
                json=json_body,
                headers=HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success", False):
                raise RuntimeError(f"API returned success=false for {path}: {payload}")
            return payload["data"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(attempt)
    raise RuntimeError(f"unreachable retry state for {path}")


def fetch_board(session: requests.Session, menu_id: int) -> dict[str, Any]:
    return request_json(session, "GET", f"/cmnBoard/getCmnBoardList/{menu_id}")


def fetch_posts(
    session: requests.Session,
    board: dict[str, Any],
    *,
    max_results: int,
) -> list[dict[str, Any]]:
    bbs_info = board["bbsInfo"]
    bbs_id = bbs_info["bbsId"]
    bbs_id_list = board["bbsIdList"]
    posts: list[dict[str, Any]] = []
    current_page = 1
    count_page = 1

    while current_page <= count_page:
        body = {
            "pagingProperty": {
                "currentPage": current_page,
                "maxResults": max_results,
                "maxLinks": 10,
                "startPage": 1,
                "endPage": 1,
                "countItem": 0,
                "loading": False,
            },
            "searchDTO": {
                "bbsIdList": bbs_id_list,
                "searchCondition": "ttl",
                "searchKeyword": "",
                "searchFieldStngVl": {},
                "bbsId": bbs_id,
            },
            "pagingYn": "Y",
        }
        data = request_json(
            session,
            "POST",
            "/cmnBoard/getCmnBoardPstList",
            json_body=body,
            timeout=120,
        )
        posts.extend(data.get("list") or [])
        paging = data.get("pagingProperty") or {}
        count_page = int(paging.get("countPage") or current_page)
        current_page += 1

    return posts


def select_files_for_post(post: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    files = post.get("atchFileList") or []
    pdfs = [file for file in files if file_extension(file) in PDF_EXTENSIONS]
    if pdfs:
        return pdfs, "pdf"
    fallbacks = [file for file in files if file_extension(file) in FALLBACK_EXTENSIONS]
    return fallbacks, "hwp_fallback"


def planned_downloads(
    menu_id: int,
    board: dict[str, Any],
    posts: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    bbs_name = board["bbsInfo"]["bbsNm"]
    board_dir = output_dir / f"{menu_id}_{sanitize_segment(bbs_name)}"
    planned: list[dict[str, Any]] = []

    for post in posts:
        selected, selection = select_files_for_post(post)
        post_dir = board_dir / (
            f"{int(post.get('descRn') or 0):03d}_"
            f"{post.get('pstId')}_"
            f"{sanitize_segment(post.get('ttl'), max_len=90)}"
        )
        used_names: set[str] = set()
        for file_meta in selected:
            logical_name = sanitize_segment(
                file_meta.get("fileLgcNm"),
                fallback=f"{file_meta.get('fileRscId')}.{file_extension(file_meta) or 'bin'}",
                max_len=180,
            )
            target_name = logical_name
            if target_name in used_names:
                stem, suffix = os.path.splitext(logical_name)
                target_name = f"{stem}_{file_meta.get('fileRscId')}{suffix}"
            used_names.add(target_name)
            planned.append(
                {
                    "menu_id": menu_id,
                    "board_name": bbs_name,
                    "bbs_id": board["bbsInfo"]["bbsId"],
                    "post": {
                        "pstId": post.get("pstId"),
                        "descRn": post.get("descRn"),
                        "title": post.get("ttl"),
                        "created_at": post.get("crtDt"),
                        "fieldVal": post.get("fieldVal"),
                    },
                    "selection": selection,
                    "file": file_meta,
                    "target_path": str(post_dir / target_name),
                    "source_page": f"{WEB_BASE}/cmnBoard/view/{menu_id}/{post.get('pstId')}",
                }
            )

    return planned


def get_signed_url(file_rsc_id: int) -> str:
    with requests.Session() as session:
        data = request_json(session, "GET", f"/fileRsc/downloadFile/{file_rsc_id}")
    if not isinstance(data, str) or not data.startswith("http"):
        raise RuntimeError(f"unexpected signed URL response for fileRscId={file_rsc_id}: {data!r}")
    return data


def download_one(plan: dict[str, Any], *, force: bool) -> dict[str, Any]:
    target = Path(plan["target_path"])
    expected_size = plan["file"].get("fileByte")
    if (
        target.exists()
        and not force
        and (not expected_size or target.stat().st_size == int(expected_size))
    ):
        return {**plan, "status": "skipped_existing", "bytes": target.stat().st_size}

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_name(f"{target.name}.part")
    file_rsc_id = int(plan["file"]["fileRscId"])
    signed_url = get_signed_url(file_rsc_id)

    for attempt in range(1, 4):
        try:
            with requests.get(signed_url, stream=True, headers=HEADERS, timeout=(10, 180)) as response:
                response.raise_for_status()
                with temp_target.open("wb") as out:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
            temp_target.replace(target)
            return {**plan, "status": "downloaded", "bytes": target.stat().st_size}
        except Exception as exc:
            if temp_target.exists():
                temp_target.unlink()
            if attempt == 3:
                return {**plan, "status": "failed", "error": str(exc)}
            signed_url = get_signed_url(file_rsc_id)
            time.sleep(attempt)

    return {**plan, "status": "failed", "error": "unreachable retry state"}


def write_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--menu-id",
        dest="menu_ids",
        type=int,
        action="append",
        help="Edunet cmnBoard menu id. Repeat to download multiple boards.",
    )
    parser.add_argument(
        "--output-dir",
        default="downloads/edunet_attachments",
        help="Directory where files and manifest.json will be written.",
    )
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--force", action="store_true", help="Redownload files even when present.")
    parser.add_argument("--dry-run", action="store_true", help="Only fetch metadata and write a plan.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    menu_ids = args.menu_ids or DEFAULT_MENU_IDS
    output_dir = Path(args.output_dir)

    all_boards: list[dict[str, Any]] = []
    all_plans: list[dict[str, Any]] = []
    with requests.Session() as session:
        for menu_id in menu_ids:
            board = fetch_board(session, menu_id)
            posts = fetch_posts(session, board, max_results=args.max_results)
            plans = planned_downloads(menu_id, board, posts, output_dir)
            all_boards.append(
                {
                    "menu_id": menu_id,
                    "board": board,
                    "post_count": len(posts),
                    "selected_file_count": len(plans),
                    "selected_bytes": sum(int(p["file"].get("fileByte") or 0) for p in plans),
                }
            )
            all_plans.extend(plans)
            log(
                f"[plan] menu={menu_id} board={board['bbsInfo']['bbsNm']} "
                f"posts={len(posts)} selected_files={len(plans)}"
            )

    manifest: dict[str, Any] = {
        "generated_at": now_iso(),
        "source_urls": [f"{WEB_BASE}/cmnBoard/list/{menu_id}" for menu_id in menu_ids],
        "selection_policy": "per post: download PDF attachments if any exist; otherwise download HWP/HWPX attachments",
        "boards": all_boards,
        "planned_downloads": all_plans,
        "results": [],
    }

    if args.dry_run:
        manifest_path = write_manifest(output_dir, manifest)
        log(f"[dry-run] wrote plan manifest: {manifest_path}")
        return 0

    total = len(all_plans)
    completed = 0
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(download_one, plan, force=args.force) for plan in all_plans]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            completed += 1
            status = result["status"]
            if status == "failed":
                failures += 1
            name = result["file"].get("fileLgcNm")
            log(f"[{completed}/{total}] {status}: {name}")
            manifest["results"].append(result)

    manifest["completed_at"] = now_iso()
    manifest["summary"] = {
        "planned": total,
        "downloaded": sum(1 for item in manifest["results"] if item["status"] == "downloaded"),
        "skipped_existing": sum(
            1 for item in manifest["results"] if item["status"] == "skipped_existing"
        ),
        "failed": failures,
    }
    manifest_path = write_manifest(output_dir, manifest)
    log(f"[done] wrote manifest: {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
