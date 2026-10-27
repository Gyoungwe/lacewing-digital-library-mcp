"""Scope-wide metadata harvest (public endpoints, no login, no PDFs).

For every taxon in a frozen scope it caches ``getSpecByPk`` plus the Taxon,
Distribution and Image citation payloads under
``<LDL_MCP_HOME>/metadata/<scope_id>/species/<CombObjID>.json``, then writes:

* ``species_catalog.csv`` – names, authorship, status, extant/extinct, original
  combination, type kind/locality, LDL distribution summary;
* ``citations.csv`` – one row per species × citation (category, BibObjID,
  EDocID, page, geography, record status, whether a whole-work file exists);
* ``literature_plan.json`` – deduplicated documents ranked by species coverage.

It is resumable (cached species are skipped) and checks a stop callback
between species so a background job can pause or cancel cleanly.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import LDLClient
from .downloader import CITATION_APIS, SCREENS, aggregate_records, collect_citation_records_from_api
from .ldl_http import JobPaused
from .paths import now_iso, read_json, subdir, write_json_atomic
from .research_scope import load_scope

CATALOG_FIELDS = (
    "accepted_name", "author", "year", "status", "order", "family", "genus", "extant_extinct",
    "original_combination", "type_kind", "type_locality", "distribution_summary", "CombObjID", "TaxObjID",
)
CITATION_FIELDS = (
    "accepted_name", "category", "BibObjID", "EDocID", "has_file", "source_page", "citation",
    "geography", "record_status", "cited_as",
)


def folder(scope_id: str) -> Path:
    path = subdir("metadata") / scope_id
    (path / "species").mkdir(parents=True, exist_ok=True)
    return path


def harvest(client: LDLClient, scope_id: str, should_stop: Callable[[], str | None] | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    scope = load_scope(scope_id)
    base = folder(scope_id)
    taxa = [t for t in scope["taxa"] if t.get("CombObjID")]
    done = 0
    for number, taxon in enumerate(taxa, start=1):
        path = base / "species" / f"{int(taxon['CombObjID'])}.json"
        if path.exists():
            done += 1
            continue
        if should_stop:
            action = should_stop()
            if action in {"pause", "cancel"}:
                raise JobPaused(action)
        entry: dict[str, Any] = {"scope_taxon": taxon, "fetched_at": now_iso(), "errors": {}}
        try:
            entry["epithet"] = client.epithet(int(taxon["TaxObjID"])) if taxon.get("TaxObjID") else {}
        except Exception as exc:
            entry["errors"]["epithet"] = type(exc).__name__
        for _, category in SCREENS:
            endpoint, _ = CITATION_APIS[category]
            try:
                entry[category] = client.raw_public(f"species/{endpoint}", {"CombObjID": int(taxon["CombObjID"])})
            except Exception as exc:
                entry["errors"][category] = type(exc).__name__
        write_json_atomic(path, entry)
        done += 1
        if number % 25 == 0 or number == len(taxa):
            log(f"[{now_iso()}] metadata {number}/{len(taxa)} {taxon['accepted_name']}")
    return build_outputs(scope_id) | {"species_cached": done}


def _entries(scope_id: str) -> list[dict[str, Any]]:
    base = folder(scope_id) / "species"
    return [e for e in (read_json(p, None) for p in sorted(base.glob("*.json"))) if e and "scope_taxon" in e]


def build_outputs(scope_id: str) -> dict[str, Any]:
    scope = load_scope(scope_id)
    base = folder(scope_id)
    entries = _entries(scope_id)
    catalog, citations, records, unavailable, errors = [], [], [], [], 0
    for entry in entries:
        taxon, epi = entry["scope_taxon"], entry.get("epithet") or {}
        errors += bool(entry.get("errors"))
        catalog.append({
            **{k: taxon.get(k) for k in ("accepted_name", "author", "year", "status", "order", "family", "genus", "CombObjID", "TaxObjID")},
            "extant_extinct": epi.get("ExtantExtinct") or taxon.get("extant_extinct"),
            "original_combination": epi.get("OrigCombSciName"),
            "type_kind": epi.get("EpithPrimTypeKind"),
            "type_locality": epi.get("EpithPrimTypeLocality"),
            "distribution_summary": epi.get("PresCombGeogDistSummary"),
        })
        for _, category in SCREENS:
            payload = entry.get(category)
            if not isinstance(payload, dict):
                continue
            recs, unav = collect_citation_records_from_api(payload, category, category)
            _, fields = CITATION_APIS[category]
            raw_rows = next((payload[f] for f in fields if isinstance(payload.get(f), list) and payload[f]), [])
            for item in raw_rows:
                base_edoc = item.get("Source_EDocBase") or item.get("BibObj_EDocBase")
                edoc = (base_edoc or {}).get("EDocID") if isinstance(base_edoc, dict) else None
                citations.append({
                    "accepted_name": taxon["accepted_name"],
                    "category": category,
                    "BibObjID": item.get("Source_BibObjID") or item.get("BibObjID"),
                    "EDocID": edoc or item.get("Source_EDocID") or item.get("BibObj_EDocID") or "",
                    "has_file": bool(edoc or item.get("Source_EDocID") or item.get("BibObj_EDocID")),
                    "source_page": item.get("SourcePageCite") or item.get("BibObjPagePlateCitation"),
                    "citation": item.get("SourceCiteShort") or item.get("SourceAuthYearCite") or item.get("BibObjCiteShort"),
                    "geography": item.get("RecordFullGeogCite"),
                    "record_status": item.get("RecordStatus"),
                    "cited_as": item.get("CitedAsCombCite") or item.get("NameUsedInWorkCite"),
                })
            records += [{**r, "source_page": taxon["accepted_name"]} for r in recs]
            unavailable += [{"species": taxon["accepted_name"], **u} for u in unav]
    docs = aggregate_records(records)
    for doc in docs:
        doc["species"] = sorted({s for s in doc.pop("source_page").split("; ") if s})
        doc["species_count"] = len(doc["species"])
    docs.sort(key=lambda d: (-d["species_count"], d["reference"]))
    for name, fields, rows in (("species_catalog.csv", CATALOG_FIELDS, catalog), ("citations.csv", CITATION_FIELDS, citations)):
        with (base / name).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    plan = {
        "scope_id": scope_id,
        "built_at": now_iso(),
        "taxa_in_scope": scope["taxon_count"],
        "taxa_harvested": len(entries),
        "complete": len(entries) >= len([t for t in scope["taxa"] if t.get("CombObjID")]),
        "documents_with_file": len(docs),
        "citations_without_file": len(unavailable),
        "species_with_errors": errors,
        "documents": docs,
    }
    write_json_atomic(base / "literature_plan.json", plan)
    return {
        **{k: v for k, v in plan.items() if k != "documents"},
        "citations": len(citations),
        "outputs": {n: str(base / n) for n in ("species_catalog.csv", "citations.csv", "literature_plan.json")},
        "top_documents": docs[:10],
    }


def cached_plan(scope_id: str) -> dict[str, Any] | None:
    return read_json(folder(scope_id) / "literature_plan.json", None)
