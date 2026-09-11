from __future__ import annotations

from io import BytesIO
import re
from typing import Any

from pypdf import PdfReader

from mcp.server import FastMCP

from .client import LDLClient

mcp = FastMCP("lacewing-digital-library")
_client = LDLClient()


def _resolved_context(scientific_name: str) -> tuple[dict[str, Any], int, int]:
    hit = _client.resolve_current(scientific_name)
    if not hit:
        raise ValueError(f"No LDL match for {scientific_name!r}")
    comb_id = int(hit["CombObjID"])
    tax_id = int(hit.get("CombSpec_TaxObjID") or 0)
    if not tax_id:
        raise ValueError(f"Resolved row for {scientific_name!r} has no species TaxObjID")
    return hit, comb_id, tax_id


def _accepted_summary(hit: dict[str, Any], epi: dict[str, Any] | None = None) -> dict[str, Any]:
    epi = epi or {}
    return {
        "accepted_name": hit.get("CombGenuToSubs") or " ".join(
            p for p in [hit.get("PresCombGenu"), hit.get("PresCombSpec"), hit.get("PresCombSubs")] if p
        ),
        "accepted_citation": hit.get("PresCombCite") or hit.get("CombCiteShort"),
        "status": hit.get("CombTaxStatus"),
        "author": hit.get("ASAuth"),
        "year": hit.get("ASYear"),
        "CombObjID": hit.get("CombObjID"),
        "TaxObjID": hit.get("CombSpec_TaxObjID"),
        "family": hit.get("PresCombFami") or epi.get("PresFami"),
        "order": hit.get("PresCombOrde") or epi.get("PresOrde"),
        "extant_extinct": epi.get("ExtantExtinct"),
        "original_combination": epi.get("OrigCombSciName"),
    }


@mcp.tool()
def health_check() -> dict[str, Any]:
    """Check that the MCP server can reach LDL and report dataset/version information."""
    return {"ok": True, "species_versions": _client.species_versions()}


@mcp.tool()
def resolve_taxon(scientific_name: str, include_history: bool = True) -> dict[str, Any]:
    """Resolve a Neuropterida scientific name to LDL's current taxon and identifiers."""
    hit, _, tax_id = _resolved_context(scientific_name)
    epi = _client.epithet(tax_id)
    result = {
        "input_name": scientific_name,
        **_accepted_summary(hit, epi),
        "nomenclatural_status": epi.get("EpithNomStatusShort"),
        "taxonomic_status": epi.get("EpithTaxStatusShort"),
        "type_kind": epi.get("EpithPrimTypeKind"),
        "type_locality": epi.get("EpithPrimTypeLocality"),
        "distribution_summary": epi.get("PresCombGeogDistSummary"),
    }
    if include_history:
        result["matching_name_history"] = _client.search_name(scientific_name)
    return result


