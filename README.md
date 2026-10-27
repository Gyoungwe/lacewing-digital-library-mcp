# Lacewing Digital Library MCP

A research-oriented MCP server + Codex skill for the **Lacewing Digital Library (LDL)** and **Neuropterida Species of the World (NSW)**.

It turns LDL's publicly reachable JSON endpoints into tools that match real taxonomic workflows: accepted-name resolution, historical combinations, nomenclature, type specimens and type-locality coordinates, distribution citations, bibliography, figures, phylogeny, identification keys, and batch reconciliation.

> This project is an independent research client. LDL's `/api/...` endpoints are used by the public web frontend and are not documented as a third-party public API. The server uses conservative request rates and does not bypass contributor-only access controls.

## One-line install for Codex

```bash
curl -fsSL https://raw.githubusercontent.com/Gyoungwe/lacewing-digital-library-mcp/main/install.sh | bash
```

The installer clones/updates the project under `~/.local/share/lacewing-digital-library-mcp`, creates an isolated Python virtual environment, installs the MCP server, copies the Codex skill into `~/.codex/skills/lacewing-digital-library`, and registers the stdio MCP server with `codex mcp add`.

Verify with:

```bash
codex mcp get lacewing-digital-library
```

Then ask Codex:

```text
Use $lacewing-digital-library to resolve Mantispa styriaca and summarize its accepted name, original combination, type locality, distribution and references.
```

## MCP tools

| Tool | Typical research question |
|---|---|
| `resolve_taxon` | What is the accepted/current name and LDL ID for this name? |
| `search_taxa` | Which taxa match a genus, family, author, year, or epithet? |
| `name_history` | What original/historical combinations are associated with this taxon? |
| `taxon_record` | Give me the combined LDL species/epithet/monograph record. |
| `type_info` | What is the type, type locality, and georeferenced type-specimen coordinate? |
| `distribution` | Where is this species recorded and which references support that distribution? |
| `taxonomic_references` | Which nomenclatural/taxonomic sources support this taxon? |
| `figures` | Which published figures are associated with this taxon? |
| `phylogeny` | Which LDL phylogenetic records/citations include this taxon? |
| `identification_keys` | Which identification keys include this taxon? |
| `bibliography` | What is LDL reference `BibObjID`, and which taxa/figures are linked to it? |
| `batch_resolve` | Reconcile a tree tip list / CSV taxon list to current LDL taxonomy. |
| `list_valid_extant_species` | List unique valid extant species in a family, suitable for reproducible sampling. |
| `distribution_sources` | Expand a taxon into LDL distribution records with BibObjID, EDocID, source page, and bibliography metadata. |
| `literature_distribution_seed` | Build a literature-extraction evidence package for one taxon. |
| `batch_literature_distribution_seed` | Build literature-extraction evidence packages for many taxa. |
| `credential_status` | Check whether optional contributor credentials are configured without exposing secrets. |
| `edoc_search` | With locally configured contributor access, search an LDL EDoc PDF for short keyword contexts and PDF pages. |
| `classification` | Retrieve the NSW classification data. |
| `raw_public_endpoint` | Advanced read-only access to an allowlisted LDL endpoint. |
| `health_check` | Can the server reach LDL, and what dataset/version info is visible? |

## Examples

Resolve a taxon:

```text
Resolve Chrysoperla carnea in LDL. Keep the input name, accepted name, author/year, CombObjID, TaxObjID and original combination.
```

Reconcile phylogeny tips:

```text
Use LDL to reconcile these 80 Neuropterida tree-tip names. Do not silently replace names: return input_name, accepted_name, status, CombObjID, TaxObjID and notes for ambiguous/unmatched rows.
```

Type-locality work:

```text
Find the type information and type-locality coordinates for Mantispa styriaca. State clearly whether the coordinates are type-specimen localities or occurrence data.
```

Bibliography-first workflow:

```text
Look up LDL BibObjID 18355, summarize the reference, and list taxa associated with it.
```

## Manual installation

```bash
git clone https://github.com/Gyoungwe/lacewing-digital-library-mcp.git
cd lacewing-digital-library-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
codex mcp add lacewing-digital-library -- "$PWD/.venv/bin/python" -m ldl_mcp
cp -R skill ~/.codex/skills/lacewing-digital-library
```

For another MCP client, use a stdio configuration whose command is the environment's Python and whose arguments are `-m ldl_mcp`.

## Optional LDL Contributor access

Public endpoints remain the default. Restricted EDoc search is opt-in and credentials are never accepted as MCP tool arguments or committed to the repository.

Set `LDL_USERNAME` and `LDL_PASSWORD` in the MCP process environment, or on macOS store only the username in `~/.config/lacewing-digital-library-mcp/config.json` and keep the password in Keychain under service `lacewing-digital-library-mcp`. Use `credential_status` to verify configuration without revealing the password.

`edoc_search` returns only short matching contexts and PDF page numbers. It does not return or redistribute complete restricted documents.

## Data interpretation rules

LDL search results can include current combinations, original combinations, historical placements, junior synonyms, invalid names and unavailable names in the same result set. Do not interpret every returned row as a distinct current species or as a synonym. Preserve `CombObjID`, `TaxObjID` and status fields so mappings remain auditable.

`general/getTypeSpmnCoordinates` provides **georeferenced type-specimen localities**. These coordinates are not a complete species occurrence dataset. The MCP joins them to species using LDL taxonomic identifiers rather than guessing coordinates from locality prose.

## Development and tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python tests/live_smoke.py
.venv/bin/python tests/mcp_smoke.py
```

`live_smoke.py` verifies known LDL data for *Mantispa styriaca*. `mcp_smoke.py` starts the server over stdio, performs MCP initialization, checks the tool inventory, and calls `resolve_taxon` through the MCP protocol.

## License

MIT. Data returned by LDL remain subject to LDL's own terms, access controls, attribution expectations and underlying source rights.
