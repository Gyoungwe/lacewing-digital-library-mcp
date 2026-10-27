"""Offline tests: rule extraction, country checks, gazetteer, literature→distribution, GBIF layer, scope metadata/download."""

from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ldl import FakeLDL, StubClient, write_gazetteer  # noqa: E402

from ldl_mcp import extract, geo  # noqa: E402


class ExtractTests(unittest.TestCase):
    def rows(self, text: str, name: str = "Myrmeleon bore", others: list[str] | None = None):
        out = extract.extract_page(text, extract.name_variants(name), others or [])
        for row in out:
            self.assertIn(row["evidence_quote"], text, "quote must be an exact substring of the page")
        return out

    def test_material_examined_dms_decimal_and_carry_over_country(self) -> None:
        text = (
            "Myrmeleon bore (Tjeder, 1941). Material examined. CHINA: Yunnan, Kunming, Xishan, 24°57'N 102°38'E, 1900 m, "
            "12.vii.2001, leg. Wang & Li, 2♂ 1♀; Hebei, Changli, 39.71°N 119.16°E, 20 m, 3.VIII.1995, 1♂. "
            "JAPAN: Honshu, Tokyo, 8 Aug 1987, leg. Sato, 1♀. Diagnosis. Forewing spotted."
        )
        rows = self.rows(text)
        self.assertEqual([r["country"] for r in rows], ["CHN", "CHN", "JPN"])
        self.assertAlmostEqual(rows[0]["latitude"], 24.95, places=4)
        self.assertAlmostEqual(rows[0]["longitude"], 102.633333, places=4)
        self.assertEqual(rows[0]["locality_original"], "Yunnan, Kunming, Xishan")
        self.assertEqual((rows[0]["elevation_m"], rows[0]["collection_date"], rows[0]["collector"]), ("1900", "12.vii.2001", "Wang & Li"))
        self.assertIsNone(rows[2]["latitude"])
        self.assertNotIn("Forewing", " ".join(r["evidence_quote"] for r in rows), "Diagnosis must end the section")

    def test_type_heading_west_south_and_chinese(self) -> None:
        holotype = self.rows("Myrmeleon bore Tjeder. Holotype ♂, SWEDEN: Uppland, Uppsala, 1935, leg. Tjeder. Description. Black.")
        self.assertEqual((holotype[0]["type_status"], holotype[0]["country"], holotype[0]["evidence_level"]), ("holotype", "SWE", "primary_type_label"))
        west = self.rows("Myrmeleon bore. Specimens examined: Spain, Granada, N37.0931 W3.3956, 1450–1600 m, 2010-07-14. Remarks: x.")
        self.assertEqual((west[0]["country"], west[0]["longitude"], west[0]["elevation_m"]), ("ESP", -3.3956, "1450-1600"))
        south = self.rows("Myrmeleon bore. Material examined. SOUTH AFRICA: Limpopo, 23°53'S 29°27'E, 1♂.")
        self.assertLess(south[0]["latitude"], 0)
        zh = self.rows("Myrmeleon bore 检视标本：中国：云南，昆明，西山，1900 m，2001.07.12，王某采；1♀，河北，昌黎，1995.VIII.3。鉴别特征：斑。")
        self.assertEqual([(r["country"], r["locality_original"]) for r in zh], [("CHN", "云南，昆明，西山"), ("CHN", "河北，昌黎")])
        self.assertEqual(zh[0]["collector"], "王某")

    def test_section_of_another_species_is_ignored(self) -> None:
        text = "Myrmeleon immaculatus. Material examined. USA: Ohio, Columbus, 1 ex. Myrmeleon bore. Diagnosis. none."
        self.assertEqual(self.rows(text, others=extract.name_variants("Myrmeleon immaculatus")), [])

    def test_printed_page_offset(self) -> None:
        pages = [f"{150 + i} Revision of Testus running text " + "x " * 30 + f"Journal 12 ({150 + i})" for i in range(1, 9)]
        self.assertEqual(extract.printed_page_offset(pages), 150)
        self.assertEqual(extract.cited_pdf_pages("pp. 152-154", pages), [2, 3, 4])
        self.assertEqual(extract.cited_pdf_pages("p. 999", pages), [])


