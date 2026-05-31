"""Phase 2 M10.4: tests for ``artifact_extractor`` + ``extract_artifact_text``.

Every test that writes to a database uses a temp SQLite file so the
real ``policy_ai.db`` is untouched. No test path makes a network
call, imports OpenAI, or invokes browser automation.

Covers the M10.4 spec items:
    A. Valid Korean HTML → success, language_hint="ko", word_count>0
    B. Valid English HTML → success, language_hint="en"
    C. raw_html=None → success=False, error contains "no raw_html"
    D. artifact.success=False → success=False
    E. script / style / nav / footer / header removed
    F. Sections extracted from h1/h2/h3 structure
    G. main_text truncated at MAX_MAIN_TEXT_CHARS
    H. truth_claim always False (defensive against artifact_row lying)
    I. extraction_result_to_dict carries truth_claim=False
    J. save_extraction_result forces truth_claim=0 in DB
    K. get_extraction_results filters by source_id
    L. get_extraction_results filters by artifact_id
    M. language_hint="unknown" for mixed / neither content
    N. word_count=0 for empty main_text
    O. No network / OpenAI imports in artifact_extractor.py
    P. init_db() creates artifact_text_extractions table
    Q. CLI --help exits 0
    R. CLI --list-artifacts on empty DB returns 0 with safety notes
    S. CLI dry-run does not write to artifact_text_extractions
    T. artifact_extractor not imported by main.py / api_server.py
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import artifact_extractor  # noqa: E402
import database  # noqa: E402
import postgres_storage  # noqa: E402
import scripts.extract_artifact_text as extract_cli  # noqa: E402


CLI_SCRIPT = ROOT / "scripts" / "extract_artifact_text.py"
EXTRACTOR_MODULE_PATH = ROOT / "artifact_extractor.py"

CLI_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_artifact_row(
    *, artifact_id: int = 1, source_id: str = "test_source",
    url: str = "https://example.go.kr/test", raw_html: str = "",
    success: bool = True,
    truth_claim: bool = False,
    official_source_candidate: bool = True,
) -> dict:
    """Build an artifact_row shaped exactly like what
    ``database.get_fetch_artifacts`` returns. Defaults are tuned so a
    test can pass one keyword to flip a single field under test."""
    return {
        "id": artifact_id,
        "source_id": source_id,
        "url": url,
        "fetch_timestamp": "2026-05-22T00:00:00+00:00",
        "status_code": 200,
        "content_type": "text/html; charset=utf-8",
        "success": success,
        "error": None,
        "text_content": None,
        "raw_html": raw_html,
        "fetch_duration_ms": 250,
        # Deliberately allow tests to flip truth_claim=True so we can
        # confirm the extractor refuses to surface it as True.
        "truth_claim": truth_claim,
        "official_source_candidate": official_source_candidate,
        "created_at": "2026-05-22T00:00:00+00:00",
    }


KOREAN_HTML = (
    "<html><head><title>국가법령정보센터</title></head>"
    "<body>"
    "<h1>법령 검색</h1>"
    "<p>대한민국 법령 정보를 검색하고 열람할 수 있는 공식 포털입니다.</p>"
    "<h2>주요 기능</h2>"
    "<p>법률, 시행령, 시행규칙 검색 및 조회 기능을 제공합니다.</p>"
    "<h3>이용 안내</h3>"
    "<p>검색창에 키워드를 입력하면 관련 법령을 확인할 수 있습니다.</p>"
    "</body></html>"
)

ENGLISH_HTML = (
    "<html><head><title>Open Government Data</title></head>"
    "<body>"
    "<h1>Welcome to the portal</h1>"
    "<p>This portal provides access to public datasets and reports.</p>"
    "<h2>Datasets</h2>"
    "<p>Browse curated datasets covering health, transport, and energy.</p>"
    "<h3>Support</h3>"
    "<p>Contact the support team for help with the open data portal.</p>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# A / B. Language detection on real-shaped HTML
# ---------------------------------------------------------------------------


class LanguageDetectionTests(unittest.TestCase):
    def test_korean_html_success_and_hint(self):
        row = _make_artifact_row(raw_html=KOREAN_HTML)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        self.assertEqual(result.language_hint, "ko")
        self.assertEqual(result.title, "국가법령정보센터")
        self.assertGreater(result.word_count, 0)
        self.assertIn("법령", result.main_text)
        self.assertFalse(result.truth_claim)

    def test_english_html_success_and_hint(self):
        row = _make_artifact_row(raw_html=ENGLISH_HTML)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        self.assertEqual(result.language_hint, "en")
        self.assertEqual(result.title, "Open Government Data")
        self.assertGreater(result.word_count, 0)
        self.assertFalse(result.truth_claim)

    def test_unknown_language_for_numbers_only(self):
        row = _make_artifact_row(
            raw_html="<html><body><p>123 456 789 #$%</p></body></html>",
        )
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        self.assertEqual(result.language_hint, "unknown")


# ---------------------------------------------------------------------------
# C. raw_html=None
# ---------------------------------------------------------------------------


class MissingHtmlTests(unittest.TestCase):
    def test_none_raw_html_refused(self):
        row = _make_artifact_row(raw_html=None)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertFalse(result.success)
        self.assertIn("no raw_html", result.error or "")
        self.assertFalse(result.truth_claim)

    def test_empty_raw_html_refused(self):
        row = _make_artifact_row(raw_html="")
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertFalse(result.success)
        self.assertIn("no raw_html", result.error or "")

    def test_whitespace_only_raw_html_refused(self):
        row = _make_artifact_row(raw_html="    \n\t  ")
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertFalse(result.success)
        self.assertIn("no raw_html", result.error or "")


# ---------------------------------------------------------------------------
# D. Underlying fetch failed
# ---------------------------------------------------------------------------


class FetchFailedTests(unittest.TestCase):
    def test_unsuccessful_fetch_refused(self):
        row = _make_artifact_row(raw_html=KOREAN_HTML, success=False)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertFalse(result.success)
        self.assertIn("fetch was not successful", result.error or "")
        self.assertFalse(result.truth_claim)


# ---------------------------------------------------------------------------
# E. Furniture tags stripped
# ---------------------------------------------------------------------------


class FurnitureStripTests(unittest.TestCase):
    def test_script_style_nav_footer_header_stripped(self):
        html = (
            "<html><head><title>t</title></head>"
            "<body>"
            "<nav>NAV_TEXT</nav>"
            "<header>HEADER_TEXT</header>"
            "<script>alert('SCRIPT_TEXT')</script>"
            "<style>.x{color:red}STYLE_TEXT</style>"
            "<p>BODY_TEXT</p>"
            "<footer>FOOTER_TEXT</footer>"
            "</body></html>"
        )
        row = _make_artifact_row(raw_html=html)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        self.assertIn("BODY_TEXT", result.main_text or "")
        for forbidden in (
            "NAV_TEXT", "HEADER_TEXT", "SCRIPT_TEXT",
            "STYLE_TEXT", "FOOTER_TEXT",
        ):
            self.assertNotIn(forbidden, result.main_text or "",
                             f"furniture text {forbidden!r} leaked")


# ---------------------------------------------------------------------------
# F. Sections
# ---------------------------------------------------------------------------


class SectionExtractionTests(unittest.TestCase):
    def test_sections_extracted_from_headings(self):
        html = (
            "<html><body>"
            "<h1>Heading One</h1>"
            "<p>Paragraph under one.</p>"
            "<h2>Heading Two</h2>"
            "<p>Paragraph under two.</p>"
            "<div><p>Nested paragraph still under two.</p></div>"
            "<h3>Heading Three</h3>"
            "<p>Paragraph under three.</p>"
            "</body></html>"
        )
        row = _make_artifact_row(raw_html=html)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        sections = json.loads(result.sections)
        self.assertEqual(len(sections), 3)
        self.assertEqual(sections[0]["heading"], "Heading One")
        self.assertIn("Paragraph under one", sections[0]["text"])
        self.assertEqual(sections[1]["heading"], "Heading Two")
        self.assertIn("Paragraph under two", sections[1]["text"])
        self.assertIn(
            "Nested paragraph still under two", sections[1]["text"],
        )
        self.assertEqual(sections[2]["heading"], "Heading Three")
        self.assertIn("Paragraph under three", sections[2]["text"])

    def test_sections_empty_when_no_headings(self):
        html = "<html><body><p>just a paragraph</p></body></html>"
        row = _make_artifact_row(raw_html=html)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        sections = json.loads(result.sections)
        self.assertEqual(sections, [])

    def test_sections_is_valid_json_string(self):
        row = _make_artifact_row(raw_html=KOREAN_HTML)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        # Must round-trip cleanly through json.loads.
        sections = json.loads(result.sections)
        self.assertIsInstance(sections, list)
        for entry in sections:
            self.assertIn("heading", entry)
            self.assertIn("text", entry)


# ---------------------------------------------------------------------------
# G. Truncation
# ---------------------------------------------------------------------------


class TruncationTests(unittest.TestCase):
    def test_main_text_truncated_at_cap(self):
        # Build an HTML body whose extracted text is well over the cap.
        long_body = ("hello world " * 6_000)  # ~72_000 chars
        html = f"<html><body><p>{long_body}</p></body></html>"
        row = _make_artifact_row(raw_html=html)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        self.assertLessEqual(
            len(result.main_text or ""),
            artifact_extractor.MAX_MAIN_TEXT_CHARS,
        )


# ---------------------------------------------------------------------------
# H / I. truth_claim is always False
# ---------------------------------------------------------------------------


class TruthClaimAlwaysFalseTests(unittest.TestCase):
    def test_truth_claim_false_even_if_artifact_lies(self):
        row = _make_artifact_row(raw_html=KOREAN_HTML, truth_claim=True)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertIs(result.truth_claim, False)

    def test_truth_claim_false_on_failure(self):
        row = _make_artifact_row(raw_html=None, truth_claim=True)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertIs(result.truth_claim, False)

    def test_truth_claim_false_in_serialized_dict(self):
        row = _make_artifact_row(raw_html=KOREAN_HTML, truth_claim=True)
        result = artifact_extractor.extract_text_from_artifact(row)
        d = artifact_extractor.extraction_result_to_dict(result)
        self.assertIs(d["truth_claim"], False)

    def test_serialized_dict_has_expected_keys(self):
        row = _make_artifact_row(raw_html=KOREAN_HTML)
        result = artifact_extractor.extract_text_from_artifact(row)
        d = artifact_extractor.extraction_result_to_dict(result)
        for key in (
            "artifact_id", "source_id", "url", "extraction_timestamp",
            "extraction_duration_ms", "success", "error", "title",
            "main_text", "sections", "word_count", "language_hint",
            "truth_claim", "official_source_candidate",
        ):
            self.assertIn(key, d)


# ---------------------------------------------------------------------------
# N. word_count edge cases
# ---------------------------------------------------------------------------


class WordCountTests(unittest.TestCase):
    def test_word_count_zero_for_no_text_body(self):
        html = "<html><body></body></html>"
        row = _make_artifact_row(raw_html=html)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        self.assertEqual(result.word_count, 0)

    def test_word_count_matches_split(self):
        html = "<html><body><p>one two three four five</p></body></html>"
        row = _make_artifact_row(raw_html=html)
        result = artifact_extractor.extract_text_from_artifact(row)
        self.assertTrue(result.success, msg=result.error)
        self.assertEqual(result.word_count, 5)


# ---------------------------------------------------------------------------
# J / K / L / P. Database round-trip with temp DB
# ---------------------------------------------------------------------------


class DatabaseRoundTripTests(unittest.TestCase):
    def setUp(self):
        # M12.0e-3a: round-trip via the PG-primary path. Point a private
        # sqlite:// substitute at a per-test temp file (USE_POSTGRES_WRITE
        # ON), and reset the cached engine so each test binds to its own
        # fresh DB. Env vars are snapshot/restored so the rest of the
        # process (and validate.py's dual-write-disabled determinism) is
        # untouched. Pattern copied from tests/test_m12_0d_stage2.py and
        # tests/test_m12_0e_pg_schema_startup_invariant.py.
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._pg_db = str(Path(self._tmp_dir.name) / "pg.db")
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in ("USE_POSTGRES_WRITE", "DATABASE_URL")
        }
        os.environ["USE_POSTGRES_WRITE"] = "true"
        os.environ["DATABASE_URL"] = f"sqlite:///{self._pg_db}"
        postgres_storage.reset_engine_for_tests()

    def tearDown(self):
        import gc as _gc
        for key, value in self._env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        postgres_storage.reset_engine_for_tests()
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def _round_trip_one(
        self, *, source_id, artifact_id, html=KOREAN_HTML,
        truth_claim=False,
    ):
        row = _make_artifact_row(
            artifact_id=artifact_id, source_id=source_id,
            raw_html=html, truth_claim=truth_claim,
        )
        result = artifact_extractor.extract_text_from_artifact(row)
        d = artifact_extractor.extraction_result_to_dict(result)
        return database.save_extraction_result(d)

    def test_save_then_get_round_trip(self):
        row_id = self._round_trip_one(
            source_id="sample_source", artifact_id=42,
        )
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)
        results = database.get_extraction_results(
            source_id="sample_source",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_id"], "sample_source")
        self.assertEqual(results[0]["artifact_id"], 42)
        self.assertTrue(results[0]["success"])
        self.assertIs(results[0]["truth_claim"], False)
        self.assertEqual(results[0]["language_hint"], "ko")
        self.assertGreater(int(results[0]["word_count"] or 0), 0)

    def test_save_forces_truth_claim_zero_even_when_input_lies(self):
        row = _make_artifact_row(
            artifact_id=7, source_id="liar_source",
            raw_html=KOREAN_HTML, truth_claim=True,
        )
        result = artifact_extractor.extract_text_from_artifact(row)
        d = artifact_extractor.extraction_result_to_dict(result)
        # Re-mutate the dict to simulate a misbehaving caller.
        d["truth_claim"] = True
        database.save_extraction_result(d)
        results = database.get_extraction_results(
            source_id="liar_source",
        )
        self.assertEqual(len(results), 1)
        self.assertIs(results[0]["truth_claim"], False)

    def test_get_filters_by_source_id(self):
        self._round_trip_one(source_id="src_a", artifact_id=1)
        self._round_trip_one(source_id="src_b", artifact_id=2)
        self._round_trip_one(source_id="src_a", artifact_id=3,
                             html=ENGLISH_HTML)
        a_rows = database.get_extraction_results(source_id="src_a")
        b_rows = database.get_extraction_results(source_id="src_b")
        all_rows = database.get_extraction_results()
        self.assertEqual(len(a_rows), 2)
        self.assertEqual(len(b_rows), 1)
        self.assertEqual(len(all_rows), 3)

    def test_get_filters_by_artifact_id(self):
        self._round_trip_one(source_id="src_a", artifact_id=1)
        self._round_trip_one(source_id="src_b", artifact_id=2)
        rows = database.get_extraction_results(artifact_id=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["artifact_id"], 2)
        self.assertEqual(rows[0]["source_id"], "src_b")

    def test_get_filters_by_source_and_artifact(self):
        self._round_trip_one(source_id="src_a", artifact_id=1)
        self._round_trip_one(source_id="src_b", artifact_id=1)
        rows = database.get_extraction_results(
            source_id="src_a", artifact_id=1,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_id"], "src_a")

    def test_save_rejects_missing_required_fields(self):
        for bad in (
            None,
            {},
            {"artifact_id": 1},                          # missing source_id
            {"artifact_id": 1, "source_id": "x"},        # missing url
            {"artifact_id": 1, "source_id": "x", "url": "u"},  # missing ts
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    database.save_extraction_result(bad)


# ---------------------------------------------------------------------------
# P. init_db() creates the artifact_text_extractions table — SQLite-specific.
#
# Deliberately NOT migrated to the PG-substitute path: this pins the
# SQLite schema-creation behaviour (init_db builds the table, verified via
# sqlite_master) that 0e-5 still depends on. It runs with NO dual-write env
# — a DB_PATH swap + raw sqlite3 read against an isolated temp file.
# ---------------------------------------------------------------------------


class InitDbSqliteSchemaTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        import gc as _gc
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_init_db_creates_artifact_text_extractions_table(self):
        # Point database.DB_PATH at a fresh temp file, then call init_db().
        fresh_db = str(Path(self._tmp_dir.name) / "fresh_init.db")
        original = database.DB_PATH
        database.DB_PATH = Path(fresh_db)
        try:
            database.init_db()
            connection = sqlite3.connect(fresh_db)
            try:
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='artifact_text_extractions'"
                ).fetchone()
            finally:
                connection.close()
        finally:
            database.DB_PATH = original
        self.assertIsNotNone(
            row, "init_db() must create artifact_text_extractions table",
        )


# ---------------------------------------------------------------------------
# Q / R / S. CLI smokes
# ---------------------------------------------------------------------------


def _run_cli_subprocess(*args, timeout=CLI_TIMEOUT_SECONDS, env=None):
    completed = subprocess.run(
        [sys.executable, str(CLI_SCRIPT)] + [str(a) for a in args],
        cwd=str(ROOT),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    return completed.returncode, completed.stdout, completed.stderr


class CliSmokeTests(unittest.TestCase):
    def setUp(self):
        # M12.0e-3a: the CLI is PG-only. Point a private sqlite://
        # substitute at a per-test temp file (USE_POSTGRES_WRITE ON) and
        # reset the cached engine. The CLI subprocess inherits both env
        # vars via _run_cli_subprocess's env={**os.environ, ...} merge,
        # so parent (seeding) and child (CLI) share the same substitute
        # file. Schema auto-creates via ensure_schema on first
        # get_engine() — no manual CREATE TABLE needed.
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._pg_db = str(Path(self._tmp_dir.name) / "cli_smoke_pg.db")
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in ("USE_POSTGRES_WRITE", "DATABASE_URL")
        }
        os.environ["USE_POSTGRES_WRITE"] = "true"
        os.environ["DATABASE_URL"] = f"sqlite:///{self._pg_db}"
        postgres_storage.reset_engine_for_tests()

    def tearDown(self):
        import gc as _gc
        for key, value in self._env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        postgres_storage.reset_engine_for_tests()
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def _seed_artifact(self, *, artifact_id, source_id, raw_html):
        # Seed the INPUT row via the public PG-primary writer (dual-write
        # ON → lands in the PG substitute). PG's SERIAL assigns the id, so
        # ``artifact_id`` is not pinned — the smokes filter by source_id.
        # Mirrors tests/test_postgres_storage.py's save_fetch_artifact
        # PG-only precedent.
        database.save_fetch_artifact({
            "source_id": source_id,
            "url": "https://example.go.kr/test",
            "fetch_timestamp": "2026-05-22T00:00:00+00:00",
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "success": True,
            "error": None,
            "text_content": None,
            "raw_html": raw_html,
            "fetch_duration_ms": 250,
            "official_source_candidate": True,
        })

    def test_help_exits_0(self):
        rc, stdout, _ = _run_cli_subprocess("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Extract structured text", stdout)
        self.assertIn("Exit codes", stdout)

    def test_list_artifacts_empty_db(self):
        rc, stdout, _ = _run_cli_subprocess("--list-artifacts")
        self.assertEqual(rc, 0)
        self.assertIn("source_fetch_artifacts", stdout)
        self.assertIn("Total: 0", stdout)
        self.assertIn("truth_claim=False", stdout)

    def test_list_artifacts_json(self):
        self._seed_artifact(
            artifact_id=1, source_id="src_a", raw_html=KOREAN_HTML,
        )
        rc, stdout, _ = _run_cli_subprocess(
            "--list-artifacts", "--json",
        )
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["mode"], "list_artifacts")
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["summary"]["with_raw_html"], 1)
        self.assertIn("truth", payload["safety_notes"])

    def test_dry_run_does_not_write_extractions(self):
        self._seed_artifact(
            artifact_id=1, source_id="src_a", raw_html=KOREAN_HTML,
        )
        rc, stdout, _ = _run_cli_subprocess(
            "--source-id", "src_a", "--dry-run",
        )
        self.assertEqual(rc, 0, msg=stdout)
        self.assertIn("Extraction Result", stdout)
        self.assertIn("dry_run: True", stdout)
        # No extractions persisted — read back via the public PG API.
        self.assertEqual(
            len(database.get_extraction_results(source_id="src_a")),
            0,
            "dry-run must not insert rows",
        )

    def test_save_writes_extraction_row(self):
        self._seed_artifact(
            artifact_id=1, source_id="src_a", raw_html=KOREAN_HTML,
        )
        rc, stdout, _ = _run_cli_subprocess(
            "--source-id", "src_a", "--save",
        )
        self.assertEqual(rc, 0, msg=stdout)
        self.assertIn("saved_row_id", stdout)
        rows = database.get_extraction_results(source_id="src_a")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["success"])
        self.assertIs(rows[0]["truth_claim"], False)

    def test_no_filter_is_usage_error(self):
        rc, _stdout, stderr = _run_cli_subprocess()
        self.assertEqual(rc, 2)
        self.assertIn("--source-id", stderr)

    def test_save_and_dry_run_mutually_exclusive(self):
        rc, _stdout, stderr = _run_cli_subprocess(
            "--source-id", "src_a", "--dry-run", "--save",
        )
        self.assertEqual(rc, 2)
        self.assertIn("mutually exclusive", stderr)

    def test_empty_artifact_set_exits_1(self):
        rc, stdout, _ = _run_cli_subprocess(
            "--source-id", "src_none", "--dry-run",
        )
        self.assertEqual(rc, 1)
        self.assertIn("no source_fetch_artifacts rows", stdout)


# ---------------------------------------------------------------------------
# O / T. Static safety
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def _import_lines(self, path):
        text = path.read_text(encoding="utf-8")
        return [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]

    def test_extractor_does_not_import_network_or_openai(self):
        joined = "\n".join(self._import_lines(EXTRACTOR_MODULE_PATH))
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"artifact_extractor.py must not import {forbidden!r}",
            )

    def test_cli_does_not_import_network_or_openai(self):
        joined = "\n".join(self._import_lines(CLI_SCRIPT))
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"extract_artifact_text.py must not import {forbidden!r}",
            )

    def test_extractor_not_imported_by_pipeline_entry_points(self):
        for module_name in ("main.py", "api_server.py", "scheduler.py"):
            module_path = ROOT / module_name
            if not module_path.exists():
                continue
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "artifact_extractor", text,
                f"{module_name} must not import artifact_extractor",
            )
            self.assertNotIn(
                "extract_artifact_text", text,
                f"{module_name} must not import extract_artifact_text",
            )


if __name__ == "__main__":
    unittest.main()
