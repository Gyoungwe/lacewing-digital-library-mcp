"""Merge several ``LDL_<Genus>`` libraries into one SHA-256-deduplicated unified library.

Cross-platform replacement for the Windows-only merge/organize tools. It is
deliberately non-destructive: source genus folders are never modified or
deleted. Each unique document is copied (or hard-linked when possible) into
``<destination>/00_唯一文献`` and verified by SHA-256 before the index is written.
The resulting ``global_library_index.json`` uses the same document fields as the
original LDL 文献管理器, so ``library_*`` tools and the old manager can both read it.
"""

from __future__ import annotations

import csv
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .library import open_library
from .paths import now_iso, read_json, sha256_file, write_json_atomic

DESTINATION_NAME = "LDL_统一文献库"
LIBRARY_NAME = "00_唯一文献"
REPORT_NAME = "02_报告"
MANIFEST_NAME = "global_library_index.json"
CATALOG_FIELDS = ("sha256", "clear_name", "references", "edoc_urls", "genera", "species", "categories", "citations", "global_file", "source_files")


def _safe(value: str, limit: int) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", re.sub(r"\s+", " ", value or "")).strip(" .")
    return (value[:limit].rstrip(" .") or "document")


def _ref_key(value: str) -> tuple[int, str]:
    return (int(value), value) if value.isdigit() else (10**12, value)


def _clear_name(digest: str, item: dict[str, Any], suffix: str) -> str:
    refs = sorted(item["references"], key=_ref_key)
    prefix = f"r{int(refs[0]):06d}" if refs and refs[0].isdigit() else "document"
    citation = item["citations"][0] if item["citations"] else ""
    citation = re.sub(r"^r#\d+\s*\|\s*", "", citation)
    return _safe(f"{prefix}_{digest[:10]}_{_safe(citation, 90)}", 135) + (suffix or ".pdf")


