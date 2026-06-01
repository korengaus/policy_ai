"""Phase 2 M10.5: tests for ``artifact_evidence_linker`` +
``link_artifact_evidence``.

Every test that writes to a database uses a temp SQLite file so the
real ``policy_ai.db`` is untouched. No test path makes a network
call, imports OpenAI, or invokes browser automation.

Covers the M10.5 spec items:
    A. Matching Korean claim and main_text → score >= 0.15, candidate
    B. Non-matching content → empty list
    C. match_score always in [0.0, 1.0]
    D. supporting_passage capped at SUPPORTING_PASSAGE_CHARS
    E. truth_claim always False in EvidenceCandidate
    F. operator_review_required always True in EvidenceCandidate
    G. notes always contains "requires human review"
    H. candidate_to_dict serializes correctly
    I. save_evidence_candidate forces truth_claim=0 in DB
    J. save_evidence_candidate forces operator_review_required=1 in DB
    K. get_evidence_candidates filters by analysis_id
    L. get_evidence_candidates filters by source_id
    M. empty main_text → empty list
    N. min_score=0.0 returns candidate for any overlap
    O. init_db() creates artifact_evidence_candidates table
    P. No network calls in any test path
    Q. No OpenAI imports in artifact_evidence_linker.py (static scan)
    R. artifact_evidence_linker not imported by main.py / api_server.py
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

import artifact_evidence_linker as linker  # noqa: E402
import database  # noqa: E402
import postgres_storage  # noqa: E402
import scripts.link_artifact_evidence as link_cli  # noqa: E402


CLI_SCRIPT = ROOT / "scripts" / "link_artifact_evidence.py"
LINKER_MODULE_PATH = ROOT / "artifact_evidence_linker.py"

CLI_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


KOREAN_CLAIM = "청년 전월세 보증금 대출 한도 확대"
KOREAN_BODY_TEXT = (
    "정부는 청년 전월세 보증금 대출 한도를 확대한다고 발표했습니다. "
    "청년 대출 한도 인상은 주거 안정성을 높이기 위한 조치입니다. "
    "보증금 부담을 완화하는 정책이 추가로 검토되고 있습니다."
)

ENGLISH_CLAIM = "Government raises youth housing loan limit"
ENGLISH_BODY_TEXT = (
    "The government announced that it will raise the youth housing "
    "loan limit to ease rental deposit burdens. The new loan limit "
    "policy targets first-time renters in major cities."
)


def _make_extraction_row(
    *, extraction_id: int = 1, source_id: str = "test_source",
    url: str = "https://example.go.kr/notice",
    main_text: str = KOREAN_BODY_TEXT,
    official_source_candidate: bool = True,
) -> dict:
    """Build an extraction_row shaped like
    ``database.get_extraction_results`` returns."""
    return {
        "id": extraction_id,
        "artifact_id": 100 + extraction_id,
        "source_id": source_id,
        "url": url,
        "extraction_timestamp": "2026-05-22T00:00:00+00:00",
        "extraction_duration_ms": 10,
        "success": True,
        "error": None,
        "title": "공고",
        "main_text": main_text,
        "sections": "[]",
        "word_count": (len(main_text.split()) if main_text else 0),
        "language_hint": "ko" if "청년" in (main_text or "") else "en",
        "truth_claim": False,
        "official_source_candidate": official_source_candidate,
        "created_at": "2026-05-22T00:00:00+00:00",
    }


def _make_analysis_row(
    *, analysis_id: int = 7, claim_text: str = KOREAN_CLAIM,
    claims=None, normalized_claims=None,
) -> dict:
    """Build an analysis_row shaped like ``database.get_result_by_id``
    returns. ``claims`` / ``normalized_claims`` may be Python lists or
    None (default no extras)."""
    return {
        "id": analysis_id,
        "query": "청년 정책",
        "title": "테스트 분석",
        "original_url": "https://news.example/article-1",
        "claim_text": claim_text,
        "claims": (
            json.dumps(claims, ensure_ascii=False) if claims else None
        ),
        "normalized_claims": (
            json.dumps(normalized_claims, ensure_ascii=False)
            if normalized_claims else None
        ),
    }


# ---------------------------------------------------------------------------
# A / B / N. Core matching behaviour
# ---------------------------------------------------------------------------


class CoreMatchingTests(unittest.TestCase):
    def test_korean_claim_matches_korean_body(self):
        candidates = linker.find_evidence_candidates(
            _make_extraction_row(), _make_analysis_row(),
        )
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertGreaterEqual(c.match_score, 0.15)
        self.assertLessEqual(c.match_score, 1.0)
        self.assertIn("청년", c.matched_tokens)
        self.assertIn("대출", c.matched_tokens)
        self.assertIn("한도", c.matched_tokens)
        self.assertFalse(c.truth_claim)
        self.assertTrue(c.operator_review_required)

    def test_english_claim_matches_english_body(self):
        row = _make_extraction_row(main_text=ENGLISH_BODY_TEXT)
        analysis = _make_analysis_row(claim_text=ENGLISH_CLAIM)
        candidates = linker.find_evidence_candidates(row, analysis)
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertGreaterEqual(c.match_score, 0.15)
        # Lowercased tokens in the overlap.
        self.assertIn("loan", c.matched_tokens)
        self.assertIn("limit", c.matched_tokens)

    def test_non_matching_returns_empty(self):
        row = _make_extraction_row(
            main_text="완전히 다른 주제. 날씨, 음악, 영화 리뷰만 있는 페이지.",
        )
        analysis = _make_analysis_row()  # claim is about 청년 대출
        candidates = linker.find_evidence_candidates(row, analysis)
        self.assertEqual(candidates, [])

    def test_empty_main_text_returns_empty(self):
        row = _make_extraction_row(main_text="")
        candidates = linker.find_evidence_candidates(
            row, _make_analysis_row(),
        )
        self.assertEqual(candidates, [])

    def test_whitespace_only_main_text_returns_empty(self):
        row = _make_extraction_row(main_text="   \n\t  ")
        candidates = linker.find_evidence_candidates(
            row, _make_analysis_row(),
        )
        self.assertEqual(candidates, [])

    def test_min_score_zero_returns_for_any_overlap(self):
        # Body shares only ONE token ("대출") with the claim. The
        # default threshold (0.15) keeps this out for a 6-token claim
        # (1/6 = 0.167 ≈ above default — pick a weaker overlap to be
        # below default but above 0).
        row = _make_extraction_row(
            main_text="시장은 대출 시장의 변동성을 강조했습니다.",
        )
        # Long claim → 1 / N is small but > 0.
        analysis = _make_analysis_row(
            claim_text=(
                "청년 전월세 보증금 대출 한도 확대 정책 발표 일정 "
                "검토 결과 정부 부처별 협의 진행 상황"
            ),
        )
        # Default threshold filters this out.
        self.assertEqual(
            linker.find_evidence_candidates(row, analysis),
            [],
        )
        # min_score=0.0 lets it through.
        candidates = linker.find_evidence_candidates(
            row, analysis, min_score=0.0,
        )
        self.assertEqual(len(candidates), 1)
        self.assertGreater(candidates[0].match_score, 0.0)


# ---------------------------------------------------------------------------
# C. Score range invariant
# ---------------------------------------------------------------------------


class ScoreRangeTests(unittest.TestCase):
    def _score_range_ok(self, candidates):
        for c in candidates:
            self.assertGreaterEqual(c.match_score, 0.0)
            self.assertLessEqual(c.match_score, 1.0)

    def test_exact_match_yields_score_1(self):
        # Body literally contains every claim token.
        analysis = _make_analysis_row(claim_text="alpha beta gamma")
        row = _make_extraction_row(
            main_text="alpha beta gamma additional filler text",
        )
        candidates = linker.find_evidence_candidates(
            row, analysis, min_score=0.0,
        )
        self.assertEqual(len(candidates), 1)
        self.assertAlmostEqual(candidates[0].match_score, 1.0, places=4)
        self._score_range_ok(candidates)

    def test_partial_overlap_yields_fractional_score(self):
        analysis = _make_analysis_row(
            claim_text="alpha beta gamma delta",
        )
        row = _make_extraction_row(
            main_text="alpha gamma elsewhere irrelevant words",
        )
        candidates = linker.find_evidence_candidates(
            row, analysis, min_score=0.0,
        )
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertAlmostEqual(c.match_score, 0.5, places=4)
        self._score_range_ok(candidates)

    def test_empty_claim_returns_empty(self):
        # claim_text="" + no claims list → no candidates.
        analysis = _make_analysis_row(claim_text="")
        candidates = linker.find_evidence_candidates(
            _make_extraction_row(), analysis, min_score=0.0,
        )
        self.assertEqual(candidates, [])


# ---------------------------------------------------------------------------
# D. supporting_passage capped
# ---------------------------------------------------------------------------


class SupportingPassageTests(unittest.TestCase):
    def test_supporting_passage_capped_at_window(self):
        long_text = ("청년 대출 한도 " * 2_000)  # ~14_000 chars Korean
        row = _make_extraction_row(main_text=long_text)
        analysis = _make_analysis_row()
        candidates = linker.find_evidence_candidates(row, analysis)
        self.assertEqual(len(candidates), 1)
        self.assertLessEqual(
            len(candidates[0].supporting_passage),
            linker.SUPPORTING_PASSAGE_CHARS,
        )

    def test_supporting_passage_contains_overlap(self):
        body = (
            ("filler " * 200)
            + "청년 대출 한도 인상 발표는 매우 중요한 정책입니다. "
            + ("filler " * 200)
        )
        row = _make_extraction_row(main_text=body)
        analysis = _make_analysis_row()
        candidates = linker.find_evidence_candidates(row, analysis)
        self.assertEqual(len(candidates), 1)
        passage = candidates[0].supporting_passage
        # The selected window should land near the overlap region.
        self.assertTrue(
            "청년" in passage or "대출" in passage or "한도" in passage,
            f"supporting_passage did not include any matched token: {passage!r}",
        )


# ---------------------------------------------------------------------------
# E / F / G / H. Safety-flag invariants on the dataclass + serializer
# ---------------------------------------------------------------------------


class SafetyFlagsTests(unittest.TestCase):
    def test_truth_claim_always_false_on_dataclass(self):
        candidates = linker.find_evidence_candidates(
            _make_extraction_row(), _make_analysis_row(),
        )
        for c in candidates:
            self.assertIs(c.truth_claim, False)

    def test_operator_review_required_always_true(self):
        candidates = linker.find_evidence_candidates(
            _make_extraction_row(), _make_analysis_row(),
        )
        for c in candidates:
            self.assertIs(c.operator_review_required, True)

    def test_notes_contains_human_review(self):
        candidates = linker.find_evidence_candidates(
            _make_extraction_row(), _make_analysis_row(),
        )
        for c in candidates:
            self.assertIn("requires human review", c.notes)

    def test_candidate_to_dict_has_expected_keys(self):
        candidates = linker.find_evidence_candidates(
            _make_extraction_row(), _make_analysis_row(),
        )
        self.assertGreater(len(candidates), 0)
        d = linker.candidate_to_dict(candidates[0])
        for key in (
            "extraction_id", "source_id", "url", "analysis_id",
            "claim_text", "match_score", "matched_tokens",
            "supporting_passage", "candidate_timestamp",
            "truth_claim", "official_source_candidate",
            "operator_review_required", "notes",
        ):
            self.assertIn(key, d)
        self.assertIs(d["truth_claim"], False)
        self.assertIs(d["operator_review_required"], True)
        # matched_tokens is JSON-encoded by the serializer (DB stores it
        # as TEXT). Round-trip must give back a list.
        self.assertIsInstance(d["matched_tokens"], str)
        self.assertIsInstance(json.loads(d["matched_tokens"]), list)

    def test_candidate_to_dict_forces_safety_even_if_lied(self):
        candidates = linker.find_evidence_candidates(
            _make_extraction_row(), _make_analysis_row(),
        )
        c = candidates[0]
        # Mutate the dataclass fields to simulate a misbehaving caller.
        c.truth_claim = True               # type: ignore[assignment]
        c.operator_review_required = False  # type: ignore[assignment]
        d = linker.candidate_to_dict(c)
        self.assertIs(d["truth_claim"], False)
        self.assertIs(d["operator_review_required"], True)


# ---------------------------------------------------------------------------
# Multi-claim handling (claims + normalized_claims)
# ---------------------------------------------------------------------------


class MultiClaimTests(unittest.TestCase):
    def test_claims_list_produces_one_candidate_per_match(self):
        analysis = _make_analysis_row(
            claim_text="청년 전월세 보증금 대출 한도 확대",
            claims=["청년 주거 안정성 강화"],
            normalized_claims=["보증금 부담 완화 정책"],
        )
        row = _make_extraction_row(main_text=KOREAN_BODY_TEXT)
        candidates = linker.find_evidence_candidates(row, analysis)
        # All three claims share words with the body; expect ≥ 2.
        self.assertGreaterEqual(len(candidates), 2)
        claim_texts = {c.claim_text for c in candidates}
        # The primary claim is always represented.
        self.assertIn("청년 전월세 보증금 대출 한도 확대", claim_texts)

    def test_dict_shaped_claim_entries_supported(self):
        analysis = _make_analysis_row(
            claim_text="primary claim",
            claims=[{"text": "청년 대출 한도 확대"}],
        )
        row = _make_extraction_row(main_text=KOREAN_BODY_TEXT)
        candidates = linker.find_evidence_candidates(
            row, analysis, min_score=0.0,
        )
        self.assertGreater(len(candidates), 0)
        claim_texts = {c.claim_text for c in candidates}
        self.assertIn("청년 대출 한도 확대", claim_texts)


# ---------------------------------------------------------------------------
# I / J / K / L / O. DB round-trip with temp DB
# ---------------------------------------------------------------------------


class DatabaseRoundTripTests(unittest.TestCase):
    def setUp(self):
        # M12.0e-3a: round-trip via the PG-primary path. Point a private
        # sqlite:// substitute at a per-test temp file (USE_POSTGRES_WRITE
        # ON) and reset the cached engine so each test binds to its own
        # fresh DB. Env vars are snapshot/restored so the rest of the
        # process (and validate.py's dual-write-disabled determinism) is
        # untouched. Pattern copied from the #1-migrated
        # tests/test_artifact_extractor.py.
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

    def _save_one(self, *, analysis_id="42", source_id="src_a",
                  extraction_id=11, lie=False):
        row = _make_extraction_row(
            extraction_id=extraction_id, source_id=source_id,
        )
        analysis = _make_analysis_row(analysis_id=int(analysis_id))
        candidates = linker.find_evidence_candidates(row, analysis)
        self.assertGreater(len(candidates), 0)
        d = linker.candidate_to_dict(candidates[0])
        if lie:
            d["truth_claim"] = True
            d["operator_review_required"] = False
        return database.save_evidence_candidate(d)

    def test_round_trip_basic(self):
        row_id = self._save_one()
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)
        rows = database.get_evidence_candidates(analysis_id="42")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["analysis_id"], "42")
        self.assertEqual(r["source_id"], "src_a")
        self.assertEqual(r["extraction_id"], 11)
        self.assertIs(r["truth_claim"], False)
        self.assertIs(r["operator_review_required"], True)
        # matched_tokens stored as JSON string of a list.
        decoded = json.loads(r["matched_tokens"])
        self.assertIsInstance(decoded, list)
        self.assertIn(r["notes"], (linker.NOTES_HUMAN_REVIEW, r["notes"]))
        self.assertIn("requires human review", r["notes"])

    def test_save_forces_truth_claim_zero_even_when_caller_lies(self):
        self._save_one(lie=True)
        rows = database.get_evidence_candidates()
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["truth_claim"], False)

    def test_save_forces_operator_review_required_one_even_when_lied(self):
        self._save_one(lie=True)
        rows = database.get_evidence_candidates()
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["operator_review_required"], True)

    def test_get_filters_by_analysis_id(self):
        self._save_one(analysis_id="1", extraction_id=1)
        self._save_one(analysis_id="2", extraction_id=2)
        self._save_one(analysis_id="1", extraction_id=3)
        ones = database.get_evidence_candidates(analysis_id="1")
        twos = database.get_evidence_candidates(analysis_id="2")
        all_rows = database.get_evidence_candidates()
        self.assertEqual(len(ones), 2)
        self.assertEqual(len(twos), 1)
        self.assertEqual(len(all_rows), 3)

    def test_get_filters_by_source_id(self):
        self._save_one(analysis_id="1", source_id="src_a", extraction_id=1)
        self._save_one(analysis_id="1", source_id="src_b", extraction_id=2)
        only_a = database.get_evidence_candidates(source_id="src_a")
        only_b = database.get_evidence_candidates(source_id="src_b")
        self.assertEqual(len(only_a), 1)
        self.assertEqual(only_a[0]["source_id"], "src_a")
        self.assertEqual(len(only_b), 1)
        self.assertEqual(only_b[0]["source_id"], "src_b")

    def test_get_filters_by_extraction_id(self):
        self._save_one(analysis_id="1", extraction_id=1)
        self._save_one(analysis_id="1", extraction_id=2)
        rows = database.get_evidence_candidates(extraction_id=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["extraction_id"], 2)

    def test_save_rejects_missing_required_fields(self):
        for bad in (
            None,
            {},
            {"extraction_id": 1},
            {"extraction_id": 1, "source_id": "x"},
            {"extraction_id": 1, "source_id": "x", "url": "u"},
            {"extraction_id": 1, "source_id": "x", "url": "u",
             "analysis_id": "7"},
            {"extraction_id": 1, "source_id": "x", "url": "u",
             "analysis_id": "7", "claim_text": "c"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    database.save_evidence_candidate(bad)


# ---------------------------------------------------------------------------
# M12.0e-6b-1: InitDbSqliteSchemaTests removed. It pinned init_db()'s SQLite
# schema-creation (artifact_evidence_candidates, verified via sqlite_master)
# — that machinery is intentionally retired in 0e-6b-3, so the coverage is
# dropped here rather than left coupled to soon-to-be-removed symbols. PG
# schema is owned by postgres_storage.ensure_schema.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI smokes
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
        self._analysis_seq = 0
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

    def _seed_extraction(
        self, *, extraction_id, source_id, main_text=KOREAN_BODY_TEXT,
    ):
        # Seed the INPUT extraction via the public PG-primary writer
        # (dual-write ON → PG substitute). PG's SERIAL assigns the
        # extraction id; the smokes filter by analysis_id/source_id.
        database.save_extraction_result({
            "artifact_id": 100 + extraction_id,
            "source_id": source_id,
            "url": "https://example.go.kr/notice",
            "extraction_timestamp": "2026-05-22T00:00:00+00:00",
            "extraction_duration_ms": 10,
            "success": True,
            "error": None,
            "title": "공고",
            "main_text": main_text,
            "sections": "[]",
            "word_count": len(main_text.split()),
            "language_hint": "ko",
            "official_source_candidate": True,
        })

    def _seed_analysis(self, *, claim_text=KOREAN_CLAIM):
        # Seed the INPUT analysis row via the public PG-primary writer and
        # RETURN the PG-SERIAL id (save_analysis_result returns
        # {"id": <pg id>}). A unique original_url per seed dodges the
        # duplicate-by-url guard in save_analysis_result.
        self._analysis_seq += 1
        original_url = f"https://news.example/analysis-{self._analysis_seq}"
        result = database.save_analysis_result(
            {"claim_text": claim_text, "original_url": original_url},
            query="청년 정책",
        )
        analysis_id = result.get("id")
        self.assertIsNotNone(
            analysis_id,
            f"save_analysis_result did not persist a row (result={result})",
        )
        return analysis_id

    def test_help_exits_0(self):
        rc, stdout, _ = _run_cli_subprocess("--help")
        self.assertEqual(rc, 0)
        self.assertIn("evidence candidates", stdout)
        self.assertIn("Exit codes", stdout)

    def test_list_extractions_empty(self):
        rc, stdout, _ = _run_cli_subprocess("--list-extractions")
        self.assertEqual(rc, 0)
        self.assertIn("artifact_text_extractions", stdout)
        self.assertIn("Total: 0", stdout)
        # All four safety notes present.
        self.assertIn(
            "unreviewed keyword-match candidates only", stdout,
        )
        self.assertIn("truth_claim=False", stdout)
        self.assertIn("operator_review_required=True", stdout)
        self.assertIn("do not feed into the live analysis pipeline", stdout)

    def test_list_candidates_empty(self):
        rc, stdout, _ = _run_cli_subprocess("--list-candidates")
        self.assertEqual(rc, 0)
        self.assertIn("artifact_evidence_candidates", stdout)
        self.assertIn("Total: 0", stdout)

    def test_link_dry_run_does_not_write(self):
        self._seed_extraction(extraction_id=1, source_id="src_a")
        analysis_id = self._seed_analysis()
        rc, stdout, _ = _run_cli_subprocess(
            "--analysis-id", str(analysis_id),
            "--dry-run",
        )
        self.assertEqual(rc, 0, msg=stdout)
        self.assertIn("Evidence Candidate", stdout)
        self.assertIn("dry_run: True", stdout)
        # No rows written to artifact_evidence_candidates — read via the
        # public PG API.
        self.assertEqual(
            len(database.get_evidence_candidates(
                analysis_id=str(analysis_id),
            )),
            0,
            "dry-run must not insert rows",
        )

    def test_link_save_writes_candidate(self):
        self._seed_extraction(extraction_id=1, source_id="src_a")
        analysis_id = self._seed_analysis()
        rc, stdout, _ = _run_cli_subprocess(
            "--analysis-id", str(analysis_id),
            "--save",
        )
        self.assertEqual(rc, 0, msg=stdout)
        self.assertIn("saved_row_id", stdout)
        rows = database.get_evidence_candidates(
            analysis_id=str(analysis_id),
        )
        self.assertGreater(len(rows), 0)
        self.assertIs(rows[0]["truth_claim"], False)
        self.assertIs(rows[0]["operator_review_required"], True)

    def test_link_unknown_analysis_id_exits_1(self):
        self._seed_extraction(extraction_id=1, source_id="src_a")
        rc, stdout, _ = _run_cli_subprocess(
            "--analysis-id", "999",
            "--dry-run",
        )
        self.assertEqual(rc, 1)
        self.assertIn("no analysis_results row", stdout)

    def test_link_all_below_threshold_exits_1(self):
        # Extraction body shares nothing with the claim.
        self._seed_extraction(
            extraction_id=1, source_id="src_a",
            main_text="완전히 다른 주제. 음악, 영화, 날씨 리뷰만 있는 페이지.",
        )
        analysis_id = self._seed_analysis(claim_text=KOREAN_CLAIM)
        rc, stdout, _ = _run_cli_subprocess(
            "--analysis-id", str(analysis_id),
            "--dry-run",
        )
        self.assertEqual(rc, 1)
        self.assertIn("no (extraction, claim) pair met min_score", stdout)

    def test_missing_analysis_id_is_usage_error(self):
        rc, _stdout, stderr = _run_cli_subprocess()
        self.assertEqual(rc, 2)
        self.assertIn("--analysis-id", stderr)

    def test_save_and_dry_run_mutually_exclusive(self):
        rc, _stdout, stderr = _run_cli_subprocess(
            "--analysis-id", "1",
            "--dry-run", "--save",
        )
        self.assertEqual(rc, 2)
        self.assertIn("mutually exclusive", stderr)


# ---------------------------------------------------------------------------
# Q / R. Static safety
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def _import_lines(self, path):
        text = path.read_text(encoding="utf-8")
        return [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]

    def test_linker_does_not_import_network_or_openai(self):
        joined = "\n".join(self._import_lines(LINKER_MODULE_PATH))
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"artifact_evidence_linker.py must not import {forbidden!r}",
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
                f"link_artifact_evidence.py must not import {forbidden!r}",
            )

    def test_linker_not_imported_by_pipeline_entry_points(self):
        for module_name in ("main.py", "api_server.py", "scheduler.py"):
            module_path = ROOT / module_name
            if not module_path.exists():
                continue
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "artifact_evidence_linker", text,
                f"{module_name} must not import artifact_evidence_linker",
            )
            self.assertNotIn(
                "link_artifact_evidence", text,
                f"{module_name} must not import link_artifact_evidence",
            )


# ---------------------------------------------------------------------------
# Tokenize unit tests
# ---------------------------------------------------------------------------


class TokenizeUnitTests(unittest.TestCase):
    def test_basic_lowercase_split(self):
        self.assertEqual(
            linker.tokenize("Hello, World! Foo-bar."),
            ["hello", "world", "foo", "bar"],
        )

    def test_drops_short_tokens(self):
        # 'a' and 'i' both drop (len < MIN_TOKEN_LEN).
        self.assertEqual(
            linker.tokenize("a big idea i had"),
            ["big", "idea", "had"],
        )

    def test_korean_tokens_preserved(self):
        toks = linker.tokenize("청년, 대출 한도 (확대).")
        self.assertIn("청년", toks)
        self.assertIn("대출", toks)
        self.assertIn("한도", toks)
        self.assertIn("확대", toks)

    def test_empty_input(self):
        self.assertEqual(linker.tokenize(""), [])
        self.assertEqual(linker.tokenize(None), [])


if __name__ == "__main__":
    unittest.main()
