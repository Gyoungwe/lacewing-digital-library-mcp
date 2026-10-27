"""Country polygons (bundled Natural Earth 1:50m) and country-name normalization.

Used to cross-check literature points: does a point fall in the country the
paper states, and is that country one of LDL's accepted distribution records?
"""

from __future__ import annotations

import gzip
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

ADMIN0 = Path(__file__).resolve().parent / "data" / "ne_50m_admin0.geojson.gz"

# LDL / literature spellings that differ from Natural Earth names.
ALIASES = {
    "united states": "USA", "usa": "USA", "u.s.a.": "USA", "united states of america": "USA",
    "russia": "RUS", "russian federation": "RUS", "ussr": "RUS",
    "czechia": "CZE", "czech republic": "CZE", "czechoslovakia": "CZE",
    "united kingdom": "GBR", "great britain": "GBR", "england": "GBR", "scotland": "GBR", "wales": "GBR",
    "democratic republic of the congo": "COD", "congo (kinshasa)": "COD", "zaire": "COD", "dr congo": "COD",
    "republic of the congo": "COG", "congo (brazzaville)": "COG", "congo": "COG",
    "ivory coast": "CIV", "cote d'ivoire": "CIV",
    "north macedonia": "MKD", "macedonia": "MKD",
    "south korea": "KOR", "korea, republic of": "KOR", "republic of korea": "KOR",
    "north korea": "PRK", "korea, democratic people's republic of": "PRK",
    "iran": "IRN", "persia": "IRN", "syria": "SYR", "laos": "LAO", "vietnam": "VNM", "viet nam": "VNM",
    "burma": "MMR", "myanmar": "MMR", "ceylon": "LKA", "sri lanka": "LKA",
    "taiwan": "TWN", "formosa": "TWN", "tanzania": "TZA", "bolivia": "BOL", "venezuela": "VEN",
    "moldova": "MDA", "turkey": "TUR", "turkiye": "TUR", "eswatini": "SWZ", "swaziland": "SWZ",
    "east timor": "TLS", "timor-leste": "TLS", "cape verde": "CPV", "cabo verde": "CPV",
    "bosnia and herzegovina": "BIH", "serbia": "SRB", "kosovo": "KOS", "palestine": "PSX",
    "the gambia": "GMB", "gambia": "GMB", "the bahamas": "BHS", "bahamas": "BHS",
    "brunei": "BRN", "micronesia": "FSM", "vatican": "VAT", "holy see": "VAT",
    "中国": "CHN", "日本": "JPN", "韩国": "KOR", "朝鲜": "PRK", "俄罗斯": "RUS", "蒙古": "MNG", "台湾": "TWN",
    "越南": "VNM", "老挝": "LAO", "泰国": "THA", "缅甸": "MMR", "印度": "IND", "尼泊尔": "NPL", "美国": "USA",
}


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"\s+", " ", "".join(c for c in decomposed if not unicodedata.combining(c))).strip().casefold()


@lru_cache(maxsize=1)
def _countries() -> list[dict[str, Any]]:
    data = json.loads(gzip.decompress(ADMIN0.read_bytes()).decode("utf-8"))
    out = []
    for feature in data["features"]:
        props = feature["properties"]
        geometry = feature["geometry"]
        polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        rings = []
        for polygon in polygons:
            outer = [(float(x), float(y)) for x, y, *_ in polygon[0]]
            holes = [[(float(x), float(y)) for x, y, *_ in hole] for hole in polygon[1:]]
            xs, ys = [p[0] for p in outer], [p[1] for p in outer]
            rings.append({"outer": outer, "holes": holes, "bbox": (min(xs), max(xs), min(ys), max(ys))})
        code = props.get("ADM0_A3") or props.get("ISO_A3")
        out.append({
            "code": code,
            "iso2": props.get("ISO_A2_EH") if props.get("ISO_A2_EH") not in (None, "-99") else None,
            "name": props.get("ADMIN"),
            "name_zh": props.get("NAME_ZH"),
            "names": {fold(str(props.get(k))) for k in ("ADMIN", "NAME", "NAME_LONG", "FORMAL_EN", "NAME_ZH") if props.get(k)},
            "rings": rings,
        })
    return out


