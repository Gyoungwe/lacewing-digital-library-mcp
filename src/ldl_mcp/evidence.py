"""Evidence table: literature-derived records with a verifiable quote → page → file chain.

Division of labour: the agent reads pages (``library_search`` /
``library_page_text``) and proposes structured rows; this module checks every
row against the cached page text before storing it. A row whose quote cannot be
found on the cited page is kept but marked ``quote_not_found`` so it can never
silently pass as verified evidence.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .library import _flat, _fold, find_document, page_texts
from .paths import now_iso, subdir

EVIDENCE_LEVELS = {
    "primary_type_label",
    "primary_material",
    "literature_distribution_summary",
    "ldl_type_record",
    "ldl_distribution_citation",
}
COORDINATE_BASES = {"type_specimen", "literature_locality", "georeferenced", "occurrence", "country_summary", "inferred", "none"}
FIELDS = (
    "evidence_id", "dataset", "species", "accepted_name", "input_name",
    "locality_original", "locality_normalized", "country",
    "latitude", "longitude", "coordinate_basis", "coordinate_uncertainty_m", "type_status",
    "collector", "collection_date", "source_citation",
    "BibObjID", "EDocID", "sha256", "pdf_page", "evidence_quote",
    "evidence_level", "confidence", "review_status", "quote_check", "notes", "recorded_at",
)


def _path(dataset: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", dataset):
        raise ValueError("dataset must be 1-80 chars of letters, digits, . _ -")
    return subdir("evidence") / f"{dataset}.jsonl"


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_quote(quote: str, page: str) -> str:
    if not quote or not quote.strip():
        return "no_quote"
    q, p = _fold(_flat(quote)), _fold(_flat(page))
    if q in p:
        return "exact"
    # tolerate hyphenation / line-break artefacts from PDF text layers
    squash = lambda s: re.sub(r"[\W_]+", "", s)
    if squash(q) and squash(q) in squash(p):
        return "normalized"
    return "not_found"


def record(dataset: str, rows: list[dict[str, Any]], library_roots: list[str]) -> dict[str, Any]:
    if not rows:
        raise ValueError("Provide at least one row")
    if len(rows) > 500:
        raise ValueError("At most 500 rows per call")
    existing = {r["evidence_id"] for r in load(dataset)}
    stored, problems = [], []
    with _path(dataset).open("a", encoding="utf-8") as handle:
        for number, raw in enumerate(rows, start=1):
            row = {k: raw.get(k) for k in FIELDS if k in raw}
            issues: list[str] = []
            lat, lon = _number(row.get("latitude")), _number(row.get("longitude"))
            row["latitude"], row["longitude"] = lat, lon
            if (lat is None) != (lon is None):
                issues.append("only one coordinate component")
            if lat is not None and not -90 <= lat <= 90:
                issues.append("latitude out of range")
            if lon is not None and not -180 <= lon <= 180:
                issues.append("longitude out of range")
            basis = row.get("coordinate_basis") or ("none" if lat is None else "literature_locality")
            if basis not in COORDINATE_BASES:
                issues.append(f"unknown coordinate_basis {basis!r}")
            row["coordinate_basis"] = basis
            level = row.get("evidence_level") or "primary_material"
            if level not in EVIDENCE_LEVELS:
                issues.append(f"unknown evidence_level {level!r}")
            row["evidence_level"] = level
            quote_check = "no_document"
            sha = str(row.get("sha256") or "")
            page_no = int(row.get("pdf_page") or 0)
            if sha:
                found = find_document(sha, library_roots)
                if not found:
                    quote_check = "document_not_in_library"
                else:
                    texts = page_texts(found[1])
                    if not 1 <= page_no <= texts["page_count"]:
                        quote_check = "page_out_of_range"
                    else:
                        quote_check = check_quote(str(row.get("evidence_quote") or ""), texts["pages"][page_no - 1])
                    row.setdefault("BibObjID", "; ".join(found[1].references))
                    row.setdefault("EDocID", "; ".join(found[1].edoc_ids))
            row["quote_check"] = quote_check
            verified = quote_check in {"exact", "normalized"}
            requested = row.get("review_status")
            row["review_status"] = "quote_verified" if verified else "unverified"
            if requested == "needs_review":
                row["review_status"] = "needs_review"
            elif requested and requested not in {"quote_verified", "unverified"}:
                issues.append(f"review_status {requested!r} ignored; human decisions go through review_evidence")
            if requested == "quote_verified" and not verified:
                issues.append("review_status downgraded: quote not found on cited page")
            row["dataset"] = dataset
            row["recorded_at"] = now_iso()
            identity = json.dumps(
                [row.get("species"), row.get("locality_original"), lat, lon, sha, page_no, row.get("evidence_quote")],
                ensure_ascii=False,
            )
            row["evidence_id"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            if issues:
                row["notes"] = "; ".join(filter(None, [str(row.get("notes") or ""), *issues]))
                problems.append({"row": number, "issues": issues})
            if row["evidence_id"] in existing:
                problems.append({"row": number, "issues": ["duplicate of an existing evidence row; skipped"]})
                continue
            existing.add(row["evidence_id"])
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            stored.append(row)
    return {
        "dataset": dataset,
        "stored": len(stored),
        "quote_verified": sum(1 for r in stored if r["review_status"] == "quote_verified"),
        "unverified": sum(1 for r in stored if r["review_status"] != "quote_verified"),
        "problems": problems,
        "rows": stored,
    }


def load(dataset: str) -> list[dict[str, Any]]:
    path = _path(dataset)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def set_review(dataset: str, evidence_ids: list[str], review_status: str, note: str = "") -> dict[str, Any]:
    if review_status not in {"accepted", "rejected", "needs_review", "quote_verified", "unverified"}:
        raise ValueError("review_status must be accepted, rejected, needs_review, quote_verified or unverified")
    rows = load(dataset)
    wanted = set(evidence_ids)
    changed = 0
    for row in rows:
        if row.get("evidence_id") in wanted:
            row["review_status"] = review_status
            if note:
                row["notes"] = "; ".join(filter(None, [str(row.get("notes") or ""), f"review: {note}"]))
            changed += 1
    path = _path(dataset)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)
    return {"dataset": dataset, "changed": changed}


def export_csv(dataset: str, path: Path) -> Path:
    rows = load(dataset)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def table(dataset: str, review_status: str | None = None, limit: int = 500) -> dict[str, Any]:
    rows = load(dataset)
    if review_status:
        rows = [r for r in rows if r.get("review_status") == review_status]
    counts: dict[str, int] = {}
    for row in load(dataset):
        counts[str(row.get("review_status"))] = counts.get(str(row.get("review_status")), 0) + 1
    return {"dataset": dataset, "total": len(load(dataset)), "review_counts": counts, "rows": rows[: max(1, min(limit, 5000))]}
