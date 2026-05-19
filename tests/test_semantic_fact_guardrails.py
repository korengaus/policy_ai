"""Phase 2 M5.7: critical-fact guardrails for semantic evidence matching.

Verifies:
    * extractors are pure / deterministic and never raise on bad input,
    * Korean monetary, date, eligibility, finality, and negation patterns
      produce the expected critical-fact dicts,
    * ``compare_critical_facts`` flags mismatches and proposes the right
      support-level cap,
    * the semantic evidence agent applies the cap to its exposed
      ``support_level`` while preserving the raw value for diagnostics,
    * the calibration evaluator (``semantic_calibration``) carries the new
      guardrail fields through ``evaluate_case`` and
      ``summarize_calibration_results``.

CI-safety contract: no network, no OpenAI key, no Postgres, no temp DB.
"""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import semantic_calibration
import semantic_embeddings
import semantic_evidence_agent
import semantic_fact_guardrails as guardrails


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


class NormalizationTests(unittest.TestCase):
    def test_empty_inputs_return_empty_string(self):
        self.assertEqual(guardrails.normalize_fact_text(None), "")
        self.assertEqual(guardrails.normalize_fact_text(""), "")
        self.assertEqual(guardrails.normalize_fact_text("   "), "")

    def test_unicode_normalization_folds_fullwidth_digits(self):
        # 全角 "１００" should fold to ASCII "100".
        normalized = guardrails.normalize_fact_text("１００만원")
        self.assertIn("100만원", normalized)

    def test_extractors_never_raise_on_bad_input(self):
        for bad in [None, 0, 3.14, [], {}, b"bytes"]:
            guardrails.extract_numbers(bad)
            guardrails.extract_dates(bad)
            guardrails.extract_eligibility_terms(bad)
            guardrails.extract_finality_terms(bad)
            guardrails.extract_negation_terms(bad)
            guardrails.compare_critical_facts(bad, bad)


class NumberExtractionTests(unittest.TestCase):
    def test_extracts_korean_money_with_unit(self):
        nums = guardrails.extract_numbers("정부는 1인당 100만원의 긴급 지원금을 지급한다")
        values = [(n["value"], n["unit"]) for n in nums]
        self.assertIn((100.0, "만원"), values)

    def test_ignores_year_lookalikes(self):
        # 2026 followed by 년 must not be picked up as an amount.
        nums = guardrails.extract_numbers("2026년 5월부터 시행")
        self.assertFalse(any(n["value"] == 2026 for n in nums))

    def test_extracts_comma_grouped_amount(self):
        nums = guardrails.extract_numbers("한도를 5,000만원으로 상향")
        self.assertTrue(any(n["value"] == 5000.0 and n["unit"] == "만원" for n in nums))

    def test_percent_unit_is_recognized(self):
        nums = guardrails.extract_numbers("지원율을 30% 인상한다")
        self.assertTrue(any(n["unit"] == "%" and n["value"] == 30.0 for n in nums))


class DateExtractionTests(unittest.TestCase):
    def test_year_month_pattern(self):
        dates = guardrails.extract_dates("2026년 5월부터 시행한다")
        self.assertTrue(any(d["year"] == 2026 and d["month"] == 5 for d in dates))

    def test_year_only_pattern(self):
        dates = guardrails.extract_dates("2025년 일부 지역에서 시범 운영")
        self.assertTrue(any(d["year"] == 2025 and d["month"] is None for d in dates))

    def test_dash_separated_date(self):
        dates = guardrails.extract_dates("2024-03 시행 안내")
        self.assertTrue(any(d["year"] == 2024 and d["month"] == 3 for d in dates))


class EligibilityTests(unittest.TestCase):
    def test_universal_claim_flagged(self):
        elig = guardrails.extract_eligibility_terms("누구나 신청할 수 있다")
        self.assertTrue(elig["has_universal_claim"])
        self.assertFalse(elig["has_restriction"])

    def test_restriction_claim_flagged(self):
        elig = guardrails.extract_eligibility_terms(
            "소득 기준과 거주 요건을 충족한 가구에 한해 신청을 받는다"
        )
        self.assertTrue(elig["has_restriction"])


class FinalityTests(unittest.TestCase):
    def test_final_terms(self):
        fin = guardrails.extract_finality_terms("정책을 최종 확정했다")
        self.assertTrue(fin["has_finality"])

    def test_tentative_terms(self):
        fin = guardrails.extract_finality_terms("시범 운영 중이며 추후 공지 예정")
        self.assertTrue(fin["has_tentative"])

    def test_negated_finality_does_not_count_as_final(self):
        fin = guardrails.extract_finality_terms("시행 여부는 아직 확정되지 않았다")
        # 확정 should be neutralized because 확정되지 is present.
        self.assertFalse(fin["has_finality"])
        self.assertTrue(fin["has_tentative"])


class NegationTests(unittest.TestCase):
    def test_negation_words(self):
        neg = guardrails.extract_negation_terms("해당 보도는 사실이 아닙니다")
        self.assertTrue(neg["has_negation"])


