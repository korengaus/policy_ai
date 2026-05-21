"""Phase 2 M10.2: tests for ``source_crawler`` + ``fetch_registry_source``.

Every test mocks the HTTP layer — no real network call ever fires.
Database round-trip tests use a temp SQLite DB so the real
``policy_ai.db`` is untouched. Subprocess tests confirm the operator
CLI is offline by default (``--dry-run`` is the safe default).

Covers spec items A–N:
    A. default_enabled=False refuses → no network call, success=False.
    B. operator_review_required=True refuses → no network call.
    C. Non-https URL refuses → no network call.
    D. URL host not in allowed_domains refuses → no network call.
    E. browser_automation="required" refuses → no network call.
    F. Mock 200 + html → success=True, text_content populated,
       truth_claim=False.
    G. Mock raises ConnectionError → success=False, error populated,
       truth_claim=False.
    H. Mock 404 → success=False with status_code=404, truth_claim=False.
    I. Mock Content-Length > 2MB → success=False, no content stored.
    J. text_content truncated at 50_000 chars.
    K. truth_claim is always False in every FetchResult.
    L. network_fetch_performed=True only when fetch was actually attempted.
    M. DB save_fetch_artifact round-trip.
    N. fetch_registry_source.py --dry-run makes no network call.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import source_crawler  # noqa: E402
import source_registry as registry_mod  # noqa: E402
import scripts.fetch_registry_source as fetch_cli  # noqa: E402


CLI_SCRIPT = ROOT / "scripts" / "fetch_registry_source.py"
SEED_REGISTRY = ROOT / "data" / "source_registry.json"


# A reusable source dict that *would* be allowed by every safety check
# except the explicit refusal under test. Each test then flips ONE
# field to verify that specific refusal in isolation.
def _allowed_source() -> dict:
    return {
        "source_id": "test_source",
        "display_name": "Test source",
        "source_type": "law_or_regulation",
        "jurisdiction": "KR",
        "base_url": "https://example.go.kr",
        "allowed_domains": ["example.go.kr"],
        "allow_subdomains": False,
        "default_enabled": True,                  # explicitly enabled
        "capture_method": "manual_or_http",
        "browser_automation": "not_required",     # not required
        "operator_review_required": False,        # waived for tests
        "official_source_candidate": True,
        "truth_claim": False,
    }


def _make_response(
    *,
    status_code: int = 200,
    text: str = "<html><body>hi</body></html>",
    content: bytes = b"<html><body>hi</body></html>",
    content_type: str = "text/html; charset=utf-8",
    content_length: object = None,
    history=None,
) -> MagicMock:
    """Build a mock HTTP response shaped like ``requests.Response``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content
    headers = {"Content-Type": content_type}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    resp.headers = headers
    resp.history = list(history or [])
    return resp


def _fake_requests(*, response=None, raise_exc=None):
    """Return a MagicMock that mimics the ``requests`` module's
    ``get`` interface (the only attribute ``source_crawler`` touches).
    Records every call for assertions."""
    mod = MagicMock()
    if raise_exc is not None:
        mod.get.side_effect = raise_exc
    else:
        mod.get.return_value = response or _make_response()
    return mod


# ---------------------------------------------------------------------------
# A – E. Safety checks refuse without touching the network
# ---------------------------------------------------------------------------


