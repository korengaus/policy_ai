"""M11.6 — pins for the official_crawler mojibake sentinel removal.

claude_audit_phase1.md §1.5 #6 identified two string literals in
official_crawler.py whose byte sequences are encoding-corruption ("mojibake")
and therefore never match any real Korean web page. The sentinels were
short-circuit guards meant to detect FSS (Financial Supervisory Service)
"error page" responses; because the literal bytes never matched, the
guards were silently dead. M11.6 deletes them.

This suite pins:
  (a) The mojibake byte sequences (and the dead error-message string)
      do not reappear in official_crawler.py via a future copy-paste
      from a wrong-encoded source.
  (b) The crawler module still imports cleanly and exposes its public
      entry points (catches accidental scope creep in the deletion).
  (c) `fetch_best_official_document` still returns the expected
      result shape on a representative non-matching input — the
      surrounding control flow that used to live alongside the dead
      branch still produces the same fall-through behavior.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_CRAWLER_PATH = _PROJECT_ROOT / "official_crawler.py"


# Byte sequences captured directly from the pre-M11.6 file. Keeping
# them as raw bytes (rather than visually-similar Python str literals)
# avoids any UTF-8 re-encoding mismatch — `bytes in bytes` is exact.
_SENTINEL_1_BYTES = (
    b"?\xeb\xa8\xae\xec\x9c\xad?\xec\x84\x8f\xec\x94\xa0"
    b"\xef\xa7\x9e\xc2\x80"
)
_SENTINEL_2_BYTES = (
    b"?\xe7\x99\x92?\xec\x91\x8e??\xeb\xa5\x81\xeb\xb5\xa0"
    b"\xe7\xad\x8c\xec\x99\x96\xc2\x80"
)
# The only call sites of this error string were inside the two
# deleted blocks; if it reappears, mojibake or restoration has
# crept back in.
_DEAD_ERROR_MESSAGE = b"FSS search returned error page"


# ---------------------------------------------------------------------------
# Sentinel-absence pins.
# ---------------------------------------------------------------------------


class MojibakeAbsenceTests(unittest.TestCase):
    def setUp(self):
        self.crawler_bytes = _CRAWLER_PATH.read_bytes()

    def test_sentinel_1_bytes_absent(self):
        """First FSS mojibake sentinel (was at L1010) must not reappear."""
        self.assertNotIn(
            _SENTINEL_1_BYTES, self.crawler_bytes,
            "M11.6 removed this mojibake byte sequence from "
            "official_crawler.py. Its return likely means a copy-paste "
            "from a CP949/EUC-KR source mis-encoded as UTF-8.",
        )

    def test_sentinel_2_bytes_absent(self):
        """Second FSS mojibake sentinel (was at L1133) must not reappear."""
        self.assertNotIn(
            _SENTINEL_2_BYTES, self.crawler_bytes,
            "M11.6 removed this mojibake byte sequence from "
            "official_crawler.py. Its return likely means a copy-paste "
            "from a CP949/EUC-KR source mis-encoded as UTF-8.",
        )

    def test_dead_error_message_absent(self):
        """The 'FSS search returned error page' string was only assigned
        inside the two deleted blocks. If it reappears, either the
        dead block was reintroduced or someone restored the check
        intentionally — both are out of scope for M11.6's
        default-to-delete contract."""
        self.assertNotIn(
            _DEAD_ERROR_MESSAGE, self.crawler_bytes,
            "M11.6 expected this error-message string to be gone with "
            "the dead branches that wrote it. If a future PR re-adds "
            "an FSS error-page detector, this pin should be updated "
            "alongside that change.",
        )

    def test_no_lone_question_marks_adjacent_to_hangul(self):
        """Heuristic: literal `?` followed immediately by a Hangul
        syllable inside a quoted string is a classic mojibake
        fingerprint (CP949 byte → invalid UTF-8 fallback). Catches
        new mojibake we don't yet have a specific byte signature for.

        Whitelisted strings (real Korean usage where `?` legitimately
        precedes Hangul, e.g. inside docstrings) are listed below."""
        WHITELIST = ()  # No known legitimate `?<Hangul>` strings.
        crawler_text = self.crawler_bytes.decode("utf-8", errors="strict")
        # Walk character by character so we don't need an external regex
        # engine and we get precise positions for the failure message.
        for i in range(len(crawler_text) - 1):
            if crawler_text[i] != "?":
                continue
            nxt = crawler_text[i + 1]
            if "가" <= nxt <= "힣":
                snippet = crawler_text[max(0, i - 20):i + 30]
                if any(allowed in snippet for allowed in WHITELIST):
                    continue
                self.fail(
                    f"Suspected mojibake `?<Hangul>` at char offset {i}: "
                    f"...{snippet!r}..."
                )


# ---------------------------------------------------------------------------
# Module-shape and behavior smokes.
# ---------------------------------------------------------------------------


class CrawlerStillUsableTests(unittest.TestCase):
    def test_module_imports_cleanly(self):
        import importlib
        import official_crawler

        importlib.reload(official_crawler)
        self.assertTrue(hasattr(official_crawler, "fetch_best_official_document"))

    def test_fetch_best_official_document_returns_expected_shape(self):
        """`fetch_best_official_document` must always return a dict with
        the public-shape keys. This exercises the same return contract
        the deleted dead branches used to populate. Network is mocked so
        the test stays offline and deterministic."""
        from official_crawler import fetch_best_official_document
        import official_crawler

        class _FakeResponse:
            status_code = 200

            def __init__(self, body: str):
                self.text = body
                self.content = body.encode("utf-8")
                self.encoding = "utf-8"
                self.headers = {}

            def raise_for_status(self):
                return None

        empty_korean_page = (
            "<html><head><title>금융감독원 검색</title></head>"
            "<body><p>검색 결과가 없습니다.</p></body></html>"
        )
        with mock.patch.object(
            official_crawler, "_request_url",
            return_value=_FakeResponse(empty_korean_page),
        ):
            result = fetch_best_official_document(
                {
                    "source_name": "Financial Supervisory Service",
                    "source_type": "financial_regulator",
                    "search_query": "사기 대출",
                    "official_search_url": "https://www.fss.or.kr/search?q=test",
                }
            )

        for key in (
            "source_name",
            "source_type",
            "search_query",
            "fetched",
            "usable",
            "weakly_usable",
            "error",
            "search_attempt_results",
        ):
            self.assertIn(
                key, result,
                f"fetch_best_official_document must always include {key!r} "
                "in its result dict — pin the public shape.",
            )
        self.assertIsInstance(result["search_attempt_results"], list)
        # The deleted dead branch would have synthesized this string;
        # with normal Korean input, the fall-through path must not.
        self.assertNotEqual(
            result.get("error"), "FSS search returned error page",
        )


# ---------------------------------------------------------------------------
# Sentinel-1 fall-through behavior: when the FSS search page parses as a
# normal Korean title (not the mojibake the dead branch was guarding
# against), the crawler must continue into link extraction instead of
# returning a hard `usable=False`.
# ---------------------------------------------------------------------------


class Sentinel1FallThroughTests(unittest.TestCase):
    def test_fss_normal_title_does_not_short_circuit(self):
        """A real FSS search page title (with proper UTF-8 Korean) used
        to be checked against the dead mojibake guard. Removing that
        guard means the response continues into the link-extraction
        path. Mock _request_url so the test runs offline."""
        import official_crawler

        class _FakeResponse:
            status_code = 200

            def __init__(self, body: str):
                self._body = body
                self.text = body
                self.content = body.encode("utf-8")
                self.encoding = "utf-8"
                self.headers = {}

            def raise_for_status(self):
                return None

        normal_korean_title = (
            "<html><head><title>금융감독원 통합검색 결과</title></head>"
            "<body><h1>금융감독원 통합검색</h1>"
            "<a href='/detail/1234'>가계대출 동향 보도자료</a>"
            "</body></html>"
        )
        with mock.patch.object(
            official_crawler, "_request_url",
            return_value=_FakeResponse(normal_korean_title),
        ):
            result = official_crawler.fetch_best_official_document(
                {
                    "source_name": "Financial Supervisory Service",
                    "source_type": "financial_regulator",
                    "search_query": "가계대출",
                    "official_search_url": "https://www.fss.or.kr/search?q=가계대출",
                }
            )
        # The deleted dead branch would have set this exact error string;
        # the fall-through path must NOT.
        self.assertNotEqual(
            result.get("error"), "FSS search returned error page",
            "fall-through path must not synthesize the removed dead "
            "branch's error message.",
        )
        # search page itself was fetched OK.
        self.assertTrue(result.get("fetched_search_page"))


if __name__ == "__main__":
    unittest.main()
