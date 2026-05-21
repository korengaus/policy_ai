"""Phase 2 M10.1: tests for ``scripts/classify_source_url.py``.

Every CLI test invokes the script via subprocess so it runs against
its own real arg-parsing + exit-code logic. Unit tests use the
module's internal helpers where assertions are easier (status mapping,
summary counts, etc.). No network calls, no OpenAI calls, no Render
calls.

Covers the M10.1 spec cases A–K:
    A. Matched URL → MATCHED, allowed=True, exit 0.
    B. Unknown URL → NO_MATCH, exit 1.
    C. Rejected URL (credentialed) → REJECTED, exit 1.
    D. --json with matched → valid JSON, summary.matched=1,
       summary.all_matched_safely=true.
    E. --json with no_match → valid JSON, summary.no_match=1,
       summary.all_matched_safely=false.
    F. No URLs provided → exit 2.
    G. Multiple URLs all matched → exit 0, counts correct.
    H. Multiple URLs with mix → exit 1, counts correct.
    I. --registry-path pointing to a temp file → loads.
    J. Human-readable output never asserts truth.
    K. A single no_match in a batch forces exit 1 even when others
       match.

The matched fixture URL is ``https://www.law.go.kr/test``, which
points at the real ``kr_law_open_data_candidate`` entry in
``data/source_registry.json`` (verified by ``DefaultRegistryTests``
below).
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import source_registry as registry_mod  # noqa: E402
import scripts.classify_source_url as cli  # noqa: E402


CLI_SCRIPT = ROOT / "scripts" / "classify_source_url.py"
SEED_REGISTRY = ROOT / "data" / "source_registry.json"

# Real registry entry used for "matched" test cases. Pinned by the
# fixture-sanity test below.
MATCHED_FIXTURE_URL = "https://www.law.go.kr/test"
MATCHED_FIXTURE_SOURCE_ID = "kr_law_open_data_candidate"

# Real registry entry that won't match anything.
UNKNOWN_FIXTURE_URL = "https://unknown-example.com/page"

# Per-invocation timeout for subprocess-based tests. Conservative; the
# CLI does no I/O beyond reading the registry JSON.
CLI_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli_subprocess(*args, timeout=CLI_TIMEOUT_SECONDS):
    """Run the CLI script via subprocess; return (rc, stdout, stderr).

    Uses ``sys.executable`` so the subprocess Python matches the test
    runner's Python (avoids accidentally hitting the system Python on
    CI). Times out at 5 seconds — the CLI must be fast enough that
    every test in this file finishes well inside that budget."""
    completed = subprocess.run(
        [sys.executable, str(CLI_SCRIPT)] + [str(a) for a in args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _run_cli_inproc(argv):
    """Run the CLI's ``main()`` in-process — fast for assertion-heavy
    tests where subprocess overhead is unwanted. Returns (rc, stdout,
    stderr)."""
    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
    except SystemExit as e:
        rc = int(e.code) if e.code is not None else 0
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Fixture sanity — confirm the matched URL really belongs to the seed
# ---------------------------------------------------------------------------


class FixtureSanityTests(unittest.TestCase):
    """Pins the assumption that the matched-fixture URL points at the
    real seed entry. If a future registry edit removes
    ``kr_law_open_data_candidate`` or changes its allowed domains,
    this test will fail first — fix the fixtures before re-running
    the rest of the suite."""

    def test_matched_fixture_resolves_to_real_seed_entry(self):
        registry = registry_mod.load_source_registry(SEED_REGISTRY)
        result = registry_mod.classify_url_against_registry(
            registry, MATCHED_FIXTURE_URL,
        )
        self.assertEqual(result["matched_source_id"],
                         MATCHED_FIXTURE_SOURCE_ID)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "matched")

    def test_unknown_fixture_does_not_resolve(self):
        registry = registry_mod.load_source_registry(SEED_REGISTRY)
        result = registry_mod.classify_url_against_registry(
            registry, UNKNOWN_FIXTURE_URL,
        )
        self.assertIsNone(result["matched_source_id"])
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "no_match")


# ---------------------------------------------------------------------------
# A — Matched URL → MATCHED, allowed, exit 0
# ---------------------------------------------------------------------------


class MatchedUrlTests(unittest.TestCase):
    def test_matched_url_human_output(self):
        rc, stdout, stderr = _run_cli_subprocess(MATCHED_FIXTURE_URL)
        self.assertEqual(rc, 0, msg=stdout + "\n" + stderr)
        self.assertIn("Status: MATCHED", stdout)
        self.assertIn("source_id: kr_law_open_data_candidate", stdout)
        self.assertIn("source_type: law_or_regulation", stdout)
        self.assertIn("allowed: True", stdout)
        self.assertIn("capture_method: manual_or_http", stdout)
        self.assertIn("browser_automation: maybe_required", stdout)
        # Safety notes present in both [Important] and [Safety] blocks.
        self.assertIn(
            "official_source_candidate does not imply truth", stdout,
        )
        self.assertIn(
            "No scraping or crawling is performed", stdout,
        )


# ---------------------------------------------------------------------------
# B — Unknown URL → NO_MATCH, exit 1
# ---------------------------------------------------------------------------


class NoMatchUrlTests(unittest.TestCase):
    def test_unknown_url_is_no_match(self):
        rc, stdout, stderr = _run_cli_subprocess(UNKNOWN_FIXTURE_URL)
        self.assertEqual(rc, 1, msg=stdout + "\n" + stderr)
        self.assertIn("Status: NO_MATCH", stdout)
        self.assertIn("No matching registry entry found.", stdout)
        # Safety notes still printed.
        self.assertIn(
            "official_source_candidate does not imply truth", stdout,
        )


# ---------------------------------------------------------------------------
# C — Rejected URL (credentialed) → REJECTED, exit 1
# ---------------------------------------------------------------------------


class RejectedUrlTests(unittest.TestCase):
    def test_credential_bearing_url_rejected(self):
        rc, stdout, _stderr = _run_cli_subprocess(
            "https://user:pass@www.law.go.kr/test",
        )
        self.assertEqual(rc, 1)
        self.assertIn("Status: REJECTED", stdout)
        self.assertIn("Reason: credentials_in_url", stdout)

    def test_non_https_url_falls_through_to_no_match(self):
        # The helper does not return a rejection reason for arbitrary
        # http URLs — it tries to classify them and finds no_match
        # because no seed source allows http for that host. This pin
        # documents the M10.0 helper's behavior so we don't drift.
        rc, stdout, _stderr = _run_cli_subprocess(
            "http://www.law.go.kr/test",
        )
        self.assertEqual(rc, 1)
        self.assertIn("Status: NO_MATCH", stdout)


# ---------------------------------------------------------------------------
# D + E — --json output shape
# ---------------------------------------------------------------------------


class JsonOutputTests(unittest.TestCase):
    def test_json_matched_url(self):
        rc, stdout, _stderr = _run_cli_subprocess("--json", MATCHED_FIXTURE_URL)
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["cli_version"], "1.0")
        self.assertIn("registry_path", payload)
        self.assertIn("processed_at", payload)
        self.assertEqual(len(payload["results"]), 1)
        result = payload["results"][0]
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(
            result["classification"]["matched_source_id"],
            MATCHED_FIXTURE_SOURCE_ID,
        )
        self.assertTrue(result["classification"]["allowed"])
        self.assertEqual(
            result["classification"]["source_type"],
            "law_or_regulation",
        )
        # Capture plan present + network_fetch_performed=False.
        plan = result["capture_plan"]
        self.assertEqual(plan["capture_method"], "manual_or_http")
        self.assertEqual(plan["browser_automation"], "maybe_required")
        self.assertEqual(plan["plan_status"], "manual_review")
        self.assertFalse(plan["network_fetch_performed"])
        # Summary.
        self.assertEqual(payload["summary"]["matched"], 1)
        self.assertEqual(payload["summary"]["no_match"], 0)
        self.assertTrue(payload["summary"]["all_matched_safely"])

    def test_json_no_match(self):
        rc, stdout, _stderr = _run_cli_subprocess("--json", UNKNOWN_FIXTURE_URL)
        self.assertEqual(rc, 1)
        payload = json.loads(stdout)
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["status"], "NO_MATCH")
        self.assertIsNone(payload["results"][0]["capture_plan"])
        self.assertEqual(payload["summary"]["no_match"], 1)
        self.assertFalse(payload["summary"]["all_matched_safely"])

    def test_json_includes_safety_notes(self):
        rc, stdout, _stderr = _run_cli_subprocess("--json", MATCHED_FIXTURE_URL)
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        # Per-result safety_note + a top-level safety_notes block.
        self.assertEqual(
            payload["results"][0]["safety_note"],
            "official_source_candidate does not imply truth",
        )
        notes = payload["safety_notes"]
        self.assertIn(
            "official_source_candidate does not imply truth",
            notes["not_truth"],
        )
        self.assertIn(
            "No scraping or crawling is performed",
            notes["no_network"],
        )
        self.assertIn(
            "default_enabled=false",
            notes["review"],
        )


# ---------------------------------------------------------------------------
# F — No URLs → exit 2
# ---------------------------------------------------------------------------


class CliUsageTests(unittest.TestCase):
    def test_no_urls_exits_2(self):
        rc, _stdout, stderr = _run_cli_subprocess()
        self.assertEqual(rc, 2)
        self.assertIn("no URLs provided", stderr)

    def test_help_exits_0(self):
        rc, stdout, _stderr = _run_cli_subprocess("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Classify URLs against", stdout)
        self.assertIn("Exit codes", stdout)

    def test_unrecognized_argument_exits_2(self):
        rc, _stdout, stderr = _run_cli_subprocess(
            "--definitely-not-a-real-flag", MATCHED_FIXTURE_URL,
        )
        self.assertEqual(rc, 2)
        # argparse prints to stderr; the exact message is argparse's.
        self.assertIn("unrecognized arguments", stderr.lower())


# ---------------------------------------------------------------------------
# G + H + K — multiple URLs and conservative exit policy
# ---------------------------------------------------------------------------


class MultipleUrlsTests(unittest.TestCase):
    def test_all_matched_exits_0(self):
        rc, stdout, _stderr = _run_cli_subprocess(
            MATCHED_FIXTURE_URL,
            "https://www.assembly.go.kr/legislation/x",
        )
        self.assertEqual(rc, 0, msg=stdout)
        self.assertEqual(stdout.count("Status: MATCHED"), 2)
        self.assertIn("matched=2", stdout)
        self.assertIn("no_match=0", stdout)

    def test_one_no_match_in_batch_forces_exit_1(self):
        # Even when N-1 URLs match, a single NO_MATCH must force exit 1.
        rc, stdout, _stderr = _run_cli_subprocess(
            MATCHED_FIXTURE_URL,
            UNKNOWN_FIXTURE_URL,
        )
        self.assertEqual(rc, 1, msg=stdout)
        self.assertIn("Status: MATCHED", stdout)
        self.assertIn("Status: NO_MATCH", stdout)
        self.assertIn("matched=1", stdout)
        self.assertIn("no_match=1", stdout)

    def test_mixed_via_repeated_url_flag(self):
        # Positional + --url are combinable.
        rc, stdout, _stderr = _run_cli_subprocess(
            "--url", MATCHED_FIXTURE_URL,
            "--url", UNKNOWN_FIXTURE_URL,
        )
        self.assertEqual(rc, 1)
        self.assertIn("matched=1", stdout)
        self.assertIn("no_match=1", stdout)


# ---------------------------------------------------------------------------
# I — --registry-path with a temp registry file
# ---------------------------------------------------------------------------


class RegistryPathTests(unittest.TestCase):
    def test_temp_registry_loads(self):
        # A minimal valid registry with exactly one source that owns
        # an explicit host. We confirm the CLI can be pointed at any
        # well-formed JSON file (not just the seed) and still match.
        minimal = {
            "schema_version": 1,
            "registry_name": "policy_ai_source_registry",
            "sources": [
                {
                    "source_id": "tmp_fixture_source",
                    "display_name": "Temp fixture source",
                    "source_type": "demo",
                    "jurisdiction": "KR",
                    "base_url": "https://tmp.example.go.kr",
                    "allowed_domains": ["tmp.example.go.kr"],
                    "allow_subdomains": False,
                    "default_enabled": False,
                    "capture_method": "manual_or_http",
                    "browser_automation": "not_required",
                    "operator_review_required": True,
                    "official_source_candidate": False,
                    "truth_claim": False,
                    "notes": "Test fixture (사람 검토 필요).",
                    "tags": ["fixture"],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tmp.json"
            p.write_text(json.dumps(minimal), encoding="utf-8")
            rc, stdout, _stderr = _run_cli_subprocess(
                "--registry-path", str(p),
                "https://tmp.example.go.kr/page",
            )
        self.assertEqual(rc, 0, msg=stdout)
        self.assertIn("source_id: tmp_fixture_source", stdout)

    def test_missing_registry_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "no_such_registry.json"
            rc, _stdout, stderr = _run_cli_subprocess(
                "--registry-path", str(missing),
                MATCHED_FIXTURE_URL,
            )
        self.assertEqual(rc, 1)
        self.assertIn("failed to load registry", stderr)


# ---------------------------------------------------------------------------
# J — Output never asserts truth
# ---------------------------------------------------------------------------


class NoTruthClaimsTests(unittest.TestCase):
    FORBIDDEN_PHRASES = (
        "truth guaranteed",
        "guaranteed truth",
        "verified true",
        "is true",
        "is verified",
        "공식 진실",
        "진실 보장",
        "사실 보장",
        "100% 사실",
    )

    def _assert_no_truth_claims(self, text: str, *, ctx: str) -> None:
        for phrase in self.FORBIDDEN_PHRASES:
            self.assertNotIn(
                phrase, text,
                msg=f"forbidden truth-claim phrase {phrase!r} in {ctx}",
            )

    def test_human_output_carries_no_truth_claim(self):
        rc, stdout, _stderr = _run_cli_subprocess(MATCHED_FIXTURE_URL)
        self.assertEqual(rc, 0)
        self._assert_no_truth_claims(stdout, ctx="matched human stdout")

    def test_no_match_output_carries_no_truth_claim(self):
        rc, stdout, _stderr = _run_cli_subprocess(UNKNOWN_FIXTURE_URL)
        self.assertEqual(rc, 1)
        self._assert_no_truth_claims(stdout, ctx="no-match human stdout")

    def test_json_output_carries_no_truth_claim(self):
        rc, stdout, _stderr = _run_cli_subprocess(
            "--json", MATCHED_FIXTURE_URL, UNKNOWN_FIXTURE_URL,
        )
        self.assertEqual(rc, 1)
        self._assert_no_truth_claims(stdout, ctx="mixed json stdout")

    def test_help_output_carries_no_truth_claim(self):
        rc, stdout, _stderr = _run_cli_subprocess("--help")
        self.assertEqual(rc, 0)
        self._assert_no_truth_claims(stdout, ctx="help stdout")


# ---------------------------------------------------------------------------
# Static safety — no banned imports / no network / no git
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def test_cli_imports_no_network_libs(self):
        text = CLI_SCRIPT.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "urllib.request", "urllib3", "requests", "httpx",
            "openai", "anthropic",
            "playwright", "browser_use", "openclaw",
            "sqlite3", "database", "fastapi", "uvicorn",
            "socket", "subprocess",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"classify_source_url.py must not import {forbidden!r}",
            )

    def test_cli_does_not_invoke_git(self):
        text = CLI_SCRIPT.read_text(encoding="utf-8")
        for token in ("subprocess.run", "subprocess.call",
                      "subprocess.Popen", "os.system"):
            self.assertNotIn(
                token, text,
                f"classify_source_url.py must not call out via {token}",
            )


# ---------------------------------------------------------------------------
# In-process unit tests on helpers (fast — no subprocess overhead)
# ---------------------------------------------------------------------------


class StatusMappingTests(unittest.TestCase):
    def test_status_matched(self):
        c = {"reason": "matched", "allowed": True,
             "matched_source_id": "x", "host": "x.example"}
        self.assertEqual(cli._status_from_classification(c), "MATCHED")

    def test_status_no_match(self):
        c = {"reason": "no_match", "allowed": False,
             "matched_source_id": None, "host": "x.example"}
        self.assertEqual(cli._status_from_classification(c), "NO_MATCH")

    def test_status_rejected_for_each_reject_reason(self):
        for reason in cli._REJECT_REASONS:
            c = {"reason": reason, "allowed": False,
                 "matched_source_id": None, "host": ""}
            self.assertEqual(
                cli._status_from_classification(c), "REJECTED",
                msg=f"reason={reason!r}",
            )

    def test_status_error_for_each_error_reason(self):
        for reason in cli._ERROR_REASONS:
            c = {"reason": reason, "allowed": False,
                 "matched_source_id": None, "host": ""}
            self.assertEqual(
                cli._status_from_classification(c), "ERROR",
                msg=f"reason={reason!r}",
            )


class SummaryTests(unittest.TestCase):
    def test_summary_counts(self):
        results = [
            {"status": "MATCHED"},
            {"status": "MATCHED"},
            {"status": "NO_MATCH"},
            {"status": "REJECTED"},
            {"status": "ERROR"},
        ]
        summary = cli._summarize(results)
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["matched"], 2)
        self.assertEqual(summary["no_match"], 1)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertFalse(summary["all_matched_safely"])

    def test_summary_all_matched(self):
        results = [{"status": "MATCHED"} for _ in range(3)]
        summary = cli._summarize(results)
        self.assertTrue(summary["all_matched_safely"])

    def test_empty_results_not_safe(self):
        # Defensive — an empty result list is NOT "safe" (it's just
        # nothing). Pin the explicit policy.
        summary = cli._summarize([])
        self.assertFalse(summary["all_matched_safely"])


if __name__ == "__main__":
    unittest.main()