class SafetyCheckTests(unittest.TestCase):
    def _assert_refused(self, result, *, expected_error_fragment: str,
                        fake_requests):
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        self.assertIn(expected_error_fragment, result.error)
        self.assertFalse(
            result.network_fetch_performed,
            "safety refusal must never set network_fetch_performed=True",
        )
        self.assertFalse(result.truth_claim, "truth_claim must always be False")
        fake_requests.get.assert_not_called()

    def test_default_enabled_false_refuses(self):
        source = _allowed_source()
        source["default_enabled"] = False
        fake = _fake_requests()
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self._assert_refused(
            result,
            expected_error_fragment="source not enabled for automated fetch",
            fake_requests=fake,
        )

    def test_operator_review_required_true_refuses(self):
        source = _allowed_source()
        source["operator_review_required"] = True
        fake = _fake_requests()
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self._assert_refused(
            result,
            expected_error_fragment="operator review required before fetch",
            fake_requests=fake,
        )

    def test_non_https_url_refuses(self):
        source = _allowed_source()
        fake = _fake_requests()
        result = source_crawler.fetch_source_url(
            "http://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self._assert_refused(
            result,
            expected_error_fragment="only https urls are permitted",
            fake_requests=fake,
        )

    def test_url_host_not_in_allowed_domains_refuses(self):
        source = _allowed_source()
        fake = _fake_requests()
        result = source_crawler.fetch_source_url(
            "https://evil.example.com/x", source,
            config={"requests_module": fake},
        )
        self._assert_refused(
            result,
            expected_error_fragment="url host not in allowed_domains",
            fake_requests=fake,
        )

    def test_browser_automation_required_refuses(self):
        source = _allowed_source()
        source["browser_automation"] = "required"
        fake = _fake_requests()
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self._assert_refused(
            result,
            expected_error_fragment=(
                "source requires browser automation, "
                "static fetch not appropriate"
            ),
            fake_requests=fake,
        )

    def test_subdomain_disallowed_by_default(self):
        # Pinning the existing M10.0 allow_subdomains=False default —
        # the crawler must defer to it even though it has its own
        # safety checks.
        source = _allowed_source()
        source["allow_subdomains"] = False
        fake = _fake_requests()
        result = source_crawler.fetch_source_url(
            "https://sub.example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self._assert_refused(
            result,
            expected_error_fragment="url host not in allowed_domains",
            fake_requests=fake,
        )

    def test_subdomain_allowed_when_flag_set(self):
        source = _allowed_source()
        source["allow_subdomains"] = True
        fake = _fake_requests()
        result = source_crawler.fetch_source_url(
            "https://sub.example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self.assertTrue(result.success, msg=result.error)
        self.assertTrue(result.network_fetch_performed)
        fake.get.assert_called_once()


# ---------------------------------------------------------------------------
# F. Successful fetch (mock 200 + html)
# ---------------------------------------------------------------------------


class SuccessfulFetchTests(unittest.TestCase):
    def test_200_html_returns_text_content(self):
        source = _allowed_source()
        html = (
            "<html><head><title>t</title></head>"
            "<body><nav>skip</nav><script>alert('skip')</script>"
            "<p>안녕하세요</p><footer>skip</footer></body></html>"
        )
        fake = _fake_requests(response=_make_response(
            status_code=200, text=html,
            content=html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        ))
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/page", source,
            config={"requests_module": fake},
        )
        self.assertTrue(result.success, msg=result.error)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.url, "https://example.go.kr/page")
        self.assertEqual(result.source_id, "test_source")
        self.assertTrue(result.network_fetch_performed)
        self.assertFalse(result.truth_claim)
        self.assertTrue(result.official_source_candidate)
        self.assertIsNotNone(result.text_content)
        # Stripped tags must NOT show up in the extracted text.
        self.assertNotIn("alert", result.text_content)
        self.assertNotIn("skip", result.text_content)
        # Korean character preserved.
        self.assertIn("안녕하세요", result.text_content)
        fake.get.assert_called_once()

    def test_success_carries_raw_html(self):
        source = _allowed_source()
        html = "<html><body><p>hello world</p></body></html>"
        fake = _fake_requests(response=_make_response(
            status_code=200, text=html,
            content=html.encode("utf-8"),
        ))
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/", source,
            config={"requests_module": fake},
        )
        self.assertTrue(result.success)
        self.assertEqual(result.raw_html, html)

    def test_success_sets_no_cookies_and_uses_neutral_user_agent(self):
        source = _allowed_source()
        fake = _fake_requests()
        source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        kwargs = fake.get.call_args.kwargs
        # No cookies parameter, no auth parameter.
        self.assertNotIn("cookies", kwargs)
        self.assertNotIn("auth", kwargs)
        # User-Agent is the documented neutral string.
        headers = kwargs.get("headers") or {}
        ua = headers.get("User-Agent") or ""
        self.assertIn("policy_ai-source-crawler", ua)
        # Timeout passed through.
        self.assertEqual(kwargs.get("timeout"), source_crawler.DEFAULT_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# G. ConnectionError
# ---------------------------------------------------------------------------


class ConnectionErrorTests(unittest.TestCase):
    def test_connection_error_returns_failure(self):
        source = _allowed_source()
        fake = _fake_requests(raise_exc=ConnectionError("DNS failed"))
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self.assertFalse(result.success)
        # network_fetch_performed=True because requests.get WAS invoked.
        self.assertTrue(result.network_fetch_performed)
        self.assertFalse(result.truth_claim)
        self.assertIn("ConnectionError", result.error)
        self.assertIsNone(result.status_code)


# ---------------------------------------------------------------------------
# H. 404 response — implementation chooses success=False + status_code=404
# ---------------------------------------------------------------------------


class Status404Tests(unittest.TestCase):
    def test_404_returns_failure_with_status_code(self):
        source = _allowed_source()
        fake = _fake_requests(response=_make_response(
            status_code=404, text="<html>missing</html>",
            content=b"<html>missing</html>",
        ))
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/missing", source,
            config={"requests_module": fake},
        )
        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 404)
        self.assertTrue(result.network_fetch_performed)
        self.assertFalse(result.truth_claim)
        self.assertIn("upstream status 404", result.error)


