"""Offline georeferencing against GeoNames per-country dumps (CC BY 4.0).

``<CC>.zip`` is downloaded once per country into ``<LDL_MCP_HOME>/gazetteer`` and
indexed into SQLite. A locality resolves only inside the country the source
states, preferably within the stated province; ambiguous names are returned as
candidates for review instead of being guessed. Every result carries the
GeoNames id, feature class and an explicit uncertainty radius.
"""

from __future__ import annotations

import io
import math
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import requests

from . import geo
from .paths import now_iso, read_json, subdir, write_json_atomic

BASE = "https://download.geonames.org/export/dump"
KEEP_CLASSES = {"P", "A", "T", "H", "L", "S", "V"}
NOISE = re.compile(
    r"\b(env(irons)?|vicinity|vic|near|nr|ca|circa|prov(ince)?|pref(ecture)?|distr(ict)?|dist|co(unty)?|"
    r"reg(ion)?|state|oblast|krai|kray|city|town|village|vill?|mt|mts|mount|mountains?|shan|"
    r"km|n|s|e|w|ne|nw|se|sw|of|the|de|del|la|le)\b\.?",
    re.IGNORECASE,
)


def _dir() -> Path:
    return subdir("gazetteer")


def _fold(text: str) -> str:
    return geo.fold(re.sub(r"[\"'’`]", "", text or ""))


def _download(name: str, timeout: int = 600, attempts: int = 4) -> bytes:
    import time

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(f"{BASE}/{name}", timeout=timeout, headers={"User-Agent": "lacewing-digital-library-mcp/0.4 (research use)"})
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last = exc
            time.sleep(min(30, 3 * 2**attempt))
    raise RuntimeError(f"GeoNames download failed for {name}: {type(last).__name__}")


def _admin1(iso2: str) -> dict[str, str]:
    """Map folded admin1 names -> admin1 code for one country."""
    path = _dir() / "admin1CodesASCII.txt"
    if not path.exists():
        path.write_bytes(_download("admin1CodesASCII.txt"))
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].startswith(iso2 + "."):
            code = parts[0].split(".", 1)[1]
            for name in (parts[1], parts[2]):
                folded = _fold(name)
                out[folded] = code
                stripped = _fold(re.sub(r"\b(Sheng|Shi|Province|Oblast|Prefecture|Region|Zizhiqu|Autonomous Region)\b", "", name))
                if stripped:
                    out.setdefault(stripped, code)
    return out


def ensure_country(iso2: str, source: Path | None = None) -> Path:
    """Build (once) the SQLite index for one country. ``source`` allows an offline TSV/zip."""
    iso2 = iso2.upper()
    if not re.fullmatch(r"[A-Z]{2}", iso2):
        raise ValueError(f"Bad ISO2 country code {iso2!r}")
    db_path = _dir() / f"{iso2}.sqlite"
    if db_path.exists():
        return db_path
    raw = source.read_bytes() if source else _download(f"{iso2}.zip")
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            text = archive.read(f"{iso2}.txt").decode("utf-8")
    else:
        text = raw.decode("utf-8")
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE place (id INTEGER, name TEXT, lat REAL, lon REAL, fclass TEXT, fcode TEXT, admin1 TEXT, pop INTEGER)")
    con.execute("CREATE TABLE alias (key TEXT, id INTEGER)")
    places, aliases = [], []
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 15 or p[6] not in KEEP_CLASSES:
            continue
        gid = int(p[0])
        places.append((gid, p[1], float(p[4]), float(p[5]), p[6], p[7], p[10], int(p[14] or 0)))
        names = {p[1], p[2], *[a for a in p[3].split(",") if a and len(a) <= 60]}
        aliases.extend((_fold(n), gid) for n in names if _fold(n))
    con.executemany("INSERT INTO place VALUES (?,?,?,?,?,?,?,?)", places)
    con.executemany("INSERT INTO alias VALUES (?,?)", aliases)
    con.execute("CREATE INDEX alias_key ON alias(key)")
    con.execute("CREATE UNIQUE INDEX place_id ON place(id)")
    con.commit()
    con.close()
    tmp.replace(db_path)
    meta = read_json(_dir() / "sources.json", {}) or {}
    meta[iso2] = {"file": f"{BASE}/{iso2}.zip" if not source else str(source), "indexed_at": now_iso(), "places": len(places)}
    write_json_atomic(_dir() / "sources.json", meta)
    return db_path


