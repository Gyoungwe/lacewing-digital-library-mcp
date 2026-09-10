from __future__ import annotations

import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class LDLClient:
    """Low-rate client for public endpoints used by the LDL web frontend."""

    BASE_URL = "https://lacewing.tamu.edu/api"
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