class ComparisonTests(unittest.TestCase):
    def test_number_mismatch_caps_to_weak(self):
        result = guardrails.compare_critical_facts(
            "정부가 1인당 100만원의 긴급 지원금을 지급한다",
            "정부는 1인당 50만원의 긴급 지원금을 지급한다고 발표했다",
        )
        self.assertIn("number_mismatch", result["risk_flags"])
        self.assertTrue(result["has_critical_mismatch"])
        self.assertEqual(result["support_cap"], "weak")

    def test_date_mismatch_caps_to_weak(self):
        result = guardrails.compare_critical_facts(
            "정부가 2026년 5월부터 청년 주거 안정 지원 제도를 시행한다",
            "정부는 2025년 일부 지역에서 청년 주거 안정 지원 제도의 시범 운영을 시작한다",
        )
        self.assertIn("date_mismatch", result["risk_flags"])
        self.assertEqual(result["support_cap"], "weak")

    def test_eligibility_mismatch_caps_to_weak(self):
        result = guardrails.compare_critical_facts(
            "정부의 신규 주거 지원금은 누구나 신청할 수 있다",
            "정부는 신규 주거 지원금에 대해 가구 소득 기준과 거주 요건을 "
            "충족한 가구에 한해 신청을 받는다",
        )
        self.assertIn("eligibility_mismatch", result["risk_flags"])
        self.assertEqual(result["support_cap"], "weak")

    def test_finality_mismatch_caps_to_weak(self):
        result = guardrails.compare_critical_facts(
            "정부가 청년 월세 보조금 지급 정책을 최종 확정했다",
            "정부는 청년 월세 보조금 지급 정책에 대해 관계 부처 협의를 진행 중이며, "
            "시행 여부는 아직 확정되지 않았다",
        )
        self.assertIn("finality_mismatch", result["risk_flags"])
        self.assertEqual(result["support_cap"], "weak")

    def test_negation_in_source_caps_to_weak(self):
        result = guardrails.compare_critical_facts(
            "정부가 정책을 발표했다",
            "해당 보도는 사실이 아닙니다. 정부 발표는 없었습니다.",
        )
        self.assertIn("negation_mismatch", result["risk_flags"])
        self.assertEqual(result["support_cap"], "weak")

    def test_missing_amount_caps_to_contextual(self):
        result = guardrails.compare_critical_facts(
            "정부가 청년 전세대출 한도를 5천만원 상향한다",
            "주거 금융 정책은 청년, 신혼부부, 중장년 등 생애주기별로 다른 지원 방식을 사용한다",
        )
        self.assertIn("missing_critical_fact", result["risk_flags"])
        self.assertEqual(result["support_cap"], "contextual")
        self.assertFalse(result["has_critical_mismatch"])
        self.assertTrue(result["has_missing_critical_fact"])

    def test_aligned_facts_leave_cap_at_strong(self):
        result = guardrails.compare_critical_facts(
            "정부는 1인당 50만원의 긴급 지원금을 지급한다",
            "정부는 소상공인에게 1인당 50만원의 긴급 지원금을 지급한다고 발표했다",
        )
        self.assertEqual(result["risk_flags"], [])
        self.assertEqual(result["support_cap"], "strong")
        self.assertFalse(result["has_critical_mismatch"])


class CapHelperTests(unittest.TestCase):
    def test_cap_takes_lower_rank(self):
        self.assertEqual(guardrails.cap_support_level("strong", "weak"), "weak")
        self.assertEqual(guardrails.cap_support_level("contextual", "strong"), "contextual")
        self.assertEqual(guardrails.cap_support_level("strong", "contextual"), "contextual")
        self.assertEqual(guardrails.cap_support_level("weak", "strong"), "weak")
        # Unavailable always wins (lowest).
        self.assertEqual(guardrails.cap_support_level("strong", "unavailable"), "unavailable")


