"""Phase 2 M7.2: semantic canary metrics tests.

Verifies:
    * recursive extraction finds every ``semantic_evidence_summary``
      blob in nested payloads,
    * the aggregator computes counts, distributions, runtime stats,
      cap_ratio, and provider-error counts correctly,
    * ``overstrong_like`` is strictly conservative (fires only when raw
      AND adjusted are strong AND risk flags / critical_mismatch_count
      indicate active risk),
    * health classification follows the documented pass / warn / fail
      rules,
    * the helpers never crash on missing / malformed fields and never
      mutate the input payload,
    * percentile + format helpers behave as specified,
    * no network, no OpenAI key, no Postgres required.

CI-safety: pure unit tests; no subprocess, no live server.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import semantic_canary_metrics as scm  # noqa: E402


def _make_summary(**overrides) -> dict:
    """Helper: build a realistic semantic_evidence_summary dict."""
    base = {
        "semantic_matching_enabled": True,
        "semantic_matching_available": True,
        "provider": "openai",
        "model": "text-embedding-3-small",
        "best_support_level": "weak",
        "best_raw_support_level": "weak",
        "best_overall_score_percent": 50,
        "runtime_ms": 150,
        "cache_hits": 3,
        "embedding_request_count": 1,
        "critical_mismatch_count": 0,
        "support_cap_applied_count": 0,
        "semantic_risk_flags": [],
        "errors": [],
        "limitations": [],
    }
    base.update(overrides)
    return base


def _wrap_in_result_payload(*summaries) -> dict:
    """Helper: wrap one or more summaries in a /jobs/{id}/result-style payload."""
    return {
        "status": "ok",
        "result": {
            "results": [
                {
                    "title": f"news {i}",
                    "debug_summary": {"semantic_evidence_summary": s},
                }
                for i, s in enumerate(summaries)
            ],
        },
    }


class ExtractionTests(unittest.TestCase):
    def test_finds_top_level_summary(self):
        payload = {"debug_summary": {"semantic_evidence_summary": _make_summary()}}
        out = scm.extract_semantic_summaries(payload)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["provider"], "openai")

    def test_finds_summary_under_result_results(self):
        # Two summaries with distinct content (different runtimes) must
        # both be extracted. M7.3 added content-hash dedupe — content-
        # identical duplicates (the verification_card vs debug_summary
        # JSON-deserialized duplicate pattern) collapse to one.
        payload = _wrap_in_result_payload(
            _make_summary(runtime_ms=100),
            _make_summary(runtime_ms=200),
        )
        out = scm.extract_semantic_summaries(payload)
        self.assertEqual(len(out), 2)

    def test_content_identical_summaries_dedupe(self):
        # The same logical summary often appears twice in JSON-deserialized
        # result payloads (once under debug_summary, again under
        # verification_card.debug_summary). After JSON round-trip the two
        # dicts have different id() but identical content — they must
        # collapse to one.
        s = _make_summary(runtime_ms=300)
        payload = {
            "result": {
                "results": [{
                    "debug_summary": {"semantic_evidence_summary": json.loads(json.dumps(s))},
                    "verification_card": {
                        "debug_summary": {"semantic_evidence_summary": json.loads(json.dumps(s))},
                    },
                }],
            },
        }
        out = scm.extract_semantic_summaries(payload)
        self.assertEqual(len(out), 1)

    def test_finds_summary_under_verification_card(self):
        # Mimic the alternate shape: nested verification_card → debug_summary.
        payload = {
            "result": {
                "results": [{
                    "verification_card": {
                        "debug_summary": {"semantic_evidence_summary": _make_summary()},
                    },
                }],
            },
        }
        out = scm.extract_semantic_summaries(payload)
        self.assertEqual(len(out), 1)

    def test_deduplicates_same_dict_seen_via_multiple_paths(self):
        # The agent's debug_summary may be referenced from more than one
        # place (e.g. verification_card and the top-level news_result).
        # The walker dedupes by identity so we don't double-count.
        summary = _make_summary()
        payload = {
            "a": {"debug_summary": {"semantic_evidence_summary": summary}},
            "b": {"verification_card": {"debug_summary": {"semantic_evidence_summary": summary}}},
        }
        out = scm.extract_semantic_summaries(payload)
        self.assertEqual(len(out), 1)

    def test_handles_missing_payload_safely(self):
        for bad in (None, {}, [], "string", 42, True):
            self.assertEqual(scm.extract_semantic_summaries(bad), [])

    def test_walk_does_not_mutate_input(self):
        original = _wrap_in_result_payload(_make_summary())
        snapshot = copy.deepcopy(original)
        _ = scm.extract_semantic_summaries(original)
        self.assertEqual(original, snapshot)


class SummarizeTests(unittest.TestCase):
    def test_empty_payload_returns_safe_summary(self):
        out = scm.summarize_semantic_canary({})
        self.assertEqual(out["result_count"], 0)
        self.assertEqual(out["semantic_summary_count"], 0)
        self.assertEqual(out["overstrong_like_count"], 0)
        self.assertEqual(out["provider_error_count"], 0)
        self.assertEqual(out["health"], "pass")
        # cap_ratio is a float, default 0.0
        self.assertIsInstance(out["cap_ratio"], float)
        self.assertEqual(out["cap_ratio"], 0.0)

    def test_disabled_summary_does_not_count_as_enabled(self):
        payload = _wrap_in_result_payload(_make_summary(
            semantic_matching_enabled=False,
            semantic_matching_available=False,
        ))
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["semantic_enabled_count"], 0)
        self.assertEqual(out["semantic_available_count"], 0)
        self.assertEqual(out["health"], "pass")

    def test_openai_available_summary_counted_correctly(self):
        payload = _wrap_in_result_payload(_make_summary())
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["semantic_enabled_count"], 1)
        self.assertEqual(out["semantic_available_count"], 1)
        self.assertEqual(out["provider_counts"], {"openai": 1})
        self.assertEqual(out["model_counts"], {"text-embedding-3-small": 1})
        self.assertEqual(out["best_support_distribution"], {"weak": 1})
        self.assertEqual(out["raw_support_distribution"], {"weak": 1})

    def test_cap_ratio_computed_correctly(self):
        # 3 distinct summaries, each with claim_count=1 (single-claim).
        # Total claims = 3, total caps = 2 → cap_ratio = 2/3. M7.3
        # changed the denominator from semantic_enabled_count to
        # total_claim_count when claim_count is reported.
        payload = _wrap_in_result_payload(
            _make_summary(support_cap_applied_count=1, runtime_ms=100, claim_count=1),
            _make_summary(support_cap_applied_count=1, runtime_ms=200, claim_count=1),
            _make_summary(support_cap_applied_count=0, runtime_ms=300, claim_count=1),
        )
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["semantic_enabled_count"], 3)
        self.assertEqual(out["support_cap_applied_count"], 2)
        self.assertAlmostEqual(out["cap_ratio"], 2 / 3, places=3)

    def test_cap_ratio_uses_summary_basis_when_claim_count_missing(self):
        # Legacy / minimal payload path — claim_count absent. Falls back
        # to semantic_enabled_count as the denominator.
        payload = _wrap_in_result_payload(
            _make_summary(support_cap_applied_count=1, runtime_ms=100),
            _make_summary(support_cap_applied_count=1, runtime_ms=200),
            _make_summary(support_cap_applied_count=0, runtime_ms=300),
        )
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["semantic_enabled_count"], 3)
        self.assertAlmostEqual(out["cap_ratio"], 2 / 3, places=3)

    def test_per_claim_overstrong_does_not_fire_when_other_claim_capped(self):
        # The M7.3 local canary surfaced this exact failure mode in the
        # old summary-level check: a multi-claim summary with one clean
        # strong claim AND one different claim that was correctly capped
        # to contextual was being flagged as overstrong. It must NOT be.
        payload = _wrap_in_result_payload(_make_summary(
            best_support_level="strong",
            best_raw_support_level="strong",
            critical_mismatch_count=2,
            support_cap_applied_count=1,
            semantic_risk_flags=["missing_critical_fact"],
            claim_matches=[
                # Claim that was correctly capped — has flags, NOT strong.
                {
                    "support_level": "contextual",
                    "raw_support_level": "strong",
                    "support_cap_applied": True,
                    "semantic_risk_flags": ["missing_critical_fact"],
                },
                # Unrelated clean claim — strong with no flags.
                {
                    "support_level": "strong",
                    "raw_support_level": "strong",
                    "support_cap_applied": False,
                    "semantic_risk_flags": [],
                },
            ],
        ))
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["overstrong_like_count"], 0)
        # support_cap_applied_count and critical_mismatch_count still
        # surface — they're the right signal that the guardrail did its
        # job — but they don't drive the fail classification.

    def test_per_claim_overstrong_fires_when_strong_claim_has_uncapped_flags(self):
        # The genuine M6.5-style failure mode: a strong claim with its
        # own risk flags and NO cap applied. Must fire.
        payload = _wrap_in_result_payload(_make_summary(
            best_support_level="strong",
            best_raw_support_level="strong",
            critical_mismatch_count=1,
            support_cap_applied_count=0,
            semantic_risk_flags=["policy_scope_mismatch"],
            claim_matches=[
                {
                    "support_level": "strong",
                    "raw_support_level": "strong",
                    "support_cap_applied": False,
                    "semantic_risk_flags": ["policy_scope_mismatch"],
                },
            ],
        ))
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["overstrong_like_count"], 1)
        self.assertEqual(out["health"], "fail")

    def test_runtime_avg_and_p95_computed(self):
        payload = _wrap_in_result_payload(
            _make_summary(runtime_ms=100),
            _make_summary(runtime_ms=200),
            _make_summary(runtime_ms=300),
            _make_summary(runtime_ms=400),
            _make_summary(runtime_ms=500),
            _make_summary(runtime_ms=600),
            _make_summary(runtime_ms=700),
            _make_summary(runtime_ms=800),
            _make_summary(runtime_ms=900),
            _make_summary(runtime_ms=1000),
        )
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["runtime_ms_avg"], 550)
        # p95 of 10 evenly-spaced values [100..1000] = 100 + 0.95*(10-1)*100 = 955
        self.assertEqual(out["runtime_ms_p95"], 955)

    def test_provider_errors_produce_health_fail(self):
        payload = _wrap_in_result_payload(_make_summary(
            errors=["OpenAI embedding call failed: RateLimitError"],
        ))
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["provider_error_count"], 1)
        self.assertEqual(out["health"], "fail")

    def test_overstrong_like_fires_on_strong_strong_with_risk_flag(self):
        # Raw=strong + adjusted=strong + risk flag → the M6.5-style
        # failure mode this canary is built to detect.
        payload = _wrap_in_result_payload(_make_summary(
            best_support_level="strong",
            best_raw_support_level="strong",
            semantic_risk_flags=["policy_scope_mismatch"],
            critical_mismatch_count=1,
        ))
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["overstrong_like_count"], 1)
        self.assertEqual(out["health"], "fail")

    def test_overstrong_like_does_not_fire_when_cap_applied(self):
        # The post-M6.6 expected path: raw=strong, adjusted=weak,
        # risk_flags present. NOT overstrong — the guardrail did its job.
        payload = _wrap_in_result_payload(_make_summary(
            best_support_level="weak",
            best_raw_support_level="strong",
            semantic_risk_flags=["policy_scope_mismatch"],
            support_cap_applied_count=1,
            critical_mismatch_count=1,
        ))
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["overstrong_like_count"], 0)
        self.assertEqual(out["support_cap_applied_count"], 1)
        # Single case with one cap → cap_ratio = 1.0 → triggers warn.
        # The point: warn, not fail. The guardrail did its job.
        self.assertEqual(out["health"], "warn")

    def test_clean_strong_does_not_fire_overstrong_like(self):
        # Raw=strong, adjusted=strong, NO risk flags, NO critical
        # mismatches — a legitimate direct_support match. Must NOT
        # fire the overstrong_like signal.
        payload = _wrap_in_result_payload(_make_summary(
            best_support_level="strong",
            best_raw_support_level="strong",
            semantic_risk_flags=[],
            critical_mismatch_count=0,
        ))
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["overstrong_like_count"], 0)
        self.assertEqual(out["health"], "pass")

    def test_high_cap_ratio_warns_but_does_not_fail(self):
        # 5 summaries, 5 caps applied → cap_ratio = 1.0 > WARN_CAP_RATIO.
        payload = _wrap_in_result_payload(
            *[_make_summary(support_cap_applied_count=1) for _ in range(5)]
        )
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["cap_ratio"], 1.0)
        self.assertEqual(out["health"], "warn")

    def test_high_runtime_p95_warns(self):
        payload = _wrap_in_result_payload(
            *[_make_summary(runtime_ms=2000) for _ in range(5)]
        )
        out = scm.summarize_semantic_canary(payload)
        self.assertGreater(out["runtime_ms_p95"], scm.WARN_RUNTIME_MS_P95)
        self.assertEqual(out["health"], "warn")

    def test_enabled_but_unavailable_warns(self):
        # The "configured but not actually working" pattern — e.g.
        # OPENAI_API_KEY revoked or rate-limited.
        payload = _wrap_in_result_payload(_make_summary(
            semantic_matching_enabled=True,
            semantic_matching_available=False,
        ))
        out = scm.summarize_semantic_canary(payload)
        self.assertEqual(out["semantic_enabled_count"], 1)
        self.assertEqual(out["semantic_available_count"], 0)
        self.assertEqual(out["health"], "warn")

    def test_missing_fields_do_not_crash(self):
        # Sparse summary with most fields missing.
        payload = {
            "result": {
                "results": [{
                    "debug_summary": {
                        "semantic_evidence_summary": {
                            "semantic_matching_enabled": True,
                        },
                    },
                }],
            },
        }
        out = scm.summarize_semantic_canary(payload)
        # Should populate everything to safe defaults without raising.
        self.assertEqual(out["semantic_summary_count"], 1)
        self.assertEqual(out["semantic_enabled_count"], 1)
        self.assertEqual(out["runtime_ms_avg"], 0)
        self.assertEqual(out["health"], "warn")  # enabled but unavailable=0

    def test_summary_does_not_mutate_payload(self):
        payload = _wrap_in_result_payload(_make_summary())
        snapshot = copy.deepcopy(payload)
        _ = scm.summarize_semantic_canary(payload)
        self.assertEqual(payload, snapshot)


class HealthClassificationTests(unittest.TestCase):
    def test_classify_returns_pass_for_clean_summary(self):
        out = scm.classify_canary_health({
            "provider_error_count": 0,
            "overstrong_like_count": 0,
            "cap_ratio": 0.1,
            "runtime_ms_p95": 200,
            "semantic_enabled_count": 1,
            "semantic_available_count": 1,
        })
        self.assertEqual(out["health"], "pass")
        self.assertEqual(out["reasons"], [])

    def test_provider_error_overrides_warn(self):
        # Even with everything else clean, any provider error → fail.
        out = scm.classify_canary_health({
            "provider_error_count": 1,
            "overstrong_like_count": 0,
            "cap_ratio": 0.0,
            "runtime_ms_p95": 100,
        })
        self.assertEqual(out["health"], "fail")
        self.assertTrue(any("provider_error_count" in r for r in out["reasons"]))

    def test_overstrong_overrides_warn(self):
        out = scm.classify_canary_health({
            "provider_error_count": 0,
            "overstrong_like_count": 1,
            "cap_ratio": 0.0,
            "runtime_ms_p95": 100,
        })
        self.assertEqual(out["health"], "fail")

    def test_fail_takes_precedence_over_warn(self):
        # All warn triggers + a fail trigger → fail.
        out = scm.classify_canary_health({
            "provider_error_count": 1,
            "overstrong_like_count": 1,
            "cap_ratio": 1.0,
            "runtime_ms_p95": 9999,
        })
        self.assertEqual(out["health"], "fail")


class FormatterTests(unittest.TestCase):
    def test_format_summary_line_is_stable(self):
        out = scm.format_summary_line({
            "result_count": 3, "semantic_summary_count": 3,
            "semantic_enabled_count": 3, "semantic_available_count": 3,
            "provider_error_count": 0, "overstrong_like_count": 0,
            "cap_ratio": 0.25, "runtime_ms_p95": 200, "health": "pass",
        })
        self.assertIn("result_count=3", out)
        self.assertIn("health=pass", out)
        self.assertIn("cap_ratio=0.250", out)
        self.assertNotIn("verified", out.lower())

    def test_format_markdown_report_includes_disclaimer(self):
        text = scm.format_markdown_report({
            "result_count": 1, "semantic_enabled_count": 1,
            "health": "pass",
        }, base_url="http://127.0.0.1:8000")
        self.assertIn("# Semantic Debug Canary Report", text)
        self.assertIn("metadata only", text)
        self.assertIn("authoritative", text)
        self.assertIn("http://127.0.0.1:8000", text)
        # Must never claim verification.
        self.assertNotIn("verified", text.lower())


class PercentileTests(unittest.TestCase):
    def test_empty_returns_zero(self):
        self.assertEqual(scm.percentile([], 95), 0.0)

    def test_single_value_returns_self(self):
        self.assertEqual(scm.percentile([42.0], 50), 42.0)
        self.assertEqual(scm.percentile([42.0], 100), 42.0)

    def test_known_percentile(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertAlmostEqual(scm.percentile(values, 50), 30.0)
        self.assertAlmostEqual(scm.percentile(values, 100), 50.0)
        self.assertAlmostEqual(scm.percentile(values, 0), 10.0)

    def test_clamps_out_of_range_p(self):
        values = [10.0, 20.0]
        self.assertAlmostEqual(scm.percentile(values, -50), 10.0)
        self.assertAlmostEqual(scm.percentile(values, 200), 20.0)


class VerdictIsolationTests(unittest.TestCase):
    def test_verdict_modules_do_not_reference_canary_metrics(self):
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists())
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "semantic_canary_metrics", text,
                f"{module_name}.py must not import semantic_canary_metrics",
            )


if __name__ == "__main__":
    unittest.main()
