"""Bundle a dataset, its evidence, figures and references into one auditable research package."""

from __future__ import annotations

import csv
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from . import evidence
from .client import LDLClient
from .distribution import ROW_FIELDS
from .distribution import load as load_dataset
from .paths import now_iso, read_json, sha256_file, subdir


def _bibtex_entry(bib_id: int, record: Any) -> str:
    rec = record if isinstance(record, dict) else {}
    rec = rec.get("BibObj", rec) if isinstance(rec.get("BibObj"), dict) else rec

    def pick(*keys: str) -> str:
        for key in keys:
            value = rec.get(key)
            if value not in (None, ""):
                return re.sub(r"[{}]", "", str(value)).strip()
        return ""

    fields = {
        "author": pick("Auth", "Author", "Authors"),
        "title": pick("Title", "BibObjTitle"),
        "year": pick("NomYear", "Year", "PubYear"),
        "journal": pick("Journal", "JournalTitle", "Periodical"),
        "volume": pick("Volume", "Vol"),
        "pages": pick("Pages", "PageRange"),
        "note": f"LDL Bibliography of the Neuropterida r#{bib_id}",
        "url": f"https://lacewing.tamu.edu/Bibliography/BiblioRecord?BibObjID={bib_id}",
    }
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
    kind = "article" if fields["journal"] else "misc"
    return f"@{kind}{{ldl{bib_id},\n{body}\n}}\n"


def export_package(
    client: LDLClient,
    dataset_id: str,
    figure_ids: list[str] | None = None,
    include_bibtex: bool = True,
    table_formats: list[str] | None = None,
) -> dict[str, Any]:
    dataset = load_dataset(dataset_id)
    table_formats = table_formats or ["csv", "tsv", "md"]
    folder = subdir("exports") / f"{dataset_id}-{now_iso()[:19].replace(':', '').replace('-', '')}"
    folder.mkdir(parents=True, exist_ok=True)
    rows = dataset["rows"]
    files: list[Path] = []
    for fmt in table_formats:
        path = folder / f"distribution.{fmt}"
        if fmt in {"csv", "tsv"}:
            with path.open("w", encoding="utf-8-sig" if fmt == "csv" else "utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=ROW_FIELDS, extrasaction="ignore", delimiter="," if fmt == "csv" else "\t")
                writer.writeheader()
                writer.writerows(rows)
        elif fmt == "md":
            cols = ["species", "locality_normalized", "country", "latitude", "longitude", "coordinate_basis", "evidence_level", "BibObjID", "pdf_page", "qc_flags"]
            lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            for row in rows:
                lines.append("| " + " | ".join(str(row.get(c) if row.get(c) is not None else "").replace("|", "/") for c in cols) + " |")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            raise ValueError("table_formats may contain csv, tsv, md")
        files.append(path)
    (folder / "qc.json").write_text(json.dumps(dataset.get("qc", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    files.append(folder / "qc.json")
    if dataset.get("evidence_dataset"):
        files.append(evidence.export_csv(dataset["evidence_dataset"], folder / "evidence.csv"))
    bib_ids = sorted({int(x) for r in rows for x in str(r.get("BibObjID") or "").replace(";", " ").split() if x.isdigit()})
    bib_errors = []
    if include_bibtex and bib_ids:
        entries = []
        for bib_id in bib_ids[:500]:
            try:
                entries.append(_bibtex_entry(bib_id, client.bibliography(bib_id)))
            except Exception as exc:
                bib_errors.append({"BibObjID": bib_id, "error": type(exc).__name__})
                entries.append(_bibtex_entry(bib_id, {}))
        (folder / "references.bib").write_text("\n".join(entries), encoding="utf-8")
        files.append(folder / "references.bib")
    for figure_id in figure_ids or []:
        manifest = read_json(subdir("figures") / figure_id / "figure_manifest.json", None)
        if manifest:
            files.extend(p for p in (subdir("figures") / figure_id).iterdir() if p.is_file())
    run_manifest = {
        "created_at": now_iso(),
        "dataset_id": dataset_id,
        "scope_id": dataset.get("scope_id"),
        "dataset_content_sha256": dataset.get("content_sha256"),
        "figures": figure_ids or [],
        "bibliography_errors": bib_errors,
        "files": [],
        "evidence_chain": "scope snapshot -> accepted taxon -> BibObjID/EDocID -> file SHA-256 -> PDF page -> quote -> row -> figure",
    }
    zip_path = folder.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            arcname = path.name if path.parent == folder else f"figures/{path.parent.name}/{path.name}"
            archive.write(path, arcname)
            run_manifest["files"].append({"name": arcname, "sha256": sha256_file(path), "bytes": path.stat().st_size})
        archive.writestr("run_manifest.json", json.dumps(run_manifest, ensure_ascii=False, indent=2))
    (folder / "run_manifest.json").write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"package": str(zip_path), "folder": str(folder), "files": run_manifest["files"], "bibliography_entries": len(bib_ids)}
