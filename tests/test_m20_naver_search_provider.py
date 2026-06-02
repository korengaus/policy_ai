"""Tests for the M20 Phase 1 Naver news SearchProvider.

Run with: python tests/test_m20_naver_search_provider.py

Covers:
* Normal normalization (b-tag + HTML-entity stripping; original_url ==
  originallink; pubDate parses; source == "naver_api"; total_available
  from "total"; google_link == original_url).
* Empty result (total:0, items:[] -> items==[], available=True, error None).
* Key-absent disable (resolver returns DisabledSearchProvider; available=False;
  reason populated; requests.get assert_not_called()).
* 429 / quota + error-body variant (-> items==[], error set, never raises).
* Malformed JSON / unexpected shape (items missing or non-list -> empty +
  error, no exception).
* display/start clamping (limit>100 / start>1000 clamped to Naver maxima).
* config.describe_naver_config reports presence booleans only (no secrets).

NO real API call is ever made — requests.get is always patched.
"""

from __future__ import annotations

import os
import sys
import unittest
from email.utils import parsedate_to_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import config  # noqa: E402
from providers import (  # noqa: E402
    DisabledSearchProvider,
    MockNaverSearchProvider,
    NaverNewsSearchProvider,
    get_search_provider,
)
from providers.naver_search import MAX_DISPLAY, MAX_START  # noqa: E402


# ---------------------------------------------------------------------------
# Env scope helper — mirrors test_m13_1b_openai_provider._EnvScope.
# ---------------------------------------------------------------------------


