"""M11.7a-2 — pins for the 5 remaining Category 2/4 exception sites
flagged by docs/EXCEPTION_HANDLING_AUDIT.md.

M11.7 audit identified 9 broad-except swallow sites. After M11.7a (Sites
1 + 5e), M11.7b (Site 4), and M11.5c (Site 5a, dead code), 5 sites
remained unhandled. M11.7a-2 adds structured ``log.warning`` calls
(or upgrades existing ``log.error`` to structured form) at each
remaining site:

  Site 2  article_extractor.fetch_article_body
           (event: article_extractor.fetch_failed)
  Site 3a news_collector.resolve_google_news_url
           (preserved Korean f-string message + structured extras)
  Site 5b official_crawler._extract_candidate_links
           (event: official_crawler.site_specific_parser_failed)
  Site 5c official_crawler per-attempt retry loop
           (event: official_crawler.attempt_failed)
  Site 5d official_crawler per-candidate evaluation
           (event: official_crawler.candidate_evaluation_failed)

All fixes are LOGGING-ONLY — return shapes are byte-identical. These
pins fire on the failure path AND check the warning was emitted with
the documented event/message + expected ``extra`` payload fields, and
confirm the happy path emits no warning of that event.

Pattern mirrors ``tests/test_m11_7a_category2_logging.py``.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class _CapturingHandler(logging.Handler):
    """Records emitted LogRecords filtered by logger name prefix."""

    def __init__(self, name_prefix: str):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._name_prefix = name_prefix

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == self._name_prefix or record.name.startswith(
            self._name_prefix + "."
        ):
            self.records.append(record)


def _attach_capturing_handler(logger_name: str) -> _CapturingHandler:
    logger = logging.getLogger(logger_name)
    handler = _CapturingHandler(logger_name)
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.WARNING:
        logger.setLevel(logging.WARNING)
    return handler


def _detach_handler(logger_name: str, handler: logging.Handler) -> None:
    logging.getLogger(logger_name).removeHandler(handler)


def _records_with_event(
    records: list[logging.LogRecord], event_name: str
) -> list[logging.LogRecord]:
    return [r for r in records if r.getMessage() == event_name]


def _records_with_substring(
    records: list[logging.LogRecord], substring: str
) -> list[logging.LogRecord]:
    return [r for r in records if substring in r.getMessage()]


# ---------------------------------------------------------------------------
# Site 2 — article_extractor.fetch_article_body
# ---------------------------------------------------------------------------


class ArticleExtractorFetchFailedWarningTests(unittest.TestCase):
    """When `fetch_article_body`'s try-body raises, the function must
    emit `article_extractor.fetch_failed` at WARNING with url,
    max_chars, exception_type, exception_message, fallback_returned —
    BEFORE the 6 pinned `log.error` field-name lines. Return value
    (empty string) preserved."""

    LOGGER_NAME = "article_extractor"
    EVENT_NAME = "article_extractor.fetch_failed"

    def setUp(self):
        self.handler = _attach_capturing_handler(self.LOGGER_NAME)

    def tearDown(self):
        _detach_handler(self.LOGGER_NAME, self.handler)

    def test_fetch_failure_emits_warning_with_extras(self):
        import article_extractor

        with mock.patch.object(
            article_extractor,
            "_fetch_html_candidates",
            side_effect=RuntimeError("simulated network explosion"),
        ):
            result = article_extractor.fetch_article_body(
                "https://example.kr/article/123", max_chars=1234,
            )

        # Return shape preserved: empty string.
        self.assertEqual(result, "")

        # Warning event fired exactly once with expected payload.
        warnings = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(warnings), 1,
            f"Expected exactly one '{self.EVENT_NAME}' record, got "
            f"{[(r.levelname, r.getMessage()) for r in self.handler.records]!r}.",
        )
        record = warnings[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(
            getattr(record, "url"), "https://example.kr/article/123",
        )
        self.assertEqual(getattr(record, "max_chars"), 1234)
        self.assertEqual(getattr(record, "exception_type"), "RuntimeError")
        self.assertIn(
            "simulated network explosion",
            getattr(record, "exception_message"),
        )
        self.assertEqual(
            getattr(record, "fallback_returned"), "empty_string",
        )

        # The 6 pinned log.error field-name lines must STILL fire after
        # the warning — M11.7a-2 must not silence the existing
        # EXCEPTED_EXCEPT_ERRORS contract for article_extractor.py.
        error_records = [
            r for r in self.handler.records if r.levelno == logging.ERROR
        ]
        self.assertEqual(
            len(error_records), 6,
            "M11.7a-2 must not silence the 6 existing log.error lines "
            "inside fetch_article_body's except block. Got "
            f"{[r.getMessage() for r in error_records]!r}.",
        )

    def test_happy_path_emits_no_warning(self):
        """A successful fetch returns a non-empty string and emits no
        `article_extractor.fetch_failed` record."""
        import article_extractor

        long_html = (
            "<html><body>"
            + ("이 기사는 정책 관련 보도입니다. " * 30)
            + "</body></html>"
        )
        candidates = [(long_html, "utf-8", False)]

        with mock.patch.object(
            article_extractor,
            "_fetch_html_candidates",
            return_value=candidates,
        ):
            result = article_extractor.fetch_article_body(
                "https://example.kr/article/ok", max_chars=5000,
            )

        # We don't assert on the exact extracted content (extraction is
        # heuristic) — but we DO assert the fetch_failed warning never
        # fired.
        self.assertIsInstance(result, str)
        warnings = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(warnings), 0,
            "Happy path must not emit the fetch_failed warning.",
        )

    def test_empty_body_fallback_emits_no_warning(self):
        """A legitimate `_is_probably_broken` / short-text fallback
        returns "" via the normal control-flow path (not via the
        except). The fetch_failed warning must remain silent — only
        the actual exception path may fire it."""
        import article_extractor

        # Empty HTML triggers the "extracted < 100 chars" fallback path,
        # which returns "" without going through the except.
        candidates = [("<html><body></body></html>", "utf-8", False)]

        with mock.patch.object(
            article_extractor,
            "_fetch_html_candidates",
            return_value=candidates,
        ):
            result = article_extractor.fetch_article_body(
                "https://example.kr/article/empty", max_chars=5000,
            )

        self.assertEqual(result, "")
        warnings = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(warnings), 0,
            "Legitimate empty-body fallback must not emit the "
            "fetch_failed warning — that path is reserved for actual "
            "exceptions.",
        )


# ---------------------------------------------------------------------------
# Site 3a — news_collector.resolve_google_news_url
# ---------------------------------------------------------------------------


class NewsCollectorResolveGoogleUrlStructuredErrorTests(unittest.TestCase):
    """When `gnewsdecoder` raises, the function must emit a single
    `log.error` whose MESSAGE preserves the Korean substring
    '원문 URL 변환 실패' (pinned by PRESERVED_REAL_ERRORS) AND whose
    `extra` payload now includes url, exception_type, exception_message
    (M11.7a-2 structured upgrade). Happy / short-circuit paths must
    not emit the error."""

    LOGGER_NAME = "news_collector"
    KOREAN_MARKER = "원문 URL 변환 실패"

    def setUp(self):
        self.handler = _attach_capturing_handler(self.LOGGER_NAME)

    def tearDown(self):
        _detach_handler(self.LOGGER_NAME, self.handler)

    def test_decoder_failure_emits_korean_error_with_extras(self):
        import news_collector

        with mock.patch.object(
            news_collector,
            "gnewsdecoder",
            side_effect=ValueError("malformed decode payload"),
        ):
            result = news_collector.resolve_google_news_url(
                "https://news.google.com/rss/articles/abc?x=1",
            )

        # Return shape preserved: original Google URL returned.
        self.assertEqual(
            result, "https://news.google.com/rss/articles/abc?x=1",
        )

        # Exactly one error record matching the Korean marker.
        matching = _records_with_substring(
            self.handler.records, self.KOREAN_MARKER,
        )
        self.assertEqual(
            len(matching), 1,
            f"Expected exactly one record containing "
            f"{self.KOREAN_MARKER!r}, got "
            f"{[r.getMessage() for r in self.handler.records]!r}.",
        )
        record = matching[0]
        self.assertEqual(record.levelno, logging.ERROR)
        # Korean message text preserved verbatim (pin compatibility).
        self.assertIn(self.KOREAN_MARKER, record.getMessage())
        # M11.7a-2 structured extras present.
        self.assertEqual(
            getattr(record, "url"),
            "https://news.google.com/rss/articles/abc?x=1",
        )
        self.assertEqual(getattr(record, "exception_type"), "ValueError")
        self.assertIn(
            "malformed decode payload",
            getattr(record, "exception_message"),
        )

    def test_happy_decode_emits_no_error(self):
        import news_collector

        decoded = {
            "status": True,
            "decoded_url": "https://www.chosun.com/article/123",
        }
        with mock.patch.object(
            news_collector,
            "gnewsdecoder",
            return_value=decoded,
        ):
            result = news_collector.resolve_google_news_url(
                "https://news.google.com/rss/articles/happy",
            )

        self.assertEqual(result, "https://www.chosun.com/article/123")
        matching = _records_with_substring(
            self.handler.records, self.KOREAN_MARKER,
        )
        self.assertEqual(
            len(matching), 0,
            "Happy-path decode must not emit the URL-decode error.",
        )

    def test_non_google_url_short_circuit_no_error(self):
        """When the URL is already a non-Google host, the function
        short-circuits BEFORE the try-block — no decode attempt, no
        error log."""
        import news_collector

        with mock.patch.object(
            news_collector, "gnewsdecoder",
        ) as mocked_decoder:
            result = news_collector.resolve_google_news_url(
                "https://www.fss.or.kr/notice/123",
            )

        self.assertEqual(result, "https://www.fss.or.kr/notice/123")
        mocked_decoder.assert_not_called()
        matching = _records_with_substring(
            self.handler.records, self.KOREAN_MARKER,
        )
        self.assertEqual(
            len(matching), 0,
            "Non-Google URL must not invoke the decoder or emit the "
            "URL-decode error.",
        )


# ---------------------------------------------------------------------------
# Site 5b — official_crawler._extract_candidate_links
# ---------------------------------------------------------------------------


class OfficialCrawlerSiteSpecificParserFailedTests(unittest.TestCase):
    """When `extract_links_for_site` raises, the function must emit
    `official_crawler.site_specific_parser_failed` at WARNING with
    source_name, search_url, query, exception_type/message,
    fallback_returned. It must then fall through to the generic
    fallback parser (return shape preserved)."""

    LOGGER_NAME = "official_crawler"
    EVENT_NAME = "official_crawler.site_specific_parser_failed"

    def setUp(self):
        self.handler = _attach_capturing_handler(self.LOGGER_NAME)

    def tearDown(self):
        _detach_handler(self.LOGGER_NAME, self.handler)

    def test_site_specific_parser_failure_emits_warning_and_falls_through(self):
        import official_crawler

        with mock.patch.object(
            official_crawler,
            "extract_links_for_site",
            side_effect=ValueError("malformed site HTML"),
        ), mock.patch.object(
            official_crawler,
            "extract_official_result_links",
            return_value=[
                {"url": "https://example.kr/fallback/1", "score": 30, "text": "fallback"},
            ],
        ):
            candidates, parser_used = official_crawler._extract_candidate_links(
                search_html="<html></html>",
                search_url="https://www.fss.or.kr/search?q=test",
                source_name="Financial Supervisory Service",
                query="대출 한도",
            )

        # Return shape: falls through to generic fallback.
        self.assertEqual(parser_used, "generic_fallback")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["url"], "https://example.kr/fallback/1")

        # Warning fired with expected payload.
        warnings = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(warnings), 1,
            f"Expected exactly one '{self.EVENT_NAME}' record, got "
            f"{[r.getMessage() for r in self.handler.records]!r}.",
        )
        record = warnings[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(
            getattr(record, "source_name"),
            "Financial Supervisory Service",
        )
        self.assertEqual(
            getattr(record, "search_url"),
            "https://www.fss.or.kr/search?q=test",
        )
        self.assertEqual(getattr(record, "query"), "대출 한도")
        self.assertEqual(getattr(record, "exception_type"), "ValueError")
        self.assertIn(
            "malformed site HTML", getattr(record, "exception_message"),
        )
        self.assertEqual(
            getattr(record, "fallback_returned"), "generic_fallback",
        )

    def test_happy_site_specific_parser_no_warning(self):
        import official_crawler

        with mock.patch.object(
            official_crawler,
            "extract_links_for_site",
            return_value=[
                {"url": "https://www.fss.or.kr/n/123", "score": 100, "text": "ok"},
            ],
        ):
            candidates, parser_used = official_crawler._extract_candidate_links(
                search_html="<html></html>",
                search_url="https://www.fss.or.kr/search?q=test",
                source_name="Financial Supervisory Service",
                query="대출 한도",
            )

        self.assertEqual(parser_used, "site_specific")
        self.assertEqual(len(candidates), 1)
        warnings = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(warnings), 0,
            "Happy site-specific parse must not emit the failure warning.",
        )


# ---------------------------------------------------------------------------
# Shared helper: build a search-result dict that exercises the
# fetch_best_official_document outer try without forcing source-specific
# code-paths (FSC / IBK / Bank of Korea).
# ---------------------------------------------------------------------------


def _build_search_result_for_fss(variants: list[str]) -> dict:
    return {
        "source_name": "Financial Supervisory Service",
        "source_type": "financial_regulator",
        "search_query": variants[0] if variants else "",
        "search_query_variants": variants,
        "official_search_url": "https://www.fss.or.kr/search?q=test",
    }


def _fake_response(status_code: int = 200, body: bytes = b"<html></html>"):
    response = mock.MagicMock()
    response.status_code = status_code
    response.content = body
    response.headers = {}
    response.raise_for_status = mock.MagicMock()
    response.text = body.decode("utf-8", errors="replace")
    return response


# ---------------------------------------------------------------------------
# Site 5c — official_crawler per-attempt retry loop
# ---------------------------------------------------------------------------


class OfficialCrawlerAttemptFailedTests(unittest.TestCase):
    """When a per-attempt fetch raises inside the retry loop, the
    function must emit `official_crawler.attempt_failed` at WARNING
    with source_name, site_key, attempt_query, attempt_url,
    exception_type/message. The attempt_result is still appended
    to search_attempt_results with the error string (return shape
    preserved)."""

    LOGGER_NAME = "official_crawler"
    EVENT_NAME = "official_crawler.attempt_failed"

    def setUp(self):
        self.handler = _attach_capturing_handler(self.LOGGER_NAME)

    def tearDown(self):
        _detach_handler(self.LOGGER_NAME, self.handler)

    def test_per_attempt_request_failure_emits_warning(self):
        import official_crawler

        call_count = {"n": 0}

        def request_side_effect(url):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # initial search succeeds — returns fake response
                return _fake_response()
            # all subsequent (per-attempt) requests raise
            raise RuntimeError(f"simulated attempt failure for {url}")

        with mock.patch.object(
            official_crawler, "_request_url",
            side_effect=request_side_effect,
        ), mock.patch.object(
            official_crawler, "_extract_candidate_links",
            return_value=([], "site_specific"),
        ), mock.patch.object(
            official_crawler, "_count_rejected_links", return_value=0,
        ), mock.patch.object(
            official_crawler, "_extract_html_text",
            return_value=("Mock Title", "x" * 250),
        ), mock.patch.object(
            official_crawler, "extract_rendered_links", None,
        ):
            result = official_crawler.fetch_best_official_document(
                _build_search_result_for_fss(
                    ["원본 질의", "변형 질의 1", "변형 질의 2"],
                ),
            )

        # Return shape sanity: outer fetch_best_official_document
        # produced a result dict, not None.
        self.assertIsInstance(result, dict)
        # At least one per-attempt error captured in search_attempt_results
        # (plus the first-attempt success record).
        attempts = result.get("search_attempt_results") or []
        attempt_errors = [a for a in attempts if a.get("error")]
        self.assertGreaterEqual(
            len(attempt_errors), 1,
            "Per-attempt error string must still be captured on the "
            "attempt_result — return shape preserved.",
        )
        self.assertTrue(
            any(
                "simulated attempt failure" in (a.get("error") or "")
                for a in attempt_errors
            ),
            "Per-attempt error string must include the raised "
            "exception message.",
        )

        # At least one attempt_failed warning fired (one per failing
        # attempt URL). Each must carry the expected extras.
        warnings = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertGreaterEqual(
            len(warnings), 1,
            f"Expected at least one '{self.EVENT_NAME}' record, got "
            f"{[r.getMessage() for r in self.handler.records]!r}.",
        )
        record = warnings[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(
            getattr(record, "source_name"),
            "Financial Supervisory Service",
        )
        self.assertEqual(getattr(record, "site_key"), "fss")
        self.assertIn("RuntimeError", getattr(record, "exception_type"))
        self.assertIn(
            "simulated attempt failure",
            getattr(record, "exception_message"),
        )
        # attempt_url should be a string (truncated, but non-empty).
        self.assertIsInstance(getattr(record, "attempt_url"), str)
        self.assertGreater(len(getattr(record, "attempt_url")), 0)

    def test_outer_wrapper_failure_does_not_double_log_attempt_failed(self):
        """If the OUTER try-block raises (M11.7a Site 5e path), only
        the outer_wrapper_failure warning fires — the inner
        attempt_failed warning must remain silent because the
        per-attempt loop is never reached."""
        import official_crawler

        with mock.patch.object(
            official_crawler, "_request_url",
            side_effect=RuntimeError("outer-only explosion"),
        ):
            official_crawler.fetch_best_official_document(
                _build_search_result_for_fss(["원본 질의"]),
            )

        attempt_warnings = _records_with_event(
            self.handler.records, self.EVENT_NAME,
        )
        outer_warnings = _records_with_event(
            self.handler.records,
            "official_crawler.outer_wrapper_failure",
        )
        self.assertEqual(
            len(attempt_warnings), 0,
            "Outer-wrapper-only failures must not emit attempt_failed.",
        )
        self.assertGreaterEqual(
            len(outer_warnings), 1,
            "M11.7a Site 5e outer_wrapper_failure must still fire when "
            "the outer try-block raises.",
        )

    def test_happy_first_attempt_no_attempt_failed_warning(self):
        """When the initial search yields candidate links, the per-
        attempt retry loop never activates — no attempt_failed
        warning must fire."""
        import official_crawler

        ok_candidates = [
            {
                "url": "https://www.fss.or.kr/notice/1",
                "score": 100,
                "text": "공고: 대출 한도 규제 강화",
                "is_detail_page": True,
                "id_detected": True,
                "url_depth_score": 3,
                "reason": "site-specific",
            },
        ]

        with mock.patch.object(
            official_crawler, "_request_url",
            return_value=_fake_response(),
        ), mock.patch.object(
            official_crawler, "_extract_candidate_links",
            return_value=(ok_candidates, "site_specific"),
        ), mock.patch.object(
            official_crawler, "_count_rejected_links", return_value=0,
        ), mock.patch.object(
            official_crawler, "_extract_html_text",
            return_value=("Mock Title", "x" * 250),
        ), mock.patch.object(
            official_crawler, "extract_rendered_links", None,
        ), mock.patch.object(
            official_crawler, "is_bad_official_link", return_value=False,
        ), mock.patch.object(
            official_crawler, "score_document_relevance",
            return_value={
                "relevance_score": 80,
                "relevance_level": "high",
                "matched_query_terms": [],
                "matched_concepts": [],
                "relevance_reasons": [],
                "error_page_detected": False,
                "error_page_reason": None,
            },
        ), mock.patch.object(
            official_crawler, "_extract_document_content",
            return_value={
                "document_title": "공고: 대출 한도 규제 강화",
                "document_text_snippet": "본문 내용",
                "document_text_length": 5,
                "document_title_quality": "specific",
                "extraction_method": "stub",
            },
        ), mock.patch.object(
            official_crawler, "classify_official_document",
            return_value={"evidence_grade": "A", "document_type": "notice"},
        ):
            official_crawler.fetch_best_official_document(
                _build_search_result_for_fss(["원본 질의"]),
            )

        attempt_warnings = _records_with_event(
            self.handler.records, self.EVENT_NAME,
        )
        self.assertEqual(
            len(attempt_warnings), 0,
            "Happy first-attempt path must not emit attempt_failed.",
        )


# ---------------------------------------------------------------------------
# Site 5d — official_crawler per-candidate evaluation
# ---------------------------------------------------------------------------


class OfficialCrawlerCandidateEvaluationFailedTests(unittest.TestCase):
    """When per-candidate document evaluation raises, the function
    must emit `official_crawler.candidate_evaluation_failed` at
    WARNING with source_name, site_key, candidate_url, candidate_score,
    exception_type/message, fallback_relevance_level. The candidate
    is still marked relevance_level='error_page' (return shape
    preserved)."""

    LOGGER_NAME = "official_crawler"
    EVENT_NAME = "official_crawler.candidate_evaluation_failed"

    def setUp(self):
        self.handler = _attach_capturing_handler(self.LOGGER_NAME)

    def tearDown(self):
        _detach_handler(self.LOGGER_NAME, self.handler)

    def test_candidate_eval_failure_emits_warning(self):
        import official_crawler

        candidate = {
            "url": "https://www.fss.or.kr/notice/777",
            "score": 100,
            "text": "공고: 대출 한도 규제 강화",
            "is_detail_page": True,
            "id_detected": True,
            "url_depth_score": 3,
            "reason": "site-specific",
        }

        call_count = {"n": 0}

        def request_side_effect(url):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # initial search succeeds
                return _fake_response()
            # per-candidate fetch raises
            raise ConnectionError(f"simulated candidate fetch failure for {url}")

        with mock.patch.object(
            official_crawler, "_request_url",
            side_effect=request_side_effect,
        ), mock.patch.object(
            official_crawler, "_extract_candidate_links",
            return_value=([candidate], "site_specific"),
        ), mock.patch.object(
            official_crawler, "_count_rejected_links", return_value=0,
        ), mock.patch.object(
            official_crawler, "_extract_html_text",
            return_value=("Mock Title", "x" * 250),
        ), mock.patch.object(
            official_crawler, "extract_rendered_links", None,
        ), mock.patch.object(
            official_crawler, "is_bad_official_link", return_value=False,
        ):
            result = official_crawler.fetch_best_official_document(
                _build_search_result_for_fss(["원본 질의"]),
            )

        # Candidate marked as error_page (return shape preserved).
        self.assertIsInstance(result, dict)
        cands = result.get("candidate_links") or []
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].get("relevance_level"), "error_page")
        self.assertEqual(cands[0].get("relevance_score"), 0)
        self.assertIn(
            "simulated candidate fetch failure",
            cands[0].get("relevance_error") or "",
        )

        # Warning fired with expected payload.
        warnings = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(warnings), 1,
            f"Expected exactly one '{self.EVENT_NAME}' record, got "
            f"{[r.getMessage() for r in self.handler.records]!r}.",
        )
        record = warnings[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(
            getattr(record, "source_name"),
            "Financial Supervisory Service",
        )
        self.assertEqual(getattr(record, "site_key"), "fss")
        self.assertEqual(
            getattr(record, "candidate_url"),
            "https://www.fss.or.kr/notice/777",
        )
        self.assertEqual(getattr(record, "candidate_score"), 100)
        self.assertEqual(getattr(record, "exception_type"), "ConnectionError")
        self.assertIn(
            "simulated candidate fetch failure",
            getattr(record, "exception_message"),
        )
        self.assertEqual(
            getattr(record, "fallback_relevance_level"), "error_page",
        )

    def test_happy_candidate_eval_no_warning(self):
        """When the per-candidate fetch + scoring path succeeds, the
        candidate_evaluation_failed warning must remain silent."""
        import official_crawler

        candidate = {
            "url": "https://www.fss.or.kr/notice/888",
            "score": 100,
            "text": "공고: 대출 한도 규제 강화",
            "is_detail_page": True,
            "id_detected": True,
            "url_depth_score": 3,
            "reason": "site-specific",
        }

        with mock.patch.object(
            official_crawler, "_request_url",
            return_value=_fake_response(),
        ), mock.patch.object(
            official_crawler, "_extract_candidate_links",
            return_value=([candidate], "site_specific"),
        ), mock.patch.object(
            official_crawler, "_count_rejected_links", return_value=0,
        ), mock.patch.object(
            official_crawler, "_extract_html_text",
            return_value=("Mock Title", "x" * 250),
        ), mock.patch.object(
            official_crawler, "extract_rendered_links", None,
        ), mock.patch.object(
            official_crawler, "is_bad_official_link", return_value=False,
        ), mock.patch.object(
            official_crawler, "score_document_relevance",
            return_value={
                "relevance_score": 80,
                "relevance_level": "high",
                "matched_query_terms": [],
                "matched_concepts": [],
                "relevance_reasons": [],
                "error_page_detected": False,
                "error_page_reason": None,
            },
        ), mock.patch.object(
            official_crawler, "_extract_document_content",
            return_value={
                "document_title": "공고: 대출 한도 규제 강화",
                "document_text_snippet": "본문 내용",
                "document_text_length": 5,
                "document_title_quality": "specific",
                "extraction_method": "stub",
            },
        ), mock.patch.object(
            official_crawler, "classify_official_document",
            return_value={"evidence_grade": "A", "document_type": "notice"},
        ):
            official_crawler.fetch_best_official_document(
                _build_search_result_for_fss(["원본 질의"]),
            )

        warnings = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(warnings), 0,
            "Happy per-candidate eval must not emit the failure warning.",
        )


if __name__ == "__main__":
    unittest.main()