def _uncertainty_m(fclass: str, fcode: str, pop: int) -> int:
    if fclass == "P":
        return 20000 if pop >= 1_000_000 else 10000 if pop >= 100_000 else 5000 if pop >= 10_000 else 3000
    if fclass == "A":
        return {"ADM1": 200000, "ADM2": 40000}.get(fcode, 15000)
    if fclass == "T":
        return 50000 if fcode in {"MTS", "HLLS", "PLAT", "UPLD"} else 10000
    if fclass == "L":
        return 20000
    if fclass == "H":
        return 5000
    return 3000


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def _tokens(locality: str) -> list[list[str]]:
    """Locality parts, broad to specific; each part as [as written, without generic words]."""
    out = []
    for part in re.split(r"[,;:/()\[\]]| - ", locality or ""):
        part = re.sub(r"\d+(?:[.,]\d+)?\s*(?:°|'|′|\"|″|m\b|km\b|ft\b)?", " ", part)
        raw = re.sub(r"\s+", " ", part).strip(" .-")
        cleaned = re.sub(r"\s+", " ", NOISE.sub(" ", raw)).strip(" .-")
        variants = [v for v in dict.fromkeys([raw, cleaned]) if len(v) >= 3]
        if variants:
            out.append(variants)
    return out


def georeference(locality: str, country: str, admin1: str | None = None, gazetteer_file: Path | None = None) -> dict[str, Any]:
    """Resolve one locality string within a country (ADM0_A3 or name). Never guesses between distant candidates."""
    code = country if country and len(country) == 3 and country.isupper() else geo.normalize_country(country)
    info = geo.country_info(code) if code else None
    if not info or not info.get("iso2"):
        return {"status": "no_country", "locality": locality, "message": f"Country {country!r} not recognised"}
    iso2 = info["iso2"]
    db = sqlite3.connect(ensure_country(iso2, gazetteer_file))
    try:
        return _resolve(db, iso2, code, locality, admin1, gazetteer_file)
    finally:
        db.close()


SQL = (
    "SELECT p.id, p.name, p.lat, p.lon, p.fclass, p.fcode, p.admin1, p.pop "
    "FROM alias a JOIN place p ON p.id = a.id WHERE a.key = ?"
)
RANK = {"P": 0, "A": 1, "T": 2, "L": 3, "H": 4}


def _lookup(db: sqlite3.Connection, variants: list[str], admin_code: str | None) -> tuple[str, list[tuple]]:
    for token in variants:
        rows = list({r[0]: r for r in db.execute(SQL, (_fold(token),)).fetchall()}.values())
        if rows:
            if admin_code:
                rows = [r for r in rows if r[6] == admin_code] or rows
            rows.sort(key=lambda r: (RANK.get(r[4], 5), -r[7]))
            return token, rows
    return variants[0], []


def _container(rows: list[tuple]) -> tuple | None:
    """An ADM1/ADM2 unit among the matches means the token names an administrative container."""
    units = [r for r in rows if r[4] == "A" and r[5] in {"ADM1", "ADM2"}]
    return units[0] if units else None


def _context_point(rows: list[tuple]) -> tuple[float, float] | None:
    """A broader locality that resolves to one place (not a whole province) gives spatial context."""
    unit = _container(rows)
    if unit is not None and unit[5] == "ADM2":
        cities = [r for r in rows if r[4] == "P" and r[7] >= 50_000 and _km((r[2], r[3]), (unit[2], unit[3])) <= 100]
        return (cities[0][2], cities[0][3]) if cities else (unit[2], unit[3])
    usable = [r for r in rows if not (r[4] == "A" and r[5] == "ADM1")]
    if not usable:
        return None
    first = usable[0]
    if all(_km((first[2], first[3]), (r[2], r[3])) <= 25 for r in usable[1:4]) or (first[7] and first[7] >= 10 * max([r[7] for r in usable[1:]] or [0])):
        return (first[2], first[3])
    return None


