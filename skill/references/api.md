# LDL JSON API notes

Observed against `https://lacewing.tamu.edu` on 2026-09-10. These endpoints are used by the LDL React frontend; they are not a documented third-party API.

Base: `https://lacewing.tamu.edu/api`

## Endpoint routing

Use the endpoint that matches the requested datum:

| Need | Endpoint | Join key / important note |
|---|---|---|
| Resolve names and current combinations | `POST species/doSpeciesSearch` | Search rows contain both current combinations and historical combinations |
| Full combination record | `POST species/getSpecCombPk` | Payload: `{"CombObjID": <id>}` |
| Epithet, accepted name, status, type summary, distribution summary | `POST species/getSpecByPk` | Payload: `{"TaxObjID": <id>}` |
| Type specimen details | `POST species/getSpeciesWithTypeByPK` | Payload: `{"CombObjID": <id>}`; may expose nested type lists |
| Georeferenced type-locality coordinates | `GET general/getTypeSpmnCoordinates` | Fetch once, then join `Type_TaxObjID` to species `TaxObjID` |
| Distribution citations by country/region | `POST species/getSpecDistCitByPk` | Useful for cited distribution text; not the numeric coordinate source |
| Taxonomic citations | `POST species/getSpecTaxCitByPk` | Citation records |
| Figures, phylogeny, identification keys | Corresponding `species/*` endpoint | Resolve the current combination first |

Species endpoints confirmed in the frontend include `species/doSpeciesSearch`, `getSpecCombPk`, `getMonoByPk`, `getSpecByPk`, `getSpeciesWithTypeByPK`, `getSpecTaxCitByPk`, `getSpecFigCitByPk`, `getSpecDistCitByPk`, `getSpecPhyloByPk`, and `getSpeciesKeyTerminalsByPk`.

## Coordinate endpoint

`GET /api/general/getTypeSpmnCoordinates` returned 4,284 rows in the 2026-09-10 test. The observed record schema was:

```json
{
  "TypeSpmnID": 2,
  "Type_TaxObjID": 3,
  "Spmn_AgentID": 24617,
  "TypeKind": "holotype",
  "Sex": "? (sex unknown)",
  "TypeLocalityQuote": "\"Darjeeling\"",
  "TypeLocalityInterpreted": "India : West Bengal (state) : Darjeeling (district) : Darjeeling (town) [27.039°N 88.265°E]",
  "LatitudeDecDeg": 27.038999557495117,
  "LongitudeDecDeg": 88.26499938964844,
  "DateCollectedReared": "??.vii.1886",
  "Collector": "(not available)",
  "Chronostrat": "Holocene (Greenlandian / Meghalayan) [0.0117 - 0 Ma]",
  "Lithostrat": "(non-fossil)",
  "SpecimenNote": "–"
}
```

Use `LatitudeDecDeg` and `LongitudeDecDeg` as numeric values. Preserve the original precision or round only in the presentation layer. Interpret longitude sign convention as decimal degrees: west is negative and east is positive. A coordinate row with a null coordinate is not a georeferenced record and should be flagged, not converted from a locality string by guesswork.

The endpoint is global, so it has no family filter. Fetching it once is the intended batch pattern; repeatedly requesting it for each species adds no information.

Search requests use JSON like:

```json
{"where":{"PresCombGenu":{"Op.eq":"Mantispa"},"StrPatternSpecEpith":{"Op.startsWith":"styri"},"TaxonRankBlockID":26,"IsUnidSp":0}}
```

Supported observed operators: `Op.eq`, `Op.startsWith`, `Op.substring`, `Op.endsWith`, `Op.gt`, `Op.lt`.

Useful search/result fields observed: `CombObjID`, `CombCiteShort`, `CombTaxStatus`, `PresCombGenu`, `PresCombSpec`, `CombSpec_TaxObjID`, `ASAuth`, `ASYear`, and `Comb_BibObjID`.

`getSpecByPk` accepts `TaxObjID` and returns nomenclatural fields such as `EpithNameAuthDate`, `EpithNomStatusShort`, `EpithTaxStatusShort`, `EpithPrimTypeKind`, `EpithPrimTypeLocality`, `OrigComb_CombObjID`, `OrigCombSciName`, `OrigCombAuth`, `OrigCombYear`, `PresComb_CombObjID`, `PresCombCite`, `PresCombClassification`, `PresCombGeogDistSummary`, `ExtantExtinct`, and present classification/distribution summary fields.

Anonymous HTTP 200 JSON responses were confirmed for `doSpeciesSearch`, `getSpecCombPk`, `getMonoByPk`, `getSpecByPk`, `getSpeciesWithTypeByPK`, and `general/getTypeSpmnCoordinates` during testing. Other endpoints may expose only the public subset of their record under anonymous access.

## Confirmed failure modes

1. A genus or family search returns combination history. For example, a current species can appear alongside original combinations, old genus placements, junior synonyms, and unavailable names. Filter on `CombTaxStatus`, then group by `CombSpec_TaxObjID`.
2. Searching only `Chrysopa` is not equivalent to searching the family `Chrysopidae`; many valid Chrysopidae taxa are in other genera. For Chinese “草蛉” tasks, use `PresCombFami = Chrysopidae` unless the user narrows the scope.
3. The ordinary distribution endpoint exposes lists such as `Syn_DistGeogBibObjList` with `RecordFullGeogCite`, `RecordStatus`, and citation fields. It does not replace `getTypeSpmnCoordinates`.
4. A full frontend bundle is unnecessary for normal extraction. The endpoint name and schema above are already confirmed; inspect the bundle only when an endpoint stops responding or its payload changes.

Treat `-1/0` database flags cautiously: preserve raw values unless the corresponding semantic display field is available. Never reinterpret restricted or absent data as negative biological evidence.
