---
name: lacewing-digital-library
description: Query and resolve Neuropterida taxonomy through the Lacewing Digital Library (LDL), especially Neuropterida Species of the World (NSW). Use for accepted names, original combinations, synonymy/combination history, authorship, type information, distribution, georeferenced type-locality coordinates, taxonomic citations, figures, phylogeny, identification keys, CombObjID/TaxObjID lookup, and batch taxonomic name cleaning for Neuroptera, Megaloptera, and Raphidioptera.
metadata:
  short-description: Query LDL/NSW taxonomy for Neuropterida
---

# Lacewing Digital Library

Use the bundled client for reproducible access to LDL's publicly reachable JSON endpoints instead of scraping rendered HTML.

## Fast path for coordinates

When the user asks for latitude/longitude, georeferenced localities, or a map-ready taxon table:

1. Resolve the taxon scope first. In this workflow, Chinese “草蛉” means family `Chrysopidae` unless the user specifies another taxonomic scope.
2. Search `species/doSpeciesSearch` with the family or name condition. Keep only current, valid, species-level combinations for the main output.
3. Fetch `GET /api/general/getTypeSpmnCoordinates` once. Build an in-memory index by `Type_TaxObjID`.
4. Join coordinate rows to resolved taxa using `Type_TaxObjID = CombSpec_TaxObjID` (or the corresponding species `TaxObjID` from the resolved record).
5. Deduplicate by underlying `TaxObjID`, not by returned combination row. Keep all coordinate rows when one taxon has multiple type specimens.
6. Fetch `getSpecByPk` only for the selected taxa when accepted-name, extant/extinct, type, or distribution-summary fields are needed.

The coordinate endpoint returns georeferenced type-specimen localities. These are type-locality coordinates, not a complete occurrence dataset and not every locality in the species distribution. Preserve both decimal coordinates and `TypeLocalityInterpreted`; do not reconstruct coordinates from prose when LDL supplies numeric values.

## Core workflow

For a scientific name, first resolve it with `scripts/ldl_client.py`. Prefer the current valid combination, but retain historical combinations and original combinations when they matter to the user's analysis.

For nomenclatural questions, report identifiers alongside names when useful: `CombObjID` identifies a combination record and `TaxObjID` identifies the underlying taxonomic/epithet object.

For batch lists, preserve the user's input name and add resolved fields rather than silently replacing source data. Flag ambiguous, unmatched, or multiple-valid hits for review.

Use only anonymous/publicly reachable data unless the user separately provides and authorizes contributor access. Do not bypass LDL access controls.

## Query guidance

Name matching supports LDL operators `Op.eq`, `Op.startsWith`, `Op.substring`, `Op.endsWith`, `Op.gt`, and `Op.lt`. For ordinary binomials, resolve genus exactly and species by prefix first; broaden only when needed.

A current accepted name search can return historical combinations that point to the same present taxon. Do not treat every returned row as a synonym. Distinguish combination history, nomenclatural synonyms, and currently valid taxa from the status fields.

When type, distribution, citations, figures, phylogeny, or keys are requested, resolve the current combination first and then call the corresponding record endpoint.

For coordinate work or endpoint discovery, read [references/api.md](references/api.md). For batch name/coordinate processing, read [references/batch-resolution.md](references/batch-resolution.md). For specimen/locality extraction from cited papers, read [references/literature-distribution.md](references/literature-distribution.md).

## Literature-level distribution workflow

LDL's distribution list is a source index, not the final locality dataset. When the user asks for literature-derived distribution points, expand every cited source/page and extract specimen/type/material records from the document. Preserve an evidence level so downstream analyses can distinguish direct paper material from LDL-only regional summaries. Match the user's locality-table convention when one is supplied, and keep LDL IDs for auditability.

Use public copies first. Contributor access is optional and must use secure local credential discovery; never put a password in a prompt-generated command, MCP tool argument, output table, repository, or log.

## Execution

Prefer the `lacewing-digital-library` MCP server when it is available. Use its high-level tools before reaching for raw endpoints. For literature-level distribution work, prefer `list_valid_extant_species`, `distribution_sources`, `literature_distribution_seed`, and `batch_literature_distribution_seed`; these preserve the cited `BibObjID`, `EDocID`, and page needed to recover specimen/locality evidence. Use `raw_public_endpoint` only for advanced read-only queries against the allowlisted LDL frontend endpoints.

If the MCP server is unavailable, fall back to the bundled Python client. Keep request rates conservative because these endpoints are used by the LDL web frontend and are not a documented third-party API.

Do not infer missing taxonomic facts from field names alone. If a requested datum is absent or ambiguous, say so and preserve the raw LDL value where relevant.

## Round-one lessons

The first 10-taxon coordinate test exposed four reusable pitfalls:

- A genus search can return hundreds of historical combinations and junior synonyms. A returned row is not automatically a separate current species.
- `PresCombGeogDistSummary` and `Syn_DistGeogBibObjList` describe geographic distribution and citations; they do not provide the numeric type-locality coordinates needed for a map.
- The coordinate data are not attached to the species-search rows. They are in the general endpoint and must be joined through `Type_TaxObjID` and `CombSpec_TaxObjID`.
- Some returned valid taxa are extinct fossil taxa. Preserve `ExtantExtinct` or the equivalent semantic field instead of silently treating every coordinate as a modern occurrence.

The resolved output should therefore include at least `input_name`, `accepted_name`, `CombObjID`, `TaxObjID`, `TypeSpmnID`, `TypeLocalityInterpreted`, `latitude_dec_deg`, `longitude_dec_deg`, and an extant/extinct status field.
