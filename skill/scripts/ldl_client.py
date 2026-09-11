from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class LDLClient:
    """Low-rate client for public endpoints used by the LDL web frontend."""

    SITE_URL = "https://lacewing.tamu.edu"
    BASE_URL = f"{SITE_URL}/api"
    OPS = {
        "eq": "Op.eq",
        "starts": "Op.startsWith",
        "contains": "Op.substring",
        "ends": "Op.endsWith",
        "gt": "Op.gt",
        "lt": "Op.lt",
    }

    PUBLIC_ENDPOINTS = {
        "species/doSpeciesSearch": "POST",
        "species/searchTaxObject": "POST",
        "species/getSpCatClassification": "GET",
        "species/getSpCatFamilies": "POST",
        "species/getSpCatGenera": "POST",
        "species/getSpeciesVersions": "GET",
        "species/getSpecCombPk": "POST",
        "species/getMonoByPk": "POST",
        "species/getSpecByPk": "POST",
        "species/getSpeciesWithTypeByPK": "POST",
        "species/getSpecTaxCitByPk": "POST",
        "species/getSpecFigCitByPk": "POST",
        "species/getSpecDistCitByPk": "POST",
        "species/getSpecPhyloByPk": "POST",
        "species/getPhyloByPk": "POST",
        "species/getSpeciesKeyTerminalsByPk": "POST",
        "species/getImageCombObjIDs": "GET",
        "species/getAllFigCitImg": "GET",
        "species/getAllFigCit": "GET",
        "general/getTypeSpmnCoordinates": "GET",
        "general/getStringPatternIDs": "GET",
        "general/getStringPatterns": "GET",
        "biblio/doBiblioSearch": "POST",
        "biblio/getBiblioByPk": "POST",
        "biblio/getTaxaByBiblioPk": "POST",
        "biblio/getFigureByBiblioPk": "POST",
        "biblio/getFigureByPk": "POST",
    }

    def __init__(self, delay: float = 0.35, timeout: float = 30.0):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.6,
            allowed_methods=frozenset({"GET", "POST"}),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            "User-Agent": "lacewing-digital-library-mcp/0.1 (research use; low-rate)",
            "Accept": "application/json",
        })
        self._last_request = 0.0
        self._coordinate_cache: tuple[float, list[dict[str, Any]]] | None = None

    def _throttle(self) -> None:
        wait = self.delay - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        self._throttle()
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        if method == "GET":
            response = self.session.get(url, timeout=self.timeout)
        else:
            response = self.session.post(url, json=payload or {}, timeout=self.timeout)
        self._last_request = time.time()
        response.raise_for_status()
        return response.json()

    def raw_public(self, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        endpoint = endpoint.lstrip("/")
        method = self.PUBLIC_ENDPOINTS.get(endpoint)
        if not method:
            raise ValueError("Endpoint is not in the read-only public allowlist")
        return self._request(method, endpoint, payload)

    @classmethod
    def condition(cls, value: Any, op: str = "eq") -> dict[str, Any]:
        if op not in cls.OPS:
            raise ValueError(f"Unsupported operator {op!r}; choose from {sorted(cls.OPS)}")
        return {cls.OPS[op]: value}

    def search(self, where: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._request("POST", "species/doSpeciesSearch", {"where": where})
        return data if isinstance(data, list) else []

    def search_taxa(
        self,
        *,
        genus: str | None = None,
        species: str | None = None,
        family: str | None = None,
        author: str | None = None,
        year: str | int | None = None,
        valid_only: bool = False,
        extant_only: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {"TaxonRankBlockID": 26, "IsUnidSp": 0}
        if genus:
            where["PresCombGenu"] = self.condition(genus, "eq")
        if species:
            where["StrPatternSpecEpith"] = self.condition(species, "starts")
        if family:
            where["PresCombFami"] = self.condition(family, "eq")
        if author:
            where["ASAuth"] = self.condition(author, "contains")
        if year is not None:
            where["ASYear"] = self.condition(str(year), "eq")
        if valid_only:
            where["CombLeastTaxObjIsValid"] = -1
        if extant_only:
            where["TaxonIsExtant"] = -1
        rows = self.search(where)
        return rows[: max(1, min(limit, 1000))]

    def search_name(self, scientific_name: str) -> list[dict[str, Any]]:
        parts = scientific_name.strip().split()
        if not parts:
            return []
        if len(parts) == 1:
            return self.search_taxa(genus=parts[0])
        return self.search_taxa(genus=parts[0], species=parts[1], limit=500)

    def resolve_current(self, scientific_name: str) -> dict[str, Any] | None:
        rows = self.search_name(scientific_name)
        if not rows:
            return None
        parts = scientific_name.strip().split()
        genus = parts[0] if parts else ""
        species = parts[1] if len(parts) > 1 else ""
        exact_taxon = [r for r in rows if r.get("PresCombGenu") == genus and r.get("PresCombSpec") == species]
        candidates = exact_taxon or rows
        for row in candidates:
            if row.get("CombTaxStatus") == "valid" and row.get("CombGenuToSubs") == scientific_name:
                return row
        for row in candidates:
            if row.get("CombTaxStatus") == "valid":
                return row
        return candidates[0]

    def combination(self, comb_obj_id: int) -> dict[str, Any]:
        return self._request("POST", "species/getSpecCombPk", {"CombObjID": comb_obj_id})

    def monograph(self, comb_obj_id: int) -> dict[str, Any]:
        return self._request("POST", "species/getMonoByPk", {"CombObjID": comb_obj_id})

    def epithet(self, tax_obj_id: int) -> dict[str, Any]:
        return self._request("POST", "species/getSpecByPk", {"TaxObjID": tax_obj_id})

    def type_record(self, comb_obj_id: int) -> dict[str, Any]:
        return self._request("POST", "species/getSpeciesWithTypeByPK", {"CombObjID": comb_obj_id})

    def taxonomic_citations(self, comb_obj_id: int) -> Any:
        return self._request("POST", "species/getSpecTaxCitByPk", {"CombObjID": comb_obj_id})

    def figure_citations(self, comb_obj_id: int) -> Any:
        return self._request("POST", "species/getSpecFigCitByPk", {"CombObjID": comb_obj_id})

    def distribution_citations(self, comb_obj_id: int) -> Any:
        return self._request("POST", "species/getSpecDistCitByPk", {"CombObjID": comb_obj_id})

    def phylogeny(self, comb_obj_id: int) -> Any:
        return self._request("POST", "species/getSpecPhyloByPk", {"CombObjID": comb_obj_id})

    def identification_keys(self, comb_obj_id: int) -> Any:
        return self._request("POST", "species/getSpeciesKeyTerminalsByPk", {"CombObjID": comb_obj_id})

    def type_coordinates(self, tax_obj_id: int | None = None, cache_seconds: int = 3600) -> list[dict[str, Any]]:
        now = time.time()
        if self._coordinate_cache and now - self._coordinate_cache[0] < cache_seconds:
            rows = self._coordinate_cache[1]
        else:
            data = self._request("GET", "general/getTypeSpmnCoordinates")
            rows = data if isinstance(data, list) else []
            self._coordinate_cache = (now, rows)
        if tax_obj_id is None:
            return rows
        return [r for r in rows if r.get("Type_TaxObjID") == tax_obj_id]

    def classification(self) -> Any:
        return self._request("GET", "species/getSpCatClassification")

    def species_versions(self) -> Any:
        return self._request("GET", "species/getSpeciesVersions")

    def bibliography(self, bib_obj_id: int) -> Any:
        return self._request("POST", "biblio/getBiblioByPk", {"BibObjID": bib_obj_id})

    def taxa_for_reference(self, bib_obj_id: int) -> Any:
        return self._request("POST", "biblio/getTaxaByBiblioPk", {"BibObjID": bib_obj_id})

    def figures_for_reference(self, bib_obj_id: int) -> Any:
        return self._request("POST", "biblio/getFigureByBiblioPk", {"BibObjID": bib_obj_id})

    def search_bibliography(self, criteria: dict[str, Any]) -> Any:
        return self._request("POST", "biblio/doBiblioSearch", criteria)

    def valid_extant_species(self, family: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return deterministic current valid extant species, deduplicated by species TaxObjID."""
        rows = self.search_taxa(family=family, valid_only=True, extant_only=True, limit=1000)
        by_tax: dict[int, dict[str, Any]] = {}
        for row in rows:
            tax_id = row.get("CombSpec_TaxObjID")
            if not tax_id or row.get("CombTaxStatus") != "valid":
                continue
            if row.get("TaxonRankBlockID") != 26 or row.get("IsUnidSp") != 0:
                continue
            by_tax.setdefault(int(tax_id), row)
        def name_key(row: dict[str, Any]) -> str:
            return " ".join(str(x) for x in [row.get("PresCombGenu"), row.get("PresCombSpec"), row.get("PresCombSubs")] if x).lower()
        return sorted(by_tax.values(), key=name_key)[: max(1, min(limit, 200))]

    def distribution_source_records(self, comb_obj_id: int, include_bibliography: bool = True) -> list[dict[str, Any]]:
        """Flatten LDL's cited geographic records and optionally attach bibliography metadata."""
        raw = self.distribution_citations(comb_obj_id)
        records = raw.get("Syn_DistGeogBibObjList", []) if isinstance(raw, dict) else []
        bib_cache: dict[int, Any] = {}
        out: list[dict[str, Any]] = []
        for rec in records:
            row = dict(rec)
            bib_id = row.get("Source_BibObjID")
            if include_bibliography and bib_id:
                bid = int(bib_id)
                if bid not in bib_cache:
                    bib_cache[bid] = self.bibliography(bid)
                row["Bibliography"] = bib_cache[bid]
            out.append(row)
        return out

    def credential_status(self) -> dict[str, Any]:
        """Report whether optional contributor credentials are discoverable without exposing secrets."""
        user, password, source = self._credential_pair()
        return {"configured": bool(user and password), "source": source, "username_configured": bool(user)}

    def _credential_pair(self) -> tuple[str | None, str | None, str]:
        user = os.getenv("LDL_USERNAME")
        password = os.getenv("LDL_PASSWORD")
        if user and password:
            return user, password, "environment"
        config_path = Path.home() / ".config" / "lacewing-digital-library-mcp" / "config.json"
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text())
                user = user or cfg.get("username")
            except Exception:
                pass
        if user and platform.system() == "Darwin":
            try:
                proc = subprocess.run(
                    ["security", "find-generic-password", "-s", "lacewing-digital-library-mcp", "-a", user, "-w"],
                    capture_output=True, text=True, timeout=5, check=True,
                )
                password = proc.stdout.rstrip("\n")
                if password:
                    return user, password, "macos-keychain"
            except Exception:
                pass
        return user, password, "not-configured"

    def authenticate_contributor(self) -> bool:
        """Authenticate using env/keychain credentials; never accepts secrets as function arguments."""
        user, password, _ = self._credential_pair()
        if not user or not password:
            return False
        self._throttle()
        response = self.session.post(
            f"{self.BASE_URL}/users/login",
            json={"UserName": user, "Password": password},
            timeout=self.timeout,
        )
        self._last_request = time.time()
        return bool(response.ok)

    def fetch_edoc_pdf(self, edoc_id: int) -> bytes:
        """Fetch an LDL EDoc PDF with optional contributor auth; never exposes credentials."""
        if not self.authenticate_contributor():
            raise PermissionError("LDL contributor credentials are not configured in env/keychain")
        self._throttle()
        response = self.session.get(
            f"{self.SITE_URL}/edoc/{int(edoc_id)}", timeout=self.timeout, allow_redirects=True
        )
        self._last_request = time.time()
        response.raise_for_status()
        if not response.content.startswith(b"%PDF"):
            raise PermissionError("LDL EDoc did not return a PDF; contributor access may be unavailable for this record")
        return response.content