class _EnvScope:
    KEYS = (
        "NAVER_CLIENT_ID",
        "NAVER_CLIENT_SECRET",
        "NAVER_SEARCH_ENABLED",
        "NAVER_SEARCH_TIMEOUT_SECONDS",
    )

    def __enter__(self):
        self._snapshot = {key: os.environ.get(key) for key in self.KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self._snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _set_env(**values):
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _enable_with_keys():
    _set_env(
        NAVER_SEARCH_ENABLED="true",
        NAVER_CLIENT_ID="test-client-id",
        NAVER_CLIENT_SECRET="test-client-secret",
    )


# ---------------------------------------------------------------------------
# Fake requests.Response builder — deterministic, no network.
# ---------------------------------------------------------------------------


def _make_response(*, status_code: int = 200, json_payload=None, raise_json=False) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if raise_json:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_payload
    return resp


def _news_payload(items, total=None):
    return {
        "lastBuildDate": "Mon, 02 Jun 2025 09:30:00 +0900",
        "total": total if total is not None else len(items),
        "start": 1,
        "display": len(items),
        "items": items,
    }


_SAMPLE_ITEM = {
    "title": "<b>전세대출</b> 규제 강화 &quot;실수요자 보호&quot;",
    "originallink": "https://www.example-press.co.kr/article/123",
    "link": "https://n.news.naver.com/mnews/article/001/0000000123",
    "description": "정부가 <b>전세대출</b> 규제를 강화한다고 밝혔다. &lt;관계부처&gt; 협의.",
    "pubDate": "Mon, 02 Jun 2025 09:30:00 +0900",
}


# ---------------------------------------------------------------------------
# (a) Normal normalization
# ---------------------------------------------------------------------------


class NormalizationTests(unittest.TestCase):
    def test_normal_normalization(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            self.assertTrue(provider.available)
            payload = _news_payload([_SAMPLE_ITEM], total=4321)
            with patch("requests.get", return_value=_make_response(json_payload=payload)) as mock_get:
                result = provider.search("전세대출", limit=10)
            mock_get.assert_called_once()

        self.assertTrue(result["available"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["total_available"], 4321)
        self.assertEqual(result["fetched_count"], 1)
        hit = result["items"][0]
        # b-tags stripped, entities unescaped.
        self.assertNotIn("<b>", hit["title"])
        self.assertNotIn("&quot;", hit["title"])
        self.assertIn("전세대출", hit["title"])
        self.assertIn('"실수요자 보호"', hit["title"])
        self.assertNotIn("<b>", hit["summary"])
        self.assertIn("<관계부처>", hit["summary"])
        # original_url == originallink; link == naver link; google_link mirrors original.
        self.assertEqual(hit["original_url"], _SAMPLE_ITEM["originallink"])
        self.assertEqual(hit["link"], _SAMPLE_ITEM["link"])
        self.assertEqual(hit["google_link"], hit["original_url"])
        self.assertEqual(hit["publisher"], "example-press.co.kr")
        self.assertEqual(hit["source"], "naver_api")
        # published parses; published_at is ISO.
        self.assertEqual(hit["published"], _SAMPLE_ITEM["pubDate"])
        self.assertIsNotNone(parsedate_to_datetime(hit["published"]))
        self.assertTrue(hit["published_at"].startswith("2025-06-02"))
        self.assertIn("raw", hit)

    def test_sort_and_params_passed(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            payload = _news_payload([_SAMPLE_ITEM])
            with patch("requests.get", return_value=_make_response(json_payload=payload)) as mock_get:
                provider.search("전세대출", limit=5, start=3, sort="date")
            _args, kwargs = mock_get.call_args
            self.assertEqual(kwargs["params"]["sort"], "date")
            self.assertEqual(kwargs["params"]["display"], 5)
            self.assertEqual(kwargs["params"]["start"], 3)
            # Secrets travel in headers only.
            self.assertIn("X-Naver-Client-Id", kwargs["headers"])
            self.assertIn("X-Naver-Client-Secret", kwargs["headers"])


# ---------------------------------------------------------------------------
# (b) Empty result
# ---------------------------------------------------------------------------


class EmptyResultTests(unittest.TestCase):
    def test_empty_result(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            payload = _news_payload([], total=0)
            with patch("requests.get", return_value=_make_response(json_payload=payload)):
                result = provider.search("없는검색어")
        self.assertEqual(result["items"], [])
        self.assertTrue(result["available"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["total_available"], 0)
        self.assertEqual(result["fetched_count"], 0)


# ---------------------------------------------------------------------------
# (c) Key-absent disable
# ---------------------------------------------------------------------------


class DisableTests(unittest.TestCase):
    def test_resolver_disabled_when_keys_absent(self):
        with _EnvScope():
            _set_env(
                NAVER_SEARCH_ENABLED="true",
                NAVER_CLIENT_ID=None,
                NAVER_CLIENT_SECRET=None,
            )
            with patch("requests.get") as mock_get:
                provider = get_search_provider("naver")
                self.assertIsInstance(provider, DisabledSearchProvider)
                self.assertFalse(provider.available)
                self.assertTrue(provider.reason)
                self.assertIn("NAVER_CLIENT_ID", provider.reason)
                result = provider.search("전세대출")
            mock_get.assert_not_called()
        self.assertEqual(result["items"], [])
        self.assertFalse(result["available"])
        self.assertIsNotNone(result["error"])

    def test_resolver_disabled_when_gate_off(self):
        with _EnvScope():
            _set_env(
                NAVER_SEARCH_ENABLED="false",
                NAVER_CLIENT_ID="id",
                NAVER_CLIENT_SECRET="secret",
            )
            with patch("requests.get") as mock_get:
                provider = get_search_provider("naver")
                self.assertIsInstance(provider, DisabledSearchProvider)
                self.assertIn("NAVER_SEARCH_ENABLED", provider.reason)
                provider.search("전세대출")
            mock_get.assert_not_called()

    def test_secret_missing_reason(self):
        with _EnvScope():
            _set_env(
                NAVER_SEARCH_ENABLED="true",
                NAVER_CLIENT_ID="id",
                NAVER_CLIENT_SECRET=None,
            )
            provider = NaverNewsSearchProvider()
            self.assertFalse(provider.available)
            self.assertIn("NAVER_CLIENT_SECRET", provider.reason)

    def test_unsupported_provider_name(self):
        with _EnvScope():
            _enable_with_keys()
            provider = get_search_provider("bing")
            self.assertIsInstance(provider, DisabledSearchProvider)
            self.assertIn("unsupported", provider.reason)

    def test_disabled_search_never_hits_network_on_search(self):
        # A directly-constructed disabled provider also performs zero network.
        provider = DisabledSearchProvider(reason="disabled for test")
        with patch("requests.get") as mock_get:
            result = provider.search("q")
        mock_get.assert_not_called()
        self.assertEqual(result["items"], [])


# ---------------------------------------------------------------------------
# (d) 429 / quota + error-body variant
# ---------------------------------------------------------------------------


class ErrorStatusTests(unittest.TestCase):
    def test_429_returns_empty_with_error(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            resp = _make_response(status_code=429, json_payload={"errorCode": "024", "errorMessage": "Rate exceeded"})
            with patch("requests.get", return_value=resp):
                result = provider.search("전세대출")
        self.assertEqual(result["items"], [])
        self.assertIsNotNone(result["error"])
        self.assertIn("429", result["error"])
        self.assertEqual(result["debug"]["status_code"], 429)

    def test_401_error_body_variant(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            resp = _make_response(status_code=401, json_payload={"errorMessage": "Not Authorized", "errorCode": "024"})
            with patch("requests.get", return_value=resp):
                result = provider.search("전세대출")
        self.assertEqual(result["items"], [])
        self.assertIsNotNone(result["error"])

    def test_transport_exception_never_raises(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            with patch("requests.get", side_effect=RuntimeError("connection reset")):
                result = provider.search("전세대출")
        self.assertEqual(result["items"], [])
        self.assertIsNotNone(result["error"])
        self.assertIn("request failed", result["error"])


# ---------------------------------------------------------------------------
# (e) Malformed JSON / unexpected shape
# ---------------------------------------------------------------------------


class MalformedShapeTests(unittest.TestCase):
    def test_json_parse_failure(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            with patch("requests.get", return_value=_make_response(raise_json=True)):
                result = provider.search("전세대출")
        self.assertEqual(result["items"], [])
        self.assertIsNotNone(result["error"])
        self.assertIn("json parse failed", result["error"])

    def test_items_missing(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            with patch("requests.get", return_value=_make_response(json_payload={"total": 5})):
                result = provider.search("전세대출")
        self.assertEqual(result["items"], [])
        self.assertIn("missing items array", result["error"])

    def test_items_not_a_list(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            with patch("requests.get", return_value=_make_response(json_payload={"items": "nope"})):
                result = provider.search("전세대출")
        self.assertEqual(result["items"], [])
        self.assertIn("missing items array", result["error"])

    def test_payload_not_dict(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            with patch("requests.get", return_value=_make_response(json_payload=["a", "b"])):
                result = provider.search("전세대출")
        self.assertEqual(result["items"], [])
        self.assertIn("unexpected response shape", result["error"])

    def test_total_non_numeric_falls_back_to_count(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            payload = {"items": [_SAMPLE_ITEM], "total": "lots"}
            with patch("requests.get", return_value=_make_response(json_payload=payload)):
                result = provider.search("전세대출")
        self.assertEqual(result["total_available"], 1)
        self.assertEqual(result["fetched_count"], 1)


# ---------------------------------------------------------------------------
# (f) display / start clamping
# ---------------------------------------------------------------------------


class ClampingTests(unittest.TestCase):
    def test_clamping_to_maxima(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            payload = _news_payload([_SAMPLE_ITEM])
            with patch("requests.get", return_value=_make_response(json_payload=payload)) as mock_get:
                provider.search("전세대출", limit=500, start=99999)
            _args, kwargs = mock_get.call_args
            self.assertEqual(kwargs["params"]["display"], MAX_DISPLAY)
            self.assertEqual(kwargs["params"]["start"], MAX_START)

    def test_clamping_lower_bound(self):
        with _EnvScope():
            _enable_with_keys()
            provider = NaverNewsSearchProvider()
            payload = _news_payload([_SAMPLE_ITEM])
            with patch("requests.get", return_value=_make_response(json_payload=payload)) as mock_get:
                provider.search("전세대출", limit=0, start=-5)
            _args, kwargs = mock_get.call_args
            self.assertEqual(kwargs["params"]["display"], 1)
            self.assertEqual(kwargs["params"]["start"], 1)


# ---------------------------------------------------------------------------
# Mock provider + config snapshot
# ---------------------------------------------------------------------------


class MockProviderTests(unittest.TestCase):
    def test_mock_provider_no_network(self):
        provider = MockNaverSearchProvider()
        with patch("requests.get") as mock_get:
            result = provider.search("전세대출")
        mock_get.assert_not_called()
        self.assertTrue(result["available"])
        self.assertGreaterEqual(len(result["items"]), 1)
        hit = result["items"][0]
        self.assertNotIn("<b>", hit["title"])
        self.assertEqual(hit["source"], "naver_api")
        self.assertEqual(hit["google_link"], hit["original_url"])

    def test_mock_provider_custom_items(self):
        provider = MockNaverSearchProvider(items=[_SAMPLE_ITEM], total=99)
        result = provider.search("q", limit=10)
        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["total_available"], 99)


class ConfigSnapshotTests(unittest.TestCase):
    def test_describe_naver_config_presence_only_no_secrets(self):
        with _EnvScope():
            _set_env(
                NAVER_SEARCH_ENABLED="true",
                NAVER_CLIENT_ID="super-secret-id",
                NAVER_CLIENT_SECRET="super-secret-value",
            )
            snapshot = config.describe_naver_config()
        self.assertTrue(snapshot["enabled"])
        self.assertTrue(snapshot["client_id_present"])
        self.assertTrue(snapshot["client_secret_present"])
        # The actual secret values must NEVER appear in the snapshot.
        serialized = repr(snapshot)
        self.assertNotIn("super-secret-id", serialized)
        self.assertNotIn("super-secret-value", serialized)

    def test_provider_status_no_secrets(self):
        with _EnvScope():
            _set_env(
                NAVER_SEARCH_ENABLED="true",
                NAVER_CLIENT_ID="super-secret-id",
                NAVER_CLIENT_SECRET="super-secret-value",
            )
            status = NaverNewsSearchProvider().provider_status()
        serialized = repr(status)
        self.assertNotIn("super-secret-id", serialized)
        self.assertNotIn("super-secret-value", serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