class GeoTests(unittest.TestCase):
    def test_points_and_names(self) -> None:
        self.assertEqual(geo.country_at(25.04, 102.71)["code"], "CHN")
        self.assertEqual(geo.country_at(48.85, 2.35)["code"], "FRA")
        self.assertEqual(geo.country_at(-29.3, 27.5)["code"], "LSO")  # hole inside South Africa
        self.assertIsNone(geo.country_at(0, 0)["code"])
        self.assertEqual(geo.normalize_country("China : Fujian (province)"), "CHN")
        self.assertEqual(geo.normalize_country("United States"), "USA")
        self.assertEqual(geo.normalize_country("中国"), "CHN")
        self.assertTrue(geo.same_country("TWN", "CHN"))
        self.assertEqual(geo.admin1_hint("China : Taiwan (province)"), "Taiwan")


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ldl_pipe_")
        self.home = Path(self.tmp.name) / "home"
        os.environ["LDL_MCP_HOME"] = str(self.home)
        from ldl_mcp import auth

        auth.save_cookies([{"name": "lacewing_session", "value": "abc", "domain": "lacewing.tamu.edu", "path": "/"}], source="test")
        write_gazetteer(self.home)
        self.client = StubClient()

    def tearDown(self) -> None:
        os.environ.pop("LDL_MCP_HOME", None)
        self.tmp.cleanup()

    def download(self, **kwargs):
        from ldl_mcp import auth, downloader
        from ldl_mcp.ldl_http import RequestsAPIContext

        session = auth.build_session(require_cookies=True)
        session.mount("https://", FakeLDL())
        with redirect_stdout(io.StringIO()):
            return downloader.run_genus_job(RequestsAPIContext(session, min_interval=0), downloader.monograph_url_for(1), Path(self.tmp.name) / "libs", **kwargs)


class GazetteerTests(PipelineTestCase):
    def test_resolution_rules(self) -> None:
        from ldl_mcp import gazetteer

        r = gazetteer.georeference("Yunnan, Kunming, 1900 m", "China")
        self.assertEqual(r["status"], "resolved")
        self.assertIn("geonameid 1804651", r["georeference_source"])
        self.assertEqual(r["coordinate_uncertainty_m"], 20000)
        self.assertEqual(gazetteer.georeference("Hokkaido", "Japan")["status"], "coarse")
        self.assertEqual(gazetteer.georeference("Atlantis", "Japan")["status"], "not_found")
        self.assertEqual(gazetteer.georeference("Tokyo", "Narnia")["status"], "no_country")


