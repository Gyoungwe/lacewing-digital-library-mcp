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


## Literature download and research workflow (v0.3)

The genus literature downloader is now built into the MCP. Typical flow:

1. `auth_start` opens a **separate** Chrome/Edge window (isolated profile, never your everyday browser). Log in to LDL and click the purple **“我已登录，保存会话 / Save LDL session”** button. Only `lacewing.tamu.edu` cookies are saved to `~/.local/share/ldl-mcp/auth/ldl_cookies.json` (mode 0600). No password is ever passed to or stored by the MCP. `auth_status` shows cookie names and expiry, never values.
2. `download_start(genus="Micromus")` starts a **detached background job** (it keeps running if Codex closes). Modes: `fast`, `stable`, `retry-failed`. Output is `LDL_<Genus>/`, the same layout as the original downloader, so existing libraries resume without re-downloading.
3. `download_status` / `download_control(action=pause|cancel|resume|retry_failed)` / `download_verify` (online integrity check against LDL's current citations).
4. `library_status`, `library_verify`, `library_documents`, `library_search` (full-text, page + snippet + BibObjID/EDocID), `library_page_text`.
5. `freeze_taxon_scope` → `literature_plan` → `record_evidence` (each quote is checked against the cited PDF page; unfound quotes stay `unverified`) → `review_evidence`.
6. `distribution_dataset` (LDL type coordinates + LDL distribution citations + literature evidence, with coordinate QC) → `distribution_map` (`nature_like` or `plain_report`; SVG/PDF/PNG + manifest + caption) → `figure_validate` → `export_research_package` (zip with tables, evidence, BibTeX, figures, file hashes).

Maps use a bundled Natural Earth 1:50m land/borders layer by default (public domain). `basemap="cartopy"` and non-plate-carrée projections need `pip install 'lacewing-digital-library-mcp[maps]'`. Country-level summaries and inferred coordinates are never drawn as points, and a map stays `draft` until dataset QC passes.

Data root: `~/.local/share/ldl-mcp/` (override with `LDL_MCP_HOME`).

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
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # offline, includes a simulated genus download
.venv/bin/python tests/live_smoke.py
.venv/bin/python tests/mcp_smoke.py
```

`live_smoke.py` verifies known LDL data for *Mantispa styriaca*. `mcp_smoke.py` starts the server over stdio, performs MCP initialization, checks the tool inventory, and calls `resolve_taxon` through the MCP protocol.

## License

MIT. Data returned by LDL remain subject to LDL's own terms, access controls, attribution expectations and underlying source rights.
