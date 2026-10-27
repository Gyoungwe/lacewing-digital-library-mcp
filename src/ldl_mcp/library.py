"""Read-only adapter over libraries produced by the genus downloader.

Supports both layouts written by the original tool and by MCP jobs:

* a genus folder ``LDL_<Genus>/`` with ``library_index.json`` + ``progress.json``;
* a unified / merged library with ``global_library_index.json``.

Nothing here modifies documents. PDF text is cached per SHA-256 under
``<LDL_MCP_HOME>/text_cache`` so repeated searches do not re-parse PDFs.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import now_iso, read_json, sha256_file, subdir, write_json_atomic

TEXT_CACHE_VERSION = 1


@dataclass
class Document:
    sha256: str
    path: Path
    size: int
    references: list[str] = field(default_factory=list)
    edoc_urls: list[str] = field(default_factory=list)
    species: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)

    @property
    def edoc_ids(self) -> list[str]:
        out = []
        for url in self.edoc_urls:
            match = re.search(r"/edoc\d*/(?:.*?-)?(\d+)", url)
            if match and match.group(1) not in out:
                out.append(match.group(1))
        return out

    def public(self, root: Path) -> dict[str, Any]:
        try:
            relative = str(self.path.relative_to(root))
        except ValueError:
            relative = str(self.path)
        return {
            "sha256": self.sha256,
            "file": relative,
            "size": self.size,
            "BibObjIDs": self.references,
            "EDocIDs": self.edoc_ids,
            "species": self.species,
            "categories": self.categories,
            "citation": " | ".join(self.citations[:2]),
        }


@dataclass
class Library:
    root: Path
    layout: str
    genus: str
    documents: dict[str, Document]
    progress: dict[str, Any]
    snapshot: dict[str, Any]


def _add(values: list[str], new: Any) -> None:
    for value in new if isinstance(new, (list, tuple, set)) else [new]:
        value = str(value or "").strip()
        if value and value not in values:
            values.append(value)


def open_library(root: str | Path) -> Library:
    root = Path(root).expanduser().resolve()
    if (root / "library_index.json").exists():
        index = read_json(root / "library_index.json", {}) or {}
        progress = read_json(root / "progress.json", {}) or {}
        snapshot = read_json(root / "物种列表快照.json", {}) or {}
        docs: dict[str, Document] = {}
        for digest, meta in (index.get("documents") or {}).items():
            if isinstance(meta, dict) and meta.get("path"):
                docs[digest] = Document(digest, root / str(meta["path"]), int(meta.get("size", -1)))
        for url, entry in (index.get("edocs") or {}).items():
            if isinstance(entry, dict) and entry.get("sha256") in docs:
                _add(docs[entry["sha256"]].edoc_urls, url)
        for ref, digest in (index.get("references") or {}).items():
            if digest in docs:
                _add(docs[digest].references, ref)
        for species, relations in (progress.get("relations") or {}).items():
            for digest, rel in (relations or {}).items():
                if digest in docs and isinstance(rel, dict):
                    doc = docs[digest]
                    _add(doc.species, species)
                    _add(doc.categories, rel.get("categories", []))
                    _add(doc.references, rel.get("references", []))
                    _add(doc.citations, rel.get("citations", []))
                    _add(doc.edoc_urls, rel.get("edoc_urls", []))
        return Library(root, "genus", str(progress.get("genus") or snapshot.get("genus") or ""), docs, progress, snapshot)
    if (root / "global_library_index.json").exists():
        manifest = read_json(root / "global_library_index.json", {}) or {}
        docs = {}
        for digest, meta in (manifest.get("documents") or {}).items():
            if not isinstance(meta, dict) or not meta.get("path"):
                continue
            doc = Document(digest, root / str(meta["path"]), int(meta.get("size", -1)))
            _add(doc.references, meta.get("references", []))
            _add(doc.edoc_urls, meta.get("edoc_urls", []))
            _add(doc.species, meta.get("species", []))
            _add(doc.categories, meta.get("categories", []))
            _add(doc.citations, meta.get("citations", meta.get("citation", [])))
            docs[digest] = doc
        return Library(root, "unified", "", docs, {}, {})
    raise FileNotFoundError(
        f"{root} is not an LDL library (expected library_index.json or global_library_index.json)"
    )


def find_libraries(parent: str | Path, max_depth: int = 3) -> list[dict[str, Any]]:
    parent = Path(parent).expanduser().resolve()
    found = []
    for marker in ("library_index.json", "global_library_index.json"):
        for path in parent.rglob(marker):
            if len(path.relative_to(parent).parts) <= max_depth + 1:
                found.append({"root": str(path.parent), "layout": "genus" if marker == "library_index.json" else "unified"})
    return found


def status(root: str | Path) -> dict[str, Any]:
    lib = open_library(root)
    progress = lib.progress
    species_total = int(lib.snapshot.get("count") or len(lib.snapshot.get("species", []) or []))
    present = [d for d in lib.documents.values() if d.path.exists()]
    verification = read_json(lib.root / "完整性核查状态.json", {}) or {}
    return {
        "root": str(lib.root),
        "layout": lib.layout,
        "genus": lib.genus,
        "species_total": species_total or None,
        "species_completed": len(progress.get("completed_species", [])),
        "species_with_failures": len(progress.get("failed_species", [])),
        "failed_downloads": len(progress.get("failed_downloads", [])),
        "citations_without_file": len(progress.get("unavailable", [])),
        "documents_indexed": len(lib.documents),
        "documents_present": len(present),
        "bytes_present": sum(d.path.stat().st_size for d in present),
        "last_integrity_check": {
            "result": verification.get("last_result"),
            "at": verification.get("last_report_at"),
        },
        "updated_at": progress.get("updated_at"),
    }


def verify(root: str | Path, full_hash: bool = False) -> dict[str, Any]:
    lib = open_library(root)
    problems = []
    checked = 0
    for doc in lib.documents.values():
        checked += 1
        if not doc.path.exists():
            problems.append({"sha256": doc.sha256, "problem": "missing", "file": str(doc.path)})
            continue
        size = doc.path.stat().st_size
        if doc.size >= 0 and size != doc.size:
            problems.append({"sha256": doc.sha256, "problem": "size_mismatch", "expected": doc.size, "actual": size})
            continue
        if full_hash and sha256_file(doc.path) != doc.sha256:
            problems.append({"sha256": doc.sha256, "problem": "sha256_mismatch", "file": str(doc.path)})
    return {
        "root": str(lib.root),
        "checked": checked,
        "full_hash": full_hash,
        "ok": not problems,
        "problems": problems[:200],
        "problem_count": len(problems),
        "note": "Offline check only. Use download_verify for an online comparison against LDL's current citations.",
    }


def documents(root: str | Path, species: str | None = None, limit: int = 200) -> dict[str, Any]:
    lib = open_library(root)
    wanted = species.casefold() if species else None
    rows = [
        d.public(lib.root)
        for d in sorted(lib.documents.values(), key=lambda d: (d.references[:1] or ["~"], d.sha256))
        if not wanted or any(s.casefold() == wanted for s in d.species)
    ]
    return {"root": str(lib.root), "count": len(rows), "documents": rows[: max(1, min(limit, 2000))]}


# ---------------------------------------------------------------------------
# Text extraction and search
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def page_texts(doc: Document) -> dict[str, Any]:
    """Return cached per-page text for a document; extracts with pypdf on first use."""
    cache = subdir("text_cache") / f"{doc.sha256}.json"
    cached = read_json(cache, None)
    if isinstance(cached, dict) and cached.get("version") == TEXT_CACHE_VERSION:
        return cached
    pages: list[str] = []
    error = ""
    method = "pdf_text_layer"
    try:
        with doc.path.open("rb") as handle:
            head = handle.read(5)
        if head != b"%PDF-":
            method = "unsupported"
            error = f"not a PDF ({doc.path.suffix or 'no extension'})"
        else:
            from pypdf import PdfReader

            reader = PdfReader(str(doc.path))
            for page in reader.pages:
                try:
                    pages.append(_flat(page.extract_text() or ""))
                except Exception as exc:  # one bad page must not hide the rest
                    pages.append("")
                    error = f"some pages failed: {type(exc).__name__}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    value = {
        "version": TEXT_CACHE_VERSION,
        "sha256": doc.sha256,
        "method": method,
        "ocr": False,
        "page_count": len(pages),
        "empty_pages": sum(1 for p in pages if len(p) < 20),
        "error": error,
        "extracted_at": now_iso(),
        "pages": pages,
    }
    write_json_atomic(cache, value)
    return value


def search(
    root: str | Path,
    terms: list[str],
    species: str | None = None,
    sha256: str | None = None,
    match: str = "any",
    max_hits: int = 50,
    context_chars: int = 240,
) -> dict[str, Any]:
    clean = [t.strip() for t in terms if t and t.strip()]
    if not clean:
        raise ValueError("Provide at least one search term")
    if len(clean) > 20:
        raise ValueError("At most 20 terms")
    folded_terms = [_fold(t) for t in clean]
    lib = open_library(root)
    wanted = species.casefold() if species else None
    docs = [
        d
        for d in lib.documents.values()
        if (not sha256 or d.sha256.startswith(sha256))
        and (not wanted or any(s.casefold() == wanted for s in d.species))
        and d.path.exists()
    ]
    hits: list[dict[str, Any]] = []
    scanned = 0
    no_text: list[str] = []
    limit = max(1, min(max_hits, 300))
    for doc in docs:
        texts = page_texts(doc)
        scanned += 1
        if texts["page_count"] and texts["empty_pages"] == texts["page_count"]:
            no_text.append(doc.sha256)
        for number, page in enumerate(texts["pages"], start=1):
            folded = _fold(page)
            present = [t for t in folded_terms if t in folded]
            if not present or (match == "all" and len(present) != len(folded_terms)):
                continue
            pos = folded.find(present[0])
            start = max(0, pos - context_chars)
            end = min(len(page), pos + len(present[0]) + context_chars)
            hits.append(
                {
                    "sha256": doc.sha256,
                    "pdf_page": number,
                    "terms": [clean[folded_terms.index(t)] for t in present],
                    "snippet": page[start:end],
                    "BibObjIDs": doc.references,
                    "EDocIDs": doc.edoc_ids,
                    "species": doc.species,
                    "text_source": texts["method"],
                }
            )
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    return {
        "root": str(lib.root),
        "documents_scanned": scanned,
        "hits": hits,
        "truncated": len(hits) >= limit,
        "documents_without_text_layer": no_text[:50],
        "note": "Scanned PDFs without a text layer cannot be searched until OCR is added; they are listed, not treated as 'no mention'.",
    }


def page_text(root: str | Path, sha256: str, pdf_page: int, max_chars: int = 8000) -> dict[str, Any]:
    lib = open_library(root)
    matches = [d for d in lib.documents.values() if d.sha256.startswith(sha256)]
    if len(matches) != 1:
        raise ValueError(f"sha256 prefix {sha256!r} matched {len(matches)} documents")
    doc = matches[0]
    texts = page_texts(doc)
    if not 1 <= pdf_page <= texts["page_count"]:
        raise ValueError(f"pdf_page must be between 1 and {texts['page_count']}")
    text = texts["pages"][pdf_page - 1]
    return {
        **doc.public(lib.root),
        "pdf_page": pdf_page,
        "page_count": texts["page_count"],
        "text_source": texts["method"],
        "text": text[: max(500, min(max_chars, 20000))],
        "truncated": len(text) > max_chars,
    }


def find_document(sha256: str, roots: list[str | Path]) -> tuple[Library, Document] | None:
    for root in roots:
        try:
            lib = open_library(root)
        except FileNotFoundError:
            continue
        for doc in lib.documents.values():
            if doc.sha256 == sha256:
                return lib, doc
    return None