def _place(source: Path, target: Path, digest: str, mode: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    if temporary.exists():
        temporary.unlink()
    used = "copy"
    if mode == "hardlink":
        try:
            os.link(source, temporary)
            used = "hardlink"
        except OSError:
            shutil.copy2(source, temporary)
    else:
        shutil.copy2(source, temporary)
    if sha256_file(temporary) != digest:
        temporary.unlink()
        raise RuntimeError(f"SHA-256 mismatch after {used}: {source}")
    os.replace(temporary, target)
    return used


def merge_libraries(
    parent: str | Path,
    destination: str | Path | None = None,
    mode: str = "copy",
    dry_run: bool = True,
) -> dict[str, Any]:
    if mode not in {"copy", "hardlink"}:
        raise ValueError("mode must be copy or hardlink")
    parent = Path(parent).expanduser().resolve()
    dest = Path(destination).expanduser().resolve() if destination else parent / DESTINATION_NAME
    roots = sorted(p.parent for p in parent.rglob("library_index.json") if dest not in p.parents)
    if not roots:
        raise FileNotFoundError(f"No LDL_<Genus> libraries (library_index.json) under {parent}")

    items: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for root in roots:
        lib = open_library(root)
        genus = lib.genus or root.name.removeprefix("LDL_")
        for doc in lib.documents.values():
            if not doc.path.exists():
                warnings.append(f"missing file skipped: {doc.path}")
                continue
            if doc.size >= 0 and doc.path.stat().st_size != doc.size:
                warnings.append(f"size differs from index, skipped: {doc.path}")
                continue
            item = items.setdefault(
                doc.sha256,
                {"sources": [], "references": set(), "edoc_urls": set(), "genera": set(), "species": set(),
                 "categories": set(), "citations": [], "associations": set(), "size": doc.size},
            )
            item["sources"].append(doc.path)
            item["references"].update(doc.references)
            item["edoc_urls"].update(doc.edoc_urls)
            item["genera"].add(genus)
            item["species"].update(doc.species)
            item["categories"].update(doc.categories)
            item["citations"] += [c for c in doc.citations if c not in item["citations"]]
            item["associations"].update((genus, s) for s in doc.species)

    source_files = sum(len(i["sources"]) for i in items.values())
    source_bytes = sum(p.stat().st_size for i in items.values() for p in i["sources"])
    unique_bytes = sum(i["sources"][0].stat().st_size for i in items.values())
    summary: dict[str, Any] = {
        "parent": str(parent),
        "destination": str(dest),
        "genus_libraries": [str(r) for r in roots],
        "unique_documents": len(items),
        "source_files": source_files,
        "duplicate_files_across_genera": source_files - len(items),
        "source_bytes": source_bytes,
        "unique_bytes": unique_bytes,
        "mode": mode,
        "dry_run": dry_run,
        "warnings": warnings[:100],
        "sources_modified": False,
    }
    if dry_run:
        summary["next_step"] = "Re-run with dry_run=False to build the unified library. Source folders are never deleted."
        return summary

    library_dir = dest / LIBRARY_NAME
    report_dir = dest / REPORT_NAME
    manifest = read_json(dest / MANIFEST_NAME, None) or {"version": 1, "created_at": now_iso(), "documents": {}}
    manifest.update({"updated_at": now_iso(), "source_parent": str(parent), "generator": "lacewing-digital-library-mcp merge"})
    rows, placed = [], {"copy": 0, "hardlink": 0, "reused": 0}
    for digest, item in sorted(items.items()):
        existing = manifest["documents"].get(digest, {})
        target = dest / existing["path"] if existing.get("path") else None
        if target is not None and target.exists() and sha256_file(target) == digest:
            placed["reused"] += 1
        else:
            source = next((p for p in item["sources"] if sha256_file(p) == digest), None)
            if source is None:
                warnings.append(f"no source matches indexed SHA-256 {digest[:12]}; not merged")
                continue
            target = library_dir / _clear_name(digest, item, source.suffix)
            if target.exists() and sha256_file(target) != digest:
                target = target.with_name(f"{target.stem}_{digest[:10]}{target.suffix}")
            if not target.exists():
                placed[_place(source, target, digest, mode)] += 1
            else:
                placed["reused"] += 1
        old = existing if isinstance(existing, dict) else {}
        refs = sorted(set(old.get("references", [])) | item["references"], key=_ref_key)
        species = sorted(set(old.get("species", [])) | item["species"])
        genera = sorted(set(old.get("genera", [])) | item["genera"])
        associations = sorted({(a.get("genus"), a.get("species")) for a in old.get("associations", [])} | item["associations"])
        manifest["documents"][digest] = {
            "path": str(target.relative_to(dest)),
            "clear_name": target.name,
            "size": target.stat().st_size,
            "references": refs,
            "edoc_urls": sorted(set(old.get("edoc_urls", [])) | item["edoc_urls"]),
            "genera": genera,
            "species": species,
            "categories": sorted(set(old.get("categories", [])) | item["categories"]),
            "citations": list(dict.fromkeys(list(old.get("citations", [])) + item["citations"])),
            "associations": [{"genus": g, "species": s} for g, s in associations],
        }
        doc = manifest["documents"][digest]
        rows.append({
            "sha256": digest, "clear_name": target.name, "references": "; ".join(refs),
            "edoc_urls": "; ".join(doc["edoc_urls"]), "genera": "; ".join(genera), "species": "; ".join(species),
            "categories": "; ".join(doc["categories"]), "citations": " | ".join(doc["citations"][:3]),
            "global_file": doc["path"], "source_files": "; ".join(str(p) for p in item["sources"]),
        })
    write_json_atomic(dest / MANIFEST_NAME, manifest)
    report_dir.mkdir(parents=True, exist_ok=True)
    with (report_dir / "全局文献目录.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    report = [
        "LDL 跨属合并报告（MCP，非破坏性）",
        "=" * 36,
        f"时间：{now_iso()}",
        f"源文件夹：{parent}",
        f"统一文献库：{dest}",
        f"属文献库：{len(roots)}",
        f"唯一文献：{len(rows)}；跨属重复文件：{source_files - len(items)}",
        f"新复制：{placed['copy']}；新硬链接：{placed['hardlink']}；复用已有：{placed['reused']}",
        "说明：源属文件夹未做任何修改或删除；每个统一库文件都经过 SHA-256 校验。",
        *([f"警告：{w}" for w in warnings[:50]]),
    ]
    (report_dir / "合并报告.txt").write_text("\n".join(report) + "\n", encoding="utf-8-sig")
    summary.update({"placed": placed, "merged_documents": len(rows), "warnings": warnings[:100], "report": str(report_dir / "合并报告.txt")})
    return summary
