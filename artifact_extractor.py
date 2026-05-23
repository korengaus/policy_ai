"""Phase 2 M10.4: structured text extraction for stored fetch artifacts.

Reads ``source_fetch_artifacts`` rows (raw HTML + metadata) and
produces an ``ExtractionResult`` with cleaned title, body text, section
list, word count, and a coarse language hint. Pure offline transform:
no HTTP, no browser, no DB writes, no OpenAI.

Hard contract:
    * Never invoked automatically. The pipeline (``main.py`` /
      ``analyze_pipeline`` / ``api_server.py``) does not import this
      module.
    * Never modifies ``source_fetch_artifacts`` rows.
    * Never modifies verdict logic, ``policy_confidence``,
      ``verification_card``, or semantic matching.
    * ``truth_claim`` is forced to ``False`` on every
      ``ExtractionResult``, regardless of input.
    * No ``requests`` / ``httpx`` / ``urllib.request`` / ``socket``
      imports. ``urllib.parse`` is allowed for URL parsing only.
    * No ``openai`` / ``anthropic`` / ``playwright`` /
      ``browser_use`` / ``openclaw`` / ``selenium`` imports.

Public surface (stable, pinned by tests):

    MAX_MAIN_TEXT_CHARS
    KOREAN_RATIO_THRESHOLD
    ENGLISH_RATIO_THRESHOLD
    ExtractionResult                                    (dataclass)
    extraction_result_to_dict(result)
    extract_text_from_artifact(artifact_row) -> ExtractionResult
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from structured_logging import get_logger


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------


# Same 50_000 cap the M10.2 crawler uses for stored text. Keeps stored
# extractions bounded and roughly comparable to fetch_text_content.
MAX_MAIN_TEXT_CHARS = 50_000

# Language hint thresholds. Coarse on purpose — this is metadata for
# triage, not a classification model. A Korean source page is
# overwhelmingly Hangul; an English page is overwhelmingly ASCII alpha.
KOREAN_RATIO_THRESHOLD = 0.20
ENGLISH_RATIO_THRESHOLD = 0.60

# Tags the extractor strips before computing main_text or sections so
# JS / styling / navigation furniture does not pollute the body.
_STRIP_TAGS = ("script", "style", "nav", "footer", "header")

_HEADING_TAGS = ("h1", "h2", "h3")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """Stable wire shape consumed by tests, the CLI, and the DB save
    helper. Every field is set by ``extract_text_from_artifact`` —
    partial failures still produce a fully-populated result so
    persistence and logging stay consistent.

    Safety-flag fields (``truth_claim``, ``official_source_candidate``)
    are always present so consumers do not have to handle ``None``.
    """
    artifact_id: int
    source_id: str
    url: str
    extraction_timestamp: str
    extraction_duration_ms: int
    success: bool
    error: Optional[str]
    title: Optional[str]
    main_text: Optional[str]
    sections: Optional[str]
    word_count: int
    language_hint: str
    # Always False. The extractor never asserts truth — pinned by tests.
    truth_claim: bool = False
    official_source_candidate: bool = False


def extraction_result_to_dict(result: ExtractionResult) -> Dict[str, Any]:
    """Serialize an ``ExtractionResult`` to a plain dict (the shape
    ``database.save_extraction_result`` expects). ``truth_claim`` is
    re-asserted as ``False`` so a defensive serializer cannot leak a
    True value even if a caller mutated the dataclass field."""
    payload = asdict(result)
    payload["truth_claim"] = False
    return payload


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_result(
    *, artifact_id: int, source_id: str, url: str,
    official_source_candidate: bool,
) -> ExtractionResult:
    return ExtractionResult(
        artifact_id=int(artifact_id or 0),
        source_id=str(source_id or ""),
        url=str(url or ""),
        extraction_timestamp=_utc_now_iso(),
        extraction_duration_ms=0,
        success=False,
        error=None,
        title=None,
        main_text=None,
        sections=None,
        word_count=0,
        language_hint="unknown",
        truth_claim=False,
        official_source_candidate=bool(official_source_candidate),
    )


def _language_hint(text: str) -> str:
    """Coarse three-way classifier: ``ko`` / ``en`` / ``unknown``.

    Korean wins if Hangul codepoints are >20% of the non-whitespace
    character mass. English wins if ASCII alpha is >60%. Otherwise the
    hint is ``unknown`` — including the all-whitespace / empty case
    so callers do not pretend to know.
    """
    if not text:
        return "unknown"
    total = 0
    korean = 0
    english = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            korean += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            english += 1
    if total <= 0:
        return "unknown"
    if korean / total > KOREAN_RATIO_THRESHOLD:
        return "ko"
    if english / total > ENGLISH_RATIO_THRESHOLD:
        return "en"
    return "unknown"


def _count_words(text: Optional[str]) -> int:
    if not text:
        return 0
    return len([token for token in text.split() if token])


def _extract_title(soup) -> Optional[str]:
    try:
        tag = soup.find("title")
    except Exception:
        return None
    if tag is None:
        return None
    try:
        text = tag.get_text(strip=True)
    except Exception:
        return None
    return text or None


def _strip_furniture(soup) -> None:
    for tag in soup(list(_STRIP_TAGS)):
        try:
            tag.decompose()
        except Exception:
            continue


def _extract_main_text(soup) -> str:
    try:
        text = soup.get_text(separator=" ", strip=True)
    except Exception:
        return ""
    if len(text) > MAX_MAIN_TEXT_CHARS:
        text = text[:MAX_MAIN_TEXT_CHARS]
    return text


def _extract_sections(soup) -> List[Dict[str, str]]:
    """Walk the DOM in document order. Each h1/h2/h3 opens a new
    section; every subsequent text node accumulates into the current
    section's body until the next heading. Text inside the heading
    tags themselves is skipped (the heading is already captured)."""
    try:
        from bs4 import NavigableString, Tag  # type: ignore
    except Exception:
        return []

    sections: List[Dict[str, str]] = []
    current_heading: Optional[str] = None
    current_parts: List[str] = []

    def _flush() -> None:
        if current_heading is None:
            return
        body = " ".join(current_parts).strip()
        sections.append({"heading": current_heading, "text": body})

    try:
        descendants = list(soup.descendants)
    except Exception:
        return []

    for element in descendants:
        if isinstance(element, Tag) and element.name in _HEADING_TAGS:
            _flush()
            try:
                current_heading = element.get_text(strip=True)
            except Exception:
                current_heading = ""
            current_parts = []
            continue
        if isinstance(element, NavigableString):
            if current_heading is None:
                continue
            # Skip text that lives inside a heading tag (already captured).
            try:
                parents = element.parents
            except Exception:
                parents = ()
            inside_heading = False
            for parent in parents:
                if isinstance(parent, Tag) and parent.name in _HEADING_TAGS:
                    inside_heading = True
                    break
            if inside_heading:
                continue
            piece = str(element).strip()
            if piece:
                current_parts.append(piece)
    _flush()
    return sections


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_text_from_artifact(artifact_row: Dict[str, Any]) -> ExtractionResult:
    """Run the offline extraction pipeline against one
    ``source_fetch_artifacts`` row (the shape
    ``database.get_fetch_artifacts`` returns).

    Never raises. Every failure path returns a populated
    ``ExtractionResult`` with ``success=False`` and a descriptive
    ``error`` string. ``truth_claim`` is always ``False`` regardless
    of the row's stored value.
    """
    started = time.perf_counter()

    if not isinstance(artifact_row, dict):
        result = _empty_result(
            artifact_id=0, source_id="", url="",
            official_source_candidate=False,
        )
        result.error = "artifact_row must be a dict"
        result.extraction_duration_ms = int(
            (time.perf_counter() - started) * 1000
        )
        return result

    artifact_id = int(artifact_row.get("id") or 0)
    source_id = str(artifact_row.get("source_id") or "")
    url = str(artifact_row.get("url") or "")
    official_candidate = bool(
        artifact_row.get("official_source_candidate", False)
    )

    result = _empty_result(
        artifact_id=artifact_id, source_id=source_id, url=url,
        official_source_candidate=official_candidate,
    )

    if not bool(artifact_row.get("success", False)):
        result.error = "source fetch was not successful"
        result.extraction_duration_ms = int(
            (time.perf_counter() - started) * 1000
        )
        return result

    raw_html = artifact_row.get("raw_html")
    if raw_html is None or (isinstance(raw_html, str) and not raw_html.strip()):
        result.error = "no raw_html"
        result.extraction_duration_ms = int(
            (time.perf_counter() - started) * 1000
        )
        return result

    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception as import_error:
        result.error = f"beautifulsoup_unavailable: {import_error}"
        result.extraction_duration_ms = int(
            (time.perf_counter() - started) * 1000
        )
        return result

    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        title = _extract_title(soup)
        _strip_furniture(soup)
        main_text = _extract_main_text(soup)
        sections = _extract_sections(soup)
        word_count = _count_words(main_text)
        language_hint = _language_hint(main_text or "")

        result.success = True
        result.error = None
        result.title = title
        result.main_text = main_text or ""
        result.sections = json.dumps(sections, ensure_ascii=False)
        result.word_count = word_count
        result.language_hint = language_hint
    except Exception as error:
        result.success = False
        result.error = f"{type(error).__name__}: {error}"
    finally:
        # truth_claim re-asserted in case any code path above mutated
        # the dataclass field. The contract is absolute.
        result.truth_claim = False
        result.extraction_duration_ms = int(
            (time.perf_counter() - started) * 1000
        )

    return result
