from __future__ import annotations

from typing import Any

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
