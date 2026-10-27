"""Offline tests for the MCP download + research workflow (no network, no browser)."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests

from fake_ldl import FakeLDL, StubClient


class WorkflowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ldl_mcp_test_")
        self.home = Path(self.tmp.name) / "home"
        os.environ["LDL_MCP_HOME"] = str(self.home)
        from ldl_mcp import auth

        self.auth = auth
        auth.save_cookies(
            [
                {"name": "lacewing_session", "value": "abc", "domain": "lacewing.tamu.edu", "path": "/", "secure": True, "expires": -1},
                {"name": "tracker", "value": "zzz", "domain": ".example.com", "path": "/"},
            ],
            source="test",
        )

    def tearDown(self) -> None:
        os.environ.pop("LDL_MCP_HOME", None)
        self.tmp.cleanup()

    def request_context(self, fake: FakeLDL, control=None):
        from ldl_mcp.ldl_http import RequestsAPIContext

        session = self.auth.build_session(require_cookies=True)
        session.mount("https://", fake)
        return RequestsAPIContext(session, min_interval=0, control=control)

    def run_job(self, fake: FakeLDL, **kwargs):
        from ldl_mcp import downloader

        out = io.StringIO()
        with redirect_stdout(out):
            return downloader.run_genus_job(
                self.request_context(fake, kwargs.pop("control", None)),
                downloader.monograph_url_for(1),
                Path(self.tmp.name) / "libs",
                **kwargs,
            )


class AuthTests(WorkflowTestCase):
    def test_cookie_store_is_private_and_filtered(self) -> None:
        path = self.auth.cookie_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        cookies = self.auth.load_cookies()
        self.assertEqual([c["name"] for c in cookies], ["lacewing_session"])
        summary = self.auth.cookie_summary()
        self.assertNotIn("abc", json.dumps(summary))
        self.assertEqual(summary["cookie_count"], 1)

    def test_no_session_error(self) -> None:
        self.auth.clear_cookies()
        with self.assertRaises(self.auth.NoSessionError):
            self.auth.build_session(require_cookies=True)


class DownloaderTests(WorkflowTestCase):
    def test_original_self_test_still_passes(self) -> None:
        from ldl_mcp import downloader

        with redirect_stdout(io.StringIO()):
            self.assertEqual(downloader.self_test(), 0)

    def test_full_genus_job_dedups_and_sends_cookies(self) -> None:
        fake = FakeLDL()
        summary = self.run_job(fake)
        self.assertEqual(summary["genus"], "Testus")
        self.assertEqual(summary["species_total"], 2)
        self.assertEqual(summary["unique_documents"], 2)  # edoc 100 shared by both species and two categories
        self.assertEqual(summary["failed_downloads"], 0)
        self.assertEqual(summary["unavailable_citations"], 1)
        edoc_calls = [p for m, p in fake.calls if p.startswith("/edoc/")]
        self.assertEqual(sorted(set(edoc_calls)), ["/edoc/100", "/edoc/101"])
        self.assertEqual(edoc_calls.count("/edoc/100"), 1, "shared EDoc must be downloaded once")
        self.assertTrue(all("lacewing_session=abc" in h for h in fake.cookie_headers))
        self.assertTrue(all("tracker" not in h for h in fake.cookie_headers))
        root = Path(summary["root"])
        for name in ("library_index.json", "progress.json", "物种列表快照.json", "文献关联总表.csv", "未提供文件的引用.csv"):
            self.assertTrue((root / name).exists(), name)

        again = self.run_job(fake)
        self.assertEqual(again["new_documents"], 0)

        verify = self.run_job(fake, verify_only=True)
        self.assertTrue(verify["passed"])

    def test_pause_then_resume(self) -> None:
        from ldl_mcp.ldl_http import JobPaused

        fake = FakeLDL()
        state = {"n": 0}

        def control():
            state["n"] += 1
            return "pause" if state["n"] == 6 else None

        with self.assertRaises(JobPaused):
            self.run_job(fake, control=control)
        summary = self.run_job(fake)
        self.assertEqual(summary["unique_documents"], 2)
        self.assertEqual(summary["failed_downloads"], 0)

    def test_login_expiry_raises_and_keeps_progress(self) -> None:
        from ldl_mcp.downloader import LoginExpiredError

        fake = FakeLDL()
        fake.login_expired = True
        with self.assertRaises(LoginExpiredError):
            self.run_job(fake)
        root = Path(self.tmp.name) / "libs" / "LDL_Testus"
        self.assertTrue((root / "progress.json").exists())
        fake.login_expired = False
        summary = self.run_job(fake)
        self.assertEqual(summary["unique_documents"], 2)

    def test_retry_failed_mode(self) -> None:
        fake = FakeLDL()
        fake.edocs[101] = b""  # empty body -> chunk failure recorded, job continues
        first = self.run_job(fake)
        self.assertEqual(first["failed_downloads"], 1)
        from fake_ldl import PDF_101

        fake.edocs[101] = PDF_101
        retried = self.run_job(fake, download_mode="retry-failed")
        self.assertEqual((retried["retried_ok"], retried["failed_downloads"]), (1, 0))


class JobStateTests(WorkflowTestCase):
    def test_dead_worker_is_reported_interrupted_and_control_file(self) -> None:
        from ldl_mcp import jobs
        from ldl_mcp.paths import jobs_dir, write_json_atomic

        job_dir = jobs_dir() / "download-test"
        job_dir.mkdir(parents=True)
        write_json_atomic(job_dir / "job.json", {"job_id": "download-test", "kind": "download", "status": "running", "pid": 999999, "params": {}})
        self.assertEqual(jobs.load("download-test")["status"], "interrupted")
        # A live pid that is not this job's worker (pid reuse) is also reported interrupted.
        write_json_atomic(job_dir / "job.json", {"job_id": "download-test", "kind": "download", "status": "running", "pid": os.getpid(), "params": {}})
        self.assertEqual(jobs.load("download-test")["status"], "interrupted")
        self.assertIsNone(jobs.request_stop("download-test", "pause")["requested"])
        with patch.object(jobs, "_is_worker", return_value=True):
            self.assertEqual(jobs.load("download-test")["status"], "running")
            jobs.request_stop("download-test", "pause")
        self.assertEqual(jobs.control_action("download-test"), "pause")
        with self.assertRaises(ValueError):
            jobs.job_dir("../etc")


class ResearchPipelineTests(WorkflowTestCase):
    def test_library_search_evidence_distribution_map_export(self) -> None:
        from ldl_mcp import distribution, evidence, export, figures, library, research_scope

        summary = self.run_job(FakeLDL())
        root = summary["root"]

        status = library.status(root)
        self.assertEqual(status["documents_present"], 2)
        self.assertTrue(library.verify(root, full_hash=True)["ok"])

        hits = library.search(root, ["material examined", "Kunming"], match="all")
        self.assertEqual(len(hits["hits"]), 1)
        hit = hits["hits"][0]
        self.assertEqual(hit["pdf_page"], 2)
        self.assertIn("10", hit["BibObjIDs"])
        self.assertIn("100", hit["EDocIDs"])

        page = library.page_text(root, hit["sha256"][:12], 2)
        self.assertIn("Holotype", page["text"])

        stored = evidence.record(
            "testus",
            [
                {
                    "species": "Testus alpha", "accepted_name": "Testus alpha",
                    "locality_original": "CHINA: Yunnan, Kunming", "country": "China",
                    "latitude": 25.04, "longitude": 102.71, "coordinate_basis": "literature_locality",
                    "type_status": "holotype", "sha256": hit["sha256"], "pdf_page": 2,
                    "evidence_quote": "Holotype male, CHINA: Yunnan, Kunming, 25.04N 102.71E",
                    "evidence_level": "primary_type_label", "review_status": "accepted",
                },
                {
                    "species": "Testus alpha", "accepted_name": "Testus alpha",
                    "locality_original": "Invented place", "latitude": 30.0, "longitude": 100.0,
                    "sha256": hit["sha256"], "pdf_page": 2, "evidence_quote": "this sentence is not in the paper",
                },
            ],
            [root],
        )
        self.assertEqual(stored["quote_verified"], 1)
        self.assertEqual(stored["unverified"], 1)
        self.assertTrue(any("ignored" in "; ".join(p["issues"]) for p in stored["problems"]))
        again = evidence.record("testus", [stored["rows"][0]], [root])
        self.assertEqual(again["stored"], 0)

        client = StubClient()
        scope = research_scope.freeze_scope(client, names=["Testus alpha", "Testus beta", "Nonexistus nobody"])
        self.assertEqual(scope["taxon_count"], 2)
        self.assertEqual(scope["unresolved_count"], 1)
        same = research_scope.freeze_scope(client, names=["Testus alpha", "Testus beta", "Nonexistus nobody"])
        self.assertEqual(same["scope_id"], scope["scope_id"])

        plan = research_scope.literature_plan(client, scope["scope_id"])
        self.assertEqual(plan["documents_with_file"], 2)
        shared = next(d for d in plan["documents"] if d["edoc_url"].endswith("/100"))
        self.assertEqual(shared["species_count"], 2)

        ds = distribution.build(client, "testus", scope_id=scope["scope_id"], evidence_dataset="testus", extent=[90, 150, 10, 50])
        bases = ds["coordinate_basis_counts"]
        self.assertEqual(bases["type_specimen"], 3)
        self.assertEqual(bases["country_summary"], 3)  # alpha: China; beta: Japan + Russia (doubtful)
        self.assertEqual(ds["qc"]["status"], "needs_attention")  # the 0/0 type row
        self.assertIn("zero_zero", ds["qc"]["flag_counts"])

        draft = figures.render_map(ds["dataset_id"], basemap="none", dpi=300)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["counts"]["by_coordinate_basis"].get("type_specimen"), 2)
        self.assertEqual(draft["counts"]["by_coordinate_basis"].get("literature_locality"), 1, "unverified evidence is excluded")
        svg = Path(draft["outputs"]["svg"]).read_text(encoding="utf-8")
        self.assertIn("<text", svg)
        with open(Path(draft["folder"]) / "map_data.csv", encoding="utf-8-sig") as handle:
            plotted = list(csv.DictReader(handle))
        self.assertFalse(any(r["coordinate_basis"] == "country_summary" for r in plotted))
        with self.assertRaises(ValueError):
            figures.render_map(ds["dataset_id"], coordinate_bases=["country_summary"], basemap="none")

        land = Path(self.tmp.name) / "land.geojson"
        land.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[95, 15], [145, 15], [145, 45], [95, 45], [95, 15]]]}}]}))
        faceted = figures.render_map(ds["dataset_id"], basemap=str(land), facet=True, width="double", theme_name="plain_report", dpi=300)
        self.assertEqual(faceted["basemap"]["polygons"], 1)
        self.assertEqual(faceted["status"], "draft")  # QC still has the 0/0 row
        check = figures.validate(faceted["figure_id"])
        names = {c["check"]: c["ok"] for c in check["checks"]}
        self.assertTrue(names["svg text is editable (not outlined)"])
        self.assertTrue(names["png dpi"])
        self.assertTrue(names["basemap present"])
        self.assertFalse(names["dataset QC passed"])

        clean = distribution.build(client, "testus-alpha", names=["Testus alpha"], evidence_dataset="testus")
        self.assertEqual(clean["qc"]["status"], "pass")
        final = figures.render_map(clean["dataset_id"], basemap=str(land), dpi=300)
        self.assertEqual(final["status"], "final")

        package = export.export_package(client, clean["dataset_id"], figure_ids=[final["figure_id"]])
        with zipfile.ZipFile(package["package"]) as archive:
            names = set(archive.namelist())
            self.assertIn("references.bib", names)
            self.assertIn("evidence.csv", names)
            self.assertIn("run_manifest.json", names)
            self.assertTrue(any(n.endswith("map.svg") for n in names))
            self.assertIn("@article{ldl10", archive.read("references.bib").decode())


class OcrMergeGeoreferenceTests(WorkflowTestCase):
    def _scanned_pdf(self) -> bytes:
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("L", (1700, 500), 255)
        draw = ImageDraw.Draw(image)
        font = None
        for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
            if Path(candidate).exists():
                font = ImageFont.truetype(candidate, 48)
                break
        if font is None:
            self.skipTest("no TrueType font for the OCR fixture")
        draw.text((60, 80), "Material examined. Holotype female,", fill=0, font=font)
        draw.text((60, 180), "JAPAN: Honshu, Tokyo, leg. Sato.", fill=0, font=font)
        out = io.BytesIO()
        image.convert("RGB").save(out, format="PDF", resolution=200)
        return out.getvalue()

    def test_ocr_fills_scanned_pages_and_is_labelled(self) -> None:
        from ldl_mcp import evidence, library

        if not library.ocr_available()["available"]:
            self.skipTest("tesseract/pypdfium2 not installed")
        fake = FakeLDL()
        fake.edocs[101] = self._scanned_pdf()
        root = self.run_job(fake)["root"]
        before = library.search(root, ["Holotype female"])
        self.assertEqual(before["hits"], [])
        self.assertEqual(len(before["documents_without_text_layer"]), 1)
        result = library.ocr_library(root, lang="eng")
        self.assertEqual(result["documents_ocred"], 1)
        self.assertEqual(result["results"][0]["pages_from_ocr"], 1)
        after = library.search(root, ["Holotype female"])
        self.assertEqual(len(after["hits"]), 1)
        self.assertEqual(after["hits"][0]["text_source"], "ocr")
        hit = after["hits"][0]
        stored = evidence.record("ocr", [{
            "species": "Testus beta", "locality_original": "JAPAN: Honshu, Tokyo",
            "sha256": hit["sha256"], "pdf_page": 1, "evidence_quote": "Holotype female",
        }], [root])
        self.assertEqual(stored["rows"][0]["quote_text_source"], "ocr")
        self.assertTrue(any("OCR" in "; ".join(p["issues"]) for p in stored["problems"]))

    def test_merge_is_non_destructive_and_readable(self) -> None:
        from ldl_mcp import downloader, library, merge

        parent = Path(self.tmp.name) / "many"
        for sub in ("a", "b"):
            with redirect_stdout(io.StringIO()):
                downloader.run_genus_job(self.request_context(FakeLDL()), downloader.monograph_url_for(1), parent / sub)
        before = sorted(str(p) for p in parent.rglob("*") if p.is_file())
        plan = merge.merge_libraries(parent)
        self.assertTrue(plan["dry_run"])
        self.assertEqual((plan["unique_documents"], plan["source_files"]), (2, 4))
        self.assertFalse((parent / merge.DESTINATION_NAME).exists())
        done = merge.merge_libraries(parent, dry_run=False, mode="hardlink")
        self.assertEqual(done["merged_documents"], 2)
        for path in before:
            self.assertTrue(Path(path).exists(), "sources must never be deleted")
        unified = library.open_library(parent / merge.DESTINATION_NAME)
        self.assertEqual(unified.layout, "unified")
        self.assertTrue(library.verify(unified.root, full_hash=True)["ok"])
        self.assertEqual(len(library.search(unified.root, ["Kunming"])["hits"]), 1)
        again = merge.merge_libraries(parent, dry_run=False)
        self.assertEqual(again["placed"]["reused"], 2)

    def test_georeferenced_rows_need_provenance(self) -> None:
        from ldl_mcp import evidence

        stored = evidence.record("geo", [
            {"species": "Testus alpha", "locality_original": "Kunming", "latitude": 25.0, "longitude": 102.7, "coordinate_basis": "georeferenced"},
            {"species": "Testus alpha", "locality_original": "Dali", "latitude": 25.6, "longitude": 100.2, "coordinate_basis": "georeferenced",
             "georeference_source": "GeoNames 2026-10 id 1805753", "coordinate_uncertainty_m": 5000},
        ], [])
        issues = {p["row"]: "; ".join(p["issues"]) for p in stored["problems"]}
        self.assertIn("georeference_source", issues[1])
        self.assertIn("coordinate_uncertainty_m", issues[1])
        self.assertNotIn(2, issues)


class ServerTests(unittest.TestCase):
    def test_tool_inventory(self) -> None:
        from ldl_mcp import server

        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        for required in (
            "auth_start", "auth_status", "download_start", "download_status", "download_control", "download_verify",
            "library_status", "library_search", "library_page_text", "freeze_taxon_scope", "literature_plan",
            "record_evidence", "distribution_dataset", "distribution_map", "figure_validate", "export_research_package",
            "resolve_taxon", "type_info", "edoc_search", "library_ocr", "library_merge",
        ):
            self.assertIn(required, names)
        self.assertLessEqual(len(names), 50)

    def test_download_start_requires_login(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            os.environ["LDL_MCP_HOME"] = home
            try:
                from ldl_mcp import server

                result = server.download_start(genus="Testus")
                self.assertEqual(result["status"], "auth_required")
            finally:
                os.environ.pop("LDL_MCP_HOME", None)


if __name__ == "__main__":
    unittest.main()
