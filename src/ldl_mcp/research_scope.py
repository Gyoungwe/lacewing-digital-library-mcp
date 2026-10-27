"""Frozen taxon scopes and literature plans.

A scope snapshot records exactly which accepted taxa a study used, when, and
against which LDL data version, so later evidence tables, maps and exports can
cite one immutable ``scope_id``. A literature plan expands a scope into the
deduplicated set of cited EDocs without downloading anything.
"""

from __future__ import annotations

from typing import Any

from .client import LDLClient
from .downloader import CITATION_APIS, SCREENS, aggregate_records, collect_citation_records_from_api
from .paths import now_iso, read_json, stable_id, subdir, write_json_atomic


def _scopes():
    return subdir("scopes")


def _plans():
    return subdir("plans")


def _taxon_row(client: LDLClient, hit: dict[str, Any], input_name: str) -> dict[str, Any]:
    tax_id = int(hit.get("CombSpec_TaxObjID") or 0)
    epi = client.epithet(tax_id) if tax_id else {}
    return {
        "input_name": input_name,
        "accepted_name": hit.get("CombGenuToSubs")
        or " ".join(p for p in [hit.get("PresCombGenu"), hit.get("PresCombSpec"), hit.get("PresCombSubs")] if p),
        "author": hit.get("ASAuth"),
        "year": hit.get("ASYear"),
        "status": hit.get("CombTaxStatus"),
        "CombObjID": hit.get("CombObjID"),
        "TaxObjID": tax_id or None,
        "family": hit.get("PresCombFami") or epi.get("PresFami"),
        "genus": hit.get("PresCombGenu"),
        "extant_extinct": epi.get("ExtantExtinct"),
        "original_combination": epi.get("OrigCombSciName"),
    }


def freeze_scope(
    client: LDLClient,
    genus: str | None = None,
    family: str | None = None,
    names: list[str] | None = None,
    extant_only: bool = False,
    label: str | None = None,
) -> dict[str, Any]:
    if not (genus or family or names):
        raise ValueError("Provide genus, family or names")
    taxa: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    if names:
        if len(names) > 500:
            raise ValueError("At most 500 names per scope")
        for name in names:
            try:
                hit = client.resolve_current(name)
            except Exception as exc:
                unresolved.append({"input_name": name, "reason": type(exc).__name__})
                continue
            if not hit:
                unresolved.append({"input_name": name, "reason": "no LDL match"})
                continue
            taxa.append(_taxon_row(client, hit, name))
    else:
        rows = client.search_taxa(genus=genus, family=family, valid_only=True, extant_only=extant_only, limit=1000)
        by_tax: dict[int, dict[str, Any]] = {}
        for row in rows:
            tax_id = row.get("CombSpec_TaxObjID")
            if tax_id and row.get("CombTaxStatus") == "valid":
                by_tax.setdefault(int(tax_id), row)
        for row in sorted(by_tax.values(), key=lambda r: str(r.get("CombGenuToSubs") or "").casefold()):
            taxa.append(_taxon_row(client, row, str(row.get("CombGenuToSubs") or "")))
    seen: set[Any] = set()
    unique = []
    for row in taxa:
        key = row["TaxObjID"] or row["CombObjID"]
        if key in seen:
            row["duplicate_of_taxon"] = True
        seen.add(key)
        unique.append(row)
    query = {"genus": genus, "family": family, "names": names, "extant_only": extant_only}
    try:
        versions = client.species_versions()
    except Exception:
        versions = None
    scope_id = stable_id(label or genus or family or "names", {"query": query, "taxa": [t["TaxObjID"] for t in unique]})
    counts: dict[str, int] = {}
    for row in unique:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    fossil = sum(1 for r in unique if str(r.get("extant_extinct") or "").casefold().startswith("extinct"))
    snapshot = {
        "scope_id": scope_id,
        "label": label,
        "query": query,
        "frozen_at": now_iso(),
        "ldl_species_versions": versions,
        "taxon_count": len(unique),
        "status_counts": counts,
        "extinct_count": fossil,
        "unresolved_count": len(unresolved),
        "taxa": unique,
        "unresolved": unresolved,
    }
    path = _scopes() / f"{scope_id}.json"
    if not path.exists():
        write_json_atomic(path, snapshot)
    return read_json(path, snapshot)


def load_scope(scope_id: str) -> dict[str, Any]:
    value = read_json(_scopes() / f"{scope_id}.json", None)
    if not value:
        raise FileNotFoundError(f"Unknown scope_id {scope_id!r}")
    return value


def list_scopes() -> list[dict[str, Any]]:
    out = []
    for path in sorted(_scopes().glob("*.json")):
        value = read_json(path, {}) or {}
        out.append({k: value.get(k) for k in ("scope_id", "label", "query", "frozen_at", "taxon_count", "unresolved_count")})
    return out


def literature_plan(client: LDLClient, scope_id: str, categories: list[str] | None = None) -> dict[str, Any]:
    scope = load_scope(scope_id)
    wanted = [c for _, c in SCREENS if not categories or c in categories]
    records: list[dict[str, str]] = []
    unavailable: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for taxon in scope["taxa"]:
        comb = taxon.get("CombObjID")
        if not comb:
            continue
        for category in wanted:
            endpoint, _ = CITATION_APIS[category]
            try:
                payload = client.raw_public(f"species/{endpoint}", {"CombObjID": int(comb)})
                recs, unav = collect_citation_records_from_api(payload if isinstance(payload, dict) else {}, category, endpoint)
            except Exception as exc:
                errors.append({"taxon": taxon["accepted_name"], "category": category, "error": type(exc).__name__})
                continue
            for rec in recs:
                records.append({**rec, "source_page": taxon["accepted_name"]})
            for item in unav:
                unavailable.append({"species": taxon["accepted_name"], **item})
    docs = aggregate_records(records)
    for doc in docs:
        doc["species"] = sorted({s for s in doc.pop("source_page").split("; ") if s})
        doc["species_count"] = len(doc["species"])
    by_ref: dict[str, dict[str, Any]] = {}
    for doc in docs:
        key = doc["reference"].split("; ")[0] if doc["reference"] else doc["edoc_url"]
        if key in by_ref:
            by_ref[key].setdefault("alternate_edoc_urls", []).append(doc["edoc_url"])
            by_ref[key]["species"] = sorted(set(by_ref[key]["species"]) | set(doc["species"]))
            by_ref[key]["species_count"] = len(by_ref[key]["species"])
        else:
            by_ref[key] = doc
    planned = sorted(by_ref.values(), key=lambda d: (-d["species_count"], d["reference"]))
    plan_id = stable_id(f"plan-{scope_id}", {"scope": scope_id, "categories": wanted, "docs": [d["edoc_url"] for d in planned]}, 10)
    plan = {
        "plan_id": plan_id,
        "scope_id": scope_id,
        "categories": wanted,
        "created_at": now_iso(),
        "documents_with_file": len(planned),
        "citations_without_file": len(unavailable),
        "api_errors": errors,
        "documents": planned,
        "citations_without_file_rows": unavailable,
        "dedup_rule": "BibObjID (r#) first, then EDoc URL; final SHA-256 dedup happens at download time.",
    }
    write_json_atomic(_plans() / f"{plan_id}.json", plan)
    return plan
