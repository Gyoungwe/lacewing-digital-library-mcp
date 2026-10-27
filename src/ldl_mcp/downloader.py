from __future__ import annotations

import argparse
import csv
import hashlib
import html
import importlib.metadata
import json
import mimetypes
import os
import platform
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from .lzstring import (
    compress_to_encoded_uri_component,
    decompress_from_encoded_uri_component,
)

# The MCP port talks to LDL through ``RequestsAPIContext`` (see ``ldl_mcp.ldl_http``),
# which mirrors the subset of Playwright's APIRequestContext used below.
APIRequestContext = Any


START_URL = "https://lacewing.tamu.edu/SpeciesCatalog/SimpleSearch"
MONOGRAPH_URL = "https://lacewing.tamu.edu/SpeciesCatalog/SimpleSearch/SearchResults/MonographRecord"
PAGE_TIMEOUT_MS = 120_000
DOWNLOAD_TIMEOUT_MS = 90_000
DOWNLOAD_RETRIES = 1
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_DELAY_SECONDS = 0.35
SCRIPT_VERSION = 17
GLOBAL_REPORT_INTERVAL = 20
RESUME_CHECKPOINT_NAME = "resume_checkpoint.json"

DOWNLOAD_MODES = {
    "fast": {
        "label": "快速首轮",
        "timeout_ms": 90_000,
        "retries": 1,
        "description": "每个文件块最长等待 90 秒，只尝试 1 次；失败后立即记录并继续。",
    },
    "stable": {
        "label": "稳定下载",
        "timeout_ms": 300_000,
        "retries": 3,
        "description": "每个文件块最长等待 5 分钟，最多尝试 3 次；最终失败仍会记录并继续。",
    },
    "retry-failed": {
        "label": "仅补失败",
        "timeout_ms": 300_000,
        "retries": 3,
        "description": "只处理 下载失败待补.csv 对应的 EDoc，不重新遍历全部物种。",
    },
}
ACTIVE_DOWNLOAD_MODE = "fast"
ACTIVE_DOWNLOAD_TIMEOUT_MS = DOWNLOAD_TIMEOUT_MS
ACTIVE_DOWNLOAD_RETRIES = DOWNLOAD_RETRIES


def configure_download_mode(mode: str) -> dict[str, Any]:
    global ACTIVE_DOWNLOAD_MODE, ACTIVE_DOWNLOAD_TIMEOUT_MS, ACTIVE_DOWNLOAD_RETRIES
    if mode not in DOWNLOAD_MODES:
        raise ValueError(f"未知下载模式：{mode}")
    config = DOWNLOAD_MODES[mode]
    ACTIVE_DOWNLOAD_MODE = mode
    ACTIVE_DOWNLOAD_TIMEOUT_MS = int(config["timeout_ms"])
    ACTIVE_DOWNLOAD_RETRIES = int(config["retries"])
    return config

# 只处理截图中的三个“文献引用”入口；Image Gallery 是图片，不属于文献。
SCREENS = (
    ("Taxon Citations", "taxon"),
    ("Distribution Citations", "distribution"),
    ("Image Citations", "image"),
)

CITATION_APIS = {
    "taxon": (
        "getSpecTaxCitByPk",
        ("Syn_BibObjAnnotList", "SynSub_BibObjAnnotList"),
    ),
    "distribution": (
        "getSpecDistCitByPk",
        ("Syn_DistGeogBibObjList", "SynSub_DistGeogBibObjList"),
    ),
    "image": (
        "getSpecFigCitByPk",
        ("Syn_BibObjPartAnnotList", "SynSub_BibObjPartAnnotList"),
    ),
}


RELATION_FIELDS = (
    "genus",
    "species",
    "categories",
    "references",
    "citation",
    "edoc_urls",
    "sha256",
    "library_file",
    "species_file",
    "relative_target",
    "link_mode",
)

UNAVAILABLE_FIELDS = (
    "genus",
    "species",
    "category",
    "reference",
    "citation",
    "source_page",
    "reason",
)

ERROR_FIELDS = (
    "time",
    "genus",
    "species",
    "category",
    "url",
    "message",
)

FAILED_DOWNLOAD_FIELDS = (
    "time",
    "genus",
    "species",
    "category",
    "reference",
    "citation",
    "edoc_url",
    "reason",
)

VERIFY_FIELDS = (
    "genus",
    "species",
    "category",
    "reference",
    "citation",
    "edoc_url",
    "status",
    "local_file",
    "notes",
)


@dataclass(frozen=True)
class SpeciesTarget:
    scientific_name: str
    epithet: str
    url: str


class LoginExpiredError(RuntimeError):
    pass


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_error_message(value: object) -> str:
    """去除 Playwright 请求日志中可能包含的会话 Cookie。"""
    message = str(value)
    message = re.sub(
        r"(?im)^\s*-\s*cookie\s*:\s*.*$",
        "  - cookie: [redacted]",
        message,
    )
    message = re.sub(
        r"(?i)(cookie\s*[:=]\s*)[^\r\n]+",
        r"\1[redacted]",
        message,
    )
    return clean_text(message)