class LiteratureDistributionTests(PipelineTestCase):
    def test_end_to_end_with_coverage_and_gbif(self) -> None:
        from ldl_mcp import distribution, evidence, figures, lit_distribution

        root = self.download()["root"]
        result = lit_distribution.run(self.client, root, "testus", names=["Testus alpha", "Testus beta"])
        per = {r["taxon"]: r for r in result["per_taxon"]}
        self.assertIn("Oldgenus beta", per["Testus beta"]["name_variants"])
        self.assertEqual(per["Testus beta"]["ldl_countries"], ["JPN"])
        self.assertEqual(per["Testus beta"]["ldl_doubtful"], ["RUS"])
        rows = evidence.load("testus")
        alpha = [r for r in rows if r["species"] == "Testus alpha"]
        beta = [r for r in rows if r["species"] == "Testus beta"]
        self.assertEqual(len(alpha), 1)
        self.assertEqual((alpha[0]["coordinate_basis"], alpha[0]["latitude"], alpha[0]["type_status"]), ("literature_locality", 25.04, "holotype"))
        self.assertEqual(alpha[0]["review_status"], "quote_verified")
        by_locality = {r["locality_original"]: r for r in beta}
        tokyo = by_locality["Honshu, Tokyo"]
        self.assertEqual((tokyo["coordinate_basis"], tokyo["pdf_page"]), ("georeferenced", 2))
        self.assertIn("GeoNames", tokyo["georeference_source"])
        self.assertEqual(tokyo["review_status"], "quote_verified")
        sapporo = by_locality["Hokkaido, Sapporo"]
        self.assertEqual(round(sapporo["latitude"], 2), 43.06)
        self.assertTrue(all(r["quote_check"] in {"exact", "normalized"} for r in rows))
        again = lit_distribution.run(self.client, root, "testus", names=["Testus alpha", "Testus beta"])
        self.assertEqual(again["stored"]["stored"], 0, "re-running must not duplicate evidence")

        fake_gbif = {
            "match": {"name": "Testus beta", "ok": True, "usageKey": 7, "note": ""},
            "total": 2,
            "rows": [
                {"gbif_key": 1, "latitude": 34.69, "longitude": 135.5, "coordinate_uncertainty_m": 100, "basis_of_record": "PRESERVED_SPECIMEN", "dataset_key": "d"},
                {"gbif_key": 2, "latitude": 55.75, "longitude": 37.62, "coordinate_uncertainty_m": None, "basis_of_record": "HUMAN_OBSERVATION", "dataset_key": "d"},
            ],
        }
        with patch("ldl_mcp.gbif.occurrences", side_effect=lambda name, **kw: fake_gbif if name == "Testus beta" else {"match": {"ok": False}, "total": 0, "rows": []}):
            ds = distribution.build(self.client, "testus", names=["Testus alpha", "Testus beta"], evidence_dataset="testus", include_gbif=True)
        cov = {r["species"]: r for r in distribution.read_coverage(ds["dataset_id"])}
        self.assertEqual(cov["Testus beta"]["ldl_countries"], ["JPN"])
        self.assertEqual(cov["Testus beta"]["ldl_doubtful_countries"], ["RUS"])
        self.assertEqual(cov["Testus beta"]["countries_with_literature_points"], ["JPN"])
        self.assertEqual(cov["Testus beta"]["countries_with_gbif_points"], ["JPN", "RUS"])
        self.assertEqual(cov["Testus beta"]["point_countries_not_in_ldl"], ["RUS"])
        self.assertEqual(cov["Testus beta"]["ldl_countries_without_points"], [])
        moscow = next(r for r in ds_rows(ds) if r.get("source", "").startswith("GBIF occurrence 2"))
        self.assertIn("outside_ldl_countries", moscow["qc_flags"])
        figure = figures.render_map(ds["dataset_id"], dpi=300, include_flagged=False)
        self.assertEqual(figure["counts"]["by_coordinate_basis"].get("occurrence"), 1, "GBIF point outside LDL countries is held back")
        self.assertIn("GBIF", figure["caption"])


def ds_rows(ds):
    from ldl_mcp import distribution

    return distribution.load(ds["dataset_id"])["rows"]


class ScopeMetadataTests(PipelineTestCase):
    def test_order_scope_metadata_and_shared_library(self) -> None:
        from ldl_mcp import downloader, metadata, research_scope

        scope = research_scope.freeze_scope(self.client, order="Neuroptera", label="testorder")
        self.assertEqual(scope["taxon_count"], 2)
        self.assertEqual(scope["taxa"][0]["extant_extinct"], "extant")
        result = metadata.harvest(self.client, scope["scope_id"], log=lambda m: None)
        self.assertTrue(result["complete"])
        self.assertEqual(result["documents_with_file"], 2)
        with open(result["outputs"]["citations.csv"], encoding="utf-8-sig") as handle:
            cites = list(csv.DictReader(handle))
        self.assertTrue(any(c["record_status"] == "unaccepted (doubtful)" for c in cites))
        plan = research_scope.literature_plan(self.client, scope["scope_id"])
        self.assertEqual(plan["source"], "metadata job cache")
        again = metadata.harvest(self.client, scope["scope_id"], log=lambda m: None)
        self.assertEqual(again["species_cached"], 2)

        targets = [downloader.SpeciesTarget(t["accepted_name"], t["accepted_name"].split()[1], downloader.monograph_url_for(t["CombObjID"])) for t in scope["taxa"]]
        summary = self.download(targets=targets, label="testorder")
        self.assertTrue(summary["root"].endswith("LDL_testorder"))
        self.assertEqual((summary["species_total"], summary["unique_documents"]), (2, 2))


class ServerToolTests(unittest.TestCase):
    def test_new_tools_registered(self) -> None:
        import asyncio

        from ldl_mcp import server

        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        for name in ("literature_distribution", "georeference_locality", "distribution_coverage"):
            self.assertIn(name, names)
        self.assertLessEqual(len(names), 50)


if __name__ == "__main__":
    unittest.main()