# ---------------------------------------------------------------------------
# I. Content-Length above 2MB aborts
# ---------------------------------------------------------------------------


class ContentLengthCapTests(unittest.TestCase):
    def test_content_length_over_cap_refuses(self):
        source = _allowed_source()
        oversized = source_crawler.MAX_CONTENT_BYTES + 1
        fake = _fake_requests(response=_make_response(
            content_length=oversized,
        ))
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/big", source,
            config={"requests_module": fake},
        )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        self.assertIn("exceeds cap", result.error)
        # No raw body / text stored.
        self.assertIsNone(result.text_content)
        self.assertIsNone(result.raw_html)

    def test_body_bytes_over_cap_refuses_even_without_header(self):
        # If Content-Length is missing but the body is bigger than
        # the cap, the crawler still refuses.
        source = _allowed_source()
        big = b"x" * (source_crawler.MAX_CONTENT_BYTES + 10)
        fake = _fake_requests(response=_make_response(
            content=big, text=big.decode("latin-1", errors="replace"),
            # No Content-Length header.
        ))
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/big", source,
            config={"requests_module": fake},
        )
        self.assertFalse(result.success)
        self.assertIn("exceeds cap", (result.error or ""))


# ---------------------------------------------------------------------------
# J. Text content truncated at 50_000 chars
# ---------------------------------------------------------------------------


class TextContentTruncationTests(unittest.TestCase):
    def test_text_content_truncated(self):
        source = _allowed_source()
        # Build an HTML body whose extracted text is bigger than the
        # truncation budget but smaller than the byte cap.
        long_text = "안녕 " * 20_000   # 80k chars
        html = f"<html><body><p>{long_text}</p></body></html>"
        # Keep raw bytes under MAX_CONTENT_BYTES (2 MB). The text we
        # built is roughly 240 KB raw — well within.
        fake = _fake_requests(response=_make_response(
            text=html, content=html.encode("utf-8"),
        ))
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/long", source,
            config={"requests_module": fake},
        )
        self.assertTrue(result.success, msg=result.error)
        self.assertIsNotNone(result.text_content)
        self.assertLessEqual(
            len(result.text_content),
            source_crawler.MAX_TEXT_CHARS,
        )


# ---------------------------------------------------------------------------
# K. truth_claim is always False
# ---------------------------------------------------------------------------


class TruthClaimAlwaysFalseTests(unittest.TestCase):
    def _check(self, result):
        self.assertIs(result.truth_claim, False)

    def test_truth_claim_false_on_safety_refusal(self):
        source = _allowed_source()
        source["default_enabled"] = False
        fake = _fake_requests()
        r = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self._check(r)

    def test_truth_claim_false_on_success(self):
        source = _allowed_source()
        fake = _fake_requests()
        r = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self._check(r)

    def test_truth_claim_false_on_connection_error(self):
        source = _allowed_source()
        fake = _fake_requests(raise_exc=RuntimeError("boom"))
        r = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self._check(r)

    def test_truth_claim_false_on_4xx(self):
        source = _allowed_source()
        fake = _fake_requests(response=_make_response(status_code=403))
        r = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self._check(r)

    def test_truth_claim_false_in_serialized_dict(self):
        source = _allowed_source()
        fake = _fake_requests()
        r = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        d = source_crawler.fetch_result_to_dict(r)
        self.assertIs(d["truth_claim"], False)


# ---------------------------------------------------------------------------
# L. network_fetch_performed reflects whether get() was called
# ---------------------------------------------------------------------------


