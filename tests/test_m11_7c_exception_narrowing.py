"""M11.7c — exception narrowing review pins.

After M11.7a-2 added structured logging at 5 broad-except sites,
M11.7c was the planned follow-up to NARROW each `except Exception`
to specific types. The narrowing review concluded that ALL 5 sites
should remain `except Exception` — narrowing was intentionally
deferred because:

  Site 2  article_extractor.fetch_article_body:
          try-body fans out across requests + trafilatura + BS4 with
          undocumented exception surfaces. Audit Category 3
          "best-effort extraction" boundary.

  Site 3a news_collector.resolve_google_news_url:
          googlenewsdecoder/decoderv2.py raises bare
          `Exception("Failed to fetch data from Google.")` — narrowing
          to ANY specific tuple would silently fail to catch library
          errors. Decisive technical finding.

  Site 5b official_crawler._extract_candidate_links:
          fallback contract — broken site-specific parser MUST NOT
          block the generic_fallback parser. Broad catch is mandatory.

  Site 5c official_crawler per-attempt retry:
          try-body fans out across HTTP + BS4 + extractors. Narrowing
          would mis-classify parse errors as outer-wrapper failures.
          M11.7a-2 `exception_type` field now collects production data
          for future evidence-based narrowing.

  Site 5d official_crawler per-candidate evaluation:
          same fan-out problem as Site 5c — HTTP + BS4 + scoring.
          Pending production-log audit.

These pins guard against future "cleanup" PRs that narrow these
handlers without operator approval. Each pin:

  (a) statically asserts the handler at the site is `except Exception`
      (not narrowed to a tuple or a more specific class),
  (b) statically asserts an `M11.7c:` inline-comment marker is present
      in the handler body documenting that the broad catch is
      intentional, and
  (c) runtime-asserts that the broad catch correctly captures both
      the primary expected exception (e.g., requests.ConnectionError)
      AND — for Site 3a specifically — a bare `Exception("...")`
      raised by the library.

This is a narrowing-deferred milestone. No production behavior change.
"""

from __future__ import annotations

import ast
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Static AST + source-text helpers
# ---------------------------------------------------------------------------


def _read_source(filename: str) -> str:
    return (_PROJECT_ROOT / filename).read_text(encoding="utf-8")


def _parse(filename: str) -> ast.Module:
    return ast.parse(_read_source(filename), filename=filename)


