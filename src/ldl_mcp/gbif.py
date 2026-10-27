"""GBIF occurrences as an independent comparison layer (never mixed into literature evidence).

Uses the public GBIF API (species/match, occurrence/search). Results are
cached for 30 days. For publication, cite a GBIF download DOI created on
gbif.org for the same query; API search results are not themselves citable.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import requests

from .paths import now_iso, read_json, subdir, write_json_atomic

API = "https://api.gbif.org/v1"
NEUROPTERIDA_ORDERS = {"Neuroptera", "Megaloptera", "Raphidioptera"}
_last = [0.0]


def _get(path: str, params: dict[str, Any]) -> Any:
    wait = 0.3 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    for attempt in range(3):
        try:
            response = requests.get(f"{API}/{path}", params=params, timeout=60, headers={"User-Agent": "lacewing-digital-library-mcp/0.4 (research use)"})
            _last[0] = time.time()
            if response.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("GBIF request failed")


def match(name: str) -> dict[str, Any]:
    data = _get("species/match", {"name": name, "rank": "SPECIES", "kingdom": "Animalia", "strict": "true"})
    ok = (
        data.get("matchType") == "EXACT"
        and data.get("order") in NEUROPTERIDA_ORDERS
        and data.get("rank") in {"SPECIES", "SUBSPECIES"}
    )
    key = data.get("acceptedUsageKey") if data.get("status") == "SYNONYM" else data.get("usageKey")
    return {
        "name": name,
        "ok": bool(ok and key),
        "usageKey": key,
        "gbif_name": data.get("scientificName"),
        "gbif_status": data.get("status"),
        "matchType": data.get("matchType"),
        "note": "GBIF treats this name as a synonym; occurrences of the GBIF accepted taxon are used" if data.get("status") == "SYNONYM" else "",
    }


def occurrences(name: str, max_records: int = 300, basis_of_record: list[str] | None = None) -> dict[str, Any]:
    m = match(name)
    if not m["ok"]:
        return {"match": m, "rows": [], "total": 0}
    params: dict[str, Any] = {
        "taxonKey": m["usageKey"],
        "hasCoordinate": "true",
        "hasGeospatialIssue": "false",
        "occurrenceStatus": "PRESENT",
    }
    if basis_of_record:
        params["basisOfRecord"] = basis_of_record
    cache_key = hashlib.sha256(json.dumps([params, max_records], sort_keys=True).encode()).hexdigest()[:16]
    cache = subdir("gbif") / f"{m['usageKey']}-{cache_key}.json"
    cached = read_json(cache, None)
    if cached and time.time() - cached.get("fetched_epoch", 0) < 30 * 86400:
        return {"match": m, **cached}
    rows: list[dict[str, Any]] = []
    offset, total = 0, 0
    while len(rows) < max_records:
        page = _get("occurrence/search", {**params, "limit": min(300, max_records - len(rows)), "offset": offset})
        total = page.get("count", 0)
        for r in page.get("results", []):
            rows.append({
                "gbif_key": r.get("key"),
                "latitude": r.get("decimalLatitude"),
                "longitude": r.get("decimalLongitude"),
                "coordinate_uncertainty_m": r.get("coordinateUncertaintyInMeters"),
                "basis_of_record": r.get("basisOfRecord"),
                "country_code": r.get("countryCode"),
                "locality": r.get("locality"),
                "event_date": r.get("eventDate"),
                "year": r.get("year"),
                "recorded_by": r.get("recordedBy"),
                "institution": r.get("institutionCode"),
                "catalog_number": r.get("catalogNumber"),
                "dataset_key": r.get("datasetKey"),
                "issues": r.get("issues", []),
            })
        offset += len(page.get("results", []))
        if page.get("endOfRecords") or not page.get("results"):
            break
    value = {"rows": rows, "total": total, "fetched_at": now_iso(), "fetched_epoch": time.time(), "query": params}
    write_json_atomic(cache, value)
    return {"match": m, **value}
