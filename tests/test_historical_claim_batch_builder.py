"""Phase 2 M7.0: historical claim batch builder tests.

Verifies:
    * the builder handles missing reports / missing SQLite gracefully,
    * it extracts claims and sources from synthetic policy_analysis JSON,
    * URLs and PII are anonymized,
    * important Korean policy terms pass through unmodified,
    * category / risk inference reproduces the documented heuristics,
    * generated output is fixture-compatible with
      ``scripts/evaluate_real_claim_batch.py``,
    * ``--dry-run`` writes nothing, ``--overwrite`` is required to
      replace an existing file,
    * generated case_ids are unique and stable across re-runs,
    * no network, no OpenAI key, no Postgres required.

CI-safety contract: every test uses an isolated ``TemporaryDirectory``
so we never touch the real ``reports/`` or ``policy_ai.db``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BUILDER_SCRIPT = ROOT / "scripts" / "build_historical_claim_batch.py"
EVALUATOR_SCRIPT = ROOT / "scripts" / "evaluate_real_claim_batch.py"


@contextmanager
def _env(**overrides):
    original = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_builder(*args: str, env_extra: Optional[dict] = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        for key, value in env_extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    return subprocess.run(
        [sys.executable, str(BUILDER_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )


def _run_evaluator(*args: str, env_extra: Optional[dict] = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        for key, value in env_extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    return subprocess.run(
        [sys.executable, str(EVALUATOR_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )


def _synth_policy_analysis(*, claim: str, body: str, url: str, title: str = "공식 안내") -> dict:
    """Build a minimal-but-realistic policy_analysis JSON shape so the
    builder's extraction helpers exercise their primary code paths."""
    return {
        "query": "테스트 쿼리",
        "total_news_count": 1,
        "news_results": [{
            "title": title,
            "original_url": "https://example.news.invalid/article",
            "normalized_claims": [{
                "claim_text": claim,
                "actor": "정부",
                "action": "발표",
            }],
            "policy_claims": [{
                "sentence": claim,
                "score": 50,
            }],
            "official_evidence_results": [{
                "source_name": "Example Government",
                "source_type": "central_government",
                "document_title": title,
                "document_text_snippet": body,
                "selected_document_url": url,
                "url": url,
            }],
            "evidence_snippets": [],
        }],
    }