def _find_function(tree: ast.Module, qualified_name: str) -> ast.AST:
    """Return the FunctionDef node for ``qualified_name``.

    Supports nested names like ``Outer.inner`` is NOT used here — these
    are all module-level top-level defs.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == qualified_name:
                return node
    raise AssertionError(
        f"function {qualified_name!r} not found in module tree"
    )


def _except_handlers_in(node: ast.AST) -> list[ast.ExceptHandler]:
    """Return every ExceptHandler nested under ``node``."""
    handlers: list[ast.ExceptHandler] = []
    for child in ast.walk(node):
        if isinstance(child, ast.ExceptHandler):
            handlers.append(child)
    return handlers


def _handler_catches_exception_only(handler: ast.ExceptHandler) -> bool:
    """True iff the handler is `except Exception [as X]:` — exactly the
    broad-Exception form, not narrower and not a tuple."""
    t = handler.type
    return isinstance(t, ast.Name) and t.id == "Exception"


def _handler_body_text(
    source: str, handler: ast.ExceptHandler
) -> str:
    """Return the source text from the line immediately after the
    `except` keyword down to the end-line of the handler body
    (inclusive). Used to grep for the M11.7c marker comment."""
    if handler.end_lineno is None:
        return ""
    lines = source.splitlines()
    # Handler lineno is the `except` line. Body starts on the next line.
    start = handler.lineno  # 1-based; this index `handler.lineno`
    end = handler.end_lineno
    # Bounds-check defensively even though end_lineno is well-defined
    # for Python 3.8+.
    start = max(start, 1)
    end = min(end, len(lines))
    return "\n".join(lines[start:end])


def _find_marker_handler(
    filename: str, function_name: str, marker_substrings: tuple[str, ...],
) -> ast.ExceptHandler:
    """Locate the single ExceptHandler inside ``function_name`` in
    ``filename`` whose body text contains EVERY string in
    ``marker_substrings``. Fail loudly if zero or multiple match.
    """
    source = _read_source(filename)
    tree = ast.parse(source, filename=filename)
    func = _find_function(tree, function_name)
    matches: list[ast.ExceptHandler] = []
    for handler in _except_handlers_in(func):
        body_text = _handler_body_text(source, handler)
        if all(m in body_text for m in marker_substrings):
            matches.append(handler)
    if not matches:
        raise AssertionError(
            f"{filename}::{function_name}: no ExceptHandler contains "
            f"all of {marker_substrings!r}. The M11.7c marker comment "
            "was removed or moved — restore it or update this pin."
        )
    if len(matches) > 1:
        raise AssertionError(
            f"{filename}::{function_name}: {len(matches)} ExceptHandlers "
            f"contain all of {marker_substrings!r}; expected exactly 1."
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Runtime warning-capture helper (same shape as M11.7a-2 tests).
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    def __init__(self, name_prefix: str):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._name_prefix = name_prefix

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == self._name_prefix or record.name.startswith(
            self._name_prefix + "."
        ):
            self.records.append(record)


def _attach(logger_name: str) -> _CapturingHandler:
    logger = logging.getLogger(logger_name)
    handler = _CapturingHandler(logger_name)
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.WARNING:
        logger.setLevel(logging.WARNING)
    return handler


def _detach(logger_name: str, handler: logging.Handler) -> None:
    logging.getLogger(logger_name).removeHandler(handler)


def _fake_response(status_code: int = 200, body: bytes = b"<html></html>"):
    response = mock.MagicMock()
    response.status_code = status_code
    response.content = body
    response.headers = {}
    response.raise_for_status = mock.MagicMock()
    response.text = body.decode("utf-8", errors="replace")
    return response


def _build_search_result_for_fss(variants: list[str]) -> dict:
    return {
        "source_name": "Financial Supervisory Service",
        "source_type": "financial_regulator",
        "search_query": variants[0] if variants else "",
        "search_query_variants": variants,
        "official_search_url": "https://www.fss.or.kr/search?q=test",
    }


# ---------------------------------------------------------------------------
# Class 1 — Static AST pins (5 tests)
# ---------------------------------------------------------------------------


# Each row: (description, filename, function_name, marker_substrings).
_M11_7C_MARKER_SITES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "Site 2 article_extractor.fetch_article_body",
        "article_extractor.py",
        "fetch_article_body",
        ("M11.7c:", "intentionally broad"),
    ),
    (
        "Site 3a news_collector.resolve_google_news_url",
        "news_collector.py",
        "resolve_google_news_url",
        ("M11.7c:", "intentionally broad", "googlenewsdecoder"),
    ),
    (
        "Site 5b official_crawler._extract_candidate_links",
        "official_crawler.py",
        "_extract_candidate_links",
        ("M11.7c:", "intentionally broad"),
    ),
    (
        "Site 5c official_crawler per-attempt retry",
        "official_crawler.py",
        "fetch_best_official_document",  # outer function containing the inner per-attempt try
        ("M11.7c:", "intentionally broad", "Site 5c"),
    ),
    (
        "Site 5d official_crawler per-candidate evaluation",
        "official_crawler.py",
        "fetch_best_official_document",
        ("M11.7c:", "intentionally broad", "Site 5d"),
    ),
)


class BroadExceptHandlersIntentionalPin(unittest.TestCase):
    """Static pin: each of the 5 sites must (a) still catch broad
    `except Exception` (not narrowed) and (b) carry an M11.7c marker
    comment documenting the rationale. Guards against future
    "cleanup" PRs that try to narrow these handlers without explicit
    operator approval."""

    def _check_site(
        self,
        description: str,
        filename: str,
        function_name: str,
        marker_substrings: tuple[str, ...],
    ) -> None:
        handler = _find_marker_handler(
            filename, function_name, marker_substrings,
        )
        self.assertTrue(
            _handler_catches_exception_only(handler),
            f"{description}: handler at {filename}:{handler.lineno} is "
            f"no longer `except Exception` — it appears to have been "
            f"narrowed without an M11.7c-style review. Restore broad "
            f"`except Exception` OR open a new milestone to justify "
            f"the narrowing.",
        )

    def test_site_2_broad_with_marker(self):
        self._check_site(*_M11_7C_MARKER_SITES[0])

    def test_site_3a_broad_with_marker(self):
        self._check_site(*_M11_7C_MARKER_SITES[1])

    def test_site_5b_broad_with_marker(self):
        self._check_site(*_M11_7C_MARKER_SITES[2])

    def test_site_5c_broad_with_marker(self):
        self._check_site(*_M11_7C_MARKER_SITES[3])

    def test_site_5d_broad_with_marker(self):
        self._check_site(*_M11_7C_MARKER_SITES[4])


# ---------------------------------------------------------------------------
# Class 2 — Library bare-Exception runtime pin (Site 3a, 1 test)
# ---------------------------------------------------------------------------


class Site3aCatchesLibraryBareExceptionPin(unittest.TestCase):
    """Decisive runtime pin for the Site 3a narrowing decision. The
    googlenewsdecoder library raises BARE `Exception("...")` (not a
    subclass) at three sites in decoderv2.py. The Site 3a broad catch
    is the only handler that can reliably swallow these. This pin
    asserts the bare-Exception path is correctly caught and the
    fallback (return original Google URL) fires.

    If a future PR narrows Site 3a, this test will FAIL with an
    uncaught exception, surfacing the regression immediately."""

    LOGGER_NAME = "news_collector"
    KOREAN_MARKER = "원문 URL 변환 실패"

    def setUp(self):
        self.handler = _attach(self.LOGGER_NAME)

    def tearDown(self):
        _detach(self.LOGGER_NAME, self.handler)

    def test_bare_library_exception_is_caught(self):
        import news_collector

        def raise_bare_exception(url):
            # Mirrors the literal raise in
            # googlenewsdecoder/decoderv2.py L26.
            raise Exception("Failed to fetch data from Google.")

        with mock.patch.object(
            news_collector, "gnewsdecoder",
            side_effect=raise_bare_exception,
        ):
            result = news_collector.resolve_google_news_url(
                "https://news.google.com/rss/articles/library-fail",
            )

        # Fallback behavior preserved.
        self.assertEqual(
            result,
            "https://news.google.com/rss/articles/library-fail",
        )

        # The existing log.error fires (Korean message + structured extras).
        matching = [
            r for r in self.handler.records
            if self.KOREAN_MARKER in r.getMessage()
        ]
        self.assertEqual(
            len(matching), 1,
            f"Expected exactly one record containing "
            f"{self.KOREAN_MARKER!r}; got "
            f"{[r.getMessage() for r in self.handler.records]!r}. "
            "Did Site 3a get narrowed? Broad `except Exception` is "
            "required to catch the library's bare Exception() raises.",
        )
        record = matching[0]
        self.assertEqual(record.levelno, logging.ERROR)
        # `exception_type` extra was added in M11.7a-2 and must remain.
        self.assertEqual(getattr(record, "exception_type"), "Exception")
        self.assertIn(
            "Failed to fetch data from Google",
            getattr(record, "exception_message"),
        )


# ---------------------------------------------------------------------------
# Class 3 — Each broad catch still catches its primary runtime exception
# ---------------------------------------------------------------------------


class Site2CatchesHttpFailurePin(unittest.TestCase):
    """Site 2 broad catch must still swallow a `requests.ConnectionError`
    raised by `_fetch_html_candidates` — the original audit failure
    scenario. If Site 2 is later narrowed and the narrow tuple omits
    a relevant type, this pin would surface the regression."""

    LOGGER_NAME = "article_extractor"
    EVENT_NAME = "article_extractor.fetch_failed"

    def setUp(self):
        self.handler = _attach(self.LOGGER_NAME)

    def tearDown(self):
        _detach(self.LOGGER_NAME, self.handler)

    def test_connection_error_is_caught(self):
        import article_extractor
        import requests

        with mock.patch.object(
            article_extractor, "_fetch_html_candidates",
            side_effect=requests.ConnectionError("DNS lookup failed"),
        ):
            result = article_extractor.fetch_article_body(
                "https://example.kr/article/conn-err",
            )

        self.assertEqual(result, "")
        warnings = [
            r for r in self.handler.records
            if r.getMessage() == self.EVENT_NAME
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            getattr(warnings[0], "exception_type"),
            "ConnectionError",
        )


class Site3aCatchesRequestsTimeoutPin(unittest.TestCase):
    """Site 3a broad catch must also handle the requests-family
    exceptions the library internally raises before its own
    bare-Exception layer (e.g., a Timeout reaching the library)."""

    LOGGER_NAME = "news_collector"
    KOREAN_MARKER = "원문 URL 변환 실패"

    def setUp(self):
        self.handler = _attach(self.LOGGER_NAME)

    def tearDown(self):
        _detach(self.LOGGER_NAME, self.handler)

    def test_requests_timeout_is_caught(self):
        import news_collector
        import requests

        with mock.patch.object(
            news_collector, "gnewsdecoder",
            side_effect=requests.Timeout("decode timed out"),
        ):
            result = news_collector.resolve_google_news_url(
                "https://news.google.com/rss/articles/timeout",
            )

        self.assertEqual(
            result, "https://news.google.com/rss/articles/timeout",
        )
        matching = [
            r for r in self.handler.records
            if self.KOREAN_MARKER in r.getMessage()
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            getattr(matching[0], "exception_type"), "Timeout",
        )


class Site5bCatchesBs4AttributeErrorPin(unittest.TestCase):
    """Site 5b broad catch protects the generic_fallback contract.
    A BS4 AttributeError raised by `extract_links_for_site` (e.g.,
    on a layout change in a per-site parser) must be caught and the
    function must fall through to `extract_official_result_links`."""

    LOGGER_NAME = "official_crawler"
    EVENT_NAME = "official_crawler.site_specific_parser_failed"

    def setUp(self):
        self.handler = _attach(self.LOGGER_NAME)

    def tearDown(self):
        _detach(self.LOGGER_NAME, self.handler)

    def test_attribute_error_falls_through_to_generic_fallback(self):
        import official_crawler

        with mock.patch.object(
            official_crawler, "extract_links_for_site",
            side_effect=AttributeError(
                "'NoneType' object has no attribute 'find_all'",
            ),
        ), mock.patch.object(
            official_crawler, "extract_official_result_links",
            return_value=[
                {"url": "https://example.kr/g/1", "score": 30, "text": "g"},
            ],
        ):
            candidates, parser_used = official_crawler._extract_candidate_links(
                search_html="<html></html>",
                search_url="https://www.fss.or.kr/search?q=test",
                source_name="Financial Supervisory Service",
                query="대출 한도",
            )

        # Fall-through contract preserved.
        self.assertEqual(parser_used, "generic_fallback")
        self.assertEqual(len(candidates), 1)

        # M11.7a-2 warning fires with AttributeError captured.
        warnings = [
            r for r in self.handler.records
            if r.getMessage() == self.EVENT_NAME
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            getattr(warnings[0], "exception_type"), "AttributeError",
        )


class Site5cCatchesHttpFailurePin(unittest.TestCase):
    """Site 5c broad catch must handle a `requests.ConnectionError`
    raised mid-attempt by `_request_url`. The attempt is recorded,
    the loop continues, and the M11.7a-2 warning fires."""

    LOGGER_NAME = "official_crawler"
    EVENT_NAME = "official_crawler.attempt_failed"

    def setUp(self):
        self.handler = _attach(self.LOGGER_NAME)

    def tearDown(self):
        _detach(self.LOGGER_NAME, self.handler)

    def test_per_attempt_connection_error_is_caught(self):
        import official_crawler
        import requests

        call_count = {"n": 0}

        def request_side_effect(url):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _fake_response()
            raise requests.ConnectionError(
                f"DNS resolution failed for {url}",
            )

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
            return_value=("Mock", "x" * 250),
        ), mock.patch.object(
            official_crawler, "extract_rendered_links", None,
        ):
            official_crawler.fetch_best_official_document(
                _build_search_result_for_fss(
                    ["원본 질의", "변형 질의 1", "변형 질의 2"],
                ),
            )

        warnings = [
            r for r in self.handler.records
            if r.getMessage() == self.EVENT_NAME
        ]
        self.assertGreaterEqual(len(warnings), 1)
        self.assertEqual(
            getattr(warnings[0], "exception_type"), "ConnectionError",
        )


class Site5dCatchesHttpErrorPin(unittest.TestCase):
    """Site 5d broad catch must handle a `requests.HTTPError` raised
    by per-candidate `_request_url`. The candidate is marked
    `error_page` and the M11.7a-2 warning fires."""

    LOGGER_NAME = "official_crawler"
    EVENT_NAME = "official_crawler.candidate_evaluation_failed"

    def setUp(self):
        self.handler = _attach(self.LOGGER_NAME)

    def tearDown(self):
        _detach(self.LOGGER_NAME, self.handler)

    def test_per_candidate_http_error_is_caught(self):
        import official_crawler
        import requests

        candidate = {
            "url": "https://www.fss.or.kr/notice/999",
            "score": 100,
            "text": "공고: 정책 자료",
            "is_detail_page": True,
            "id_detected": True,
            "url_depth_score": 3,
            "reason": "site-specific",
        }

        call_count = {"n": 0}

        def request_side_effect(url):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _fake_response()
            raise requests.HTTPError(f"503 Service Unavailable for {url}")

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
            return_value=("Mock", "x" * 250),
        ), mock.patch.object(
            official_crawler, "extract_rendered_links", None,
        ), mock.patch.object(
            official_crawler, "is_bad_official_link", return_value=False,
        ):
            result = official_crawler.fetch_best_official_document(
                _build_search_result_for_fss(["원본 질의"]),
            )

        # Candidate marked as error_page (M11.7a-2 return-shape pin).
        cands = result.get("candidate_links") or []
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].get("relevance_level"), "error_page")

        warnings = [
            r for r in self.handler.records
            if r.getMessage() == self.EVENT_NAME
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            getattr(warnings[0], "exception_type"), "HTTPError",
        )


if __name__ == "__main__":
    unittest.main()
