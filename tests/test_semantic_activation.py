"""Phase 2 M5.5: activation prep tests.

Covers the new probe script, provider hardening (model + key fail-closed),
runtime metadata, fixture ranking, and verdict isolation. None of these
tests require OpenAI, network, or Postgres.
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

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
import semantic_embeddings
import semantic_evidence_agent


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "semantic_activation_cases.json"
PROBE_SCRIPT = ROOT / "scripts" / "probe_semantic_matching.py"


@contextmanager
def _env(**overrides: str):
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


@contextmanager
def _temporary_sqlite_db():
    import gc

    fd, raw_path = tempfile.mkstemp(suffix=".db", prefix="m5_5_test_")
    os.close(fd)
    new_path = Path(raw_path)
    original = database.DB_PATH
    database.DB_PATH = new_path
    try:
        database.init_db()
        yield new_path
    finally:
        database.DB_PATH = original
        gc.collect()
        try:
            new_path.unlink()
        except (FileNotFoundError, PermissionError, OSError):
            pass


def _run_probe(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the probe script as a subprocess so we exercise the real CLI surface."""
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
        [sys.executable, str(PROBE_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )


class ProbeScriptTests(unittest.TestCase):
    def test_deterministic_mode_succeeds_without_openai_key(self):
        # Clear all OpenAI-related env so the probe can't accidentally rely on them.
        with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
            result = _run_probe(
                "--provider", "deterministic",
                "--max-cases", "3",
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("provider=deterministic-hash", result.stdout)
        self.assertIn("best_support_level=", result.stdout)
        # Conservative disclaimer must always be present when matching runs.
        self.assertIn("metadata only", result.stdout)

    def test_openai_no_network_returns_unavailable_without_call(self):
        # Even with OPENAI_API_KEY accidentally set, --no-network must prevent
        # any live request. We strip the key inside the script when --no-network
        # is paired with --provider openai, so the result should be the same.
        with _env(OPENAI_API_KEY="bogus-key", EMBEDDING_MODEL="bogus-model"):
            result = _run_probe(
                "--provider", "openai",
                "--no-network",
                "--fail-on-unavailable",
                "--max-cases", "1",
            )
        # --fail-on-unavailable triggers exit code 2 when provider unavailable.
        self.assertEqual(result.returncode, 2)
        self.assertIn("provider=openai", result.stdout)
        self.assertIn("available=False", result.stdout)
        # No live call happened: total elapsed should be near-zero ms.
        self.assertIn("[probe] FAIL", result.stderr)

    def test_json_out_writes_full_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "summary.json"
            result = _run_probe(
                "--provider", "deterministic",
                "--max-cases", "1",
                "--json-out", str(out_path),
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(out_path.exists())
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertIn("provider_status", payload)
            self.assertIn("cases", payload)
            self.assertGreater(len(payload["cases"]), 0)
            first = payload["cases"][0]
            self.assertIn("summary", first)
            self.assertIn("runtime_ms", first["summary"])


class OpenAIProviderHardeningTests(unittest.TestCase):
    def test_missing_api_key_reports_unavailable(self):
        with _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="openai",
            EMBEDDING_MODEL="text-embedding-3-small",
            OPENAI_API_KEY=None,
        ):
            provider = semantic_embeddings.get_active_provider()
            status = provider.provider_status()
            self.assertEqual(status["provider"], "openai")
            self.assertFalse(status["available"])
            self.assertFalse(status["configured"])
            self.assertTrue(status["external_calls_possible"])
            self.assertIn("OPENAI_API_KEY", status["reason"])
            # Most importantly: no exception.
            self.assertIsNone(provider.get_embedding("anything"))

    def test_missing_embedding_model_reports_unavailable(self):
        # Set a fake key so we get past the API key check and into model check.
        with _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="openai",
            EMBEDDING_MODEL=None,
            OPENAI_API_KEY="sk-fake-doesnt-leave-process",
        ):
            provider = semantic_embeddings.get_active_provider()
            status = provider.provider_status()
            self.assertFalse(status["available"])
            self.assertFalse(status["configured"])
            self.assertIn("EMBEDDING_MODEL", status["reason"])
            # No client should have been created -> no embedding call attempted.
            self.assertIsNone(provider.get_embedding("anything"))

    def test_provider_status_is_json_safe(self):
        with _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="openai",
            OPENAI_API_KEY=None,
            EMBEDDING_MODEL=None,
        ):
            provider = semantic_embeddings.get_active_provider()
            json.dumps(provider.provider_status())  # must not raise

    def test_batch_tolerates_individual_failures(self):
        # Disabled provider returns None for every input; batch must yield
        # [None, None, None] without raising.
        with _env(SEMANTIC_MATCHING_ENABLED=None):
            provider = semantic_embeddings.get_active_provider()
            self.assertEqual(provider.get_embeddings(["a", "", "b"]), [None, None, None])

    def test_provider_status_does_not_leak_api_key(self):
        with _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="openai",
            EMBEDDING_MODEL="text-embedding-3-small",
            OPENAI_API_KEY="sk-fake-secret-1234567890",
        ):
            provider = semantic_embeddings.get_active_provider()
            status_json = json.dumps(provider.provider_status())
            self.assertNotIn("sk-fake-secret-1234567890", status_json)