@lru_cache(maxsize=1)
def _name_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for country in _countries():
        for name in country["names"]:
            index.setdefault(name, country["code"])
        index.setdefault(fold(country["code"]), country["code"])
    for alias, code in ALIASES.items():
        index[fold(alias)] = code
    return index


def country_info(code: str | None) -> dict[str, Any] | None:
    for country in _countries():
        if country["code"] == code:
            return {"code": country["code"], "iso2": country["iso2"], "name": country["name"], "name_zh": country["name_zh"]}
    return None


def normalize_country(text: str | None) -> str | None:
    """Map a country string (LDL 'China : Fujian (province)', 'CHINA:', '中国') to an ADM0_A3 code."""
    if not text:
        return None
    head = re.split(r"\s*[:;,]\s*|\s+\(", str(text).strip(), maxsplit=1)[0]
    for candidate in (head, str(text)):
        code = _name_index().get(fold(candidate).strip(" .:"))
        if code:
            return code
    return None


def admin1_hint(full_geog: str | None) -> str | None:
    """'China : Fujian (province)' -> 'Fujian'."""
    if not full_geog or ":" not in full_geog:
        return None
    part = full_geog.split(":")[1]
    return re.sub(r"\s*\(.*?\)\s*", "", part).strip() or None


def _in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _segment_km(lon: float, lat: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    kx = 111.32 * math.cos(math.radians(lat))
    ax, ay = (a[0] - lon) * kx, (a[1] - lat) * 110.57
    bx, by = (b[0] - lon) * kx, (b[1] - lat) * 110.57
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / ((dx * dx + dy * dy) or 1e-12)))
    return math.hypot(ax + t * dx, ay + t * dy)


def country_at(lat: float, lon: float, coast_tolerance_km: float = 15.0) -> dict[str, Any]:
    """Country containing a point; points just offshore (1:50m coastline) snap to the nearest country."""
    for country in _countries():
        for ring in country["rings"]:
            x0, x1, y0, y1 = ring["bbox"]
            if x0 <= lon <= x1 and y0 <= lat <= y1 and _in_ring(lon, lat, ring["outer"]) and not any(_in_ring(lon, lat, h) for h in ring["holes"]):
                return {"code": country["code"], "name": country["name"], "match": "inside"}
    deg = coast_tolerance_km / 100.0 + 0.05
    best: tuple[float, dict[str, Any]] | None = None
    for country in _countries():
        for ring in country["rings"]:
            x0, x1, y0, y1 = ring["bbox"]
            if not (x0 - deg <= lon <= x1 + deg and y0 - deg <= lat <= y1 + deg):
                continue
            pts = ring["outer"]
            for a, b in zip(pts, pts[1:]):
                if abs(a[0] - lon) > deg * 3 and abs(b[0] - lon) > deg * 3:
                    continue
                d = _segment_km(lon, lat, a, b)
                if best is None or d < best[0]:
                    best = (d, country)
    if best and best[0] <= coast_tolerance_km:
        return {"code": best[1]["code"], "name": best[1]["name"], "match": "near_coast", "distance_km": round(best[0], 1)}
    return {"code": None, "name": None, "match": "sea_or_unknown"}


# Territories LDL records under a parent country (e.g. 'China : Taiwan (province)').
PARENT = {"TWN": "CHN", "HKG": "CHN", "MAC": "CHN", "ALD": "FIN", "FRO": "DNK", "GRL": "DNK"}


def same_country(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a == b or PARENT.get(a) == b or PARENT.get(b) == a or (PARENT.get(a) and PARENT.get(a) == PARENT.get(b))
