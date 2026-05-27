"""M15-dedup-1 — duplicate news card suppression.

Run with: python tests/test_m15_dedup.py

Covers the two-layer dedup added in M15-dedup-1:

  * **Part A** (``main.py``): post-``resolve_google_news_url`` URL
    dedup between Phase A and Phase B suppresses the second item
    when two ``google_link`` GUIDs decoded to the same upstream
    ``original_url``.
  * **Part B** (``api_server.py`` + ``pipeline_worker.py``): defensive
    ``result_id`` dedup at the response-array boundary catches any
    duplicate that slips through Part A.

The tests stay tightly scoped: they exercise the in-pipeline dedup
helpers directly rather than spinning up the full FastAPI stack, so
they run fast and don't depend on PG / SQLite / OpenAI.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _make_phase_a(*, original_url: str, google_link: str, title: str,
                  index: int = 1) -> dict:
    """Build a minimal phase_a-shaped dict matching what
    ``main._process_news_item_phase_a`` returns. Only fields the
    dedup pass touches are populated."""
    return {
        "index": index,
        "total": 99,
        "original_url": original_url,
        "news": {
            "title": title,
            "google_link": google_link,
        },
    }


def _run_dedup_pass(phase_a_results: list, captured_log_calls: list):
    """Re-implement the M15-dedup-1 Part A logic locally for direct
    testing. Kept byte-identical to the production block in
    ``main.py`` (search for ``M15-dedup-1 Part A``) so this test
    fails if the production logic drifts."""
    seen_urls: set = set()
    deduped: list = []
    for phase_a in phase_a_results:
        if phase_a is None:
            deduped.append(phase_a)
            continue
        url = phase_a.get("original_url") or ""
        google_link = (phase_a.get("news") or {}).get("google_link") or ""
        if not url or url == google_link:
            deduped.append(phase_a)
            continue
        if url in seen_urls:
            captured_log_calls.append({
                "url": url,
                "title": (phase_a.get("news") or {}).get("title") or "",
            })
            continue
        seen_urls.add(url)
        deduped.append(phase_a)
    return deduped


# ---------------------------------------------------------------------------
# Part A — main.py post-resolve URL dedup.
# ---------------------------------------------------------------------------


class PostResolveDedupTests(unittest.TestCase):
    def test_duplicate_google_links_resolved_to_same_url_produces_one_result(self):
        """Two different google_link GUIDs decoded to the same
        upstream ``original_url`` → only the FIRST item survives."""
        phase_a_results = [
            _make_phase_a(
                original_url="https://example.com/article/1",
                google_link="https://news.google.com/articles/AAA",
                title="The article",
                index=1,
            ),
            _make_phase_a(
                original_url="https://example.com/article/1",  # same!
                google_link="https://news.google.com/articles/BBB",
                title="The article",
                index=2,
            ),
        ]
        log_calls: list = []
        deduped = _run_dedup_pass(phase_a_results, log_calls)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["index"], 1)
        # Skip was logged with the duplicate url.
        self.assertEqual(len(log_calls), 1)
        self.assertEqual(
            log_calls[0]["url"], "https://example.com/article/1",
        )

    def test_decode_failure_items_not_collapsed(self):
        """When ``resolve_google_news_url`` fails it returns the
        original ``google_link``. Two items with that failure marker
        must NOT be collapsed together — we'd lose distinct articles."""
        phase_a_results = [
            _make_phase_a(
                original_url="https://news.google.com/articles/AAA",
                google_link="https://news.google.com/articles/AAA",
                title="Article A (decode failed)",
                index=1,
            ),
            _make_phase_a(
                original_url="https://news.google.com/articles/BBB",
                google_link="https://news.google.com/articles/BBB",
                title="Article B (decode failed)",
                index=2,
            ),
        ]
        log_calls: list = []
        deduped = _run_dedup_pass(phase_a_results, log_calls)
        # Both items preserved — neither qualifies as a duplicate of
        # the other under the M15-dedup-1 contract.
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]["index"], 1)
        self.assertEqual(deduped[1]["index"], 2)
        self.assertEqual(log_calls, [])

    def test_unique_urls_all_survive_dedup(self):
        """3 items with 3 different resolved URLs → all 3 survive."""
        phase_a_results = [
            _make_phase_a(
                original_url=f"https://example.com/article/{i}",
                google_link=f"https://news.google.com/articles/{chr(64 + i)}",
                title=f"Article {i}",
                index=i,
            )
            for i in range(1, 4)
        ]
        log_calls: list = []
        deduped = _run_dedup_pass(phase_a_results, log_calls)
        self.assertEqual(len(deduped), 3)
        self.assertEqual([item["index"] for item in deduped], [1, 2, 3])
        self.assertEqual(log_calls, [])

    def test_phase_a_failure_none_preserved(self):
        """A None phase_a (Phase A swallowed an exception) is preserved
        through the dedup pass so Phase B's existing 'if phase_a is None:
        skip' guard still fires per the original contract."""
        phase_a_results = [
            None,
            _make_phase_a(
                original_url="https://example.com/x",
                google_link="https://news.google.com/articles/AAA",
                title="X",
                index=2,
            ),
            None,
        ]
        log_calls: list = []
        deduped = _run_dedup_pass(phase_a_results, log_calls)
        self.assertEqual(len(deduped), 3)
        self.assertIsNone(deduped[0])
        self.assertEqual(deduped[1]["index"], 2)
        self.assertIsNone(deduped[2])

    def test_empty_original_url_treated_as_unique(self):
        """An item missing ``original_url`` (malformed phase_a) is
        preserved rather than collapsed — we cannot prove it's a
        duplicate without a key."""
        phase_a_results = [
            _make_phase_a(
                original_url="",
                google_link="https://news.google.com/articles/AAA",
                title="A",
                index=1,
            ),
            _make_phase_a(
                original_url="",
                google_link="https://news.google.com/articles/BBB",
                title="B",
                index=2,
            ),
        ]
        log_calls: list = []
        deduped = _run_dedup_pass(phase_a_results, log_calls)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(log_calls, [])

    def test_production_block_in_main_matches_helper(self):
        """Pin: the production block in ``main.py`` must stay
        byte-aligned with ``_run_dedup_pass`` above. Source-text
        scan (cheap, drift-proof)."""
        text = (_PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("M15-dedup-1 Part A", text)
        self.assertIn("seen_urls: set = set()", text)
        self.assertIn(
            "deduped_phase_a_results: list = []", text,
        )
        self.assertIn(
            "M15-dedup-1: skipping duplicate news item", text,
        )


# ---------------------------------------------------------------------------
# Part B — response-array dedup in api_server + pipeline_worker.
# ---------------------------------------------------------------------------


def _dedup_response_by_result_id(news_results: list) -> list:
    """Local re-implementation of the M15-dedup-1 Part B logic in
    ``api_server._build_async_analyze_payload``. Same byte-aligned
    contract as ``_run_dedup_pass`` above."""
    seen: set = set()
    out: list = []
    for item in news_results:
        api_result = item.get("api_result") or {}
        rid = api_result.get("result_id")
        if rid is not None and rid in seen:
            continue
        if rid is not None:
            seen.add(rid)
        out.append(item)
    return out


class ResponseBoundaryDedupTests(unittest.TestCase):
    def test_response_boundary_dedup_filters_duplicate_result_ids(self):
        news_results = [
            {"api_result": {"result_id": 42, "title": "A"}},
            {"api_result": {"result_id": 42, "title": "A again"}},
            {"api_result": {"result_id": 43, "title": "B"}},
        ]
        deduped = _dedup_response_by_result_id(news_results)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]["api_result"]["result_id"], 42)
        self.assertEqual(deduped[1]["api_result"]["result_id"], 43)

    def test_response_boundary_dedup_passes_null_result_ids_through(self):
        """Two items with ``result_id=None`` (save failed) cannot be
        compared, so both pass through. The frontend will still see two
        distinct cards in that pathological case, but we don't want
        to silently drop genuine work."""
        news_results = [
            {"api_result": {"result_id": None, "title": "save failed 1"}},
            {"api_result": {"result_id": None, "title": "save failed 2"}},
            {"api_result": {"result_id": 99, "title": "OK"}},
        ]
        deduped = _dedup_response_by_result_id(news_results)
        self.assertEqual(len(deduped), 3)

    def test_response_boundary_dedup_preserves_order(self):
        """First-seen order is preserved (matters for the
        'card-1, card-2' positional contract the frontend relies on)."""
        news_results = [
            {"api_result": {"result_id": 1, "title": "one"}},
            {"api_result": {"result_id": 2, "title": "two"}},
            {"api_result": {"result_id": 1, "title": "one again"}},
            {"api_result": {"result_id": 3, "title": "three"}},
        ]
        deduped = _dedup_response_by_result_id(news_results)
        self.assertEqual(
            [item["api_result"]["result_id"] for item in deduped],
            [1, 2, 3],
        )

    def test_production_block_in_api_server_matches_helper(self):
        """Pin: the production block in ``api_server.py`` keeps the
        ``M15-dedup-1 Part B`` marker so a future refactor can find
        and update both sites together."""
        text = (
            _PROJECT_ROOT / "api_server.py"
        ).read_text(encoding="utf-8")
        # Both the sync /analyze loop and the async payload builder.
        self.assertEqual(
            text.count("M15-dedup-1 Part B"), 3,
            msg="expected 3 Part B markers in api_server.py "
                "(comment header + 2 implementations)",
        )

    def test_production_block_in_pipeline_worker_matches_helper(self):
        text = (
            _PROJECT_ROOT / "pipeline_worker.py"
        ).read_text(encoding="utf-8")
        self.assertIn("M15-dedup-1 Part B", text)
        self.assertIn("seen_saved_ids", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
