# Literature-level distribution extraction

Use this workflow when the user wants distribution/locality data extracted from the literature rather than only LDL's country/province summaries.

## Evidence hierarchy

1. **Primary-paper type/label** — type material, verbatim label, or type-locality text in the cited primary paper.
2. **Primary-paper material** — non-type `Material examined`, specimen list, collecting event, or locality record in a primary/revision paper.
3. **Literature distribution summary** — a paper gives a geographic range but not recoverable specimen-level locality text.
4. **LDL type record** — LDL preserves a type locality and/or type-specimen coordinates derived from literature, but the underlying document text has not been independently recovered.
5. **LDL distribution citation** — LDL links a country/province record to `BibObjID`, `EDocID`, and page; keep it as regional evidence until the document is checked.

Never promote a country/province record into a specimen locality. Never invent coordinates from a country name or range summary.

## Preferred MCP path

1. `list_valid_extant_species(family, limit)` for a reproducible family sample.
2. `literature_distribution_seed(scientific_name)` to obtain the accepted taxon, type record/coordinates, and every cited distribution source/page.
3. Retrieve cited documents through public web sources first. If the LDL EDoc is contributor-only, use secure contributor credentials only when the user has authorized access; do not accept or log passwords as MCP tool arguments.
4. Search the document for the accepted name, original combination, historical combinations, `material examined`, `type material`, `distribution`, country/province names, and the cited page.
5. Emit one row per usable collecting/locality statement. Multiple localities from one paper become separate rows.

## Output schema

Keep the first nine columns compatible with the user's established locality workbook:

`Species, Country, OrigLocation, CorrectedLocation, Latitude, Longitude, Type, Source, Note`

Append audit fields when possible:

`CoordinateBasis, EvidenceLevel, ProcessingFlag, CombObjID, TaxObjID, BibObjID, EDocID, SourcePage, SourceURL`

`OrigLocation` should preserve the source locality meaning, but long prose should be paraphrased rather than copied wholesale. `CorrectedLocation` is the standardized modern geographic interpretation.

## Coordinates

Prefer, in order: coordinates printed with the specimen record; LDL numeric type coordinates; explicit coordinates in a type-locality statement; careful georeferencing only if the user requests it. Mark approximate coordinates and discrepancies. If LDL's numeric coordinate conflicts with its interpreted locality string, retain both in notes and flag for review.

## Contributor access

The MCP may discover credentials from environment variables or a secure OS credential store, but passwords must never be tool arguments, result fields, logs, repository files, or skill text. Public sources remain the default because they are reproducible for other users.
