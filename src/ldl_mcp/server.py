from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
from typing import Any

from pypdf import PdfReader

from mcp.server import FastMCP

from . import auth, distribution, evidence, export, figures, jobs, library, research_scope
from .client import LDLClient
from .downloader import DOWNLOAD_MODES
from .paths import libraries_dir, subdir

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


# ---------------------------------------------------------------------------
# Login session (separate browser; cookies stored locally, never passwords)
# ---------------------------------------------------------------------------


@mcp.tool()
def auth_start(verify_edoc_id: int | None = None, timeout_minutes: int = 20) -> dict[str, Any]:
    """Open a separate Chrome/Edge window for the user to log in to LDL; after they click the in-page save button the LDL cookies are stored locally (0600). Optionally verify access with an EDoc ID before saving. Returns immediately; poll auth_status."""
    return auth.start_login(verify_edoc_id=verify_edoc_id, timeout_minutes=max(1, min(timeout_minutes, 120)))


@mcp.tool()
def auth_status(probe_edoc_id: int | None = None) -> dict[str, Any]:
    """Report the login window state and saved-session summary (cookie names/expiry only, never values). Optionally probe one EDoc with the saved session."""
    return auth.status(probe_edoc_id=probe_edoc_id)


@mcp.tool()
def auth_logout(close_login_window: bool = True) -> dict[str, Any]:
    """Delete the saved LDL cookies (the isolated browser profile is kept so the next login is quick)."""
    closed = auth.stop_login() if close_login_window else False
    return {"cookies_deleted": auth.clear_cookies(), "login_window_closed": closed}


# ---------------------------------------------------------------------------
# Genus literature download jobs (detached workers; survive the MCP session)
# ---------------------------------------------------------------------------


def _download_params(
    genus: str | None,
    comb_obj_id: int | None,
    monograph_url: str | None,
    output_parent: str | None,
    download_mode: str,
    refresh: bool,
) -> dict[str, Any]:
    if not (genus or comb_obj_id or monograph_url):
        raise ValueError("Provide genus, comb_obj_id or monograph_url")
    if download_mode not in DOWNLOAD_MODES:
        raise ValueError(f"download_mode must be one of {sorted(DOWNLOAD_MODES)}")
    parent = Path(output_parent).expanduser() if output_parent else libraries_dir()
    return {
        "genus": genus.strip() if genus else None,
        "comb_obj_id": comb_obj_id,
        "monograph_url": monograph_url,
        "output_parent": str(parent.resolve()),
        "download_mode": download_mode,
        "refresh": refresh,
    }


def _job_view(job: dict[str, Any], log_lines: int = 15) -> dict[str, Any]:
    view = {k: job.get(k) for k in ("job_id", "kind", "status", "message", "genus", "root", "created_at", "started_at", "updated_at", "finished_at", "result", "runs")}
    view["params"] = {k: v for k, v in (job.get("params") or {}).items() if k != "monograph_url"}
    if job.get("root") and Path(job["root"]).exists():
        try:
            view["library"] = library.status(job["root"])
        except Exception:
            pass
    if job.get("job_id") and log_lines:
        view["log_tail"] = jobs.tail(job["job_id"], log_lines)
    return view


@mcp.tool()
def download_start(
    genus: str | None = None,
    comb_obj_id: int | None = None,
    monograph_url: str | None = None,
    output_parent: str | None = None,
    download_mode: str = "fast",
    refresh: bool = False,
) -> dict[str, Any]:
    """Start a background job downloading every whole-work EDoc cited in Taxon/Distribution/Image Citations for all species of a genus, with r#/EDoc/SHA-256 dedup, Range chunking, checkpoints and a failure list. download_mode: fast | stable | retry-failed. Requires auth_start first. Output: <output_parent>/LDL_<Genus>/ (compatible with the original downloader)."""
    if not auth.load_cookies():
        return {"status": "auth_required", "message": "No saved LDL session. Call auth_start, log in, click save, then retry."}
    params = _download_params(genus, comb_obj_id, monograph_url, output_parent, download_mode, refresh)
    return _job_view(jobs.start("download", params), 0)


@mcp.tool()
def download_verify(
    genus: str | None = None,
    comb_obj_id: int | None = None,
    monograph_url: str | None = None,
    output_parent: str | None = None,
) -> dict[str, Any]:
    """Start a background online integrity check: re-read LDL's current citations for the genus and compare with the local library (no downloads). Writes 完整性检查报告.txt and 完整性检查明细.csv."""
    params = _download_params(genus, comb_obj_id, monograph_url, output_parent, "stable", False)
    params["require_login"] = bool(auth.load_cookies())
    return _job_view(jobs.start("verify", params), 0)