class BuilderHelperTests(unittest.TestCase):
    """Unit-level tests against the builder's pure helpers."""

    def setUp(self):
        import scripts.build_historical_claim_batch as builder
        self.builder = builder

    def test_safe_get_returns_default_on_missing(self):
        obj = {"a": {"b": [1, 2]}}
        self.assertEqual(self.builder.safe_get(obj, ["a", "b", 0]), 1)
        self.assertEqual(self.builder.safe_get(obj, ["a", "z"], "fallback"), "fallback")
        self.assertEqual(self.builder.safe_get(None, ["a"], "x"), "x")

    def test_stable_hash_is_deterministic(self):
        h1 = self.builder.stable_hash("hello world")
        h2 = self.builder.stable_hash("hello world")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, self.builder.stable_hash("hello"))

    def test_truncate_text_respects_max_chars(self):
        out = self.builder.truncate_text("가" * 500, 100)
        # ``…`` ellipsis adds one char; total length must remain <= 101.
        self.assertLessEqual(len(out), 101)
        self.assertEqual(self.builder.truncate_text(None, 100), "")
        self.assertEqual(self.builder.truncate_text("   ", 100), "")

    def test_sanitize_url_strips_query_and_anonymizes(self):
        out = self.builder.sanitize_url(
            "https://www.korea.kr/search/searchList.do?srchKeyword=foo&pageSize=10",
            anonymize=True,
        )
        self.assertTrue(out.startswith("https://example.generated.go.kr/source/"))
        self.assertNotIn("srchKeyword", out)
        self.assertNotIn("pageSize", out)

    def test_sanitize_url_uses_city_specific_synthetic_host(self):
        out = self.builder.sanitize_url(
            "https://www.seoul.go.kr/some/path?id=123",
            anonymize=True,
        )
        self.assertTrue(out.startswith("https://example.generated.seoul.go.kr/"))

    def test_sanitize_url_falls_back_to_news_host(self):
        out = self.builder.sanitize_url(
            "http://www.breaknews.com/article/1234",
            anonymize=True,
        )
        self.assertTrue(out.startswith("https://example.generated.news/"))

    def test_sanitize_text_strips_email(self):
        out = self.builder.sanitize_text("문의: hello@example.com", max_chars=200)
        self.assertNotIn("hello@example.com", out)
        self.assertIn("[이메일]", out)

    def test_sanitize_text_strips_phone_and_rid(self):
        out = self.builder.sanitize_text(
            "연락처 010-1234-5678 또는 02-555-1234 / 주민 880101-1234567",
            max_chars=400,
        )
        self.assertNotIn("010-1234-5678", out)
        self.assertNotIn("02-555-1234", out)
        self.assertNotIn("880101-1234567", out)
        self.assertIn("[전화번호]", out)
        self.assertIn("[주민번호]", out)

    def test_sanitize_text_redacts_korean_name_with_honorific(self):
        out = self.builder.sanitize_text("김철수씨가 발언했다", max_chars=200)
        self.assertNotIn("김철수씨", out)
        self.assertIn("[이름]씨", out)

    def test_sanitize_text_preserves_policy_terms(self):
        # The sanitizer must NOT strip policy vocabulary — that vocabulary
        # is exactly what makes a calibration case useful.
        out = self.builder.sanitize_text(
            "정부가 전세사기 피해자에게 보조금과 대출 한도 상향을 시행한다",
            max_chars=400,
        )
        for term in ["정부", "전세사기", "보조금", "대출", "시행"]:
            self.assertIn(term, out)


class CategoryInferenceTests(unittest.TestCase):
    def setUp(self):
        import scripts.build_historical_claim_batch as builder
        self.infer = builder.infer_category_and_risk

    def test_no_body_short_circuits_to_no_body(self):
        out = self.infer("정부가 청년 보조금을 신설했다.", "")
        self.assertEqual(out["category"], "no_body")
        self.assertIn("official_body_missing", out["risk_flags"])
        self.assertTrue(out["should_be_unavailable_when_no_body"])

    def test_number_mismatch_inferred(self):
        out = self.infer(
            "정부가 1인당 100만원의 지원금을 지급한다.",
            "정부는 1인당 50만원의 지원금을 지급한다고 발표했다.",
        )
        self.assertEqual(out["category"], "number_mismatch")
        self.assertIn("number_mismatch", out["risk_flags"])
        self.assertTrue(out["should_not_be_strong"])

    def test_eligibility_mismatch_inferred(self):
        out = self.infer(
            "누구나 신청할 수 있다.",
            "가구 소득 기준을 충족한 가구에 한해 신청을 받는다.",
        )
        self.assertEqual(out["category"], "eligibility_mismatch")
        self.assertIn("eligibility_mismatch", out["risk_flags"])

    def test_finality_mismatch_inferred(self):
        out = self.infer(
            "정부가 정책을 최종 확정했다.",
            "정부는 관계 부처 협의 중이며 시행 여부는 미정이다.",
        )
        self.assertEqual(out["category"], "finality_mismatch")
        self.assertIn("finality_mismatch", out["risk_flags"])

    def test_negation_inferred(self):
        out = self.infer(
            "정부가 보조금을 신설했다.",
            "해당 보조금 신설 보도는 사실이 아닙니다.",
        )
        self.assertEqual(out["category"], "negation_or_refutation")
        self.assertIn("negation_mismatch", out["risk_flags"])

    def test_policy_scope_mismatch_inferred(self):
        out = self.infer(
            "정부가 청년 주거 대출 한도를 확대한다.",
            "정부는 청년 주거 바우처 시행 정책을 안내했다.",
        )
        self.assertEqual(out["category"], "same_topic_wrong_policy")
        self.assertIn("policy_scope_mismatch", out["risk_flags"])

    def test_local_vs_central_inferred(self):
        out = self.infer(
            "정부가 전국 영유아 가구에 보육 지원금을 신설한다.",
            "서울시는 시 거주 영유아 가구에 보육 지원금을 지급한다고 안내했다.",
        )
        self.assertEqual(out["category"], "local_vs_central_authority")
        # Either flag from the M6.6 vocabulary satisfies the documentation goal.
        self.assertTrue(
            "actor_scope_mismatch" in out["risk_flags"]
            or "local_vs_central" in out["risk_flags"]
        )

    def test_unknown_historical_fallback(self):
        # Clean-aligned text — no guardrail flag, no missing critical fact.
        out = self.infer(
            "정부가 공정거래위원회를 통해 환불 의무를 강화한다.",
            "공정거래위원회는 환불 의무 강화 정책을 시행한다고 안내했다.",
        )
        self.assertEqual(out["category"], "unknown_historical")
        self.assertIn("heuristic_unknown_historical", out["risk_flags"])
        self.assertFalse(out["should_not_be_strong"])