class AgentIntegrationTests(unittest.TestCase):
    """The semantic evidence agent must apply guardrails to its summary."""

    def test_number_mismatch_caps_agent_support_level(self):
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            provider = semantic_embeddings.get_active_provider()
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{
                    "claim_text": "정부가 소상공인에게 1인당 100만원의 긴급 지원금을 지급한다",
                }],
                source_candidates=[{
                    "source_id": "src",
                    "title": "소상공인 긴급 지원금 시행 안내",
                    "url": "https://example.go.kr/sme-emergency-aid",
                    "official_body_text": (
                        "정부는 소상공인에게 1인당 50만원의 긴급 지원금을 지급한다고 "
                        "발표했다. 매출 감소 요건을 충족한 사업자가 대상이다."
                    ),
                }],
                evidence_snippets=[],
                provider=provider,
            )
            self.assertTrue(summary["semantic_guardrails_enabled"])
            # Raw label may be strong/contextual/weak depending on cosine; the
            # adjusted label must never be 'strong' because of number_mismatch.
            self.assertNotEqual(summary["best_support_level"], "strong")
            self.assertIn("number_mismatch", summary["semantic_risk_flags"])
            self.assertGreaterEqual(summary["critical_mismatch_count"], 1)
            # The per-claim block must expose both raw and adjusted labels.
            claim = summary["claim_matches"][0]
            self.assertIn("raw_support_level", claim)
            self.assertIn("guardrail_adjusted_support_level", claim)
            self.assertNotEqual(claim["support_level"], "strong")

    def test_aligned_facts_do_not_force_cap(self):
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            provider = semantic_embeddings.get_active_provider()
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{
                    "claim_text": "정부가 소상공인에게 1인당 50만원의 긴급 지원금을 지급한다",
                }],
                source_candidates=[{
                    "source_id": "src",
                    "title": "소상공인 긴급 지원금 시행 안내",
                    "url": "https://example.go.kr/sme-emergency-aid",
                    "official_body_text": (
                        "정부는 소상공인에게 1인당 50만원의 긴급 지원금을 지급한다고 "
                        "발표했다."
                    ),
                }],
                evidence_snippets=[],
                provider=provider,
            )
            # The aligned-fact path must NOT produce a number_mismatch flag.
            self.assertNotIn("number_mismatch", summary["semantic_risk_flags"])
            # Some claims still mention amounts not present in the source — we
            # only assert that there is no *critical* (mismatch) flag.
            for flag in (
                "number_mismatch", "date_mismatch", "eligibility_mismatch",
                "finality_mismatch", "negation_mismatch",
            ):
                self.assertNotIn(flag, summary["semantic_risk_flags"])

    def test_disabled_pipeline_does_not_crash_with_guardrails(self):
        with _env(SEMANTIC_MATCHING_ENABLED=None):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": "공식 발표"}],
                source_candidates=[{
                    "official_body_text": "공식 발표 본문",
                    "url": "https://example.com",
                    "title": "공식",
                }],
                evidence_snippets=[],
            )
            # When disabled the agent short-circuits — the new fields must
            # still be present (and have sensible defaults).
            self.assertIn("semantic_guardrails_enabled", summary)
            self.assertEqual(summary["critical_mismatch_count"], 0)
            self.assertEqual(summary["support_cap_applied_count"], 0)
            self.assertEqual(summary["best_raw_support_level"], "unavailable")


class CalibrationHelperPassthroughTests(unittest.TestCase):
    """The evaluator/helper must carry guardrail fields end-to-end."""

    def test_evaluate_case_reads_guardrail_fields(self):
        summary = {
            "best_support_level": "weak",
            "best_raw_support_level": "strong",
            "best_overall_score_percent": 80,
            "semantic_risk_flags": ["number_mismatch"],
            "critical_mismatch_count": 1,
            "support_cap_applied_count": 1,
            "claim_matches": [],
        }
        evaluation = semantic_calibration.evaluate_case(
            summary,
            {"should_not_be_strong": True, "risk_flags": ["amount_mismatch"]},
        )
        self.assertEqual(evaluation["raw_support_level"], "strong")
        self.assertEqual(evaluation["semantic_risk_flags"], ["number_mismatch"])
        self.assertEqual(evaluation["critical_mismatch_count"], 1)
        self.assertTrue(evaluation["support_cap_applied"])
        # Combined risk_flags should include both expected and guardrail flags.
        self.assertIn("amount_mismatch", evaluation["risk_flags"])
        self.assertIn("number_mismatch", evaluation["risk_flags"])

    def test_summarize_aggregates_guardrail_counts(self):
        rows = [
            {
                "summary": {"runtime_ms": 1, "cache_hits": 0, "embedding_request_count": 1},
                "evaluation": {
                    "passed": True,
                    "support_level": "weak",
                    "raw_support_level": "strong",
                    "support_cap_applied": True,
                    "critical_mismatch_count": 2,
                    "semantic_risk_flags": ["number_mismatch", "missing_critical_fact"],
                    "overstrong": False,
                    "related_top1": True,
                },
            },
            {
                "summary": {"runtime_ms": 1, "cache_hits": 0, "embedding_request_count": 1},
                "evaluation": {
                    "passed": True,
                    "support_level": "strong",
                    "raw_support_level": "strong",
                    "support_cap_applied": False,
                    "critical_mismatch_count": 0,
                    "semantic_risk_flags": [],
                    "overstrong": False,
                    "related_top1": True,
                },
            },
        ]
        scorecard = semantic_calibration.summarize_calibration_results(rows)
        self.assertEqual(scorecard["support_cap_applied_count"], 1)
        self.assertEqual(scorecard["total_critical_mismatches"], 2)
        self.assertEqual(scorecard["raw_support_level_distribution"]["strong"], 2)
        self.assertEqual(scorecard["semantic_risk_flag_counts"]["number_mismatch"], 1)
        self.assertEqual(scorecard["semantic_risk_flag_counts"]["missing_critical_fact"], 1)


class VerdictIsolationTests(unittest.TestCase):
    def test_verdict_modules_do_not_reference_fact_guardrails(self):
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists())
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "semantic_fact_guardrails", text,
                f"{module_name}.py must not import semantic_fact_guardrails",
            )


if __name__ == "__main__":
    unittest.main()