def _result(status: str, best: tuple, token: str, code: str, iso2: str, locality: str, admin_code: str | None, note: str = "") -> dict[str, Any]:
    source_file = (read_json(_dir() / "sources.json", {}) or {}).get(iso2, {}).get("file", f"{BASE}/{iso2}.zip")
    return {
        "status": status,
        "locality": locality,
        "token": token,
        "country": code,
        "latitude": round(best[2], 5),
        "longitude": round(best[3], 5),
        "coordinate_uncertainty_m": _uncertainty_m(best[4], best[5], best[7]),
        "georeference_source": f"GeoNames (CC BY 4.0) geonameid {best[0]} '{best[1]}' {best[4]}.{best[5]}; {source_file}",
        "admin1_code": best[6],
        "admin1_matched": bool(admin_code and best[6] == admin_code),
        "feature": f"{best[4]}.{best[5]}",
        "note": note,
    }


def _resolve(db: sqlite3.Connection, iso2: str, code: str, locality: str, admin1: str | None, gazetteer_file: Path | None) -> dict[str, Any]:
    admin_index = _admin1(iso2) if gazetteer_file is None else {}
    admin_code = admin_index.get(_fold(admin1 or "")) if admin1 else None
    tokens = _tokens(locality)
    admin_tokens: dict[int, str] = {}
    for index, variants in enumerate(tokens):
        for token in variants:
            hit = admin_index.get(_fold(token))
            if hit:
                admin_tokens[index] = hit
                admin_code = admin_code or hit
                break
    looked = [_lookup(db, variants, admin_code) for variants in tokens]
    tried = [v[0] for v in tokens]
    containers = {i for i, (_, rows) in enumerate(looked) if rows and (i in admin_tokens or _container(rows) is not None)}
    targets = [i for i in range(len(tokens) - 1, -1, -1) if looked[i][1] and i not in containers]
    for index in targets:
        token, rows = looked[index]
        context = [p for p in (_context_point(looked[j][1]) for j in range(len(tokens)) if j != index and j not in admin_tokens) if p]
        if context:
            near = [r for r in rows if min(_km((r[2], r[3]), c) for c in context) <= 75]
            rows = near or rows
        best = rows[0]
        distant = [r for r in rows[1:] if _km((best[2], best[3]), (r[2], r[3])) > 25 and RANK.get(r[4], 5) <= RANK.get(best[4], 5)]
        if not distant:
            return _result("resolved", best, token, code, iso2, locality, admin_code, "constrained by broader locality" if context else "")
        if context:
            closest = min(rows, key=lambda r: min(_km((r[2], r[3]), c) for c in context))
            if min(_km((closest[2], closest[3]), c) for c in context) <= 25:
                return _result("resolved", closest, token, code, iso2, locality, admin_code, "nearest same-named place to the broader locality")
        if best[7] and best[7] >= 10 * max(r[7] for r in distant):
            return _result("probable", best, token, code, iso2, locality, admin_code, f"{len(distant)} other places share this name; chosen by population. Review.")
        return {
            "status": "ambiguous",
            "locality": locality,
            "token": token,
            "country": code,
            "candidates": [
                {"geonameid": r[0], "name": r[1], "lat": r[2], "lon": r[3], "feature": f"{r[4]}.{r[5]}", "admin1": r[6], "population": r[7]}
                for r in rows[:8]
            ],
            "message": "Several distant places share this name; choose one or give the province.",
        }
    for index in sorted(containers, reverse=True):
        token, rows = looked[index]
        unit = _container(rows)
        if unit is not None and unit[5] == "ADM2":
            cities = [r for r in rows if r[4] == "P" and r[7] >= 50_000 and _km((r[2], r[3]), (unit[2], unit[3])) <= 100]
            result = _result("resolved", cities[0] if cities else unit, token, code, iso2, locality, admin_code,
                             "prefecture-level unit only; seat city coordinates" if cities else "prefecture-level unit only")
            result["coordinate_uncertainty_m"] = _uncertainty_m("A", "ADM2", 0)
            return result
    for index in sorted(containers, reverse=True):
        token, rows = looked[index]
        unit = _container(rows)
        if unit is not None:
            return _result("coarse", unit, token, code, iso2, locality, admin_code, "Only a province/state could be resolved; not a point locality.")
    return {"status": "not_found", "locality": locality, "country": code, "tried": tried}