@mcp.tool()
def search_taxa(
    genus: str | None = None,
    species: str | None = None,
    family: str | None = None,
    author: str | None = None,
    year: str | None = None,
    valid_only: bool = False,
    extant_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Search LDL species records by common taxonomic fields."""
    if not any([genus, species, family, author, year]):
        raise ValueError("Provide at least one search field")
    return _client.search_taxa(
        genus=genus,
        species=species,
        family=family,
        author=author,
        year=year,
        valid_only=valid_only,
        extant_only=extant_only,
        limit=limit,
    )


@mcp.tool()
def name_history(scientific_name: str) -> dict[str, Any]:
    """Return current name, original combination, and matching historical combination rows."""
    hit, _, tax_id = _resolved_context(scientific_name)
    epi = _client.epithet(tax_id)
    rows = [r for r in _client.search_name(scientific_name) if r.get("CombSpec_TaxObjID") == tax_id]
    return {
        "input_name": scientific_name,
        "accepted": _accepted_summary(hit, epi),
        "original_combination": epi.get("OrigCombSciName"),
        "original_combination_citation": epi.get("OrigCombCite"),
        "matching_combination_history": rows,
        "note": "Historical combinations are not automatically nomenclatural synonyms; interpret LDL status fields.",
    }


@mcp.tool()
def taxon_record(scientific_name: str) -> dict[str, Any]:
    """Get a combined species record with current combination, epithet, and monograph data."""
    hit, comb_id, tax_id = _resolved_context(scientific_name)
    return {
        "resolved": _accepted_summary(hit),
        "combination": _client.combination(comb_id),
        "epithet": _client.epithet(tax_id),
        "monograph": _client.monograph(comb_id),
    }


@mcp.tool()
def type_info(scientific_name: str, include_coordinates: bool = True) -> dict[str, Any]:
    """Get primary type summary, type-specimen details, and georeferenced type-locality coordinates."""
    hit, comb_id, tax_id = _resolved_context(scientific_name)
    epi = _client.epithet(tax_id)
    result = {
        "resolved": _accepted_summary(hit, epi),
        "type_kind": epi.get("EpithPrimTypeKind"),
        "type_locality": epi.get("EpithPrimTypeLocality"),
        "type_record": _client.type_record(comb_id),
    }
    if include_coordinates:
        result["type_coordinates"] = _client.type_coordinates(tax_id)
        result["coordinate_scope_note"] = "These are LDL georeferenced type-specimen localities, not species occurrence records."
    return result


@mcp.tool()
def distribution(scientific_name: str) -> dict[str, Any]:
    """Get LDL distribution summary and cited geographic records for a taxon."""
    hit, comb_id, tax_id = _resolved_context(scientific_name)
    epi = _client.epithet(tax_id)
    return {
        "resolved": _accepted_summary(hit, epi),
        "distribution_summary": epi.get("PresCombGeogDistSummary"),
        "distribution_note": epi.get("PresCombGeogDistNote"),
        "distribution_citations": _client.distribution_citations(comb_id),
    }


@mcp.tool()
def list_valid_extant_species(family: str, limit: int = 10) -> list[dict[str, Any]]:
    """List deterministic current valid extant species in a family, deduplicated by TaxObjID and sorted by accepted name."""
    rows = _client.valid_extant_species(family, limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        tax_id = int(row["CombSpec_TaxObjID"])
        epi = _client.epithet(tax_id)
        out.append(_accepted_summary(row, epi) | {
            "distribution_summary": epi.get("PresCombGeogDistSummary"),
            "type_kind": epi.get("EpithPrimTypeKind"),
            "type_locality": epi.get("EpithPrimTypeLocality"),
        })
    return out


@mcp.tool()
def distribution_sources(scientific_name: str, include_bibliography: bool = True) -> dict[str, Any]:
    """Flatten the LDL literature sources that support geographic records for a taxon."""
    hit, comb_id, tax_id = _resolved_context(scientific_name)
    epi = _client.epithet(tax_id)
    records = _client.distribution_source_records(comb_id, include_bibliography=include_bibliography)
    return {
        "resolved": _accepted_summary(hit, epi),
        "distribution_summary": epi.get("PresCombGeogDistSummary"),
        "source_count": len(records),
        "sources": records,
    }


def _source_seed(rec: dict[str, Any]) -> dict[str, Any]:
    bib = rec.get("Bibliography") if isinstance(rec.get("Bibliography"), dict) else {}
    edoc = rec.get("Source_EDocBase") if isinstance(rec.get("Source_EDocBase"), dict) else {}
    return {
        "geography": rec.get("RecordFullGeogCite") or rec.get("RecordCountryCite"),
        "country": rec.get("RecordCountryCite"),
        "record_status": rec.get("RecordStatus"),
        "BibObjID": rec.get("Source_BibObjID"),
        "EDocID": rec.get("Source_EDocID"),
        "source_page": rec.get("SourcePageCite"),
        "source_citation": rec.get("SourceCiteShort"),
        "title": bib.get("Title"),
        "authors": bib.get("Auth"),
        "year": bib.get("NomYear"),
        "edoc_link": edoc.get("EDocLink"),
        "public_edoc": edoc.get("PublicEDoc"),
        "private_edoc": edoc.get("PrivateEDoc"),
        "evidence_level": "LDL distribution citation",
        "document_text_needed": True,
    }


@mcp.tool()
def literature_distribution_seed(scientific_name: str) -> dict[str, Any]:
    """Build a literature-extraction seed: taxon, type locality/coordinates, and every cited LDL distribution source/page."""
    hit, comb_id, tax_id = _resolved_context(scientific_name)
    epi = _client.epithet(tax_id)
    sources = _client.distribution_source_records(comb_id, include_bibliography=True)
    return {
        "input_name": scientific_name,
        "resolved": _accepted_summary(hit, epi),
        "original_combination": epi.get("OrigCombSciName"),
        "distribution_summary": epi.get("PresCombGeogDistSummary"),
        "type_kind": epi.get("EpithPrimTypeKind"),
        "type_locality": epi.get("EpithPrimTypeLocality"),
        "type_coordinates": _client.type_coordinates(tax_id),
        "literature_sources": [_source_seed(r) for r in sources],
        "recommended_output_columns": [
            "Species", "Country", "OrigLocation", "CorrectedLocation", "Latitude", "Longitude",
            "Type", "Source", "Note", "CoordinateBasis", "EvidenceLevel", "ProcessingFlag",
            "CombObjID", "TaxObjID", "BibObjID", "EDocID", "SourcePage", "SourceURL",
        ],
        "evidence_rule": "Do not turn country/province summaries into specimen localities. Extract material/type text from the cited document when available; otherwise retain the broader LDL record and flag it.",
    }


@mcp.tool()
def batch_literature_distribution_seed(names: list[str]) -> dict[str, Any]:
    """Build literature-distribution extraction seeds for up to 50 Neuropterida names."""
    if len(names) > 50:
        raise ValueError("batch_literature_distribution_seed accepts at most 50 names")
    results: list[dict[str, Any]] = []
    for name in names:
        try:
            results.append(literature_distribution_seed(name))
        except Exception as exc:
            results.append({"input_name": name, "error": str(exc)})
    return {"count": len(results), "results": results}


@mcp.tool()
def credential_status() -> dict[str, Any]:
    """Report whether optional LDL contributor credentials are available from environment/keychain without exposing them."""
    return _client.credential_status()


@mcp.tool()
def edoc_search(edoc_id: int, terms: list[str], max_hits: int = 20) -> dict[str, Any]:
    """Search an authorized LDL EDoc PDF and return short page snippets around requested terms; credentials are read only from secure local configuration."""
    clean_terms = [t.strip() for t in terms if t and t.strip()]
    if not clean_terms:
        raise ValueError("Provide at least one search term")
    if len(clean_terms) > 12:
        raise ValueError("At most 12 search terms are allowed")
    pdf = _client.fetch_edoc_pdf(int(edoc_id))
    reader = PdfReader(BytesIO(pdf))
    hits: list[dict[str, Any]] = []
    for page_index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        flat = re.sub(r"\s+", " ", text).strip()
        low = flat.lower()
        for term in clean_terms:
            pos = low.find(term.lower())
            if pos < 0:
                continue
            start = max(0, pos - 220)
            end = min(len(flat), pos + len(term) + 320)
            hits.append({
                "pdf_page": page_index + 1,
                "term": term,
                "snippet": flat[start:end],
            })
            if len(hits) >= max(1, min(max_hits, 30)):
                return {"EDocID": edoc_id, "page_count": len(reader.pages), "hits": hits, "truncated": True}
    return {"EDocID": edoc_id, "page_count": len(reader.pages), "hits": hits, "truncated": False}


@mcp.tool()
def taxonomic_references(scientific_name: str) -> dict[str, Any]:
    """Get nomenclatural/taxonomic citations for a taxon, with LDL reference IDs where available."""
    hit, comb_id, tax_id = _resolved_context(scientific_name)
    epi = _client.epithet(tax_id)
    return {
        "resolved": _accepted_summary(hit, epi),
        "taxonomic_citations": _client.taxonomic_citations(comb_id),
        "status_citation": epi.get("EpithTaxStatusCiteLong"),
        "original_reference_id": epi.get("OrigComb_BibObjID"),
    }


@mcp.tool()
def figures(scientific_name: str) -> dict[str, Any]:
    """Get figure citations associated with a taxon."""
    hit, comb_id, _ = _resolved_context(scientific_name)
    return {"resolved": _accepted_summary(hit), "figures": _client.figure_citations(comb_id)}


@mcp.tool()
def phylogeny(scientific_name: str) -> dict[str, Any]:
    """Get LDL phylogenetic citations/records associated with a taxon."""
    hit, comb_id, _ = _resolved_context(scientific_name)
    return {"resolved": _accepted_summary(hit), "phylogeny": _client.phylogeny(comb_id)}


@mcp.tool()
def identification_keys(scientific_name: str) -> dict[str, Any]:
    """Get identification-key terminals associated with a taxon."""
    hit, comb_id, _ = _resolved_context(scientific_name)
    return {"resolved": _accepted_summary(hit), "identification_keys": _client.identification_keys(comb_id)}


@mcp.tool()
def bibliography(bib_obj_id: int, include_taxa: bool = True, include_figures: bool = False) -> dict[str, Any]:
    """Look up an LDL Bibliography of the Neuropterida reference by BibObjID."""
    result = {"BibObjID": bib_obj_id, "record": _client.bibliography(bib_obj_id)}
    if include_taxa:
        result["taxa"] = _client.taxa_for_reference(bib_obj_id)
    if include_figures:
        result["figures"] = _client.figures_for_reference(bib_obj_id)
    return result


@mcp.tool()
def batch_resolve(names: list[str], include_type_summary: bool = False) -> dict[str, Any]:
    """Resolve a batch of Neuropterida names for phylogeny/taxon-table reconciliation."""
    if len(names) > 200:
        raise ValueError("batch_resolve accepts at most 200 names per call")
    rows: list[dict[str, Any]] = []
    for name in names:
        try:
            hit, _, tax_id = _resolved_context(name)
            epi = _client.epithet(tax_id)
            row = {"input_name": name, **_accepted_summary(hit, epi), "match": "resolved"}
            if include_type_summary:
                row.update({
                    "type_kind": epi.get("EpithPrimTypeKind"),
                    "type_locality": epi.get("EpithPrimTypeLocality"),
                })
            rows.append(row)
        except Exception as exc:
            rows.append({"input_name": name, "match": "unresolved", "error": str(exc)})
    return {
        "count": len(rows),
        "resolved": sum(1 for r in rows if r["match"] == "resolved"),
        "results": rows,
    }


@mcp.tool()
def classification() -> Any:
    """Return LDL/NSW classification data."""
    return _client.classification()


@mcp.tool()
def raw_public_endpoint(endpoint: str, payload: dict[str, Any] | None = None) -> Any:
    """Advanced read-only access to an allowlisted LDL frontend endpoint."""
    return _client.raw_public(endpoint, payload)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