class NetworkFetchFlagTests(unittest.TestCase):
    def test_flag_false_on_safety_refusal(self):
        source = _allowed_source()
        source["default_enabled"] = False
        fake = _fake_requests()
        r = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self.assertFalse(r.network_fetch_performed)
        fake.get.assert_not_called()

    def test_flag_true_on_actual_fetch(self):
        source = _allowed_source()
        fake = _fake_requests()
        r = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self.assertTrue(r.network_fetch_performed)
        fake.get.assert_called_once()


# ---------------------------------------------------------------------------
# M. Database round-trip via save_fetch_artifact + get_fetch_artifacts
# ---------------------------------------------------------------------------


class DatabaseRoundTripTests(unittest.TestCase):
    def setUp(self):
        import database
        self._database = database
        self._original_path = database.DB_PATH
        self._tmp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = Path(self._tmp_dir.name) / "crawler_test.db"
        database.init_db()
        database.init_source_fetch_artifacts_table()

    def tearDown(self):
        # Drop the connection reference so the temp file unlinks
        # cleanly on Windows.
        import gc as _gc
        _gc.collect()
        self._database.DB_PATH = self._original_path
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_save_then_get_round_trip(self):
        # Synthesize a FetchResult-shaped dict the crawler would
        # produce; persist it; read it back.
        source = _allowed_source()
        fake = _fake_requests(response=_make_response(
            status_code=200,
            text="<html><body><p>안녕</p></body></html>",
            content="<html><body><p>안녕</p></body></html>".encode("utf-8"),
        ))
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/x", source,
            config={"requests_module": fake},
        )
        self.assertTrue(result.success)
        row_id = self._database.save_fetch_artifact(
            source_crawler.fetch_result_to_dict(result)
        )
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)

        rows = self._database.get_fetch_artifacts(source_id="test_source")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source_id"], "test_source")
        self.assertEqual(row["url"], "https://example.go.kr/x")
        self.assertEqual(row["status_code"], 200)
        self.assertTrue(row["success"])
        # truth_claim is forced to 0 on save → bool False on read.
        self.assertIs(row["truth_claim"], False)
        self.assertIs(row["official_source_candidate"], True)
        self.assertIn("안녕", row["text_content"])

    def test_save_rejects_missing_required_fields(self):
        for bad in (
            None,
            {},
            {"source_id": "x"},                          # missing url
            {"source_id": "x", "url": "u"},              # missing fetch_timestamp
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._database.save_fetch_artifact(bad)

    def test_save_forces_truth_claim_to_zero(self):
        # Defense-in-depth: even if a caller sets truth_claim=True
        # on the dict (which the crawler would never do), the DB
        # layer must persist 0.
        fake = _fake_requests()
        result = source_crawler.fetch_source_url(
            "https://example.go.kr/x", _allowed_source(),
            config={"requests_module": fake},
        )
        d = source_crawler.fetch_result_to_dict(result)
        d["truth_claim"] = True   # try to lie
        self._database.save_fetch_artifact(d)
        rows = self._database.get_fetch_artifacts()
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["truth_claim"], False)


# ---------------------------------------------------------------------------
# N. fetch_registry_source.py --dry-run makes no network call (subprocess)
# ---------------------------------------------------------------------------