class FixtureRankingTests(unittest.TestCase):
    def test_fixture_file_exists_and_parses(self):
        self.assertTrue(FIXTURE_PATH.exists(), f"fixture not found: {FIXTURE_PATH}")
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(cases, list)
        self.assertGreaterEqual(len(cases), 3)

    def test_related_source_outranks_unrelated_with_deterministic_provider(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        case = next(c for c in cases if c["case_id"] == "housing_related_vs_unrelated")
        # The agent populates ``source_id`` from the source URL, so we
        # discriminate by a substring of that URL rather than the fixture's
        # logical id.
        related_marker = case["expected"]["related_source_url_contains"]

        with _temporary_sqlite_db(), _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="deterministic",
        ):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": case["claim_text"]}],
                source_candidates=case["sources"],
                evidence_snippets=[],
            )
            self.assertTrue(summary["semantic_matching_available"])
            top_matches = summary["claim_matches"][0]["top_matches"]
            self.assertTrue(top_matches, "expected at least one ranked match")

            def _is_from_related(match: dict) -> bool:
                blob = (
                    str(match.get("source_id") or "")
                    + " "
                    + str(match.get("source_url") or "")
                )
                return related_marker in blob

            top = top_matches[0]
            self.assertTrue(
                _is_from_related(top),
                f"top match was not from the related source: {top}",
            )
            unrelated_scores = [m["score"] for m in top_matches if not _is_from_related(m)]
            if unrelated_scores:
                self.assertGreater(top["score"], max(unrelated_scores))

    def test_no_official_body_case_marks_unavailable(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        case = next(c for c in cases if c["case_id"] == "no_official_body")
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": case["claim_text"]}],
                source_candidates=case["sources"],
                evidence_snippets=[],
            )
            self.assertEqual(summary["best_support_level"], "unavailable")
            self.assertTrue(any("no official body text" in line for line in summary["limitations"]))


class RuntimeMetadataTests(unittest.TestCase):
    def test_summary_includes_runtime_metadata(self):
        with _temporary_sqlite_db(), _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="deterministic",
        ):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": "공식 발표"}],
                source_candidates=[{
                    "official_body_text": "공식 발표 본문",
                    "url": "https://www.fsc.go.kr/x",
                    "title": "공식",
                }],
                evidence_snippets=[],
            )
            self.assertIn("runtime_ms", summary)
            self.assertIsInstance(summary["runtime_ms"], int)
            self.assertGreaterEqual(summary["runtime_ms"], 0)
            self.assertIn("provider_status", summary)
            self.assertIn("provider", summary["provider_status"])
            self.assertIn("configured", summary["provider_status"])
            self.assertIn("external_calls_possible", summary["provider_status"])
            self.assertIn("embedding_request_count", summary)
            self.assertGreaterEqual(summary["embedding_request_count"], 1)
            self.assertIn("chunk_count", summary)
            self.assertIn("cache_hits", summary)

    def test_disabled_summary_still_includes_metadata(self):
        with _env(SEMANTIC_MATCHING_ENABLED=None):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": "anything"}],
                source_candidates=[],
                evidence_snippets=[],
            )
            self.assertFalse(summary["semantic_matching_enabled"])
            self.assertIn("runtime_ms", summary)
            self.assertEqual(summary["embedding_request_count"], 0)
            self.assertEqual(summary["cache_hits"], 0)


class VerdictIsolationTests(unittest.TestCase):
    def test_enabling_semantic_does_not_call_verdict_modules(self):
        """Sanity: verdict-side imports must still not depend on semantic state.

        We verify by importing the verdict-side modules and asserting they
        don't reference ``semantic_evidence_summary`` anywhere in their
        source. (The earlier M5 audit also confirmed this; this test pins
        the invariant going forward.)
        """
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists(), f"{module_path} missing")
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn("semantic_evidence_summary", text,
                             f"{module_name}.py must not read semantic_evidence_summary")
            self.assertNotIn("semantic_matching_enabled", text)

    def test_summary_does_not_contain_verified_claim_language(self):
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": "공식 발표"}],
                source_candidates=[{
                    "official_body_text": "공식 발표 공식 발표 공식 발표",
                    "url": "x", "title": "y",
                }],
                evidence_snippets=[],
            )
            payload = json.dumps(summary, ensure_ascii=False).lower()
            for forbidden in ("verified", "확정", "검증 완료"):
                self.assertNotIn(forbidden, payload,
                                 f"summary must not include verdict word: {forbidden}")


class CISafetyTests(unittest.TestCase):
    def test_tests_do_not_require_openai_key(self):
        # Defensive: confirm we can resolve the disabled provider with no key,
        # no model, and no SEMANTIC_MATCHING_ENABLED env var present.
        with _env(
            OPENAI_API_KEY=None,
            EMBEDDING_MODEL=None,
            SEMANTIC_MATCHING_ENABLED=None,
            EMBEDDING_PROVIDER=None,
        ):
            provider = semantic_embeddings.get_active_provider()
            self.assertEqual(provider.name, "disabled")
            self.assertIsNone(provider.get_embedding("text"))


if __name__ == "__main__":
    unittest.main()
