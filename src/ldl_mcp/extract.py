"""Rule-based extraction of specimen/locality records from taxonomic page text.

Finds "Material examined" / "Type material" / "Distribution" style sections,
splits them into records, and parses country, locality, coordinates,
elevation, date, collector and type status. Every candidate keeps the exact
source substring as ``evidence_quote`` so the evidence store can verify it on
the page. Anything the rules cannot read stays as text for the agent or a
human; the rules never invent values.
"""

from __future__ import annotations

import re
from typing import Any

from . import geo

HEADING = re.compile(
    r"(?:Materials?\s+examined|Specimens?\s+examined|Material\s+studied|Examined\s+material|Type[\s-]+material|"
    r"Types?\s*[:.]|Holotype|Lectotype|Neotype|Syntypes?|Paratypes?|Other\s+material|Additional\s+material|"
    r"Non-type\s+material|New\s+records?|Records?\s*[:.]|Distribution\s*[:.]|Untersuchtes\s+Material|"
    r"Mat[ée]riel\s+(?:examin[ée]|[ée]tudi[ée])|Material\s+examinado|检视标本|观察标本|研究标本|正模|副模|分布[:：])",
    re.IGNORECASE,
)
STOP = re.compile(
    r"\b(?:Diagnosis|Description|Redescription|Remarks?|Comments?|Discussion|Etymology|Biology|Bionomics|"
    r"Larvae?|Male\s+genitalia|Female\s+genitalia|Key\s+to|Measurements|Differential\s+diagnosis|Note[s]?\s*:|"
    r"Beschreibung|Bemerkungen|Diagnose|Remarques)\b|鉴别特征|描述|讨论|词源",
    re.IGNORECASE,
)
DEG = r"[°º˚]"
MIN = r"['′’ʹ]"
SEC = r"(?:[\"″”]|'')"
DMS_LAT = rf"(\d{{1,2}})\s*{DEG}\s*(?:(\d{{1,2}}(?:[.,]\d+)?)\s*{MIN}\s*(?:(\d{{1,2}}(?:[.,]\d+)?)\s*{SEC})?)?\s*([NS])"
DMS_LON = rf"(\d{{1,3}})\s*{DEG}\s*(?:(\d{{1,2}}(?:[.,]\d+)?)\s*{MIN}\s*(?:(\d{{1,2}}(?:[.,]\d+)?)\s*{SEC})?)?\s*([EW])"
COORD_PATTERNS = [
    re.compile(DMS_LAT + r"[\s,;/]*" + DMS_LON),
    re.compile(r"(\d{1,2}[.,]\d{2,7})\s*" + DEG + r"?\s*([NS])[\s,;/]+(\d{1,3}[.,]\d{2,7})\s*" + DEG + r"?\s*([EW])"),
    re.compile(r"\b([NS])\s*(\d{1,2}[.,]\d{2,7})" + DEG + r"?[\s,;/]+([EW])\s*(\d{1,3}[.,]\d{2,7})" + DEG + r"?"),
]
ELEVATION = re.compile(r"\b(?:alt\.?\s*|elev\.?\s*|ca\.?\s*)?(\d{1,2}[ ,]?\d{3}|\d{2,4})(?:\s*[-–]\s*(\d{1,2}[ ,]?\d{3}|\d{2,4}))?\s*m\b(?!m)", re.IGNORECASE)
ROMAN_DATE = re.compile(r"\b(\d{1,2}(?:[-–]\d{1,2})?\s*\.?\s*[ivxIVX]{1,4}\s*\.?\s*(?:1[789]|20)\d{2})\b")
ROMAN_DATE_REV = re.compile(r"\b((?:1[789]|20)\d{2}\s*\.\s*[ivxIVX]{1,4}\s*\.\s*\d{1,2})\b")
ISO_DATE = re.compile(r"\b((?:1[789]|20)\d{2}[-./](?:0?[1-9]|1[0-2])[-./](?:0?[1-9]|[12]\d|3[01]))\b")
TEXT_DATE = re.compile(
    r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(?:1[789]|20)\d{2})\b", re.IGNORECASE
)
YEAR_ONLY = re.compile(r"\b((?:1[789]|20)\d{2})\b")
COLLECTOR = re.compile(r"\b(?:leg|coll|col|collr?)\.?\s*:?\s*([A-Z][\w'’.\-]*(?:\s*(?:&|and|,)\s*[A-Z][\w'’.\-]*|\s+[A-Z][\w'’.\-]*){0,4})")
COLLECTOR_ZH = re.compile(r"([一-鿿]{2,4}(?:[、,，][一-鿿]{2,4})*)\s*(?:采|采集|收集)")
COUNTRY_ANY = re.compile(r"(?:^|[\s,;，；(])([A-Z]{3,}(?:[ .'\-][A-Z]{2,})*|[一-鿿]{2,6})\s*[:：]\s*")
TYPE_STATUS = re.compile(r"\b(holotype|lectotype|neotype|syntypes?|paratypes?|paralectotypes?|allotype)\b|(正模|副模|配模)", re.IGNORECASE)
SEX = re.compile(r"(\d+\s*)?(?:♂♂|♀♀|♂|♀|males?\b|females?\b|exs?\.\b|specimens?\b)", re.IGNORECASE)
COUNTRY_PREFIX = re.compile(r"^\s*([A-Z][A-Z .'\-]{2,40}|[A-Z][a-z]+(?: [A-Z][a-z]+){0,3}|[一-鿿]{2,6})\s*[:：,]\s*")


