"""Distribution datasets: LDL type coordinates + LDL distribution citations + literature evidence, with QC.

Every row keeps ``coordinate_basis`` and ``evidence_level`` so maps can draw only
records with an explicit geographic basis and never present type localities or
country summaries as a complete species distribution.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from . import evidence
from .client import LDLClient
from .paths import now_iso, read_json, stable_id, subdir, write_json_atomic
from .research_scope import load_scope

ROW_FIELDS = (
    "row_id", "species", "accepted_name", "input_name", "TaxObjID", "CombObjID",
    "locality_original", "locality_normalized", "country",
    "latitude", "longitude", "coordinate_basis", "coordinate_uncertainty_m", "georeference_source", "type_status",
    "collector", "collection_date", "source_citation",
    "BibObjID", "EDocID", "sha256", "pdf_page", "evidence_quote",
    "evidence_level", "confidence", "review_status", "source", "qc_flags",
)
MAPPABLE_DEFAULT = ("type_specimen", "literature_locality", "georeferenced", "occurrence")


def _datasets() -> Path:
    return subdir("datasets")


def _decimals(value: float | None) -> int | None:
    if value is None:
        return None
    text = repr(float(value))
    return len(text.split(".")[1].rstrip("0")) if "." in text else 0


def qc(rows: list[dict[str, Any]], extent: list[float] | None = None) -> dict[str, Any]:
    """Flag coordinate problems in-place (``qc_flags``) and return a QC report."""
    flags: Counter[str] = Counter()
    seen: dict[tuple, str] = {}
    for row in rows:
        row_flags: list[str] = []
        lat, lon = row.get("latitude"), row.get("longitude")
        if lat is None and lon is None:
            if row.get("coordinate_basis") in MAPPABLE_DEFAULT:
                row_flags.append("missing_coordinates")
        elif lat is None or lon is None:
            row_flags.append("incomplete_coordinates")
        else:
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                row_flags.append("out_of_range")
            if lat == 0 and lon == 0:
                row_flags.append("zero_zero")
            if (_decimals(lat) or 0) <= 0 and (_decimals(lon) or 0) <= 0:
                row_flags.append("integer_degree_precision")
            if extent and not (extent[0] <= lon <= extent[1] and extent[2] <= lat <= extent[3]):
                row_flags.append("outside_study_extent")
            key = (row.get("TaxObjID") or row.get("species"), round(lat, 4), round(lon, 4))
            if key in seen:
                row_flags.append("duplicate_point")
                row.setdefault("duplicate_of", seen[key])
            else:
                seen[key] = row["row_id"]
        if row.get("coordinate_basis") == "inferred":
            row_flags.append("inferred_coordinate")
        if row.get("coordinate_basis") == "georeferenced" and (
            not row.get("georeference_source") or row.get("coordinate_uncertainty_m") in (None, "")
        ):
            row_flags.append("georeference_incomplete")
        if row.get("review_status") in {"unverified", "rejected"} and row.get("source") == "literature_evidence":
            row_flags.append(f"evidence_{row['review_status']}")
        row["qc_flags"] = ";".join(row_flags)
        flags.update(row_flags)
    blocking = {"out_of_range", "incomplete_coordinates", "zero_zero", "evidence_rejected"}
    blocking_count = sum(flags[f] for f in blocking)
    return {
        "checked_rows": len(rows),
        "flag_counts": dict(flags),
        "blocking_flags": sorted(f for f in blocking if flags[f]),
        "status": "pass" if not blocking_count else "needs_attention",
        "rules": {
            "latitude_range": [-90, 90],
            "longitude_range": [-180, 180],
            "duplicate_key": "TaxObjID + coordinates rounded to 4 decimals",
            "blocking": sorted(blocking),
        },
    }


def build(
    client: LDLClient,
    name: str,
    scope_id: str | None = None,
    names: list[str] | None = None,
    evidence_dataset: str | None = None,
    include_type_coordinates: bool = True,
    include_ldl_distribution: bool = True,
    extent: list[float] | None = None,
) -> dict[str, Any]:
    if scope_id:
        taxa = load_scope(scope_id)["taxa"]
    elif names:
        taxa = []
        for input_name in names:
            hit = client.resolve_current(input_name)
            if hit:
                taxa.append(
                    {
                        "input_name": input_name,
                        "accepted_name": hit.get("CombGenuToSubs"),
                        "CombObjID": hit.get("CombObjID"),
                        "TaxObjID": hit.get("CombSpec_TaxObjID"),
                    }
                )
    else:
        raise ValueError("Provide scope_id or names")
    if extent is not None and len(extent) != 4:
        raise ValueError("extent must be [lon_min, lon_max, lat_min, lat_max]")
    rows: list[dict[str, Any]] = []
    coord_rows = client.type_coordinates() if include_type_coordinates else []
    by_tax: dict[Any, list[dict[str, Any]]] = {}
    for c in coord_rows:
        by_tax.setdefault(c.get("Type_TaxObjID"), []).append(c)
    for taxon in taxa:
        tax_id = taxon.get("TaxObjID")
        base = {
            "species": taxon.get("accepted_name"),
            "accepted_name": taxon.get("accepted_name"),
            "input_name": taxon.get("input_name"),
            "TaxObjID": tax_id,
            "CombObjID": taxon.get("CombObjID"),
        }
        for c in by_tax.get(tax_id, []):
            lat, lon = c.get("LatitudeDecDeg"), c.get("LongitudeDecDeg")
            rows.append(
                {
                    **base,
                    "locality_original": c.get("TypeLocalityQuote") or c.get("TypeLocalityInterpreted"),
                    "locality_normalized": c.get("TypeLocalityInterpreted"),
                    "latitude": float(lat) if lat is not None else None,
                    "longitude": float(lon) if lon is not None else None,
                    "coordinate_basis": "type_specimen",
                    "type_status": c.get("TypeKind") or c.get("TypeSpmnKind"),
                    "evidence_level": "ldl_type_record",
                    "review_status": "ldl_structured",
                    "source": f"LDL general/getTypeSpmnCoordinates TypeSpmnID={c.get('TypeSpmnID')}",
                }
            )
        if include_ldl_distribution and taxon.get("CombObjID"):
            try:
                records = client.distribution_source_records(int(taxon["CombObjID"]), include_bibliography=False)
            except Exception:
                records = []
            for rec in records:
                rows.append(
                    {
                        **base,
                        "locality_original": rec.get("RecordFullGeogCite"),
                        "country": rec.get("RecordCountryCite"),
                        "latitude": None,
                        "longitude": None,
                        "coordinate_basis": "country_summary",
                        "source_citation": rec.get("SourceCiteShort"),
                        "BibObjID": rec.get("Source_BibObjID"),
                        "EDocID": rec.get("Source_EDocID"),
                        "pdf_page": None,
                        "evidence_level": "ldl_distribution_citation",
                        "review_status": "ldl_structured",
                        "source": f"LDL getSpecDistCitByPk page {rec.get('SourcePageCite') or ''}".strip(),
                    }
                )
    if evidence_dataset:
        accepted = {t.get("accepted_name") for t in taxa}
        for ev in evidence.load(evidence_dataset):
            if ev.get("accepted_name") in accepted or ev.get("species") in accepted or not accepted:
                row = {k: ev.get(k) for k in ROW_FIELDS if k in ev}
                row["source"] = "literature_evidence"
                row["species"] = ev.get("accepted_name") or ev.get("species")
                rows.append(row)
    for row in rows:
        row["row_id"] = hashlib.sha256(
            json.dumps([row.get(k) for k in ("species", "locality_original", "latitude", "longitude", "source", "BibObjID", "pdf_page")], default=str).encode()
        ).hexdigest()[:12]
    report = qc(rows, extent)
    content_hash = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    dataset_id = stable_id(name, {"scope": scope_id, "names": names, "hash": content_hash}, 8)
    folder = _datasets() / dataset_id
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "dataset.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    basis_counts = Counter(str(r.get("coordinate_basis")) for r in rows)
    meta = {
        "dataset_id": dataset_id,
        "name": name,
        "scope_id": scope_id,
        "names": names,
        "evidence_dataset": evidence_dataset,
        "created_at": now_iso(),
        "row_count": len(rows),
        "taxon_count": len(taxa),
        "coordinate_basis_counts": dict(basis_counts),
        "content_sha256": content_hash,
        "extent": extent,
        "definitions": {
            "type_specimen": "LDL georeferenced type-specimen locality; not a complete species distribution.",
            "country_summary": "LDL distribution citation (country/region); never plotted as a point.",
            "literature_locality": "Locality from a cited document page with a verifiable quote.",
        },
    }
    write_json_atomic(folder / "dataset.json", {**meta, "rows": rows})
    write_json_atomic(folder / "qc.json", report)
    return {**meta, "qc": report, "folder": str(folder)}


def load(dataset_id: str) -> dict[str, Any]:
    value = read_json(_datasets() / dataset_id / "dataset.json", None)
    if not value:
        raise FileNotFoundError(f"Unknown dataset_id {dataset_id!r}")
    value["qc"] = read_json(_datasets() / dataset_id / "qc.json", {})
    value["folder"] = str(_datasets() / dataset_id)
    return value
