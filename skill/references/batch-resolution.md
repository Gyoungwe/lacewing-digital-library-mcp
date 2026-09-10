# Batch taxonomic and coordinate resolution

Preserve the original input string in an `input_name` column. Add resolved fields such as `accepted_name`, `CombObjID`, `TaxObjID`, `author`, `year`, `status`, `original_combination`, `type_kind`, `type_locality`, and `resolution_note`.

## Coordinate batch recipe

For a family-wide or multi-name coordinate table:

1. Resolve the requested taxonomic scope with `species/doSpeciesSearch`.
2. Fetch `general/getTypeSpmnCoordinates` once and index rows by `Type_TaxObjID`.
3. Select current valid species-level rows. Use `CombSpec_TaxObjID` as the underlying species key.
4. Group search hits by `CombSpec_TaxObjID` before selecting a display combination. Prefer a row whose `CombTaxStatus` is `valid` and whose present name matches the current combination; retain the historical rows only as provenance when needed.
5. Join all coordinate rows for each selected taxon. One taxon can have multiple type specimens and therefore multiple coordinate rows.
6. Fetch `getSpecByPk` for accepted-name, nomenclatural, and `ExtantExtinct` fields. Do not derive those semantics from a coordinate or a database flag.
7. Write a row-level output and a run summary containing the endpoint, retrieval date, number of search rows, number of unique taxa, number of coordinate rows, and number of unmatched taxa.

Recommended coordinate output fields:

```text
input_name
accepted_name
accepted_citation
CombObjID
TaxObjID
TypeSpmnID
TypeKind
Sex
TypeLocalityQuote
TypeLocalityInterpreted
latitude_dec_deg
longitude_dec_deg
extant_extinct
distribution_summary
ldl_record_timestamp
retrieved_at_utc
```

The coordinate definition must be stated in the output metadata: these are LDL georeferenced type-specimen locality coordinates. They are not a complete species-occurrence dataset.

For each binomial, search the current genus exactly and the species epithet by prefix. Prefer a row whose `CombTaxStatus` is `valid` and whose `CombGenuToSubs` exactly matches the input. If no exact current combination exists, retain all plausible hits and mark the row for review rather than guessing.

Historical combinations can share the same present genus/species and species `TaxObjID`. Group by the underlying taxon before interpreting returned rows as separate taxa.

Do not collapse subspecies, infrasubspecific names, homonyms, or unavailable names without explicit evidence from LDL status fields.

## Missing and ambiguous data

- Keep taxa with no coordinate rows in a separate unmatched or `no_coordinate` result; do not infer coordinates from country names, distribution summaries, or type-locality prose.
- Keep rows with only one coordinate component as incomplete and flag them for review.
- Preserve multiple type specimens, even when their coordinates are identical.
- Preserve extinct fossil taxa and label them using `ExtantExtinct`; do not silently remove them unless the user explicitly asks for extant taxa only.
- If a current accepted name differs from the user's input, retain both values and explain the resolution in `resolution_note`.
- If several valid combinations point to one `TaxObjID`, treat them as combination history unless LDL provides evidence that they are separate taxa.

## Performance rules

The fast pattern is “one broad search, one global coordinate fetch, then local joins.” Avoid a per-species call to the coordinate endpoint. Avoid downloading or searching the frontend bundle during ordinary runs; use the confirmed endpoint and schema in [api.md](api.md). Call per-taxon endpoints only for the taxa that will appear in the final output or need additional fields.

For reproducibility, retain LDL identifiers and the data timestamp when available. Record unmatched names and API failures separately.