def _num(text: str | None) -> float:
    return float(str(text).replace(",", ".")) if text else 0.0


def parse_coordinates(text: str) -> dict[str, Any] | None:
    for index, pattern in enumerate(COORD_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        g = match.groups()
        if index == 0:
            lat = int(g[0]) + _num(g[1]) / 60 + _num(g[2]) / 3600
            lon = int(g[4]) + _num(g[5]) / 60 + _num(g[6]) / 3600
            lat *= -1 if g[3] == "S" else 1
            lon *= -1 if g[7] == "W" else 1
            precision = "seconds" if g[2] else "minutes" if g[1] else "degrees"
        elif index == 1:
            lat = _num(g[0]) * (-1 if g[1] == "S" else 1)
            lon = _num(g[2]) * (-1 if g[3] == "W" else 1)
            precision = "decimal"
        else:
            lat = _num(g[1]) * (-1 if g[0] == "S" else 1)
            lon = _num(g[3]) * (-1 if g[2] == "W" else 1)
            precision = "decimal"
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        return {"latitude": round(lat, 6), "longitude": round(lon, 6), "raw": match.group(0), "precision": precision, "span": match.span()}
    return None


def _elevation(text: str) -> str | None:
    for match in ELEVATION.finditer(text):
        low = int(re.sub(r"[ ,]", "", match.group(1)))
        if 0 < low <= 6500:
            high = match.group(2)
            return f"{low}-{int(re.sub(r'[ ,]', '', high))}" if high else str(low)
    return None


def _date(text: str) -> str | None:
    for pattern in (ROMAN_DATE, ROMAN_DATE_REV, ISO_DATE, TEXT_DATE):
        match = pattern.search(text)
        if match:
            return re.sub(r"\s+", "", match.group(1)) if pattern in (ROMAN_DATE, ROMAN_DATE_REV) else match.group(1)
    return None


STRIP = " .;:,：；，。、"


def split_records(section: str) -> list[str]:
    parts = re.split(r";\s+|；|。|\.\s+(?=[A-Z]{3,}[A-Z .'\-]*\s*[:,])|\s+[—–]\s+(?=[A-Z])", section)
    return [p.strip(STRIP) for p in parts if len(p.strip(STRIP)) >= 6]


def _country_in(record: str) -> tuple[str | None, int]:
    """First recognised country label ('CHINA:', 'Spain,', '中国：') and the index where the locality starts."""
    prefix = COUNTRY_PREFIX.match(record)
    if prefix and geo.normalize_country(prefix.group(1)):
        return geo.normalize_country(prefix.group(1)), prefix.end()
    for match in COUNTRY_ANY.finditer(record):
        code = geo.normalize_country(match.group(1))
        if code:
            return code, match.end()
    return None, 0


def _has_locality_signal(record: str) -> bool:
    return bool(
        parse_coordinates(record)
        or COUNTRY_PREFIX.match(record)
        or _elevation(record)
        or _date(record)
        or COLLECTOR.search(record)
        or COLLECTOR_ZH.search(record)
        or _country_in(record)[0]
        or re.search(r"[一-鿿]{2,}(省|县|市|山|镇|乡|村)", record)
    )


def sections(page: str) -> list[tuple[int, int, str]]:
    out = []
    heads = list(HEADING.finditer(page))
    for number, head in enumerate(heads):
        start = head.start()
        limit = heads[number + 1].start() if number + 1 < len(heads) else len(page)
        stop = STOP.search(page, head.end(), limit)
        end = min(stop.start() if stop else limit, start + 4000)
        if end - head.end() >= 8:
            out.append((start, end, head.group(0)))
    return out


def _name_positions(page: str, names: list[str]) -> list[tuple[int, str]]:
    folded = geo.fold(page)
    hits = []
    for name in names:
        key = geo.fold(name)
        if len(key) < 4:
            continue
        for match in re.finditer(re.escape(key), folded):
            hits.append((match.start(), name))
    return sorted(hits)


def name_variants(accepted: str, others: list[str] | None = None) -> list[str]:
    variants = [accepted, *(others or [])]
    out: list[str] = []
    for name in variants:
        name = re.sub(r"\s+", " ", (name or "").strip())
        if not name or name == "(not available)":
            continue
        parts = name.split()
        out.append(name)
        if len(parts) >= 2:
            out.append(f"{parts[0][0]}. {' '.join(parts[1:])}")
            out.append(f"{parts[0][0]}.{' '.join(parts[1:])}")
    return list(dict.fromkeys(out))


def extract_page(
    page: str,
    target_names: list[str],
    other_names: list[str] | None = None,
    cited_page: bool = False,
    default_country: str | None = None,
) -> list[dict[str, Any]]:
    """Candidate records on one page attributed to the target taxon."""
    mentions = _name_positions(page, target_names + list(other_names or []))
    targets = set(target_names)
    out: list[dict[str, Any]] = []
    for start, end, heading in sections(page):
        before = [name for pos, name in mentions if pos <= start]
        if before:
            if before[-1] not in targets:
                continue
            attribution = "name_before_section"
        elif cited_page:
            attribution = "ldl_cited_page"
        else:
            continue
        section = page[start:end]
        heading_type = TYPE_STATUS.match(heading.strip())
        is_type_section = bool(heading_type or re.match(r"type", heading, re.IGNORECASE))
        country = default_country
        body = section[len(heading):] if len(section) > len(heading) else section
        for record in split_records(body):
            code, loc_start = _country_in(record)
            if code:
                country = code
            if not _has_locality_signal(record):
                continue
            coords = parse_coordinates(record)
            type_match = TYPE_STATUS.search(record)
            if type_match:
                type_status = type_match.group(0).lower()
            elif heading_type:
                type_status = heading_type.group(0).lower()
            else:
                type_status = "type" if is_type_section else None
            locality = record[loc_start:]
            cut = len(locality)
            for pattern in (*COORD_PATTERNS, ELEVATION, ROMAN_DATE, ROMAN_DATE_REV, ISO_DATE, TEXT_DATE, COLLECTOR, COLLECTOR_ZH, YEAR_ONLY):
                match = pattern.search(locality)
                if match:
                    cut = min(cut, match.start())
            locality_text = locality[:cut] or locality[:120]
            locality_text = TYPE_STATUS.sub("", SEX.sub("", locality_text)).strip(STRIP + " ")
            collector = COLLECTOR.search(record) or COLLECTOR_ZH.search(record)
            date = _date(record)
            out.append(
                {
                    "country": country,
                    "locality_original": locality_text[:300],
                    "latitude": coords["latitude"] if coords else None,
                    "longitude": coords["longitude"] if coords else None,
                    "coordinate_basis": "literature_locality" if coords else "none",
                    "coordinate_precision": coords["precision"] if coords else None,
                    "elevation_m": _elevation(record),
                    "collection_date": date or (YEAR_ONLY.search(record).group(1) if YEAR_ONLY.search(record) else None),
                    "collector": collector.group(1).strip(" .,") if collector else None,
                    "type_status": type_status,
                    "evidence_quote": record[:400],
                    "evidence_level": "primary_type_label" if type_status else "primary_material",
                    "section": heading.strip(),
                    "attribution": attribution,
                    "extraction_method": "rules",
                }
            )
    return out


def printed_page_offset(pages: list[str]) -> int | None:
    """Estimate printed page number minus PDF page index from running headers/footers."""
    from collections import Counter

    votes: Counter[int] = Counter()
    for index, text in enumerate(pages, start=1):
        ends = (text[:60], text[-60:])
        for chunk in ends:
            for match in re.finditer(r"(?<![\d.])(\d{1,4})(?![\d.])", chunk):
                number = int(match.group(1))
                if 1 <= number <= 5000 and not 1700 <= number <= 2100:
                    votes[number - index] += 1
    if not votes:
        return None
    offset, support = votes.most_common(1)[0]
    return offset if support >= max(2, len(pages) // 4) else None


def cited_pdf_pages(page_cite: str | None, pages: list[str]) -> list[int]:
    """'p. 166' / 'pp. 166-168' -> 1-based PDF page numbers using the detected offset."""
    if not page_cite:
        return []
    numbers = [int(n) for n in re.findall(r"\d{1,4}", page_cite)]
    if not numbers:
        return []
    first, last = numbers[0], numbers[1] if len(numbers) > 1 and numbers[1] >= numbers[0] else numbers[0]
    offset = printed_page_offset(pages)
    if offset is None:
        return []
    return [p for p in range(first - offset, min(last, first + 5) - offset + 1) if 1 <= p <= len(pages)]