class FetchCliDryRunTests(unittest.TestCase):
    """The operator CLI is offline by default. We verify via subprocess
    so the test exercises the real arg-parsing + the documented
    default-dry-run behavior end-to-end."""

    def _run_cli(self, *args, env=None):
        completed = subprocess.run(
            [sys.executable, str(CLI_SCRIPT)] + [str(a) for a in args],
            cwd=str(ROOT),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=10.0,
            env={**os.environ, **(env or {})},
        )
        return completed.returncode, completed.stdout, completed.stderr

    def test_help_exits_0(self):
        rc, stdout, _stderr = self._run_cli("--help")
        self.assertEqual(rc, 0)
        self.assertIn("fetch_registry_source", stdout)
        self.assertIn("Exit codes", stdout)

    def test_dry_run_against_disabled_seed_refuses(self):
        # The seed entry is default_enabled=false, so the safety
        # check refuses. Dry-run never touches the network.
        rc, stdout, _stderr = self._run_cli(
            "--source-id", "kr_law_open_data_candidate",
            "--url", "https://www.law.go.kr/test",
            "--dry-run",
        )
        self.assertEqual(rc, 1, msg=stdout)
        self.assertIn("DRY RUN", stdout)
        self.assertIn("safety_refusal", stdout)
        self.assertIn("default_enabled", stdout.lower() + "default_enabled")
        # Safety notes present in human output.
        self.assertIn("truth_claim: False", stdout)
        self.assertIn(
            "official_source_candidate does not guarantee content accuracy",
            stdout,
        )

    def test_dry_run_with_unknown_source_exits_1(self):
        rc, stdout, _stderr = self._run_cli(
            "--source-id", "no_such_source",
            "--url", "https://example.go.kr/x",
            "--dry-run",
        )
        self.assertEqual(rc, 1)
        self.assertIn("DRY RUN", stdout)
        self.assertIn("not in registry", stdout)

    def test_dry_run_json_payload_has_expected_keys(self):
        rc, stdout, _stderr = self._run_cli(
            "--source-id", "kr_law_open_data_candidate",
            "--url", "https://www.law.go.kr/test",
            "--dry-run", "--json",
        )
        self.assertEqual(rc, 1)
        payload = json.loads(stdout)
        for key in (
            "cli_version", "mode", "processed_at", "source_id",
            "url", "registry_path", "source_found", "safety_refusal",
            "would_fetch", "network_fetch_performed", "truth_claim",
            "safety_notes",
        ):
            self.assertIn(key, payload, msg=f"missing key: {key}")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertFalse(payload["network_fetch_performed"])
        self.assertIs(payload["truth_claim"], False)

    def test_missing_required_flag_exits_2(self):
        rc, _stdout, stderr = self._run_cli("--url", "https://example.go.kr/x")
        self.assertEqual(rc, 2)
        self.assertIn("--source-id", stderr.lower() + "--source-id")


# ---------------------------------------------------------------------------
# Static safety — no banned imports in source_crawler.py / fetch_registry_source.py
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    CRAWLER_PATH = ROOT / "source_crawler.py"
    CLI_PATH = CLI_SCRIPT

    def _import_lines(self, path):
        text = path.read_text(encoding="utf-8")
        return [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]

    def test_crawler_does_not_import_browser_or_openai(self):
        joined = "\n".join(self._import_lines(self.CRAWLER_PATH))
        for forbidden in (
            "openai", "anthropic",
            "playwright", "browser_use", "openclaw",
            "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"source_crawler.py must not import {forbidden!r}",
            )

    def test_crawler_not_imported_by_pipeline_entry_points(self):
        # Spec: source_crawler.py must not be triggered by
        # analyze_pipeline / main.py / api_server.py.
        for module_path in (ROOT / "main.py", ROOT / "api_server.py"):
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "source_crawler", text,
                f"{module_path.name} must not import source_crawler",
            )

    def test_cli_does_not_import_browser_or_openai(self):
        joined = "\n".join(self._import_lines(self.CLI_PATH))
        for forbidden in (
            "openai", "anthropic",
            "playwright", "browser_use", "openclaw",
            "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"fetch_registry_source.py must not import {forbidden!r}",
            )


# ---------------------------------------------------------------------------
# In-process unit test on _run_safety_checks (no subprocess overhead)
# ---------------------------------------------------------------------------


class SafetyCheckUnitTests(unittest.TestCase):
    def test_each_refusal_string(self):
        cases = [
            (
                dict(_allowed_source(), default_enabled=False),
                "source not enabled for automated fetch",
            ),
            (
                dict(_allowed_source(), operator_review_required=True),
                "operator review required before fetch",
            ),
            (
                dict(_allowed_source(), browser_automation="required"),
                "source requires browser automation, "
                "static fetch not appropriate",
            ),
        ]
        for source, expected in cases:
            with self.subTest(expected=expected):
                got = source_crawler._run_safety_checks(
                    "https://example.go.kr/x", source,
                )
                self.assertEqual(got, expected)

    def test_https_check(self):
        self.assertEqual(
            source_crawler._run_safety_checks(
                "http://example.go.kr/x", _allowed_source(),
            ),
            "only https urls are permitted",
        )

    def test_host_not_allowed(self):
        self.assertEqual(
            source_crawler._run_safety_checks(
                "https://evil.example.com/x", _allowed_source(),
            ),
            "url host not in allowed_domains",
        )

    def test_clean_path_returns_none(self):
        self.assertIsNone(
            source_crawler._run_safety_checks(
                "https://example.go.kr/x", _allowed_source(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