def safe_name(value: str, limit: int = 150) -> str:
    value = clean_text(value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = value.rstrip(" .")
    if not value:
        value = "unnamed"
    return value[:limit].rstrip(" .") or "unnamed"


def reference_number(text: str) -> str:
    match = re.search(r"\br\s*#\s*(\d+)\b", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def edoc_id(url: str) -> str:
    match = re.search(r"/edoc(?:\d*)/(?:.*?-)?(\d+)(?:\.[a-z0-9]+)?(?:\?|$)", url, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else default
    except Exception:
        return default


def save_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_text_atomic(path: Path, value: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding=encoding, newline="") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_error(
    path: Path,
    genus: str,
    species: str,
    category: str,
    url: str,
    message: str,
) -> None:
    is_new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8-sig" if is_new else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ERROR_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "genus": genus,
                "species": species,
                "category": category,
                "url": url,
                "message": safe_error_message(message),
            }
        )


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def canonical_monograph_url(url: str) -> str:
    parsed = urlparse(url)
    marker = "/monographrecord"
    position = parsed.path.casefold().find(marker)
    if position < 0:
        return ""
    end = position + len(marker)
    return parsed._replace(path=parsed.path[:end], fragment="").geturl()


def decode_ldl_state(url: str) -> dict[str, Any]:
    values = parse_qs(urlparse(url).query).get("state", [])
    if not values:
        raise RuntimeError("当前 LDL 网址缺少 state 参数")
    decoded = decompress_from_encoded_uri_component(values[0])
    if not decoded:
        raise RuntimeError("无法解码当前 LDL 网址中的 state 参数")
    state = json.loads(decoded)
    if not isinstance(state, dict) or not isinstance(state.get("CombObjID"), int):
        raise RuntimeError("当前 LDL 网址中没有有效的 CombObjID")
    return state


def ldl_state_url(source_url: str, state: dict[str, Any], comb_obj_id: int) -> str:
    next_state = dict(state)
    next_state["CombObjID"] = comb_obj_id
    serialized = json.dumps(next_state, ensure_ascii=False, separators=(",", ":"))
    encoded = compress_to_encoded_uri_component(serialized)
    parsed = urlparse(source_url)
    return parsed._replace(query=f"state={encoded}", fragment="").geturl()


def fetch_monograph_payload(
    request: APIRequestContext,
    source_url: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = decode_ldl_state(source_url)
    response = request.post(
        "https://lacewing.tamu.edu/api/species/getSpecCombPk",
        data={"CombObjID": state["CombObjID"]},
        timeout=PAGE_TIMEOUT_MS,
    )
    if not response.ok:
        raise RuntimeError(
            f"LDL 物种数据接口返回 HTTP {response.status}: {response.status_text}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("LDL 物种数据接口返回了非 JSON 对象")
    return state, payload


def extract_species_targets(
    source_url: str,
    request: APIRequestContext,
) -> tuple[str, list[SpeciesTarget]]:
    """由物种网址取得组合编号，再用 LDL 自己的接口获取完整同属名单。"""
    state, payload = fetch_monograph_payload(request, source_url)
    rows = payload.get("Genu_SpCatCongenList")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("LDL 数据接口没有返回同属物种名单")

    genus = clean_text(str(payload.get("PresCombGenu", "")))
    if not genus:
        genus = clean_text(str(rows[0].get("PresCombGenu", "")))
    if not re.fullmatch(r"[A-Za-z][A-Za-z-]*", genus):
        raise RuntimeError(f"LDL 数据接口返回的属名格式异常：{genus!r}")

    targets: list[SpeciesTarget] = []
    seen_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        comb_obj_id = row.get("CongenPres_CombObjID")
        epithet = clean_text(str(row.get("PresCombSpEpithet", "")))
        epithet = re.sub(r"[†°*]", "", epithet).strip()
        if not isinstance(comb_obj_id, int) or not epithet or comb_obj_id in seen_ids:
            continue
        seen_ids.add(comb_obj_id)
        targets.append(
            SpeciesTarget(
                scientific_name=f"{genus} {epithet}",
                epithet=epithet,
                url=ldl_state_url(source_url, state, comb_obj_id),
            )
        )
    if not targets:
        raise RuntimeError("LDL 数据接口返回的同属名单没有有效物种记录")
    return genus, targets


def _positive_id(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def fetch_citation_payload(
    request: APIRequestContext,
    source_url: str,
    category: str,
) -> tuple[dict[str, Any], str]:
    """直接调用 LDL 为 Citations 页面提供数据的接口，不读取网页 DOM。"""
    endpoint, _ = CITATION_APIS[category]
    state = decode_ldl_state(source_url)
    api_url = f"https://lacewing.tamu.edu/api/species/{endpoint}"
    response = request.post(
        api_url,
        data={"CombObjID": state["CombObjID"]},
        timeout=PAGE_TIMEOUT_MS,
        fail_on_status_code=False,
    )
    if response.status in {401, 403}:
        raise LoginExpiredError("LDL 登录会话已失效，请在浏览器中重新登录")
    if not response.ok:
        raise RuntimeError(f"LDL 引用接口返回 HTTP {response.status}: {response.status_text}")
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError("LDL 引用接口没有返回有效 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("LDL 引用接口返回了非 JSON 对象")
    return payload, api_url


def citation_api_rows(payload: dict[str, Any], category: str) -> list[dict[str, Any]]:
    _, fields = CITATION_APIS[category]
    empty: list[dict[str, Any]] | None = None
    for field in fields:
        value = payload.get(field)
        if isinstance(value, list):
            rows = [item for item in value if isinstance(item, dict)]
            if rows:
                return rows
            empty = rows
    return empty or []


def _citation_parts(item: dict[str, Any], category: str) -> tuple[int, int, list[str]]:
    if category == "image":
        base = item.get("BibObj_EDocBase")
        bib_id = _positive_id(item.get("BibObjID"))
        edoc_id_value = _positive_id(base.get("EDocID")) if isinstance(base, dict) else 0
        edoc_id_value = edoc_id_value or _positive_id(item.get("BibObj_EDocID"))
        parts = [
            item.get("BibObjCiteShort"),
            item.get("BibObjPageYear"),
            item.get("BibObjPagePlateCitation"),
            item.get("BibObjPartCitation"),
            item.get("Subject"),
        ]
    else:
        base = item.get("Source_EDocBase")
        bib_id = _positive_id(item.get("Source_BibObjID"))
        edoc_id_value = _positive_id(base.get("EDocID")) if isinstance(base, dict) else 0
        edoc_id_value = edoc_id_value or _positive_id(item.get("Source_EDocID"))
        if category == "taxon":
            parts = [
                item.get("SourceAuthYearCite"),
                item.get("SourcePageCiteLong"),
                item.get("NameUsedInWorkCite"),
                item.get("AnnotCiteLong"),
            ]
        else:
            parts = [
                item.get("SourcePageYear"),
                item.get("SourcePageCite"),
                item.get("CitedAsCombCite"),
                item.get("RecordFullGeogCite"),
                item.get("RecordStatus"),
                item.get("RecordNativity"),
            ]
    return bib_id, edoc_id_value, [clean_text(str(value)) for value in parts if value]


def collect_citation_records_from_api(
    payload: dict[str, Any],
    category: str,
    api_url: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """从 LDL 的结构化 JSON 中提取“Open file (work)”对应的整篇文献。"""
    records: list[dict[str, str]] = []
    unavailable: list[dict[str, str]] = []
    for item in citation_api_rows(payload, category):
        bib_id, document_id, parts = _citation_parts(item, category)
        citation_parts = ([f"r#{bib_id}"] if bib_id else []) + parts
        citation = clean_text(" | ".join(dict.fromkeys(citation_parts)))
        common = {
            "category": category,
            "citation": citation,
            "reference": str(bib_id) if bib_id else "",
            "source_page": api_url,
        }
        if document_id:
            records.append(
                {
                    **common,
                    "edoc_url": f"https://lacewing.tamu.edu/edoc/{document_id}",
                }
            )
        else:
            unavailable.append(
                {
                    **common,
                    "reason": "LDL 数据接口未提供整篇文献 EDoc（相当于没有 Open file (work)）",
                }
            )
    return records, unavailable


def content_disposition_name(headers: dict[str, str]) -> str:
    value = headers.get("content-disposition", "")
    extended = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.IGNORECASE)
    if extended:
        return unquote(extended.group(1).strip().strip('"'))
    ordinary = re.search(r"filename=\"?([^\";]+)", value, flags=re.IGNORECASE)
    return ordinary.group(1).strip() if ordinary else ""


def extension_for(content_type: str, response_url: str, body: bytes, disposition: str) -> str:
    if body.startswith(b"%PDF"):
        return ".pdf"
    for candidate in (disposition, urlparse(response_url).path):
        suffix = Path(candidate).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            return suffix
    content_type = content_type.split(";", 1)[0].strip().lower()
    explicit = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/tiff": ".tif",
    }
    return explicit.get(content_type) or mimetypes.guess_extension(content_type) or ".bin"


def parse_content_range(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", value, re.IGNORECASE)
    if not match or match.group(3) == "*":
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def request_document_part(
    request: APIRequestContext,
    url: str,
    start: int,
    end: int,
    timeout_ms: int | None = None,
    retry_limit: int | None = None,
) -> tuple[Any, bytes, dict[str, str]]:
    timeout_ms = ACTIVE_DOWNLOAD_TIMEOUT_MS if timeout_ms is None else timeout_ms
    retry_limit = ACTIVE_DOWNLOAD_RETRIES if retry_limit is None else retry_limit
    last_error = ""
    for attempt in range(1, retry_limit + 1):
        try:
            response = request.get(
                url,
                headers={"Range": f"bytes={start}-{end}"},
                timeout=timeout_ms,
                fail_on_status_code=False,
            )
            if response.status not in {200, 206}:
                raise RuntimeError(f"HTTP {response.status} {response.status_text}")
            body = response.body()
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            if not body:
                raise RuntimeError("服务器返回空文件块")
            return response, body, headers
        except Exception as exc:
            if isinstance(exc, LoginExpiredError):
                raise
            last_error = safe_error_message(exc)
            if attempt < retry_limit:
                wait_seconds = min(20, 2 ** (attempt - 1))
                print(
                    f"      文件块 {start}-{end} 下载失败；"
                    f"{wait_seconds} 秒后重试（{attempt}/{retry_limit}）……"
                )
                time.sleep(wait_seconds)
    raise RuntimeError(last_error or "文件块下载失败")


def fetch_document(
    request: APIRequestContext,
    url: str,
    depth: int = 0,
    timeout_ms: int | None = None,
    retry_limit: int | None = None,
) -> tuple[bytes, str, str]:
    timeout_ms = ACTIVE_DOWNLOAD_TIMEOUT_MS if timeout_ms is None else timeout_ms
    retry_limit = ACTIVE_DOWNLOAD_RETRIES if retry_limit is None else retry_limit
    # 先只请求第一个小区块。LDL 支持 HTTP Range，可避免大 PDF
    # 必须在一个超时窗口内全部传完。
    response, first_body, headers = request_document_part(
        request,
        url,
        0,
        DOWNLOAD_CHUNK_SIZE - 1,
        timeout_ms=timeout_ms,
        retry_limit=retry_limit,
    )
    content_type = headers.get("content-type", "").lower()
    disposition = content_disposition_name(headers)

    if "text/html" in content_type or first_body.lstrip().startswith((b"<!DOCTYPE", b"<html")):
        if depth >= 2:
            raise RuntimeError("edoc 返回 HTML，未找到可下载文件")
        page_html = first_body.decode("utf-8", errors="ignore")
        redirected_path = urlparse(response.url).path.casefold()
        if (
            "Enter Username and Password" in page_html
            or "LDL Authenticate - Log In" in page_html
            or redirected_path == "/homepage/contributors"
        ):
            raise LoginExpiredError("LDL 登录会话已失效，请在浏览器中重新登录")
        candidates = re.findall(
            r"(?:href|src)=[\"']([^\"']+\.(?:pdf|djvu|docx?|zip)(?:\?[^\"']*)?)[\"']",
            page_html,
            flags=re.IGNORECASE,
        )
        if not candidates:
            raise RuntimeError("edoc 返回 HTML，未找到 PDF/文档链接（可能需要重新登录）")
        nested = urljoin(response.url, candidates[0])
        return fetch_document(
            request,
            nested,
            depth + 1,
            timeout_ms=timeout_ms,
            retry_limit=retry_limit,
        )

    parsed_range = parse_content_range(headers.get("content-range", ""))
    if response.status == 206 and parsed_range:
        range_start, range_end, total_size = parsed_range
        if range_start != 0 or range_end + 1 != len(first_body):
            raise RuntimeError("服务器返回的首个文件块范围不正确")
        chunks = [first_body]
        received = len(first_body)
        while received < total_size:
            end = min(received + DOWNLOAD_CHUNK_SIZE - 1, total_size - 1)
            part_response, part_body, part_headers = request_document_part(
                request,
                url,
                received,
                end,
                timeout_ms=timeout_ms,
                retry_limit=retry_limit,
            )
            part_range = parse_content_range(part_headers.get("content-range", ""))
            if part_response.status != 206 or not part_range:
                raise RuntimeError("服务器在分段下载中忽略了 Range 请求")
            part_start, part_end, part_total = part_range
            if (
                part_start != received
                or part_end + 1 - part_start != len(part_body)
                or part_total != total_size
            ):
                raise RuntimeError("服务器返回的文件块范围或大小不正确")
            chunks.append(part_body)
            received += len(part_body)
            percent = min(100, int(received * 100 / total_size))
            print(f"      分段下载：{received}/{total_size} 字节（{percent}%）")
        body = b"".join(chunks)
        if len(body) != total_size:
            raise RuntimeError(f"分段下载大小不一致：{len(body)} != {total_size}")
    else:
        # 有些较旧的站点会忽略 Range；若返回 200，当前响应就是完整文件。
        body = first_body

    extension = extension_for(content_type, response.url, body, disposition)
    return body, extension, response.url


def unique_library_filename(record: dict[str, str], extension: str, digest: str) -> str:
    ref = record.get("reference", "")
    eid = edoc_id(record["edoc_url"])
    prefix = f"r{int(ref):06d}" if ref.isdigit() else (f"edoc{eid}" if eid else "document")
    citation = clean_text(record.get("citation", ""))
    # 移除常见按钮文字，避免文件名过长。
    citation = re.sub(
        r"Bibliography of the Neuropterida.*?(?=[A-Z][a-z]|$)",
        "",
        citation,
        flags=re.IGNORECASE,
    )
    title = safe_name(citation, 105) if citation else "LDL_document"
    return safe_name(f"{prefix}_{digest[:10]}_{title}", 135) + extension


def download_or_reuse(
    request: APIRequestContext,
    root: Path,
    library_dir: Path,
    index_path: Path,
    index_data: dict[str, Any],
    record: dict[str, str],
) -> tuple[str, Path, bool]:
    url = record["edoc_url"]
    edocs = index_data.setdefault("edocs", {})
    references = index_data.setdefault("references", {})
    documents = index_data.setdefault("documents", {})
    record_references = [value for value in record.get("reference", "").split("; ") if value]

    # LDL 的 r# 是文献记录唯一编号。已知 r# 时可在请求文件前直接去重。
    for ref in record_references:
        known_digest = references.get(ref)
        document = documents.get(known_digest, {}) if known_digest else {}
        relative = document.get("path", "") if isinstance(document, dict) else ""
        path = root / relative if relative else Path()
        expected_size = int(document.get("size", -1)) if isinstance(document, dict) else -1
        if (
            known_digest
            and relative
            and not document.get("invalid")
            and path.exists()
            and path.stat().st_size == expected_size
        ):
            edocs[url] = {
                "sha256": known_digest,
                "final_url": "reused-by-reference-number",
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }
            save_json_atomic(index_path, index_data)
            return known_digest, path, False

    existing = edocs.get(url)
    if isinstance(existing, dict):
        digest = existing.get("sha256", "")
        document = documents.get(digest, {})
        relative = document.get("path", "") if isinstance(document, dict) else ""
        path = root / relative if relative else Path()
        expected_size = int(document.get("size", -1)) if isinstance(document, dict) else -1
        invalid = bool(document.get("invalid")) if isinstance(document, dict) else True
        if (
            digest
            and relative
            and not invalid
            and path.exists()
            and path.stat().st_size == expected_size
        ):
            return digest, path, False

    body, extension, final_url = fetch_document(
        request,
        url,
    )
    digest = hashlib.sha256(body).hexdigest()
    document = documents.get(digest)

    if isinstance(document, dict) and document.get("path"):
        path = root / str(document["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(path, body)
        document["size"] = len(body)
        document["sha256"] = digest
        document["mtime_ns"] = path.stat().st_mtime_ns
        document.pop("invalid", None)
        created = False
    else:
        filename = unique_library_filename(record, extension, digest)
        path = library_dir / filename
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            path = library_dir / f"{path.stem}_{digest[:10]}{path.suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            write_bytes_atomic(path, body)
        documents[digest] = {
            "path": str(path.relative_to(root)),
            "size": len(body),
            "sha256": digest,
            "mtime_ns": path.stat().st_mtime_ns,
            "first_edoc_url": url,
            "final_url": final_url,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        created = True

    edocs[url] = {
        "sha256": digest,
        "final_url": final_url,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    for ref in record_references:
        references[ref] = digest
    save_json_atomic(index_path, index_data)
    time.sleep(DOWNLOAD_DELAY_SECONDS)
    return digest, path, created


def relative_target(from_folder: Path, library_file: Path) -> str:
    return os.path.relpath(library_file, start=from_folder)


def create_relative_html_pointer(library_file: Path, pointer: Path) -> None:
    pointer.parent.mkdir(parents=True, exist_ok=True)
    relative = relative_target(pointer.parent, library_file)
    relative_url = quote(relative.replace("\\", "/"), safe="/._-~")
    display_name = html.escape(library_file.name)
    display_relative = html.escape(relative)
    target_url = html.escape(relative_url, quote=True)
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url={target_url}">
  <title>打开文献：{display_name}</title>
</head>
<body>
  <p>正在打开文献：<a href="{target_url}">{display_name}</a></p>
  <p>相对位置：<code>{display_relative}</code></p>
  <p>此指引使用相对路径；移动整个 LDL 属文件夹后仍然有效。</p>
</body>
</html>
"""
    write_text_atomic(pointer, document, encoding="utf-8")


def link_into_species(library_file: Path, target: Path, primary: bool) -> tuple[str, Path]:
    target.parent.mkdir(parents=True, exist_ok=True)
    pointer = target.with_name(f"{target.stem}_打开文献.html")
    create_relative_html_pointer(library_file, pointer)
    return ("relative-html-pointer-primary" if primary else "relative-html-pointer"), pointer


def aggregate_records(records: list[dict[str, str]]) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for record in records:
        url = record["edoc_url"]
        item = by_url.setdefault(
            url,
            {
                "edoc_url": url,
                "categories": set(),
                "references": set(),
                "citations": [],
                "source_pages": set(),
            },
        )
        item["categories"].add(record["category"])
        if record.get("reference"):
            item["references"].add(record["reference"])
        if record.get("citation") and record["citation"] not in item["citations"]:
            item["citations"].append(record["citation"])
        item["source_pages"].add(record["source_page"])

    result: list[dict[str, Any]] = []
    for item in by_url.values():
        result.append(
            {
                "edoc_url": item["edoc_url"],
                "category": "; ".join(sorted(item["categories"])),
                "reference": "; ".join(sorted(item["references"], key=lambda x: int(x) if x.isdigit() else x)),
                "citation": " | ".join(item["citations"]),
                "source_page": "; ".join(sorted(item["source_pages"])),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            int(item["reference"].split("; ", 1)[0])
            if item["reference"].split("; ", 1)[0].isdigit()
            else 10**12,
            item["edoc_url"],
        ),
    )


def relation_rows_for_species(progress: dict[str, Any], species: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    docs = progress.get("relations", {}).get(species, {})
    for digest, value in sorted(docs.items()):
        rows.append(
            {
                "genus": progress.get("genus", ""),
                "species": species,
                "categories": "; ".join(sorted(value.get("categories", []))),
                "references": "; ".join(sorted(value.get("references", []))),
                "citation": " | ".join(value.get("citations", [])),
                "edoc_urls": "; ".join(sorted(value.get("edoc_urls", []))),
                "sha256": digest,
                "library_file": value.get("library_file", ""),
                "species_file": value.get("species_file", ""),
                "relative_target": value.get("relative_target", ""),
                "link_mode": value.get("link_mode", ""),
            }
        )
    return rows


def relation_rows(progress: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for species in sorted(progress.get("relations", {})):
        rows.extend(relation_rows_for_species(progress, species))
    return rows


def unavailable_rows(progress: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in progress.get("unavailable", []):
        result.append(
            {
                "genus": progress.get("genus", ""),
                "species": item.get("species", ""),
                "category": item.get("category", ""),
                "reference": item.get("reference", ""),
                "citation": item.get("citation", ""),
                "source_page": item.get("source_page", ""),
                "reason": item.get("reason", ""),
            }
        )
    return result


def failed_download_rows(progress: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in progress.get("failed_downloads", []):
        result.append(
            {
                "time": item.get("time", ""),
                "genus": progress.get("genus", ""),
                "species": item.get("species", ""),
                "category": item.get("category", ""),
                "reference": item.get("reference", ""),
                "citation": item.get("citation", ""),
                "edoc_url": item.get("edoc_url", ""),
                "reason": item.get("reason", ""),
            }
        )
    return sorted(
        result,
        key=lambda item: (str(item["species"]), str(item["reference"]), str(item["edoc_url"])),
    )


def rebuild_failed_download_report(root: Path, progress: dict[str, Any]) -> None:
    try:
        write_csv(
            root / "下载失败待补.csv",
            FAILED_DOWNLOAD_FIELDS,
            failed_download_rows(progress),
        )
    except OSError as exc:
        print(f"暂时无法更新 下载失败待补.csv（{safe_error_message(exc)}）。")


def rebuild_reports(root: Path, progress: dict[str, Any]) -> None:
    rows = relation_rows(progress)
    write_csv(root / "文献关联总表.csv", RELATION_FIELDS, rows)
    write_csv(root / "未提供文件的引用.csv", UNAVAILABLE_FIELDS, unavailable_rows(progress))
    rebuild_failed_download_report(root, progress)

    by_species: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_species.setdefault(str(row["species"]), []).append(row)
    for species, species_rows in by_species.items():
        folder = root / "01_按物种分类" / safe_name(species.replace(" ", "_"))
        write_csv(folder / "文献清单.csv", RELATION_FIELDS, species_rows)


def rebuild_global_reports(root: Path, progress: dict[str, Any]) -> None:
    """只刷新属级总表。

    旧版每处理一个物种就重写所有物种的清单，大属会因此越跑越慢。
    """
    write_csv(root / "文献关联总表.csv", RELATION_FIELDS, relation_rows(progress))
    write_csv(root / "未提供文件的引用.csv", UNAVAILABLE_FIELDS, unavailable_rows(progress))
    rebuild_failed_download_report(root, progress)


def rebuild_species_report(root: Path, progress: dict[str, Any], species: str) -> None:
    rows = relation_rows_for_species(progress, species)
    folder = root / "01_按物种分类" / safe_name(species.replace(" ", "_"))
    write_csv(folder / "文献清单.csv", RELATION_FIELDS, rows)


def merge_relation(target: dict[str, Any], saved: dict[str, Any]) -> None:
    for key in ("categories", "references", "citations", "edoc_urls"):
        current = target.setdefault(key, [])
        for value in saved.get(key, []):
            if value and value not in current:
                current.append(value)
    for key in ("library_file", "species_file", "relative_target", "link_mode"):
        if saved.get(key):
            target[key] = saved[key]


def save_resume_checkpoint(
    path: Path,
    genus: str,
    species: str,
    relations: dict[str, Any],
    unavailable: list[dict[str, Any]],
    failed_downloads: list[dict[str, Any]],
    complete: bool,
    failed: bool = False,
) -> None:
    save_json_atomic(
        path,
        {
            "version": SCRIPT_VERSION,
            "genus": genus,
            "species": species,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "complete": complete,
            "failed": failed,
            "relations": relations,
            "unavailable": [
                item for item in unavailable if item.get("species", "") == species
            ],
            "failed_downloads": [
                item for item in failed_downloads if item.get("species", "") == species
            ],
        },
    )


def restore_resume_checkpoint(
    path: Path,
    genus: str,
    progress: dict[str, Any],
) -> str:
    if not path.exists():
        return ""
    saved = load_json(path, {})
    species = clean_text(str(saved.get("species", "")))
    if not species or clean_text(str(saved.get("genus", ""))) != genus:
        return ""

    target_relations = progress.setdefault("relations", {}).setdefault(species, {})
    for digest, relation in saved.get("relations", {}).items():
        if not isinstance(relation, dict):
            continue
        current = target_relations.setdefault(digest, {})
        merge_relation(current, relation)

    known_unavailable = {
        (
            item.get("species", ""),
            item.get("category", ""),
            item.get("reference", ""),
            item.get("citation", ""),
        )
        for item in progress.setdefault("unavailable", [])
    }
    for item in saved.get("unavailable", []):
        if not isinstance(item, dict):
            continue
        key = (
            item.get("species", ""),
            item.get("category", ""),
            item.get("reference", ""),
            item.get("citation", ""),
        )
        if key not in known_unavailable:
            progress["unavailable"].append(item)
            known_unavailable.add(key)

    known_failures = {
        (item.get("species", ""), item.get("edoc_url", ""))
        for item in progress.setdefault("failed_downloads", [])
    }
    for item in saved.get("failed_downloads", []):
        if not isinstance(item, dict):
            continue
        key = (item.get("species", ""), item.get("edoc_url", ""))
        if key not in known_failures:
            progress["failed_downloads"].append(item)
            known_failures.add(key)

    if saved.get("complete"):
        completed = set(progress.get("completed_species", []))
        completed.add(species)
        progress["completed_species"] = sorted(completed)
        print(f"已恢复上次完成但尚未写入总进度的物种：{species}")
    elif target_relations:
        print(f"已恢复 {species} 的物种内断点（{len(target_relations)} 篇已处理文献）。")
    failed_species = set(progress.get("failed_species", []))
    if saved.get("failed"):
        failed_species.add(species)
    elif saved.get("complete"):
        failed_species.discard(species)
    progress["failed_species"] = sorted(failed_species)
    return species


def freeze_or_load_species_snapshot(
    root: Path,
    genus: str,
    detected: list[SpeciesTarget],
    source_url: str,
    refresh: bool,
) -> list[SpeciesTarget]:
    path = root / "物种列表快照.json"
    if path.exists() and not refresh:
        value = load_json(path, {})
        saved = value.get("species", [])
        targets = [
            SpeciesTarget(
                scientific_name=str(item["scientific_name"]),
                epithet=str(item["epithet"]),
                url=str(item["url"]),
            )
            for item in saved
            if isinstance(item, dict) and item.get("scientific_name") and item.get("url")
        ]
        if targets:
            detected_names = [item.scientific_name for item in detected]
            saved_names = [item.scientific_name for item in targets]
            if detected_names != saved_names:
                print("注意：LDL 当前物种列表与首次运行快照不同；为保证可重复，本次继续使用已保存快照。")
                print("如需采用当前列表，请使用 --refresh 运行。")
            return targets

    snapshot = {
        "version": SCRIPT_VERSION,
        "genus": genus,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source_url": source_url,
        "count": len(detected),
        "species": [
            {
                "scientific_name": item.scientific_name,
                "epithet": item.epithet,
                "url": item.url,
            }
            for item in detected
        ],
    }
    save_json_atomic(path, snapshot)
    write_csv(
        root / "物种列表快照.csv",
        ("sequence", "scientific_name", "url"),
        [
            {"sequence": number, "scientific_name": item.scientific_name, "url": item.url}
            for number, item in enumerate(detected, start=1)
        ],
    )
    return detected


def validate_library(
    root: Path,
    index_data: dict[str, Any],
    progress: dict[str, Any],
) -> list[str]:
    invalid: list[str] = []
    documents = index_data.setdefault("documents", {})
    for digest, metadata in list(documents.items()):
        if not isinstance(metadata, dict) or not metadata.get("path"):
            invalid.append(digest)
            continue
        path = root / str(metadata["path"])
        if not path.exists() or path.stat().st_size != int(metadata.get("size", -1)):
            metadata["invalid"] = True
            invalid.append(digest)
            continue
        # 文件大小和修改时间均未变化时，无需重新读取大型 PDF。
        recorded_mtime = int(metadata.get("mtime_ns", 0) or 0)
        if recorded_mtime and path.stat().st_mtime_ns == recorded_mtime:
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            metadata["invalid"] = True
            invalid.append(digest)
        else:
            metadata["mtime_ns"] = path.stat().st_mtime_ns
            metadata.pop("invalid", None)

    if invalid:
        completed = set(progress.get("completed_species", []))
        for species, relations in progress.get("relations", {}).items():
            if any(digest in relations for digest in invalid):
                completed.discard(species)
        progress["completed_species"] = sorted(completed)
        print(f"完整性检查发现 {len(invalid)} 个缺失或已变更文件；相关物种将自动重试。")
    return invalid


def repair_species_guides(
    root: Path,
    index_data: dict[str, Any],
    progress: dict[str, Any],
) -> int:
    repaired = 0
    documents = index_data.get("documents", {})
    for species, relations in progress.get("relations", {}).items():
        for digest, relation in relations.items():
            metadata = documents.get(digest, {})
            library_relative = metadata.get("path") if isinstance(metadata, dict) else ""
            if not library_relative:
                continue
            library_file = root / str(library_relative)
            if not library_file.exists():
                continue
            species_folder = root / "01_按物种分类" / safe_name(species.replace(" ", "_")) / "literature"
            current_relative = relation.get("species_file", "")
            current = root / str(current_relative) if current_relative else Path()
            if current_relative and current.exists():
                continue
            primary = clean_text(str(metadata.get("first_species", ""))) == species
            mode, entry = link_into_species(library_file, species_folder / library_file.name, primary)
            relation["species_file"] = str(entry.relative_to(root))
            relation["relative_target"] = relative_target(entry.parent, library_file)
            relation["link_mode"] = mode
            repaired += 1
    if repaired:
        print(f"已自动修复 {repaired} 个缺失的物种文献指引。")
    return repaired


def process_species(
    request: APIRequestContext,
    genus: str,
    target: SpeciesTarget,
    root: Path,
    library_dir: Path,
    index_path: Path,
    index_data: dict[str, Any],
    progress: dict[str, Any],
    error_path: Path,
    checkpoint_path: Path,
) -> tuple[int, int, bool]:
    print(f"\n[{target.scientific_name}] 从 LDL 数据接口读取三类 Citations……")
    all_records: list[dict[str, str]] = []
    all_unavailable: list[dict[str, str]] = []
    complete = True

    for visible_name, category in SCREENS:
        endpoint, _ = CITATION_APIS[category]
        url = f"https://lacewing.tamu.edu/api/species/{endpoint}"
        try:
            print(f"  - {visible_name}: 正在读取结构化 JSON……")
            payload, api_url = fetch_citation_payload(request, target.url, category)
            rows = citation_api_rows(payload, category)
            records, unavailable = collect_citation_records_from_api(payload, category, api_url)
            all_records.extend(records)
            all_unavailable.extend(unavailable)
            distinct = len({item["edoc_url"] for item in records})
            print(
                f"  - {visible_name}: 接口返回 {len(rows)} 条引用；"
                f"{distinct} 个整篇 EDoc，{len(unavailable)} 条无整篇文件引用"
            )
        except Exception as exc:
            if isinstance(exc, LoginExpiredError):
                raise
            complete = False
            append_error(error_path, genus, target.scientific_name, category, url, str(exc))
            print(f"  - {visible_name}: 读取失败（已记入错误日志）")

    # 同一 edoc 在不同 citation 类别出现时，先合并为一条。
    records = aggregate_records(all_records)
    print(f"  三类接口读取结束；共 {len(records)} 个不重复 EDoc，开始直接保存文件。")
    species_folder = root / "01_按物种分类" / safe_name(target.scientific_name.replace(" ", "_"))
    literature_folder = species_folder / "literature"
    relations: dict[str, Any] = progress.setdefault("relations", {}).setdefault(target.scientific_name, {})
    failed_downloads: list[dict[str, Any]] = progress.setdefault("failed_downloads", [])
    unique_created = 0
    linked = 0
    related_by_edoc = {
        url: digest
        for digest, relation in relations.items()
        for url in relation.get("edoc_urls", [])
        if url
    }

    save_resume_checkpoint(
        checkpoint_path,
        genus,
        target.scientific_name,
        relations,
        progress.setdefault("unavailable", []),
        progress.setdefault("failed_downloads", []),
        complete=False,
    )

    for record in records:
        try:
            identifier = edoc_id(record.get("edoc_url", "")) or record.get("edoc_url", "")
            digest = ""
            library_file = Path()
            created = False

            # 第 0 层去重：断点已记录该物种与此 EDoc 的关联时，
            # 不再进入 r# / EDoc / SHA-256 通用去重流程。
            known_digest = related_by_edoc.get(record.get("edoc_url", ""), "")
            if known_digest:
                document = index_data.get("documents", {}).get(known_digest, {})
                relative = document.get("path", "") if isinstance(document, dict) else ""
                candidate = root / str(relative) if relative else Path()
                expected_size = int(document.get("size", -1)) if isinstance(document, dict) else -1
                if (
                    relative
                    and not document.get("invalid")
                    and candidate.exists()
                    and candidate.stat().st_size == expected_size
                ):
                    digest = known_digest
                    library_file = candidate

            already_related = bool(digest)
            if already_related:
                print(f"    断点跳过 edoc {identifier}（该物种已处理）")
            else:
                print(f"    正在下载 edoc {identifier}……")
                digest, library_file, created = download_or_reuse(
                    request,
                    root,
                    library_dir,
                    index_path,
                    index_data,
                    record,
                )
            document_meta = index_data.setdefault("documents", {}).setdefault(digest, {})
            first_species = clean_text(str(document_meta.get("first_species", "")))
            if not first_species:
                first_species = target.scientific_name
                document_meta["first_species"] = first_species
                save_json_atomic(index_path, index_data)
            primary = first_species == target.scientific_name
            target_file = literature_folder / library_file.name
            mode, species_entry = link_into_species(library_file, target_file, primary=primary)
            relative = relative_target(species_entry.parent, library_file)
            was_new_relation = digest not in relations
            relation = relations.setdefault(
                digest,
                {
                    "categories": [],
                    "references": [],
                    "citations": [],
                    "edoc_urls": [],
                    "library_file": str(library_file.relative_to(root)),
                    "species_file": str(species_entry.relative_to(root)),
                    "relative_target": relative,
                    "link_mode": mode,
                },
            )
            for key, values in (
                ("categories", record["category"].split("; ") if record["category"] else []),
                ("references", record["reference"].split("; ") if record["reference"] else []),
                ("citations", [record["citation"]] if record["citation"] else []),
                ("edoc_urls", [record["edoc_url"]]),
            ):
                current = relation.setdefault(key, [])
                for value in values:
                    if value and value not in current:
                        current.append(value)
            relation["link_mode"] = mode
            relation["species_file"] = str(species_entry.relative_to(root))
            relation["relative_target"] = relative
            related_by_edoc[record["edoc_url"]] = digest
            failure_count_before = len(failed_downloads)
            failed_downloads[:] = [
                item
                for item in failed_downloads
                if not (
                    item.get("species", "") == target.scientific_name
                    and item.get("edoc_url", "") == record["edoc_url"]
                )
            ]
            if len(failed_downloads) != failure_count_before:
                rebuild_failed_download_report(root, progress)
            linked += int(was_new_relation)
            unique_created += int(created)
            if not already_related:
                marker = "新文献" if created else "已去重"
                print(f"    {marker}: {library_file.name}")
            save_resume_checkpoint(
                checkpoint_path,
                genus,
                target.scientific_name,
                relations,
                progress.setdefault("unavailable", []),
                progress.setdefault("failed_downloads", []),
                complete=False,
            )
        except Exception as exc:
            if isinstance(exc, LoginExpiredError):
                raise
            complete = False
            append_error(
                error_path,
                genus,
                target.scientific_name,
                record.get("category", ""),
                record.get("edoc_url", ""),
                str(exc),
            )
            failure = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "species": target.scientific_name,
                "category": record.get("category", ""),
                "reference": record.get("reference", ""),
                "citation": record.get("citation", ""),
                "edoc_url": record.get("edoc_url", ""),
                "reason": safe_error_message(exc),
            }
            existing_failure = next(
                (
                    item
                    for item in failed_downloads
                    if item.get("species", "") == target.scientific_name
                    and item.get("edoc_url", "") == record.get("edoc_url", "")
                ),
                None,
            )
            if existing_failure is None:
                failed_downloads.append(failure)
            else:
                existing_failure.update(failure)
            rebuild_failed_download_report(root, progress)
            save_resume_checkpoint(
                checkpoint_path,
                genus,
                target.scientific_name,
                relations,
                progress.setdefault("unavailable", []),
                failed_downloads,
                complete=False,
                failed=True,
            )
            print(
                f"    下载失败: {record.get('edoc_url', '')} "
                f"({safe_error_message(exc)})"
            )
            print(
                f"    已记入 下载失败待补.csv，"
                f"{DOWNLOAD_MODES[ACTIVE_DOWNLOAD_MODE]['label']}继续下一篇。"
            )

    known_unavailable = {
        (
            item.get("species", ""),
            item.get("category", ""),
            item.get("reference", ""),
            item.get("citation", ""),
        )
        for item in progress.setdefault("unavailable", [])
    }
    for item in all_unavailable:
        key = (target.scientific_name, item["category"], item["reference"], item["citation"])
        if key not in known_unavailable:
            progress["unavailable"].append({"species": target.scientific_name, **item})
            known_unavailable.add(key)

    save_resume_checkpoint(
        checkpoint_path,
        genus,
        target.scientific_name,
        relations,
        progress.setdefault("unavailable", []),
        progress.setdefault("failed_downloads", []),
        complete=True,
        failed=not complete,
    )

    return unique_created, linked, complete


def retry_failed_downloads(
    request: APIRequestContext,
    genus: str,
    root: Path,
    library_dir: Path,
    index_path: Path,
    index_data: dict[str, Any],
    progress_path: Path,
    progress: dict[str, Any],
    error_path: Path,
) -> tuple[int, int, int]:
    """只重试失败清单，不重新读取物种或 Citations 接口。"""
    pending: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in progress.get("failed_downloads", []):
        if not isinstance(item, dict):
            continue
        species = clean_text(str(item.get("species", "")))
        url = clean_text(str(item.get("edoc_url", "")))
        key = (species, url)
        if not species or not url or key in seen:
            continue
        seen.add(key)
        pending.append(dict(item))

    if not pending:
        print("\n下载失败待补.csv 当前没有待补文献；无需重新遍历物种。")
        rebuild_failed_download_report(root, progress)
        return 0, 0, 0

    print(f"\n仅补失败模式：共 {len(pending)} 个物种—EDoc 失败项。")
    print("不会重新读取全部物种，也不会重新扫描三类 Citations。")
    success_count = 0
    new_document_count = 0

    for number, failure in enumerate(pending, start=1):
        species = clean_text(str(failure.get("species", "")))
        url = clean_text(str(failure.get("edoc_url", "")))
        record = {
            "category": clean_text(str(failure.get("category", ""))),
            "reference": clean_text(str(failure.get("reference", ""))),
            "citation": clean_text(str(failure.get("citation", ""))),
            "edoc_url": url,
            "source_page": "retry-failed",
        }
        print(f"\n===== 补传 {number}/{len(pending)} =====")
        print(f"[{species}] {url}")
        try:
            digest, library_file, created = download_or_reuse(
                request,
                root,
                library_dir,
                index_path,
                index_data,
                record,
            )
            document_meta = index_data.setdefault("documents", {}).setdefault(digest, {})
            first_species = clean_text(str(document_meta.get("first_species", "")))
            if not first_species:
                first_species = species
                document_meta["first_species"] = first_species
                save_json_atomic(index_path, index_data)

            literature_folder = (
                root
                / "01_按物种分类"
                / safe_name(species.replace(" ", "_"))
                / "literature"
            )
            mode, species_entry = link_into_species(
                library_file,
                literature_folder / library_file.name,
                primary=first_species == species,
            )
            relations = progress.setdefault("relations", {}).setdefault(species, {})
            relation = relations.setdefault(
                digest,
                {
                    "categories": [],
                    "references": [],
                    "citations": [],
                    "edoc_urls": [],
                    "library_file": str(library_file.relative_to(root)),
                },
            )
            for key, values in (
                ("categories", record["category"].split("; ") if record["category"] else []),
                ("references", record["reference"].split("; ") if record["reference"] else []),
                ("citations", [record["citation"]] if record["citation"] else []),
                ("edoc_urls", [url]),
            ):
                current = relation.setdefault(key, [])
                for value in values:
                    if value and value not in current:
                        current.append(value)
            relation["library_file"] = str(library_file.relative_to(root))
            relation["species_file"] = str(species_entry.relative_to(root))
            relation["relative_target"] = relative_target(species_entry.parent, library_file)
            relation["link_mode"] = mode

            progress["failed_downloads"] = [
                item
                for item in progress.get("failed_downloads", [])
                if not (
                    item.get("species", "") == species
                    and item.get("edoc_url", "") == url
                )
            ]
            progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
            save_json_atomic(index_path, index_data)
            save_json_atomic(progress_path, progress)
            rebuild_species_report(root, progress, species)
            rebuild_failed_download_report(root, progress)
            success_count += 1
            new_document_count += int(created)
            print(f"    补传成功：{library_file.name}")
        except Exception as exc:
            if isinstance(exc, LoginExpiredError):
                save_json_atomic(progress_path, progress)
                rebuild_failed_download_report(root, progress)
                raise
            message = safe_error_message(exc)
            append_error(error_path, genus, species, record["category"], url, str(exc))
            updated = False
            for item in progress.get("failed_downloads", []):
                if item.get("species", "") == species and item.get("edoc_url", "") == url:
                    item["time"] = datetime.now().isoformat(timespec="seconds")
                    item["reason"] = message
                    updated = True
            if not updated:
                progress.setdefault("failed_downloads", []).append(
                    {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "species": species,
                        "category": record["category"],
                        "reference": record["reference"],
                        "citation": record["citation"],
                        "edoc_url": url,
                        "reason": message,
                    }
                )
            progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
            save_json_atomic(progress_path, progress)
            rebuild_failed_download_report(root, progress)
            print(f"    补传仍失败：{message}")
            print("    已保留在 下载失败待补.csv，继续下一项。")

    rebuild_reports(root, progress)
    remaining = len(progress.get("failed_downloads", []))
    return success_count, new_document_count, remaining


def scan_species_for_verification(
    request: APIRequestContext,
    target: SpeciesTarget,
) -> dict[str, Any]:
    print(f"[{target.scientific_name}] 核查三类 Citations 数据……")
    all_records: list[dict[str, str]] = []
    all_unavailable: list[dict[str, str]] = []
    api_status: dict[str, Any] = {}
    for visible_name, category in SCREENS:
        try:
            payload, api_url = fetch_citation_payload(request, target.url, category)
            rows = citation_api_rows(payload, category)
            records, unavailable = collect_citation_records_from_api(payload, category, api_url)
            all_records.extend(records)
            all_unavailable.extend(unavailable)
            api_status[category] = {
                "ok": True,
                "row_count": len(rows),
                "downloadable_count": len({item["edoc_url"] for item in records}),
                "unavailable_count": len(unavailable),
                "error": "",
            }
            print(f"  - {visible_name}: 成功，{len(rows)} 条引用")
        except Exception as exc:
            if isinstance(exc, LoginExpiredError):
                raise
            api_status[category] = {
                "ok": False,
                "row_count": 0,
                "downloadable_count": 0,
                "unavailable_count": 0,
                "error": safe_error_message(exc),
            }
            print(f"  - {visible_name}: 核查失败（报告中标记）")
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "api": api_status,
        "records": aggregate_records(all_records),
        "unavailable": all_unavailable,
    }


def indexed_document_for_url(
    root: Path,
    index_data: dict[str, Any],
    url: str,
) -> tuple[str, Path | None, str]:
    edocs = index_data.get("edocs", {})
    entry = edocs.get(url)
    if not isinstance(entry, dict):
        wanted_id = edoc_id(url)
        if wanted_id:
            for known_url, known_entry in edocs.items():
                if edoc_id(str(known_url)) == wanted_id and isinstance(known_entry, dict):
                    entry = known_entry
                    break
    if not isinstance(entry, dict):
        return "", None, "EDoc 未出现在去重索引中"
    digest = clean_text(str(entry.get("sha256", "")))
    metadata = index_data.get("documents", {}).get(digest, {})
    if not digest or not isinstance(metadata, dict) or not metadata.get("path"):
        return digest, None, "EDoc 索引没有对应的文件记录"
    path = root / str(metadata["path"])
    if metadata.get("invalid"):
        return digest, path, "文件已被完整性检查标记为无效"
    if not path.exists():
        return digest, path, "本地文件不存在"
    if path.stat().st_size != int(metadata.get("size", -1)):
        return digest, path, "本地文件大小与索引不一致"
    return digest, path, ""


def build_integrity_report(
    root: Path,
    genus: str,
    targets: list[SpeciesTarget],
    index_data: dict[str, Any],
    progress: dict[str, Any],
    verification: dict[str, Any],
) -> bool:
    rows: list[dict[str, Any]] = []
    expected_urls: set[str] = set()
    expected_occurrence_keys: set[tuple[str, str]] = set()
    valid_urls: set[str] = set()
    resolved_failure_keys: set[tuple[str, str]] = set()
    api_failure_species: set[str] = set()
    problem_species: set[str] = set()
    fully_checked_species = 0
    unavailable_count = 0
    species_data = verification.get("species", {})

    for target in targets:
        species = target.scientific_name
        value = species_data.get(species)
        if not isinstance(value, dict):
            problem_species.add(species)
            rows.append(
                {
                    "genus": genus,
                    "species": species,
                    "category": "",
                    "reference": "",
                    "citation": "",
                    "edoc_url": "",
                    "status": "未核查",
                    "local_file": "",
                    "notes": "本次没有取得该物种的 LDL 核查数据",
                }
            )
            continue

        api_status = value.get("api", {})
        api_ok = True
        for visible_name, category in SCREENS:
            status = api_status.get(category, {})
            if not isinstance(status, dict) or not status.get("ok"):
                api_ok = False
                api_failure_species.add(species)
                problem_species.add(species)
                rows.append(
                    {
                        "genus": genus,
                        "species": species,
                        "category": category,
                        "reference": "",
                        "citation": "",
                        "edoc_url": "",
                        "status": "引用接口读取失败",
                        "local_file": "",
                        "notes": clean_text(str(status.get("error", "未知错误"))),
                    }
                )
        if api_ok:
            fully_checked_species += 1

        species_relations = progress.get("relations", {}).get(species, {})
        for record in value.get("records", []):
            if not isinstance(record, dict):
                continue
            url = clean_text(str(record.get("edoc_url", "")))
            if not url:
                continue
            expected_urls.add(url)
            expected_occurrence_keys.add((species, url))
            digest, local_path, document_problem = indexed_document_for_url(root, index_data, url)
            status_text = "已下载"
            notes = ""
            local_file = str(local_path.relative_to(root)) if local_path and local_path.exists() else ""
            if document_problem:
                status_text = "下载失败待补" if any(
                    item.get("species", "") == species and item.get("edoc_url", "") == url
                    for item in progress.get("failed_downloads", [])
                ) else "未下载或本地文件无效"
                notes = document_problem
                problem_species.add(species)
            else:
                valid_urls.add(url)
                resolved_failure_keys.add((species, url))
                relation = species_relations.get(digest, {}) if digest else {}
                if not isinstance(relation, dict) or url not in relation.get("edoc_urls", []):
                    status_text = "文件存在但物种关联缺失"
                    notes = "去重文献库中已有文件，但该物种的关联记录不完整"
                    problem_species.add(species)
                else:
                    species_file = clean_text(str(relation.get("species_file", "")))
                    if not species_file or not (root / species_file).exists():
                        status_text = "物种指引缺失"
                        notes = "文献本体完整，但物种文件夹中的 HTML 指引不存在"
                        problem_species.add(species)
            rows.append(
                {
                    "genus": genus,
                    "species": species,
                    "category": record.get("category", ""),
                    "reference": record.get("reference", ""),
                    "citation": record.get("citation", ""),
                    "edoc_url": url,
                    "status": status_text,
                    "local_file": local_file,
                    "notes": notes,
                }
            )

        for item in value.get("unavailable", []):
            if not isinstance(item, dict):
                continue
            unavailable_count += 1
            rows.append(
                {
                    "genus": genus,
                    "species": species,
                    "category": item.get("category", ""),
                    "reference": item.get("reference", ""),
                    "citation": item.get("citation", ""),
                    "edoc_url": "",
                    "status": "LDL 未提供整篇文件",
                    "local_file": "",
                    "notes": item.get("reason", ""),
                }
            )

    progress["failed_downloads"] = [
        item
        for item in progress.get("failed_downloads", [])
        if (item.get("species", ""), item.get("edoc_url", "")) in expected_occurrence_keys
        and (item.get("species", ""), item.get("edoc_url", "")) not in resolved_failure_keys
    ]
    unresolved_failures = len(progress.get("failed_downloads", []))
    progress["failed_species"] = sorted(
        api_failure_species
        | {
            str(item.get("species", ""))
            for item in progress.get("failed_downloads", [])
            if item.get("species")
        }
        | problem_species
    )

    problem_rows = [
        row
        for row in rows
        if row["status"]
        not in {"已下载", "LDL 未提供整篇文件"}
    ]
    all_complete = (
        fully_checked_species == len(targets)
        and not problem_rows
        and unresolved_failures == 0
    )
    write_csv(root / "完整性检查明细.csv", VERIFY_FIELDS, rows)
    rebuild_failed_download_report(root, progress)

    result_text = "通过：已下载全部可获取的整篇文献" if all_complete else "未通过：仍有待处理项目"
    report = "\n".join(
        [
            "LDL 属文献完整性检查报告",
            "=" * 36,
            f"属：{genus}",
            f"核查时间：{datetime.now().isoformat(timespec='seconds')}",
            f"结论：{result_text}",
            "",
            f"物种总数：{len(targets)}",
            f"三类引用接口全部核查成功的物种：{fully_checked_species}",
            f"引用接口存在失败的物种：{len(api_failure_species)}",
            f"LDL 当前提供的不重复整篇 EDoc：{len(expected_urls)}",
            f"已确认本地文件有效的 EDoc：{len(valid_urls)}",
            f"当前未解决的下载失败：{unresolved_failures}",
            f"明细中需要处理的行：{len(problem_rows)}",
            f"LDL 有引用但未提供整篇文件：{unavailable_count}",
            "",
            "说明：",
            "- ‘通过’指当前登录账号可见、且 LDL 数据接口提供整篇 EDoc 的文献均已下载。",
            "- LDL 只有引用记录但未提供整篇文件的项目，不计为下载缺失。",
            "- 错误日志.csv 是历史日志，其中的旧错误不代表当前仍失败。",
            "- 当前状态请以本报告、完整性检查明细.csv 和 下载失败待补.csv 为准。",
        ]
    )
    write_text_atomic(root / "完整性检查报告.txt", report + "\n", encoding="utf-8-sig")
    verification["last_report_at"] = datetime.now().isoformat(timespec="seconds")
    verification["last_result"] = "pass" if all_complete else "attention_required"
    return all_complete


def self_test() -> int:
    assert configure_download_mode("fast")["retries"] == 1
    assert ACTIVE_DOWNLOAD_TIMEOUT_MS == 90_000 and ACTIVE_DOWNLOAD_RETRIES == 1
    assert configure_download_mode("stable")["retries"] == 3
    assert ACTIVE_DOWNLOAD_TIMEOUT_MS == 300_000 and ACTIVE_DOWNLOAD_RETRIES == 3
    configure_download_mode("fast")
    sample_state = json.dumps(
        {"searchProps": {"Genus": "Micromus"}, "CombObjID": 7296},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    compressed_state = compress_to_encoded_uri_component(sample_state)
    assert decompress_from_encoded_uri_component(compressed_state) == sample_state
    sample_url = (
        "https://lacewing.tamu.edu/SpeciesCatalog/SimpleSearch/SearchResults/"
        f"MonographRecord?state={compressed_state}"
    )
    assert decode_ldl_state(sample_url)["CombObjID"] == 7296
    assert canonical_monograph_url(
        sample_url.replace("MonographRecord?", "MonographRecord/Taxa?")
    ) == sample_url
    test_records, test_unavailable = collect_citation_records_from_api(
        {
            "Syn_BibObjAnnotList": [
                {
                    "Source_BibObjID": 231,
                    "Source_EDocID": 9401,
                    "SourceAuthYearCite": "Kimmins 1956",
                    "Source_EDocBase": {"EDocID": 9401},
                },
                {
                    "Source_BibObjID": 999,
                    "Source_EDocID": 0,
                    "SourceAuthYearCite": "Example 2000",
                    "Source_EDocBase": None,
                },
            ]
        },
        "taxon",
        "https://lacewing.tamu.edu/api/species/getSpecTaxCitByPk",
    )
    assert test_records[0]["edoc_url"] == "https://lacewing.tamu.edu/edoc/9401"
    assert test_records[0]["reference"] == "231"
    assert test_unavailable[0]["reference"] == "999"
    image_records, _ = collect_citation_records_from_api(
        {
            "Syn_BibObjPartAnnotList": [
                {
                    "BibObjID": 231,
                    "BibObj_EDocID": 9401,
                    "BibObj_EDocBase": {"EDocID": 9401},
                    "Graphic_EDocID": 39142,
                }
            ]
        },
        "image",
        "https://lacewing.tamu.edu/api/species/getSpecFigCitByPk",
    )
    assert image_records[0]["edoc_url"].endswith("/edoc/9401")
    assert edoc_id(
        "https://lacewing.tamu.edu/neuropterida/neur_bibliography/"
        "edoc12/kimmins1956reff231s-9401.pdf"
    ) == "9401"
    assert "secret-session-value" not in safe_error_message(
        "Call log:\n  - cookie: lacewing_sessions=secret-session-value\n  - status: timeout"
    )

    class FakeRangeResponse:
        def __init__(self, payload: bytes, start: int, end: int, total: int) -> None:
            self.status = 206
            self.status_text = "Partial Content"
            self.url = "https://lacewing.tamu.edu/edoc/1"
            self.headers = {
                "content-type": "application/pdf",
                "content-range": f"bytes {start}-{end}/{total}",
                "content-disposition": 'inline; filename="range-test.pdf"',
            }
            self._payload = payload

        def body(self) -> bytes:
            return self._payload

    class FakeRangeRequest:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def get(self, url: str, **kwargs: Any) -> FakeRangeResponse:
            range_value = kwargs["headers"]["Range"]
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", range_value)
            assert match
            start = int(match.group(1))
            end = min(int(match.group(2)), len(self.payload) - 1)
            return FakeRangeResponse(
                self.payload[start : end + 1],
                start,
                end,
                len(self.payload),
            )

    ranged_payload = b"%PDF-1.4\n" + b"x" * (DOWNLOAD_CHUNK_SIZE * 2 + 123)
    ranged_body, ranged_extension, _ = fetch_document(
        FakeRangeRequest(ranged_payload),  # type: ignore[arg-type]
        "https://lacewing.tamu.edu/edoc/1",
    )
    assert ranged_body == ranged_payload
    assert ranged_extension == ".pdf"

    class FakeFailingRequest:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url: str, **kwargs: Any) -> Any:
            self.calls += 1
            raise TimeoutError("simulated timeout")

    failing_request = FakeFailingRequest()
    try:
        request_document_part(
            failing_request,  # type: ignore[arg-type]
            "https://lacewing.tamu.edu/edoc/2",
            0,
            DOWNLOAD_CHUNK_SIZE - 1,
        )
        raise AssertionError("快速首轮的失败请求应立即结束")
    except RuntimeError as exc:
        assert "simulated timeout" in str(exc)
    assert failing_request.calls == 1

    with tempfile.TemporaryDirectory(prefix="ldl_downloader_test_") as temporary:
        base = Path(temporary)
        root = base / "LDL_Testus"
        library = root / "00_去重文献库" / "r000001_demo.pdf"
        write_bytes_atomic(library, b"%PDF-1.4\n% deterministic self-test\n")
        expected_digest = hashlib.sha256(library.read_bytes()).hexdigest()

        first_target = root / "01_按物种分类" / "Testus_alpha" / "literature" / library.name
        second_target = root / "01_按物种分类" / "Testus_beta" / "literature" / library.name
        first_mode, first_pointer = link_into_species(library, first_target, primary=True)
        second_mode, second_pointer = link_into_species(library, second_target, primary=False)

        assert first_mode == "relative-html-pointer-primary"
        assert second_mode == "relative-html-pointer"
        assert first_pointer.exists() and second_pointer.exists()
        assert not first_target.exists() and not second_target.exists()
        assert hashlib.sha256(library.read_bytes()).hexdigest() == expected_digest

        moved = base / "整体移动后的结果"
        shutil.move(str(root), str(moved))
        moved_library = moved / "00_去重文献库" / library.name
        moved_first = moved / first_pointer.relative_to(root)
        moved_second = moved / second_pointer.relative_to(root)
        for pointer in (moved_first, moved_second):
            relative = relative_target(pointer.parent, moved_library)
            resolved = (pointer.parent / relative).resolve()
            assert resolved == moved_library.resolve()
            content = pointer.read_text(encoding="utf-8")
            assert quote(relative.replace("\\", "/"), safe="/._-~") in content

        index_data = {
            "documents": {
                expected_digest: {
                    "path": str(moved_library.relative_to(moved)),
                    "size": moved_library.stat().st_size,
                    "mtime_ns": moved_library.stat().st_mtime_ns,
                    "first_species": "Testus alpha",
                }
            },
            "edocs": {
                "https://lacewing.tamu.edu/edoc/1": {
                    "sha256": expected_digest,
                    "final_url": "self-test",
                }
            },
        }
        progress = {
            "completed_species": ["Testus alpha", "Testus beta"],
            "relations": {},
        }
        assert validate_library(moved, index_data, progress) == []

        checkpoint = moved / RESUME_CHECKPOINT_NAME
        saved_relations = {
            expected_digest: {
                "categories": ["taxon"],
                "references": ["1"],
                "citations": ["Example 2000"],
                "edoc_urls": ["https://lacewing.tamu.edu/edoc/1"],
                "library_file": str(moved_library.relative_to(moved)),
                "species_file": str(moved_first.relative_to(moved)),
                "relative_target": relative_target(moved_first.parent, moved_library),
                "link_mode": "relative-html-pointer-primary",
            }
        }
        save_resume_checkpoint(
            checkpoint,
            "Testus",
            "Testus alpha",
            saved_relations,
            [],
            [
                {
                    "time": "2000-01-01T00:00:00",
                    "species": "Testus alpha",
                    "category": "taxon",
                    "reference": "2",
                    "citation": "Failed example",
                    "edoc_url": "https://lacewing.tamu.edu/edoc/2",
                    "reason": "timeout",
                }
            ],
            complete=True,
            failed=True,
        )
        restored_progress = {
            "genus": "Testus",
            "completed_species": [],
            "relations": {},
            "unavailable": [],
            "failed_downloads": [],
        }
        assert restore_resume_checkpoint(checkpoint, "Testus", restored_progress) == "Testus alpha"
        assert restored_progress["completed_species"] == ["Testus alpha"]
        assert restored_progress["failed_species"] == ["Testus alpha"]
        assert failed_download_rows(restored_progress)[0]["edoc_url"].endswith("/edoc/2")
        assert (
            restored_progress["relations"]["Testus alpha"][expected_digest]["edoc_urls"]
            == ["https://lacewing.tamu.edu/edoc/1"]
        )
        retry_payload = b"%PDF-1.4\n% retry-failed self-test\n"
        index_path = moved / "library_index.json"
        progress_path = moved / "progress.json"
        save_json_atomic(index_path, index_data)
        save_json_atomic(progress_path, restored_progress)
        configure_download_mode("retry-failed")
        retry_success, retry_new, retry_remaining = retry_failed_downloads(
            FakeRangeRequest(retry_payload),  # type: ignore[arg-type]
            "Testus",
            moved,
            moved / "00_去重文献库",
            index_path,
            index_data,
            progress_path,
            restored_progress,
            moved / "错误日志.csv",
        )
        assert (retry_success, retry_new, retry_remaining) == (1, 1, 0)
        assert not restored_progress["failed_downloads"]
        assert "https://lacewing.tamu.edu/edoc/2" in index_data["edocs"]
        configure_download_mode("fast")
        verification = {
            "species": {
                "Testus alpha": {
                    "api": {
                        category: {"ok": True, "error": ""}
                        for _, category in SCREENS
                    },
                    "records": [
                        {
                            "category": "taxon",
                            "reference": "1",
                            "citation": "Downloaded example",
                            "edoc_url": "https://lacewing.tamu.edu/edoc/1",
                        }
                    ],
                    "unavailable": [],
                }
            }
        }
        assert build_integrity_report(
            moved,
            "Testus",
            [SpeciesTarget("Testus alpha", "alpha", "https://example.invalid")],
            index_data,
            restored_progress,
            verification,
        )
        assert not restored_progress["failed_downloads"]
        assert (moved / "完整性检查报告.txt").exists()
        assert (moved / "完整性检查明细.csv").exists()

    print("自检通过：三种下载模式、仅补失败、完整性报告、分段下载、断点恢复、内容去重、原子写入、相对路径指引及整体移动均正常。")
    return 0




# ---------------------------------------------------------------------------
# MCP entry points (non-interactive replacement for the original ``run()``)
# ---------------------------------------------------------------------------


def monograph_url_for(comb_obj_id: int) -> str:
    state = json.dumps({"CombObjID": int(comb_obj_id)}, separators=(",", ":"))
    return f"{MONOGRAPH_URL}?state={compress_to_encoded_uri_component(state)}"


def resolve_source_url(
    genus: str | None = None,
    comb_obj_id: int | None = None,
    monograph_url: str | None = None,
    find_comb_obj_id: Any = None,
) -> str:
    """Return a MonographRecord URL whose state carries a CombObjID in the target genus."""
    if monograph_url:
        canonical = canonical_monograph_url(monograph_url)
        if not canonical:
            raise ValueError("monograph_url must be an LDL contributor MonographRecord URL")
        query = urlparse(monograph_url).query
        url = f"{canonical}?{query}" if query else canonical
        decode_ldl_state(url)
        return url
    if comb_obj_id:
        return monograph_url_for(int(comb_obj_id))
    if genus and find_comb_obj_id:
        found = find_comb_obj_id(genus)
        if not found:
            raise ValueError(f"No current LDL combination found for genus {genus!r}")
        return monograph_url_for(int(found))
    raise ValueError("Provide genus, comb_obj_id or monograph_url")


def _genus_root(output_parent: Path, genus: str) -> Path:
    root_name = f"LDL_{safe_name(genus)}"
    if output_parent.name.casefold() == root_name.casefold() and (
        (output_parent / "progress.json").exists() or (output_parent / "00_去重文献库").exists()
    ):
        return output_parent
    return output_parent / root_name


def save_job_environment(root: Path, source_url: str, transport: str) -> None:
    value = {
        "script_version": SCRIPT_VERSION,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "requests_version": importlib.metadata.version("requests"),
        "transport": transport,
        "source_url": source_url,
        "scope": [name for name, _ in SCREENS],
        "image_gallery_included": False,
        "citation_source": "LDL JSON APIs; no citation-page DOM recognition",
        "download_mode": ACTIVE_DOWNLOAD_MODE,
        "download_policy": DOWNLOAD_MODES[ACTIVE_DOWNLOAD_MODE]["description"],
        "deduplication": "LDL reference number (r#), then SHA-256 of downloaded bytes",
    }
    save_json_atomic(root / "运行环境.json", value)


def run_genus_job(
    request: APIRequestContext,
    source_url: str,
    output_parent: Path,
    download_mode: str = "fast",
    refresh: bool = False,
    verify_only: bool = False,
    on_root: Any = None,
    targets: list[SpeciesTarget] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Run one download / retry-failed / verify pass. Raises LoginExpiredError or KeyboardInterrupt.

    With ``targets`` (a frozen research scope: an order, family or name list) the
    whole scope shares one library, so a paper cited for species in different
    genera is downloaded once. ``label`` names the library folder (LDL_<label>).
    """
    mode_config = configure_download_mode("stable" if verify_only else download_mode)
    output_parent.mkdir(parents=True, exist_ok=True)
    if targets:
        genus, detected_targets = re.sub(r"[^A-Za-z0-9_-]+", "_", label or "scope").strip("_"), list(targets)
    else:
        genus, detected_targets = extract_species_targets(source_url, request)
    print(f"已识别：{genus} 属，共 {len(detected_targets)} 个物种。")
    print("范围：Taxon Citations + Distribution Citations + Image Citations；不含 Image Gallery。")
    print("模式：完整性检查（不下载）" if verify_only else f"模式：{mode_config['label']}。{mode_config['description']}")

    root = _genus_root(output_parent, genus)
    if on_root:
        on_root(root, genus)
    library_dir = root / "00_去重文献库"
    library_dir.mkdir(parents=True, exist_ok=True)
    targets = freeze_or_load_species_snapshot(root, genus, detected_targets, source_url, refresh)
    save_job_environment(root, source_url, "requests + saved LDL browser cookies (MCP background job)")
    progress_path = root / "progress.json"
    index_path = root / "library_index.json"
    error_path = root / "错误日志.csv"
    checkpoint_path = root / RESUME_CHECKPOINT_NAME
    verification_path = root / "完整性核查状态.json"

    progress = load_json(
        progress_path,
        {
            "version": SCRIPT_VERSION,
            "genus": genus,
            "completed_species": [],
            "relations": {},
            "unavailable": [],
            "failed_species": [],
            "failed_downloads": [],
        },
    )
    if progress.get("genus") not in {None, "", genus}:
        raise RuntimeError(f"所选目录已有 {progress.get('genus')} 属的数据，请另选保存目录。")
    progress["genus"] = genus
    progress["version"] = SCRIPT_VERSION
    restored_species = restore_resume_checkpoint(checkpoint_path, genus, progress)
    index_data = load_json(index_path, {"version": SCRIPT_VERSION, "edocs": {}, "references": {}, "documents": {}})
    index_data["version"] = SCRIPT_VERSION
    validate_library(root, index_data, progress)
    repair_species_guides(root, index_data, progress)
    save_json_atomic(index_path, index_data)
    save_json_atomic(progress_path, progress)
    if restored_species:
        rebuild_species_report(root, progress, restored_species)
    completed = set(progress.get("completed_species", []))
    failed_species = set(progress.get("failed_species", []))
    summary: dict[str, Any] = {"genus": genus, "root": str(root), "species_total": len(targets)}

    if verify_only:
        verification = load_json(verification_path, {"version": SCRIPT_VERSION, "genus": genus, "species": {}})
        resumable = (
            not refresh
            and verification.get("genus") == genus
            and verification.get("status") == "in_progress"
            and isinstance(verification.get("species"), dict)
        )
        if not resumable:
            verification = {
                "version": SCRIPT_VERSION,
                "genus": genus,
                "status": "in_progress",
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "species": {},
            }
            save_json_atomic(verification_path, verification)
        verification["version"] = SCRIPT_VERSION
        verification["status"] = "in_progress"
        for number, target in enumerate(targets, start=1):
            if target.scientific_name in verification.get("species", {}):
                continue
            print(f"\n===== 核查 {number}/{len(targets)} =====")
            verification.setdefault("species", {})[target.scientific_name] = scan_species_for_verification(request, target)
            save_json_atomic(verification_path, verification)
        passed = build_integrity_report(root, genus, targets, index_data, progress, verification)
        progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
        verification["status"] = "complete"
        verification["completed_at"] = datetime.now().isoformat(timespec="seconds")
        save_json_atomic(progress_path, progress)
        save_json_atomic(verification_path, verification)
        rebuild_global_reports(root, progress)
        summary.update(
            {
                "verified": True,
                "passed": passed,
                "failed_downloads": len(progress.get("failed_downloads", [])),
                "report": str(root / "完整性检查报告.txt"),
            }
        )
        return summary

    if download_mode == "retry-failed":
        success_count, new_count, remaining = retry_failed_downloads(
            request, genus, root, library_dir, index_path, index_data, progress_path, progress, error_path
        )
        summary.update({"retried_ok": success_count, "new_documents": new_count, "failed_downloads": remaining})
        return summary

    total_new = 0
    total_linked = 0
    positions = {target.scientific_name: number for number, target in enumerate(targets, start=1)}
    pending = targets if refresh else [t for t in targets if t.scientific_name not in completed]
    if not refresh and pending:
        print(f"续传定位：本次从第 {positions[pending[0].scientific_name]} 个物种开始，共待处理 {len(pending)} 个。")
    processed = 0
    active = ""
    try:
        for target in pending:
            active = target.scientific_name
            print(f"\n===== {positions[target.scientific_name]}/{len(targets)} =====")
            new_count, linked_count, complete = process_species(
                request, genus, target, root, library_dir, index_path, index_data, progress, error_path, checkpoint_path
            )
            total_new += new_count
            total_linked += linked_count
            completed.add(target.scientific_name)
            progress["completed_species"] = sorted(completed)
            if complete:
                failed_species.discard(target.scientific_name)
            else:
                failed_species.add(target.scientific_name)
            progress["failed_species"] = sorted(failed_species)
            progress["last_attempted_species"] = target.scientific_name
            progress["last_attempted_sequence"] = positions[target.scientific_name]
            progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
            save_json_atomic(progress_path, progress)
            rebuild_species_report(root, progress, target.scientific_name)
            processed += 1
            if processed % GLOBAL_REPORT_INTERVAL == 0:
                rebuild_global_reports(root, progress)
            active = ""
    except BaseException:
        progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
        save_json_atomic(progress_path, progress)
        if active:
            rebuild_species_report(root, progress, active)
        rebuild_global_reports(root, progress)
        raise

    rebuild_reports(root, progress)
    summary.update(
        {
            "new_documents": total_new,
            "linked": total_linked,
            "unique_documents": len(index_data.get("documents", {})),
            "failed_downloads": len(progress.get("failed_downloads", [])),
            "unavailable_citations": len(progress.get("unavailable", [])),
        }
    )
    return summary