@mcp.tool()
def download_status(job_id: str | None = None, log_lines: int = 20, limit: int = 20) -> Any:
    """Status of one job (planned/running/paused/auth_required/attention_required/completed/failed/interrupted) with library counters and recent log lines; omit job_id to list recent jobs."""
    if not job_id:
        return [_job_view(j, 0) for j in jobs.list_jobs(limit)]
    return _job_view(jobs.load(job_id), log_lines)


@mcp.tool()
def download_control(job_id: str, action: str) -> dict[str, Any]:
    """Control a job. action: pause | cancel (stop at the next request boundary, keep checkpoint, never delete verified files) | resume (continue from checkpoint, completed species skipped) | retry_failed (re-run only EDocs in 下载失败待补.csv with stable timeouts)."""
    if action in {"pause", "cancel"}:
        return jobs.request_stop(job_id, action)
    if action == "resume":
        return _job_view(jobs.resume(job_id), 0)
    if action == "retry_failed":
        return _job_view(jobs.resume(job_id, mode="retry-failed"), 0)
    raise ValueError("action must be pause, cancel, resume or retry_failed")


# ---------------------------------------------------------------------------
# Local literature library (read-only)
# ---------------------------------------------------------------------------


def _known_library_roots(extra: list[str] | None = None) -> list[str]:
    roots = list(extra or [])
    for item in library.find_libraries(libraries_dir(), max_depth=2):
        roots.append(item["root"])
    for job in jobs.list_jobs(200):
        if job.get("root"):
            roots.append(job["root"])
    return list(dict.fromkeys(roots))


@mcp.tool()
def library_status(root: str | None = None, search_parent: str | None = None) -> dict[str, Any]:
    """Summarize a local library (species completed, failed downloads, citations without file, documents present, last integrity result). Omit root to list known libraries (MCP data folder, job outputs, or under search_parent)."""
    if root:
        return library.status(root)
    if search_parent:
        return {"libraries": library.find_libraries(search_parent)}
    return {"libraries": [{"root": r} for r in _known_library_roots()]}


@mcp.tool()
def library_verify(root: str, full_hash: bool = False) -> dict[str, Any]:
    """Offline integrity check of a local library: every indexed file exists with the recorded size; full_hash=True also recomputes SHA-256."""
    return library.verify(root, full_hash=full_hash)


@mcp.tool()
def library_documents(root: str, species: str | None = None, limit: int = 200) -> dict[str, Any]:
    """List documents in a local library with SHA-256, BibObjIDs, EDocIDs, linked species and citation categories."""
    return library.documents(root, species=species, limit=limit)


@mcp.tool()
def library_search(
    root: str,
    terms: list[str],
    species: str | None = None,
    sha256: str | None = None,
    match: str = "any",
    max_hits: int = 50,
) -> dict[str, Any]:
    """Full-text search of local library PDFs (accent/case-insensitive). Returns PDF page, snippet, SHA-256, BibObjID and EDocID per hit. Scanned PDFs without text are listed separately."""
    return library.search(root, terms, species=species, sha256=sha256, match=match, max_hits=max_hits)


@mcp.tool()
def library_page_text(root: str, sha256: str, pdf_page: int, max_chars: int = 8000) -> dict[str, Any]:
    """Return the text layer of one PDF page (for reading 'Material examined' / type sections before recording evidence)."""
    return library.page_text(root, sha256, pdf_page, max_chars=max_chars)


# ---------------------------------------------------------------------------
# Research scope, literature plan, evidence, distribution, figures, export
# ---------------------------------------------------------------------------


@mcp.tool()
def freeze_taxon_scope(
    genus: str | None = None,
    family: str | None = None,
    names: list[str] | None = None,
    extant_only: bool = False,
    label: str | None = None,
) -> dict[str, Any]:
    """Freeze an immutable research scope (accepted taxa with CombObjID/TaxObjID, status, extant/extinct, unresolved names, LDL data version). Returns a scope_id used by later steps."""
    scope = research_scope.freeze_scope(_client, genus=genus, family=family, names=names, extant_only=extant_only, label=label)
    return {**{k: v for k, v in scope.items() if k != "taxa"}, "taxa": scope["taxa"][:200], "taxa_truncated": len(scope["taxa"]) > 200}


@mcp.tool()
def list_taxon_scopes() -> list[dict[str, Any]]:
    """List frozen research scopes."""
    return research_scope.list_scopes()