class BuilderEndToEndTests(unittest.TestCase):
    """Subprocess-level tests against the builder CLI using temp fixtures."""

    def _setup_synthetic_reports(self, tmp: Path) -> Path:
        reports_dir = tmp / "reports"
        reports_dir.mkdir()
        # One report per category so the generated batch is rich.
        scenarios = [
            ("number", "정부가 1인당 100만원의 지원금을 지급한다.",
             "정부는 1인당 50만원의 지원금을 지급한다고 발표했다.",
             "https://www.korea.kr/news?id=1&token=secret"),
            ("eligibility", "누구나 임대료 보조를 신청할 수 있다.",
             "가구 소득 기준을 충족한 가구에 한해 신청을 받는다.",
             "https://www.molit.go.kr/eligibility?session=abc"),
            ("finality", "정부가 정책을 최종 확정했다.",
             "정부는 관계 부처 협의 중이며 시행 여부는 미정이다.",
             "https://www.mosf.go.kr/policy?ref=foo"),
            ("scope_loan_voucher", "정부가 청년 주거 대출 한도를 확대한다.",
             "정부는 청년 주거 바우처 시행 정책을 안내했다.",
             "https://www.molit.go.kr/voucher?id=42"),
            ("local_vs_central", "정부가 전국 보육 지원금을 신설한다.",
             "서울시는 시 거주 영유아 가구에 보육 지원금을 지급한다고 안내했다.",
             "https://www.seoul.go.kr/childcare?city=seoul"),
            ("clean_direct", "공정거래위원회가 환불 의무를 강화한다.",
             "공정거래위원회는 환불 의무 강화 정책을 시행한다고 안내했다.",
             "https://www.ftc.go.kr/refund?lang=ko"),
        ]
        for i, (slug, claim, body, url) in enumerate(scenarios):
            report = _synth_policy_analysis(
                claim=claim, body=body, url=url, title=f"공식 안내 {slug}"
            )
            (reports_dir / f"policy_analysis_20260520_{i:06d}.json").write_text(
                json.dumps(report, ensure_ascii=False),
                encoding="utf-8",
            )
        return reports_dir

    def test_dry_run_writes_no_output(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            reports_dir = self._setup_synthetic_reports(tmp)
            output = tmp / "batch.generated.json"
            summary = tmp / "summary.md"
            result = _run_builder(
                "--reports-dir", str(reports_dir),
                "--sqlite-db", str(tmp / "does-not-exist.db"),
                "--output", str(output),
                "--summary-out", str(summary),
                "--source", "reports",
                "--dry-run",
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertFalse(output.exists(), "dry-run must not write output")
            self.assertFalse(summary.exists(), "dry-run must not write summary")
            self.assertIn("--dry-run: no file written", result.stdout)

    def test_existing_output_requires_overwrite(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            reports_dir = self._setup_synthetic_reports(tmp)
            output = tmp / "batch.generated.json"
            output.write_text("[]", encoding="utf-8")
            result = _run_builder(
                "--reports-dir", str(reports_dir),
                "--sqlite-db", str(tmp / "does-not-exist.db"),
                "--output", str(output),
                "--source", "reports",
            )
            self.assertEqual(result.returncode, 2, msg=result.stderr or result.stdout)
            self.assertIn("--overwrite", result.stderr)
            # Original placeholder file untouched.
            self.assertEqual(output.read_text(encoding="utf-8"), "[]")

    def test_missing_reports_and_sqlite_degrade_gracefully(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            output = tmp / "batch.generated.json"
            summary = tmp / "summary.md"
            result = _run_builder(
                "--reports-dir", str(tmp / "no-such-dir"),
                "--sqlite-db", str(tmp / "no-such-db.sqlite"),
                "--output", str(output),
                "--summary-out", str(summary),
                "--source", "both",
            )
            # No reports + no SQLite + no --strict ⇒ exit 0 with 0 cases.
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertIn("emitted=0", result.stdout)
            self.assertTrue(output.exists(), "non-dry-run must write output even when empty")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload, [])

    def test_strict_min_cases_returns_exit_3(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            output = tmp / "batch.generated.json"
            result = _run_builder(
                "--reports-dir", str(tmp / "no-such-dir"),
                "--sqlite-db", str(tmp / "no-such-db.sqlite"),
                "--output", str(output),
                "--source", "both",
                "--strict",
                "--min-cases", "5",
            )
            self.assertEqual(result.returncode, 3, msg=result.stderr or result.stdout)
            self.assertIn("minimum is 5", result.stderr)

    def test_emits_cases_from_synthetic_reports_with_correct_categories(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            reports_dir = self._setup_synthetic_reports(tmp)
            output = tmp / "batch.generated.json"
            summary = tmp / "summary.md"
            result = _run_builder(
                "--reports-dir", str(reports_dir),
                "--sqlite-db", str(tmp / "does-not-exist.db"),
                "--output", str(output),
                "--summary-out", str(summary),
                "--source", "reports",
                "--max-cases", "100",
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertTrue(output.exists())
            self.assertTrue(summary.exists())
            cases = json.loads(output.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(cases), 6)

            categories = {case["category"] for case in cases}
            self.assertIn("number_mismatch", categories)
            self.assertIn("eligibility_mismatch", categories)
            self.assertIn("finality_mismatch", categories)
            self.assertIn("same_topic_wrong_policy", categories)
            self.assertIn("local_vs_central_authority", categories)
            self.assertIn("unknown_historical", categories)

    def test_generated_case_ids_are_unique_and_stable(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            reports_dir = self._setup_synthetic_reports(tmp)
            output = tmp / "batch.generated.json"

            def _build():
                return _run_builder(
                    "--reports-dir", str(reports_dir),
                    "--sqlite-db", str(tmp / "does-not-exist.db"),
                    "--output", str(output),
                    "--summary-out", str(tmp / "summary.md"),
                    "--source", "reports",
                    "--max-cases", "100",
                    "--overwrite",
                )

            self.assertEqual(_build().returncode, 0)
            cases_a = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(_build().returncode, 0)
            cases_b = json.loads(output.read_text(encoding="utf-8"))

            ids_a = [c["case_id"] for c in cases_a]
            ids_b = [c["case_id"] for c in cases_b]
            self.assertEqual(len(ids_a), len(set(ids_a)), "case_ids must be unique")
            self.assertEqual(ids_a, ids_b, "case_ids must be stable across re-runs")

    def test_urls_are_anonymized_and_pii_scrubbed(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            reports_dir = tmp / "reports"
            reports_dir.mkdir()
            report = _synth_policy_analysis(
                claim="정부가 보조금을 시행한다. 문의 hello@example.com.",
                body="정부는 보조금을 시행한다고 안내했다. 연락처 010-1111-2222.",
                url="https://www.korea.kr/path?token=SECRET&key=DROP",
                title="공식 안내",
            )
            (reports_dir / "policy_analysis_20260520_000001.json").write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8",
            )
            output = tmp / "batch.generated.json"
            result = _run_builder(
                "--reports-dir", str(reports_dir),
                "--sqlite-db", str(tmp / "does-not-exist.db"),
                "--output", str(output),
                "--summary-out", str(tmp / "summary.md"),
                "--source", "reports",
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            cases = json.loads(output.read_text(encoding="utf-8"))
            self.assertGreater(len(cases), 0)
            blob = json.dumps(cases, ensure_ascii=False)
            # The synthetic real-host string must not survive.
            self.assertNotIn("korea.kr", blob)
            self.assertNotIn("SECRET", blob)
            self.assertNotIn("token=", blob)
            self.assertNotIn("hello@example.com", blob)
            self.assertNotIn("010-1111-2222", blob)
            # Policy vocabulary must survive.
            self.assertIn("보조금", blob)
            self.assertIn("시행", blob)

    def test_output_is_evaluator_compatible(self):
        # End-to-end: build a batch from synthetic reports, then feed it
        # to evaluate_real_claim_batch.py with the deterministic provider
        # and confirm a clean run.
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            reports_dir = self._setup_synthetic_reports(tmp)
            output = tmp / "batch.generated.json"
            result = _run_builder(
                "--reports-dir", str(reports_dir),
                "--sqlite-db", str(tmp / "does-not-exist.db"),
                "--output", str(output),
                "--summary-out", str(tmp / "summary.md"),
                "--source", "reports",
                "--max-cases", "100",
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertTrue(output.exists())
            with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
                eval_result = _run_evaluator(
                    "--provider", "deterministic",
                    "--no-network",
                    "--case-file", str(output),
                )
            self.assertEqual(
                eval_result.returncode, 0,
                msg=f"stdout:\n{eval_result.stdout}\nstderr:\n{eval_result.stderr}",
            )
            self.assertIn("provider=deterministic-hash", eval_result.stdout)
            self.assertIn("scorecard:", eval_result.stdout)


class CISafetyTests(unittest.TestCase):
    def test_no_openai_key_required(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            output = tmp / "batch.generated.json"
            with _env(
                OPENAI_API_KEY=None,
                EMBEDDING_MODEL=None,
                SEMANTIC_MATCHING_ENABLED=None,
                EMBEDDING_PROVIDER=None,
            ):
                result = _run_builder(
                    "--reports-dir", str(tmp / "no-such-dir"),
                    "--sqlite-db", str(tmp / "no-such-db.sqlite"),
                    "--output", str(output),
                    "--summary-out", str(tmp / "summary.md"),
                    "--source", "both",
                )
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)

    def test_no_postgres_required(self):
        # The builder must not import database.py or touch DATABASE_URL.
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            output = tmp / "batch.generated.json"
            with _env(DATABASE_URL=None):
                result = _run_builder(
                    "--reports-dir", str(tmp / "no-such-dir"),
                    "--sqlite-db", str(tmp / "no-such-db.sqlite"),
                    "--output", str(output),
                    "--summary-out", str(tmp / "summary.md"),
                    "--source", "both",
                )
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)

    def test_builder_module_does_not_import_database(self):
        # Pinning: the builder must not pull database.py / api_server /
        # verdict modules.
        text = (ROOT / "scripts" / "build_historical_claim_batch.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("import database", text)
        self.assertNotIn("import api_server", text)
        self.assertNotIn("import policy_decision", text)
        self.assertNotIn("import policy_scoring", text)
        self.assertNotIn("import verification_card", text)


class VerdictIsolationTests(unittest.TestCase):
    def test_verdict_modules_do_not_reference_historical_builder(self):
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists())
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "build_historical_claim_batch", text,
                f"{module_name}.py must not import build_historical_claim_batch",
            )
            self.assertNotIn(
                "semantic_historical_claim_batch", text,
                f"{module_name}.py must not reference the generated batch path",
            )


if __name__ == "__main__":
    unittest.main()
