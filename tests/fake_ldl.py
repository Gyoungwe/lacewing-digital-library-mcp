"""Offline LDL stand-in: a requests transport adapter plus a tiny text PDF builder."""

from __future__ import annotations

import json
import re
from typing import Any

import requests
from requests.adapters import BaseAdapter


def make_pdf(pages: list[str]) -> bytes:
    """Build a minimal valid PDF whose pages carry extractable Helvetica text."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        lines = [text[j : j + 80] for j in range(0, len(text), 80)] or [""]
        ops = ["BT", "/F1 10 Tf", "14 TL", "50 780 Td"]
        for line in lines:
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({safe}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", "replace")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {4 + 2 * i} 0 R >>".encode()
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


PDF_100 = make_pdf(
    [
        "Revision of Testus. Introduction page.",
        "Testus alpha sp. n. Material examined. Holotype male, CHINA: Yunnan, Kunming, 25.04N 102.71E, 1900 m, 12.vii.2001, leg. Wang.",
    ]
)
PDF_101 = make_pdf(["Testus beta. Distribution: Japan (Honshu)."])


def _citation(bib: int, edoc: int, cite: str) -> dict[str, Any]:
    return {
        "Source_BibObjID": bib,
        "Source_EDocID": edoc,
        "Source_EDocBase": {"EDocID": edoc} if edoc else None,
        "SourceAuthYearCite": cite,
        "SourcePageCite": "p. 2",
        "RecordFullGeogCite": "China: Yunnan",
        "RecordCountryCite": "China",
    }


class FakeLDL(BaseAdapter):
    """Routes https://lacewing.tamu.edu/... to canned responses and records what it saw."""

    def __init__(self, expect_cookie: str = "lacewing_session=abc") -> None:
        super().__init__()
        self.expect_cookie = expect_cookie
        self.calls: list[tuple[str, str]] = []
        self.cookie_headers: list[str] = []
        self.login_expired = False
        self.edocs = {100: PDF_100, 101: PDF_101}

    def _json(self, request: requests.PreparedRequest, value: Any, status: int = 200) -> requests.Response:
        return self._response(request, json.dumps(value).encode(), status, {"Content-Type": "application/json"})

    @staticmethod
    def _response(request: requests.PreparedRequest, body: bytes, status: int, headers: dict[str, str]) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = body
        response.headers.update(headers)
        response.url = request.url
        response.request = request
        response.reason = {200: "OK", 206: "Partial Content", 404: "Not Found"}.get(status, "")
        return response

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        path = re.sub(r"^https://lacewing\.tamu\.edu", "", request.url or "")
        self.calls.append((request.method or "", path))
        self.cookie_headers.append(request.headers.get("Cookie", ""))
        payload = json.loads(request.body) if request.body else {}
        comb = payload.get("CombObjID")
        if path == "/api/species/getSpecCombPk":
            return self._json(
                request,
                {
                    "PresCombGenu": "Testus",
                    "Genu_SpCatCongenList": [
                        {"CongenPres_CombObjID": 1, "PresCombSpEpithet": "alpha"},
                        {"CongenPres_CombObjID": 2, "PresCombSpEpithet": "beta"},
                    ],
                },
            )
        if path == "/api/species/getSpecTaxCitByPk":
            rows = {
                1: [_citation(10, 100, "Smith 2001"), _citation(11, 0, "Old note 1850")],
                2: [_citation(10, 100, "Smith 2001"), _citation(12, 101, "Sato 1999")],
            }[comb]
            return self._json(request, {"Syn_BibObjAnnotList": rows})
        if path == "/api/species/getSpecDistCitByPk":
            rows = {1: [_citation(10, 100, "Smith 2001")], 2: []}[comb]
            return self._json(request, {"Syn_DistGeogBibObjList": rows})
        if path == "/api/species/getSpecFigCitByPk":
            return self._json(request, {"Syn_BibObjPartAnnotList": []})
        match = re.fullmatch(r"/edoc/(\d+)", path)
        if match:
            if self.login_expired:
                return self._response(request, b"<html><title>LDL Authenticate - Log In</title></html>", 200, {"Content-Type": "text/html"})
            body = self.edocs[int(match.group(1))]
            rng = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("Range", ""))
            if rng:
                start, end = int(rng.group(1)), min(int(rng.group(2)), len(body) - 1)
                return self._response(
                    request,
                    body[start : end + 1],
                    206,
                    {"Content-Type": "application/pdf", "Content-Range": f"bytes {start}-{end}/{len(body)}"},
                )
            return self._response(request, body, 200, {"Content-Type": "application/pdf"})
        return self._json(request, {"error": "not found"}, 404)

    def close(self) -> None:
        pass


class StubClient:
    """Minimal LDLClient stand-in for scope/distribution/export tests."""

    def __init__(self) -> None:
        self.rows = {
            "Testus alpha": {"CombObjID": 1, "CombSpec_TaxObjID": 501, "CombGenuToSubs": "Testus alpha", "CombTaxStatus": "valid", "PresCombGenu": "Testus"},
            "Testus beta": {"CombObjID": 2, "CombSpec_TaxObjID": 502, "CombGenuToSubs": "Testus beta", "CombTaxStatus": "valid", "PresCombGenu": "Testus"},
        }

    def resolve_current(self, name: str) -> dict[str, Any] | None:
        return self.rows.get(" ".join(name.split()))

    def search_taxa(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.rows.values())

    def epithet(self, tax_id: int) -> dict[str, Any]:
        return {"ExtantExtinct": "extant", "OrigCombSciName": "Testus " + ("alpha" if tax_id == 501 else "beta")}

    def species_versions(self) -> Any:
        return [{"version": "test"}]

    def type_coordinates(self, tax_obj_id: int | None = None) -> list[dict[str, Any]]:
        rows = [
            {"Type_TaxObjID": 501, "TypeSpmnID": 9, "TypeLocalityInterpreted": "China: Yunnan: Kunming [25.04°N 102.71°E]", "LatitudeDecDeg": 25.04, "LongitudeDecDeg": 102.71},
            {"Type_TaxObjID": 502, "TypeSpmnID": 10, "TypeLocalityInterpreted": "Japan: Honshu: Tokyo", "LatitudeDecDeg": 35.68, "LongitudeDecDeg": 139.69},
            {"Type_TaxObjID": 502, "TypeSpmnID": 11, "TypeLocalityInterpreted": "bad row", "LatitudeDecDeg": 0.0, "LongitudeDecDeg": 0.0},
        ]
        return [r for r in rows if tax_obj_id is None or r["Type_TaxObjID"] == tax_obj_id]

    def distribution_source_records(self, comb_obj_id: int, include_bibliography: bool = True) -> list[dict[str, Any]]:
        return [{"RecordFullGeogCite": "China", "RecordCountryCite": "China", "Source_BibObjID": 10, "Source_EDocID": 100, "SourceCiteShort": "Smith 2001"}]

    def raw_public(self, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        adapter = FakeLDL()
        session = requests.Session()
        session.mount("https://", adapter)
        return session.post(f"https://lacewing.tamu.edu/api/{endpoint}", json=payload).json()

    def bibliography(self, bib_id: int) -> dict[str, Any]:
        return {"Auth": "Smith, J.", "Title": "Revision of Testus", "NomYear": "2001", "Journal": "Zootaxa"}