@mcp.tool()
def literature_plan(scope_id: str, categories: list[str] | None = None, top: int = 50) -> dict[str, Any]:
    """Expand a scope into the deduplicated list of cited whole-work EDocs (Taxon/Distribution/Image Citations) ranked by species coverage, plus citations without a file. Reads public metadata only; downloads nothing."""
    plan = research_scope.literature_plan(_client, scope_id, categories)
    return {
        **{k: v for k, v in plan.items() if k not in {"documents", "citations_without_file_rows"}},
        "top_documents": plan["documents"][: max(1, min(top, 500))],
        "saved_to": str(Path(subdir("plans")) / f"{plan['plan_id']}.json"),
    }


@mcp.tool()
def record_evidence(dataset: str, rows: list[dict[str, Any]], library_roots: list[str] | None = None) -> dict[str, Any]:
    """Store literature evidence rows (species, locality_original/normalized, country, latitude, longitude, coordinate_basis, type_status, collector, collection_date, sha256, pdf_page, evidence_quote, evidence_level, confidence, notes). Each quote is checked against the cited page text; rows whose quote is not found stay 'unverified'."""
    return evidence.record(dataset, rows, _known_library_roots(library_roots))


@mcp.tool()
def evidence_table(dataset: str, review_status: str | None = None, limit: int = 500) -> dict[str, Any]:
    """Return evidence rows and counts by review status."""
    return evidence.table(dataset, review_status=review_status, limit=limit)


@mcp.tool()
def review_evidence(dataset: str, evidence_ids: list[str], review_status: str, note: str = "") -> dict[str, Any]:
    """Record a human review decision (accepted/rejected/needs_review) for evidence rows."""
    return evidence.set_review(dataset, evidence_ids, review_status, note)


@mcp.tool()
def distribution_dataset(
    name: str,
    scope_id: str | None = None,
    names: list[str] | None = None,
    evidence_dataset: str | None = None,
    include_type_coordinates: bool = True,
    include_ldl_distribution: bool = True,
    extent: list[float] | None = None,
) -> dict[str, Any]:
    """Build a normalized distribution dataset from LDL type-specimen coordinates, LDL distribution citations (country_summary, never plotted) and recorded literature evidence, then run coordinate QC (range, 0/0, duplicates, precision, study extent [lon_min, lon_max, lat_min, lat_max])."""
    result = distribution.build(
        _client, name, scope_id=scope_id, names=names, evidence_dataset=evidence_dataset,
        include_type_coordinates=include_type_coordinates, include_ldl_distribution=include_ldl_distribution, extent=extent,
    )
    return result


@mcp.tool()
def distribution_map(
    dataset_id: str,
    species: list[str] | None = None,
    coordinate_bases: list[str] | None = None,
    include_flagged: bool = False,
    extent: list[float] | None = None,
    theme: str = "nature_like",
    width: str = "single",
    facet: bool = False,
    projection: str = "platecarree",
    basemap: str = "auto",
    formats: list[str] | None = None,
    dpi: int = 600,
    title: str | None = None,
) -> dict[str, Any]:
    """Render a reproducible point map (SVG/PDF/PNG + map_data.csv, map_qc.json, figure_manifest.json, caption.md). theme: nature_like | plain_report; width: single (89 mm) | double (183 mm); basemap: auto | none | cartopy | path to GeoJSON. Only records with explicit geographic basis are drawn; maps are 'draft' until QC passes and a basemap is present."""
    return figures.render_map(
        dataset_id, species=species, coordinate_bases=coordinate_bases, include_flagged=include_flagged, extent=extent,
        theme_name=theme, width=width, facet=facet, projection=projection, basemap=basemap, formats=formats, dpi=dpi, title=title,
    )


@mcp.tool()
def figure_validate(figure_id: str) -> dict[str, Any]:
    """Check a rendered figure: outputs exist, SVG text editable, PNG ≥300 dpi, QC passed, basemap present, no inferred ranges."""
    return figures.validate(figure_id)


@mcp.tool()
def export_research_package(
    dataset_id: str,
    figure_ids: list[str] | None = None,
    include_bibtex: bool = True,
    table_formats: list[str] | None = None,
) -> dict[str, Any]:
    """Export a zip with the distribution table (CSV/TSV/Markdown), QC report, evidence table, BibTeX for cited BibObjIDs, figures and a run manifest with file hashes."""
    return export.export_package(_client, dataset_id, figure_ids=figure_ids, include_bibtex=include_bibtex, table_formats=table_formats)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
