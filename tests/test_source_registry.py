"""Phase 2 M10.0: tests for the source registry foundation.

Every test exercises pure helpers and synthetic fixtures. No HTTP,
no browser automation, no OpenAI, no Render call. The repo's default
``data/source_registry.json`` is also loaded so the seed file stays
valid as the registry grows.

Covers spec items A–Z:
    A. Default registry loads.
    B. schema_version is 1.
    C. source_id uniqueness enforced.
    D. invalid source_id rejected.
    E. invalid source_type rejected.
    F. invalid capture_method rejected.
    G. invalid browser_automation rejected.
    H. https base_url accepted.
    I. credential-bearing URL rejected.
    J. allowed_domains cannot contain scheme/path.
    K. truth_claim=true rejected.
    L. operator_review_required defaults true.
    M. is_url_allowed_for_source accepts exact allowed domain.
    N. is_url_allowed_for_source rejects lookalike domains.
    O. subdomain behavior is explicit and tested.
    P. non-https URL rejected unless documented exception (demo).
    Q. token-like strings in metadata rejected.
    R. build_source_capture_plan never performs network fetch.
    S. capture_method=browser_required produces browser plan.
    T. demo/source seed entries do not claim truth.
    U. validate_source_registry.py --json has stable keys.
    V. validate_source_registry.py exits 0 for default registry.
    W. source_registry.py imports no banned libraries.
    X. No tests call external network.
    Y. Korean text remains readable in seed/fixtures.
    Z. No git add/commit/push subprocess calls.
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

import source_registry as sr  # noqa: E402
import scripts.validate_source_registry as validator_cli  # noqa: E402


REGISTRY_MODULE_PATH = ROOT / "source_registry.py"
VALIDATOR_CLI_PATH = ROOT / "scripts" / "validate_source_registry.py"
SEED_REGISTRY_PATH = ROOT / "data" / "source_registry.json"


# A minimal, valid source record for fixture-based tests. Mutate a
# copy in each test rather than the original.
def _valid_record() -> dict:
    return {
        "source_id": "fixture_source_one",
        "display_name": "Fixture source one",
        "source_type": "government_policy",
        "jurisdiction": "KR",
        "base_url": "https://example.go.kr",
        "allowed_domains": ["example.go.kr"],
        "allow_subdomains": False,
        "default_enabled": False,
        "capture_method": "manual_or_http",
        "browser_automation": "not_required",
        "operator_review_required": True,
        "official_source_candidate": True,
        "truth_claim": False,
        "notes": "Fixture record (사람 검토 필요).",
        "tags": ["fixture", "policy"],
    }


def _valid_registry(*records) -> dict:
    return {
        "schema_version": 1,
        "registry_name": "policy_ai_source_registry",
        "sources": list(records) if records else [_valid_record()],
    }


# ---------------------------------------------------------------------------
# A + B — default registry loads, schema_version is 1
# ---------------------------------------------------------------------------


class DefaultRegistryTests(unittest.TestCase):
    def test_default_registry_loads(self):
        raw = sr.load_source_registry()
        self.assertIsInstance(raw, dict)
        self.assertEqual(raw.get("schema_version"), sr.SOURCE_REGISTRY_SCHEMA_VERSION)
        self.assertEqual(raw.get("schema_version"), 1)
        self.assertEqual(raw.get("registry_name"), sr.REGISTRY_NAME)
        sources = raw.get("sources")
        self.assertIsInstance(sources, list)
        self.assertGreaterEqual(len(sources), 1)

    def test_default_registry_passes_validation(self):
        raw = sr.load_source_registry()
        normalized, errors, warnings = sr.validate_source_registry(raw)
        self.assertEqual(errors, [], msg=f"errors={errors}")
        self.assertEqual(normalized["schema_version"], 1)
        self.assertEqual(normalized["registry_name"], sr.REGISTRY_NAME)


# ---------------------------------------------------------------------------
# C — source_id uniqueness
# ---------------------------------------------------------------------------


class UniquenessTests(unittest.TestCase):
    def test_duplicate_source_id_rejected(self):
        a = _valid_record()
        b = _valid_record()
        b["display_name"] = "Different label, same id"
        reg = _valid_registry(a, b)
        _normalized, errors, _warnings = sr.validate_source_registry(reg)
        joined = " | ".join(errors)
        self.assertIn("duplicate source_id", joined, msg=joined)


# ---------------------------------------------------------------------------
# D + E + F + G — invalid enum / source_id values rejected
# ---------------------------------------------------------------------------


class InvalidEnumsTests(unittest.TestCase):
    def test_invalid_source_id_rejected(self):
        r = _valid_record()
        for bad in ("", "X-Has-Hyphen", "1starts_digit", "한국어식별자",
                    "AlsoHasCaps"):
            with self.subTest(source_id=bad):
                rec = dict(r, source_id=bad)
                _n, errors, _w = sr.validate_source_record(rec)
                self.assertTrue(
                    any("source_id" in e for e in errors),
                    msg=f"bad source_id={bad!r} did not fail",
                )

    def test_invalid_source_type_rejected(self):
        r = _valid_record()
        rec = dict(r, source_type="not_a_real_type")
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("source_type" in e for e in errors))

    def test_invalid_capture_method_rejected(self):
        r = _valid_record()
        rec = dict(r, capture_method="screenshot_with_pencil")
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("capture_method" in e for e in errors))

    def test_invalid_browser_automation_rejected(self):
        r = _valid_record()
        rec = dict(r, browser_automation="maybe_idk")
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("browser_automation" in e for e in errors))


# ---------------------------------------------------------------------------
# H + I — https base_url accepted, credentials rejected
# ---------------------------------------------------------------------------


class BaseUrlSafetyTests(unittest.TestCase):
    def test_https_accepted(self):
        rec = _valid_record()
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertEqual(errors, [], msg=f"errors={errors}")

    def test_credential_bearing_url_rejected(self):
        rec = _valid_record()
        rec["base_url"] = "https://user:pass@example.go.kr"
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(
            any("credentials" in e for e in errors),
            msg=f"errors={errors}",
        )

    def test_query_string_in_base_url_rejected(self):
        rec = _valid_record()
        rec["base_url"] = "https://example.go.kr/path?token=abc"
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(
            any("query string" in e or "fragment" in e for e in errors),
            msg=f"errors={errors}",
        )

    def test_missing_base_url_rejected(self):
        rec = _valid_record()
        rec["base_url"] = None
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("base_url" in e for e in errors))


# ---------------------------------------------------------------------------
# J — allowed_domains shape
# ---------------------------------------------------------------------------


class AllowedDomainsTests(unittest.TestCase):
    def test_scheme_in_allowed_domain_rejected(self):
        rec = _valid_record()
        rec["allowed_domains"] = ["https://example.go.kr"]
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("allowed_domains" in e for e in errors))

    def test_path_in_allowed_domain_rejected(self):
        rec = _valid_record()
        rec["allowed_domains"] = ["example.go.kr/path"]
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("allowed_domains" in e for e in errors))

    def test_wildcard_in_allowed_domain_rejected(self):
        rec = _valid_record()
        rec["allowed_domains"] = ["*.example.go.kr"]
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("'*'" in e or "wildcard" in e.lower()
                            for e in errors))

    def test_empty_allowed_domains_rejected(self):
        rec = _valid_record()
        rec["allowed_domains"] = []
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("allowed_domains" in e for e in errors))

    def test_duplicate_allowed_domain_rejected(self):
        rec = _valid_record()
        rec["allowed_domains"] = ["example.go.kr", "example.go.kr"]
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("duplicated" in e for e in errors))


# ---------------------------------------------------------------------------
# K — truth_claim=true rejected
# ---------------------------------------------------------------------------


class TruthClaimTests(unittest.TestCase):
    def test_truth_claim_true_rejected(self):
        rec = _valid_record()
        rec["truth_claim"] = True
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(
            any("truth_claim" in e for e in errors),
            msg=f"errors={errors}",
        )

    def test_truth_claim_false_default(self):
        rec = _valid_record()
        rec.pop("truth_claim", None)
        normalized, errors, _w = sr.validate_source_record(rec)
        self.assertEqual(errors, [])
        # Normalized form always carries truth_claim=False.
        self.assertEqual(normalized["truth_claim"], False)


# ---------------------------------------------------------------------------
# L — operator_review_required default + override safety
# ---------------------------------------------------------------------------


class OperatorReviewRequiredTests(unittest.TestCase):
    def test_default_is_true_when_missing(self):
        rec = _valid_record()
        rec.pop("operator_review_required", None)
        normalized, errors, _w = sr.validate_source_record(rec)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["operator_review_required"], True)

    def test_false_requires_justification(self):
        rec = _valid_record()
        rec["operator_review_required"] = False
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(
            any("justification" in e for e in errors),
            msg=f"errors={errors}",
        )

    def test_false_with_justification_accepted(self):
        rec = _valid_record()
        rec["operator_review_required"] = False
        rec["operator_review_required_justification"] = (
            "Already individually reviewed in M-x.y; conservative fallback "
            "still applies on every fetch."
        )
        normalized, errors, _w = sr.validate_source_record(rec)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["operator_review_required"], False)


# ---------------------------------------------------------------------------
# M + N + O + P — URL safety on lookups
# ---------------------------------------------------------------------------


class UrlAllowedTests(unittest.TestCase):
    def test_exact_domain_match(self):
        rec = _valid_record()
        self.assertTrue(sr.is_url_allowed_for_source(
            rec, "https://example.go.kr/policy/x",
        ))

    def test_lookalike_domain_rejected(self):
        rec = _valid_record()
        # Lookalike: trailing dot, different TLD, prefix, suffix.
        for bad in (
            "https://example.go.kr.evil.com/path",
            "https://evil-example.go.kr/path",
            "https://example.go.kr.example.net/path",
            "https://exampIe.go.kr/path",   # Capital I lookalike (also fails ASCII regex)
        ):
            with self.subTest(url=bad):
                self.assertFalse(
                    sr.is_url_allowed_for_source(rec, bad),
                    msg=f"unexpectedly allowed: {bad}",
                )

    def test_subdomain_disallowed_by_default(self):
        rec = _valid_record()
        rec["allow_subdomains"] = False
        self.assertFalse(sr.is_url_allowed_for_source(
            rec, "https://sub.example.go.kr/x",
        ))

    def test_subdomain_allowed_when_flag_set(self):
        rec = _valid_record()
        rec["allow_subdomains"] = True
        self.assertTrue(sr.is_url_allowed_for_source(
            rec, "https://sub.example.go.kr/x",
        ))
        # But not the bare lookalike: "example.go.kr.evil.com" must still fail.
        self.assertFalse(sr.is_url_allowed_for_source(
            rec, "https://example.go.kr.evil.com/x",
        ))

    def test_non_https_rejected(self):
        rec = _valid_record()
        self.assertFalse(sr.is_url_allowed_for_source(
            rec, "http://example.go.kr/x",
        ))
        self.assertFalse(sr.is_url_allowed_for_source(
            rec, "ftp://example.go.kr/x",
        ))

    def test_demo_source_allows_http_localhost(self):
        rec = _valid_record()
        rec["source_type"] = "demo"
        rec["base_url"] = "http://127.0.0.1"
        rec["allowed_domains"] = ["127.0.0.1"]
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertEqual(errors, [])
        self.assertTrue(sr.is_url_allowed_for_source(
            rec, "http://127.0.0.1/fixture",
        ))
        # http to a non-local host is still rejected even for demo.
        self.assertFalse(sr.is_url_allowed_for_source(
            rec, "http://example.com/x",
        ))

    def test_credential_url_rejected(self):
        rec = _valid_record()
        self.assertFalse(sr.is_url_allowed_for_source(
            rec, "https://user:pass@example.go.kr/x",
        ))

    def test_classify_url_against_registry(self):
        reg = _valid_registry(_valid_record())
        match = sr.classify_url_against_registry(
            reg, "https://example.go.kr/policy/123",
        )
        self.assertTrue(match["allowed"])
        self.assertEqual(match["matched_source_id"], "fixture_source_one")
        self.assertEqual(match["host"], "example.go.kr")
        self.assertEqual(match["reason"], "matched")
        # Unknown host falls through.
        unmatched = sr.classify_url_against_registry(
            reg, "https://unknown.example.org/x",
        )
        self.assertFalse(unmatched["allowed"])
        self.assertEqual(unmatched["reason"], "no_match")
        # Credential URL never reaches per-source check.
        creds = sr.classify_url_against_registry(
            reg, "https://user:pass@example.go.kr/x",
        )
        self.assertFalse(creds["allowed"])
        self.assertEqual(creds["reason"], "credentials_in_url")


# ---------------------------------------------------------------------------
# Q — token-like literals rejected in metadata
# ---------------------------------------------------------------------------


class TokenLiteralScanTests(unittest.TestCase):
    def test_hex_literal_in_notes_rejected(self):
        rec = _valid_record()
        rec["notes"] = "leaked: deadbeefcafebabe1234567890abcdef0123456789"
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("hex" in e or "token" in e for e in errors))

    def test_sdk_key_prefix_in_tags_rejected(self):
        rec = _valid_record()
        rec["tags"] = ["fixture", "sk-AAAAAAAAAAAAAAAAAAAAAAAA"]
        _n, errors, _w = sr.validate_source_record(rec)
        self.assertTrue(any("SDK-key" in e or "sk-" in e for e in errors))

    def test_clean_metadata_has_no_token_warnings(self):
        rec = _valid_record()
        normalized, errors, _w = sr.validate_source_record(rec)
        self.assertEqual(errors, [])
        body = json.dumps(normalized, ensure_ascii=False)
        # The normalized output must not invent any token-shaped string
        # either (defensive).
        for needle in ("OPENAI_API_KEY", "REVIEW_API_TOKEN", "sk-AAAA"):
            self.assertNotIn(needle, body)


# ---------------------------------------------------------------------------
# R + S — capture plan never fetches; browser_required → browser plan
# ---------------------------------------------------------------------------


class CapturePlanTests(unittest.TestCase):
    def test_plan_disabled_source_is_manual_review(self):
        rec = _valid_record()
        rec["default_enabled"] = False
        plan = sr.build_source_capture_plan(rec)
        self.assertFalse(plan["network_fetch_performed"])
        self.assertEqual(plan["next_step"], "manual_review")

    def test_plan_enabled_http_source_is_fetch_candidate(self):
        rec = _valid_record()
        rec["default_enabled"] = True
        rec["operator_review_required"] = False
        rec["operator_review_required_justification"] = "explicit dev fixture"
        plan = sr.build_source_capture_plan(rec)
        self.assertFalse(plan["network_fetch_performed"])
        self.assertEqual(plan["next_step"], "http_fetch_candidate")

    def test_plan_enabled_browser_required_source(self):
        rec = _valid_record()
        rec["default_enabled"] = True
        rec["operator_review_required"] = False
        rec["operator_review_required_justification"] = "explicit dev fixture"
        rec["capture_method"] = "browser_required"
        rec["browser_automation"] = "required"
        plan = sr.build_source_capture_plan(rec)
        self.assertEqual(plan["next_step"], "browser_candidate")
        self.assertFalse(plan["network_fetch_performed"])

    def test_plan_non_dict_returns_unsupported(self):
        plan = sr.build_source_capture_plan(None)
        self.assertEqual(plan["next_step"], "unsupported")
        self.assertFalse(plan["network_fetch_performed"])

    def test_plan_carries_url_allowed_when_url_supplied(self):
        rec = _valid_record()
        rec["default_enabled"] = True
        rec["operator_review_required"] = False
        rec["operator_review_required_justification"] = "fixture"
        plan = sr.build_source_capture_plan(
            rec, url="https://example.go.kr/policy/1",
        )
        self.assertTrue(plan["url_allowed"])
        bad = sr.build_source_capture_plan(
            rec, url="https://other.example.org/x",
        )
        self.assertFalse(bad["url_allowed"])

    def test_capture_plan_module_does_not_call_subprocess_or_net(self):
        # Runtime + import-line-only check — the module imports no
        # http / browser libs and no subprocess calls. The plan's
        # ``network_fetch_performed: False`` flag (pinned in every test
        # above) gives the runtime guarantee; here we add the import-
        # level pin. Scan only ``import``/``from`` lines so the
        # module's own docstring (which *describes* what it doesn't
        # import) doesn't trip the check.
        text = REGISTRY_MODULE_PATH.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "urllib.request", "urllib3", "requests", "httpx",
            "openai", "anthropic", "playwright", "browser_use",
            "openclaw", "subprocess",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"source_registry.py must not import {forbidden!r}",
            )


# ---------------------------------------------------------------------------
# T — seed entries never claim truth
# ---------------------------------------------------------------------------


class SeedSafetyTests(unittest.TestCase):
    def test_no_seed_entry_claims_truth(self):
        raw = sr.load_source_registry()
        for s in raw.get("sources") or []:
            self.assertFalse(
                bool(s.get("truth_claim", False)),
                msg=f"seed source {s.get('source_id')} carries truth_claim=true",
            )

    def test_every_seed_requires_operator_review(self):
        raw = sr.load_source_registry()
        for s in raw.get("sources") or []:
            self.assertTrue(
                bool(s.get("operator_review_required", True)),
                msg=f"seed source {s.get('source_id')} must require operator review",
            )

    def test_no_seed_is_enabled_by_default(self):
        raw = sr.load_source_registry()
        for s in raw.get("sources") or []:
            self.assertFalse(
                bool(s.get("default_enabled", False)),
                msg=(
                    f"seed source {s.get('source_id')} is default_enabled=true; "
                    "future ingestion must opt in explicitly"
                ),
            )


# ---------------------------------------------------------------------------
# U + V — validator CLI JSON shape + exit code
# ---------------------------------------------------------------------------


class ValidatorCLITests(unittest.TestCase):
    EXPECTED_KEYS = {
        "passed", "schema_version", "registry_name", "source_path",
        "sources_count", "enabled_count", "disabled_count",
        "source_types", "browser_required_count",
        "issues", "warnings",
    }

    def _run_cli(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = validator_cli.main(argv)
        except SystemExit as e:
            rc = int(e.code) if e.code is not None else 0
        return rc, out.getvalue(), err.getvalue()

    def test_validator_cli_exits_0_for_default_registry(self):
        rc, stdout, stderr = self._run_cli([])
        self.assertEqual(rc, 0, msg=(stdout + "\n" + stderr)[-2000:])

    def test_validator_cli_json_has_stable_keys(self):
        rc, stdout, _err = self._run_cli(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(set(payload.keys()), self.EXPECTED_KEYS)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("sources_count", payload)

    def test_validator_cli_fails_for_broken_registry(self):
        # Write a deliberately broken registry to a temp file under
        # data/ (so the relative-path normalization works either way),
        # then point the CLI at it via --registry-path.
        bad = {
            "schema_version": 1,
            "registry_name": "policy_ai_source_registry",
            "sources": [
                {
                    # Missing source_id, truth_claim=true, http base.
                    "source_type": "demo",
                    "base_url": "http://example.org/x",
                    "allowed_domains": ["example.org"],
                    "capture_method": "manual_or_http",
                    "browser_automation": "not_required",
                    "truth_claim": True,
                    "operator_review_required": True,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text(json.dumps(bad), encoding="utf-8")
            rc, stdout, _err = self._run_cli(
                ["--registry-path", str(p), "--json"],
            )
        self.assertEqual(rc, 1)
        payload = json.loads(stdout)
        self.assertFalse(payload["passed"])
        joined = " | ".join(payload["issues"])
        self.assertIn("source_id", joined)
        self.assertIn("truth_claim", joined)

    def test_validator_cli_missing_file_returns_2(self):
        rc, _stdout, stderr = self._run_cli([
            "--registry-path",
            str(ROOT / "data" / "this_file_does_not_exist.json"),
        ])
        self.assertEqual(rc, 2)
        self.assertIn("not found", stderr)


# ---------------------------------------------------------------------------
# W + X + Y + Z — static safety: no banned imports / network / git
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def test_source_registry_module_imports_only_stdlib_safe(self):
        text = REGISTRY_MODULE_PATH.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx", "urllib3", "urllib.request",
            "playwright", "browser_use", "openclaw",
            "fastapi", "uvicorn", "sqlite3", "database",
            "subprocess",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"source_registry.py must not import {forbidden!r}",
            )

    def test_validator_cli_imports_only_stdlib_safe(self):
        text = VALIDATOR_CLI_PATH.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx", "urllib3", "urllib.request",
            "playwright", "browser_use", "openclaw",
            "fastapi", "uvicorn", "subprocess",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"validate_source_registry.py must not import {forbidden!r}",
            )

    def test_neither_file_invokes_git(self):
        for path in (REGISTRY_MODULE_PATH, VALIDATOR_CLI_PATH):
            text = path.read_text(encoding="utf-8")
            for token in ("subprocess.run", "subprocess.call",
                          "subprocess.Popen", "os.system"):
                self.assertNotIn(
                    token, text,
                    f"{path.name}: must not call out via {token}",
                )

    def test_korean_text_in_seed_round_trips(self):
        raw = sr.load_source_registry()
        # The fixture record we ship for tests carries Korean text;
        # the seed includes "사람 검토 필요"-style copy implicitly via
        # the demo notes (no Korean truth claim). Round-trip a Korean
        # claim through validate_source_record to make sure UTF-8 is
        # preserved end-to-end.
        rec = _valid_record()
        rec["notes"] = "청년 월세 지원 정책 — 사람 검토 필요 (피드 데모)."
        normalized, errors, _w = sr.validate_source_record(rec)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["notes"],
                         "청년 월세 지원 정책 — 사람 검토 필요 (피드 데모).")
        # Re-dump to JSON without ensure_ascii so the text stays
        # human-readable.
        body = json.dumps(normalized, ensure_ascii=False)
        self.assertIn("사람 검토 필요", body)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


class LookupHelpersTests(unittest.TestCase):
    def test_get_source_by_id(self):
        raw = sr.load_source_registry()
        s = sr.get_source_by_id(raw, "demo_official_policy_source")
        self.assertIsNotNone(s)
        self.assertEqual(s["source_id"], "demo_official_policy_source")
        # Unknown id returns None.
        self.assertIsNone(sr.get_source_by_id(raw, "definitely-missing"))

    def test_list_sources_filtering(self):
        raw = sr.load_source_registry()
        demos = sr.list_sources(raw, source_type="demo")
        self.assertGreaterEqual(len(demos), 1)
        for s in demos:
            self.assertEqual(s["source_type"], "demo")
        # default-enabled filter — seed has none enabled.
        self.assertEqual(sr.list_sources(raw, enabled=True), [])
        # disabled-only matches everything.
        self.assertEqual(
            len(sr.list_sources(raw, enabled=False)),
            len(raw.get("sources") or []),
        )

    def test_find_sources_by_domain(self):
        raw = sr.load_source_registry()
        matches = sr.find_sources_by_domain(raw, "example.go.kr")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source_id"], "demo_official_policy_source")
        # Empty / unknown domain returns empty list.
        self.assertEqual(sr.find_sources_by_domain(raw, ""), [])
        self.assertEqual(sr.find_sources_by_domain(raw, "unknown.example.com"), [])


# ---------------------------------------------------------------------------
# Loading error paths
# ---------------------------------------------------------------------------


class LoadingErrorTests(unittest.TestCase):
    def test_missing_file_raises_source_registry_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "no_such_file.json"
            with self.assertRaises(sr.SourceRegistryError) as cm:
                sr.load_source_registry(p)
            self.assertEqual(cm.exception.reason, "file_not_found")

    def test_invalid_json_raises_source_registry_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(sr.SourceRegistryError) as cm:
                sr.load_source_registry(p)
            self.assertEqual(cm.exception.reason, "json_decode_error")

    def test_top_level_not_object_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "list.json"
            p.write_text("[]", encoding="utf-8")
            with self.assertRaises(sr.SourceRegistryError) as cm:
                sr.load_source_registry(p)
            self.assertEqual(cm.exception.reason, "top_level_not_object")


if __name__ == "__main__":
    unittest.main()
