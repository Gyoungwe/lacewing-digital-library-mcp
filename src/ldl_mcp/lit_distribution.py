"""From downloaded literature to verifiable specimen localities for each taxon.

Pipeline per accepted taxon:

1. name variants: accepted name, original combination, every historical
   combination with the same TaxObjID, and the "cited as" names LDL records
   for each distribution citation (older papers use older names);
2. pages: LDL-cited pages (printed page -> PDF page via running headers) and
   every page that mentions a variant, in documents linked to the taxon;
3. rule extraction of material/type records (``extract``);
4. georeferencing of records without printed coordinates (``gazetteer``),
   constrained to the stated country/province, never guessing between
   distant candidates;
5. cross-checks: point inside the stated country, country present among
   LDL's accepted distribution records;
6. storage through ``evidence.record`` so every quote is re-verified on its page.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import evidence, extract, gazetteer, geo, library
from .client import LDLClient
from .research_scope import load_scope


def _taxa(client: LDLClient, scope_id: str | None, names: list[str] | None) -> list[dict[str, Any]]:
    if scope_id:
        return load_scope(scope_id)["taxa"]
    out = []
    for name in names or []:
        hit = client.resolve_current(name)
        if hit:
            out.append({
                "input_name": name,
                "accepted_name": hit.get("CombGenuToSubs"),
                "CombObjID": hit.get("CombObjID"),
                "TaxObjID": hit.get("CombSpec_TaxObjID"),
                "genus": hit.get("PresCombGenu"),
            })
    return out


def _variants(client: LDLClient, taxon: dict[str, Any], ldl_records: list[dict[str, Any]]) -> list[str]:
    names = [taxon.get("original_combination") or ""]
    try:
        tax_id = taxon.get("TaxObjID")
        names += [r.get("CombGenuToSubs") for r in client.search_name(taxon["accepted_name"]) if tax_id and r.get("CombSpec_TaxObjID") == tax_id]
    except Exception:
        pass
    names += [r.get("CitedAsCombCite") for r in ldl_records]
    return extract.name_variants(taxon["accepted_name"], [n for n in names if n])


def _ldl_records(client: LDLClient, comb: Any) -> list[dict[str, Any]]:
    try:
        return client.distribution_source_records(int(comb), include_bibliography=False)
    except Exception:
        return []


def _ldl_countries(records: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    accepted, doubtful = set(), set()
    for rec in records:
        code = geo.normalize_country(rec.get("RecordFullGeogCite") or rec.get("RecordCountryCite"))
        if not code:
            continue
        status = str(rec.get("RecordStatus") or "").casefold()
        (doubtful if status.startswith("unaccepted") or "doubt" in status else accepted).add(code)
    return accepted, doubtful - accepted


def _in_any(code: str | None, codes: set[str]) -> bool:
    return any(geo.same_country(code, c) for c in codes)


def run(
    client: LDLClient,
    root: str,
    dataset: str,
    scope_id: str | None = None,
    names: list[str] | None = None,
    georeference: bool = True,
    max_pages_per_document: int = 40,
    store: bool = True,
) -> dict[str, Any]:
    lib = library.open_library(root)
    taxa = _taxa(client, scope_id, names)
    if not taxa:
        raise ValueError("No taxa: give scope_id or resolvable names")
    by_edoc: dict[str, list[library.Document]] = defaultdict(list)
    for doc in lib.documents.values():
        for edoc_id in doc.edoc_ids:
            by_edoc[edoc_id].append(doc)
    contexts = []
    for taxon in taxa:
        records = _ldl_records(client, taxon.get("CombObjID"))
        contexts.append((taxon, records, _variants(client, taxon, records)))
    all_variants = {t["accepted_name"]: v for t, _, v in contexts}

    rows: list[dict[str, Any]] = []
    report = []
    for taxon, records, variants in contexts:
        accepted_name = taxon["accepted_name"]
        others = [n for name, v in all_variants.items() if name != accepted_name for n in v]
        ldl_ok, ldl_doubt = _ldl_countries(records)
        cited: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rec in records:
            if rec.get("Source_EDocID"):
                cited[str(rec["Source_EDocID"])].append(rec)
        docs = {d.sha256: d for d in lib.documents.values() if any(geo.fold(s) == geo.fold(accepted_name) for s in d.species)}
        for edoc_id in cited:
            for doc in by_edoc.get(edoc_id, []):
                docs[doc.sha256] = doc
        stats = defaultdict(int)
        stats["documents"] = len(docs)
        stats["ldl_documents_missing_locally"] = sum(1 for e in cited if e not in by_edoc)
        for doc in docs.values():
            if not doc.path.exists():
                continue
            texts = library.page_texts(doc)
            pages = texts["pages"]
            cited_pages: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for edoc_id in doc.edoc_ids:
                for rec in cited.get(edoc_id, []):
                    for number in extract.cited_pdf_pages(rec.get("SourcePageCite"), pages):
                        cited_pages[number].append(rec)
            folded_variants = [geo.fold(v) for v in variants if len(v) >= 6]
            mention_pages = [i + 1 for i, text in enumerate(pages) if any(v in geo.fold(text) for v in folded_variants)]
            selected = sorted(set(mention_pages) | set(cited_pages))[: max(1, max_pages_per_document)]
            stats["pages_scanned"] += len(selected)
            for number in selected:
                page_recs = cited_pages.get(number, [])
                page_countries = {geo.normalize_country(r.get("RecordFullGeogCite")) for r in page_recs} - {None}
                default_country = next(iter(page_countries)) if len(page_countries) == 1 else None
                admin_hints = {geo.admin1_hint(r.get("RecordFullGeogCite")) for r in page_recs} - {None}
                for cand in extract.extract_page(pages[number - 1], variants, others, cited_page=bool(page_recs), default_country=default_country):
                    stats["candidates"] += 1
                    notes: list[str] = [f"section: {cand['section']}", f"attribution: {cand['attribution']}"]
                    needs_review = cand["attribution"] == "ldl_cited_page"
                    country = cand.get("country")
                    row = {
                        "species": accepted_name,
                        "accepted_name": accepted_name,
                        "input_name": taxon.get("input_name"),
                        "country": country,
                        "locality_original": cand["locality_original"],
                        "latitude": cand["latitude"],
                        "longitude": cand["longitude"],
                        "coordinate_basis": cand["coordinate_basis"],
                        "coordinate_precision": cand.get("coordinate_precision"),
                        "elevation_m": cand.get("elevation_m"),
                        "type_status": cand.get("type_status"),
                        "collector": cand.get("collector"),
                        "collection_date": cand.get("collection_date"),
                        "sha256": doc.sha256,
                        "pdf_page": number,
                        "evidence_quote": cand["evidence_quote"],
                        "evidence_level": cand["evidence_level"],
                        "extraction_method": cand["extraction_method"],
                        "attribution": cand["attribution"],
                        "source_citation": doc.citations[0] if doc.citations else None,
                    }
                    if row["latitude"] is not None:
                        stats["with_printed_coordinates"] += 1
                        if cand.get("coordinate_precision") == "degrees":
                            notes.append("printed coordinates are whole degrees only")
                            needs_review = True
                    elif georeference and country:
                        hint = next(iter(admin_hints)) if len(admin_hints) == 1 else None
                        try:
                            result = gazetteer.georeference(cand["locality_original"], country, hint)
                        except Exception as exc:
                            result = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
                        status = result["status"]
                        stats[f"georef_{status}"] += 1
                        if status in {"resolved", "probable"}:
                            row.update({
                                "latitude": result["latitude"],
                                "longitude": result["longitude"],
                                "coordinate_basis": "georeferenced",
                                "coordinate_uncertainty_m": result["coordinate_uncertainty_m"],
                                "georeference_source": result["georeference_source"] + f"; matched '{result['token']}'",
                            })
                            if result.get("note"):
                                notes.append(f"georef: {result['note']}")
                            if status == "probable":
                                needs_review = True
                        elif status == "ambiguous":
                            needs_review = True
                            top = "; ".join(f"{c['name']} ({c['lat']:.3f},{c['lon']:.3f}) {c['feature']}" for c in result["candidates"][:3])
                            notes.append(f"georef ambiguous for '{result['token']}': {top}")
                        elif status == "coarse":
                            notes.append(f"only province-level locality ({result['token']}); not mapped as a point")
                        else:
                            notes.append(f"georef {status}: {result.get('message') or ', '.join(result.get('tried', []))}")
                    elif georeference:
                        stats["georef_no_country"] += 1
                        notes.append("no country recognised; not georeferenced")
                    if row["latitude"] is not None:
                        where = geo.country_at(row["latitude"], row["longitude"])
                        if country and where["code"] and not geo.same_country(where["code"], country):
                            notes.append(f"point falls in {where['name']}, text says {country}")
                            needs_review = True
                            stats["country_mismatch"] += 1
                        elif not where["code"]:
                            notes.append("point falls in the sea at 1:50m")
                            needs_review = True
                    if country and (ldl_ok or ldl_doubt):
                        if _in_any(country, ldl_doubt) and not _in_any(country, ldl_ok):
                            notes.append(f"LDL marks {country} as doubtful for this taxon")
                            needs_review = True
                        elif not _in_any(country, ldl_ok):
                            notes.append(f"{country} not among LDL distribution records (possible new record or misidentification)")
                            needs_review = True
                            stats["outside_ldl_countries"] += 1
                    if needs_review:
                        row["review_status"] = "needs_review"
                    row["notes"] = "; ".join(notes)
                    rows.append(row)
        report.append({"taxon": accepted_name, "name_variants": variants[:12], "ldl_countries": sorted(ldl_ok), "ldl_doubtful": sorted(ldl_doubt), **stats})
    stored = None
    if store and rows:
        stored = {"stored": 0, "quote_verified": 0, "needs_review": 0, "unverified": 0, "duplicates_skipped": 0}
        for start in range(0, len(rows), 500):
            result = evidence.record(dataset, rows[start : start + 500], [str(lib.root)])
            stored["stored"] += result["stored"]
            stored["quote_verified"] += result["quote_verified"]
            stored["unverified"] += sum(1 for r in result["rows"] if r["review_status"] == "unverified")
            stored["needs_review"] += sum(1 for r in result["rows"] if r["review_status"] == "needs_review")
            stored["duplicates_skipped"] += sum(1 for p in result["problems"] if any("duplicate" in i for i in p["issues"]))
    return {
        "dataset": dataset,
        "library": str(lib.root),
        "taxa": len(taxa),
        "candidates": len(rows),
        "stored": stored,
        "per_taxon": report,
        "next_steps": [
            "evidence_table(dataset, review_status='needs_review') to review flagged rows",
            "distribution_dataset(..., evidence_dataset=dataset) then distribution_coverage(dataset_id)",
        ],
        "limits": "Rules read common label formats; records the rules miss can still be added by the agent with record_evidence.",
    }
