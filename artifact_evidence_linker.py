"""Phase 2 M10.5: keyword-overlap evidence candidate linker.

Compares ``artifact_text_extractions`` rows (from M10.4) against
``analysis_results`` rows (existing claim store) and produces
``EvidenceCandidate`` records the operator can review later. Pure
offline transform: no HTTP, no OpenAI, no embeddings, no DB writes
from this module. The CLI (``scripts/link_artifact_evidence.py``)
handles persistence.

Hard contract:
    * Never invoked automatically. The pipeline (``main.py`` /
      ``analyze_pipeline`` / ``api_server.py``) does not import this
      module.
    * Never modifies ``source_fetch_artifacts`` or
      ``artifact_text_extractions`` rows.
    * Never modifies verdict logic, ``policy_confidence``,
      ``verification_card``, or semantic matching.
    * ``truth_claim`` is forced to ``False`` on every
      ``EvidenceCandidate``, regardless of input.
    * ``operator_review_required`` is forced to ``True`` on every
      candidate. The linker explicitly never authorizes content
      trust on its own.
    * No ``requests`` / ``httpx`` / ``urllib.request`` / ``socket``
      imports.
    * No ``openai`` / ``anthropic`` / ``playwright`` /
      ``browser_use`` / ``openclaw`` / ``selenium`` imports.

Matching contract (deliberately simple — pinned by tests):
    * Tokenize on Unicode-aware ``\\W+`` boundaries, lowercase,
      drop tokens shorter than ``MIN_TOKEN_LEN``.
    * ``match_score = |claim_tokens ∩ text_tokens| / |claim_tokens|``
      when ``claim_tokens`` is non-empty; ``0.0`` otherwise.
    * One candidate per (extraction, claim) tuple with
      ``match_score >= min_score``.
    * Supporting passage = the ``SUPPORTING_PASSAGE_CHARS`` window
      in ``main_text`` with the highest claim-token overlap.

Public surface (stable, pinned by tests):

    DEFAULT_MIN_SCORE
    MIN_TOKEN_LEN
    SUPPORTING_PASSAGE_CHARS
    SUPPORTING_PASSAGE_STEP
    NOTES_HUMAN_REVIEW
    EvidenceCandidate                                  (dataclass)
    candidate_to_dict(candidate) -> dict
    tokenize(text) -> list[str]
    extract_claim_texts(analysis_row) -> list[str]
    find_evidence_candidates(extraction_row, analysis_row,
                             min_score=DEFAULT_MIN_SCORE)
        -> list[EvidenceCandidate]
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------


DEFAULT_MIN_SCORE = 0.15

# Drop very short tokens. A 1-character token is almost always noise
# (English article "a", residual punctuation pieces, etc.) and would
# inflate the overlap denominator without contributing signal.
MIN_TOKEN_LEN = 2

# 500-char supporting-passage window with a 100-char step. The window
# is small enough to be human-readable in CLI output; the step is
# coarse enough to keep the O(n_windows × n_tokens) scan fast.
SUPPORTING_PASSAGE_CHARS = 500
SUPPORTING_PASSAGE_STEP = 100

# Mandatory notes value the spec requires on every candidate.
NOTES_HUMAN_REVIEW = "keyword overlap only — requires human review"

# Tokenizer pattern: Unicode-aware "non-word" split. ``\W`` honors
# Unicode letters by default in Python 3, so Hangul + Latin tokens
# both survive intact.
_TOKEN_SPLIT_RE = re.compile(r"\W+", re.UNICODE)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class EvidenceCandidate:
    """Stable wire shape consumed by tests, the CLI, and the DB save
    helper. ``matched_tokens`` is a list[str] in memory but is
    serialized as a JSON string before persistence (see
    :func:`candidate_to_dict`).

    Safety-flag fields (``truth_claim``, ``operator_review_required``,
    ``official_source_candidate``) are always present so consumers do
    not have to handle ``None``.
    """
    extraction_id: int
    source_id: str
    url: str
    analysis_id: str
    claim_text: str
    match_score: float
    matched_tokens: List[str] = field(default_factory=list)
    supporting_passage: str = ""
    candidate_timestamp: str = ""
    # Always False. The linker never asserts truth — pinned by tests.
    truth_claim: bool = False
    official_source_candidate: bool = False
    # Always True. Candidates are operator-review fodder only —
    # pinned by tests.
    operator_review_required: bool = True
    notes: str = NOTES_HUMAN_REVIEW


def candidate_to_dict(candidate: EvidenceCandidate) -> Dict[str, Any]:
    """Serialize an ``EvidenceCandidate`` to a plain dict (the shape
    ``database.save_evidence_candidate`` expects).

    Re-asserts ``truth_claim=False`` and ``operator_review_required=True``
    so a defensive serializer cannot leak the wrong values even if a
    caller mutated the dataclass fields. ``matched_tokens`` is JSON-
    encoded so it lands cleanly in a ``TEXT`` column.
    """
    payload = asdict(candidate)
    payload["truth_claim"] = False
    payload["operator_review_required"] = True
    payload["notes"] = payload.get("notes") or NOTES_HUMAN_REVIEW
    payload["matched_tokens"] = json.dumps(
        list(candidate.matched_tokens or []), ensure_ascii=False,
    )
    return payload


# ---------------------------------------------------------------------------
# Tokenization + claim extraction helpers
# ---------------------------------------------------------------------------


def tokenize(text: Optional[str]) -> List[str]:
    """Lowercased, length-filtered token list. Empty list for falsy
    input. Order is preserved; duplicates are kept (the overlap
    computation uses set semantics — see :func:`_overlap`)."""
    if not text:
        return []
    try:
        raw = str(text).lower()
    except Exception:
        return []
    out: List[str] = []
    for piece in _TOKEN_SPLIT_RE.split(raw):
        if not piece:
            continue
        if len(piece) < MIN_TOKEN_LEN:
            continue
        out.append(piece)
    return out


def _coerce_claim_str(value: object) -> Optional[str]:
    """Extract the human-readable claim text from a value that may be
    a bare string or a dict with a ``text`` / ``claim`` / ``claim_text``
    field. Anything else is rejected."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("text", "claim", "claim_text", "normalized", "normalized_text"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _maybe_loads_json(value: object) -> object:
    """If ``value`` is a JSON-encoded list/dict string, return the
    decoded object. Otherwise return ``value`` unchanged."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return value


def extract_claim_texts(analysis_row: Dict[str, Any]) -> List[str]:
    """Return the unique, ordered list of human-readable claim texts
    on ``analysis_row``. Looks at ``claim_text`` (primary), then the
    JSON-encoded ``claims`` and ``normalized_claims`` lists. Never
    raises. Empty list when nothing usable is found."""
    if not isinstance(analysis_row, dict):
        return []
    out: List[str] = []
    seen: set = set()

    def _push(value: object) -> None:
        text = _coerce_claim_str(value)
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    # Primary single-claim field.
    _push(analysis_row.get("claim_text"))

    # Multi-claim JSON lists. They are stored as TEXT in the DB so
    # decode best-effort. A non-list payload is tolerated (single
    # claim treated as one entry).
    for key in ("claims", "normalized_claims"):
        raw = _maybe_loads_json(analysis_row.get(key))
        if isinstance(raw, list):
            for item in raw:
                _push(item)
        else:
            _push(raw)
    return out


# ---------------------------------------------------------------------------
# Scoring + supporting passage
# ---------------------------------------------------------------------------


def _overlap(claim_tokens: Iterable[str],
             text_tokens: Iterable[str]) -> List[str]:
    """Set-intersection of ``claim_tokens`` and ``text_tokens``,
    returned as a list ordered by first appearance in
    ``claim_tokens``."""
    claim_list = list(claim_tokens)
    text_set = set(text_tokens)
    seen: set = set()
    out: List[str] = []
    for tok in claim_list:
        if tok in text_set and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _score(claim_tokens: List[str], matched_tokens: List[str]) -> float:
    unique_claim = set(claim_tokens)
    if not unique_claim:
        return 0.0
    return round(len(set(matched_tokens)) / len(unique_claim), 4)


def _best_supporting_passage(
    main_text: Optional[str], claim_tokens: List[str],
) -> str:
    """Sliding-window search for the most claim-overlapping passage.

    Walks ``main_text`` in ``SUPPORTING_PASSAGE_STEP``-char strides;
    scores each ``SUPPORTING_PASSAGE_CHARS``-char window by the count
    of distinct claim tokens it contains; returns the highest-scoring
    window (ties broken by earliest occurrence). Falls back to the
    leading ``SUPPORTING_PASSAGE_CHARS`` chars when no window has any
    overlap (or when ``claim_tokens`` is empty).
    """
    if not isinstance(main_text, str) or not main_text:
        return ""
    if len(main_text) <= SUPPORTING_PASSAGE_CHARS:
        return main_text
    if not claim_tokens:
        return main_text[:SUPPORTING_PASSAGE_CHARS]
    claim_set = set(claim_tokens)
    best_score = -1
    best_start = 0
    pos = 0
    length = len(main_text)
    while pos < length:
        window = main_text[pos:pos + SUPPORTING_PASSAGE_CHARS]
        window_tokens = set(tokenize(window))
        score = len(claim_set & window_tokens)
        if score > best_score:
            best_score = score
            best_start = pos
        if pos + SUPPORTING_PASSAGE_CHARS >= length:
            break
        pos += SUPPORTING_PASSAGE_STEP
    if best_score <= 0:
        return main_text[:SUPPORTING_PASSAGE_CHARS]
    return main_text[best_start:best_start + SUPPORTING_PASSAGE_CHARS]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _coerce_analysis_id(analysis_row: Dict[str, Any]) -> str:
    """``analysis_results.id`` is INTEGER; the candidate table stores
    ``analysis_id`` as TEXT. Coerce whatever's on the row to a stable
    string. Empty string if absent."""
    raw = analysis_row.get("id")
    if raw is None:
        # Fall back to "analysis_id" if the caller already passed
        # the column name they want.
        raw = analysis_row.get("analysis_id")
    if raw is None:
        return ""
    return str(raw)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def find_evidence_candidates(
    extraction_row: Dict[str, Any],
    analysis_row: Dict[str, Any],
    min_score: float = DEFAULT_MIN_SCORE,
) -> List[EvidenceCandidate]:
    """Compute keyword-overlap evidence candidates between one
    extraction row and one analysis row.

    ``extraction_row`` shape matches ``database.get_extraction_results``.
    ``analysis_row`` shape matches ``database.get_result_by_id``.
    ``min_score`` is clamped to ``[0.0, 1.0]``. Returns an empty list
    when:

        * either input is not a dict, or
        * the extraction has no usable ``main_text``, or
        * the analysis has no usable claim text, or
        * no (extraction, claim) pair exceeds ``min_score``,
        * or any unexpected exception fires.

    Never raises.
    """
    if not isinstance(extraction_row, dict) or not isinstance(analysis_row, dict):
        return []
    try:
        clamped_min = float(min_score)
    except (TypeError, ValueError):
        clamped_min = DEFAULT_MIN_SCORE
    if clamped_min < 0.0:
        clamped_min = 0.0
    if clamped_min > 1.0:
        clamped_min = 1.0

    try:
        main_text = extraction_row.get("main_text") or ""
        if not isinstance(main_text, str) or not main_text.strip():
            return []
        text_tokens = tokenize(main_text)
        if not text_tokens:
            return []
        claim_texts = extract_claim_texts(analysis_row)
        if not claim_texts:
            return []

        extraction_id = int(extraction_row.get("id") or 0)
        source_id = str(extraction_row.get("source_id") or "")
        url = str(extraction_row.get("url") or "")
        official_candidate = bool(
            extraction_row.get("official_source_candidate", False)
        )
        analysis_id = _coerce_analysis_id(analysis_row)
        timestamp = _utc_now_iso()

        out: List[EvidenceCandidate] = []
        seen_claims: set = set()
        for claim in claim_texts:
            if claim in seen_claims:
                continue
            seen_claims.add(claim)
            claim_tokens = tokenize(claim)
            if not claim_tokens:
                continue
            matched = _overlap(claim_tokens, text_tokens)
            score = _score(claim_tokens, matched)
            if score < clamped_min:
                continue
            passage = _best_supporting_passage(main_text, matched or claim_tokens)
            # Enforce the supporting-passage cap defensively — the
            # window builder already returns at most SUPPORTING_PASSAGE_CHARS
            # but a future refactor of that helper must not be able to
            # leak a larger string past this point.
            if len(passage) > SUPPORTING_PASSAGE_CHARS:
                passage = passage[:SUPPORTING_PASSAGE_CHARS]
            out.append(EvidenceCandidate(
                extraction_id=extraction_id,
                source_id=source_id,
                url=url,
                analysis_id=analysis_id,
                claim_text=claim,
                match_score=float(score),
                matched_tokens=list(matched),
                supporting_passage=passage,
                candidate_timestamp=timestamp,
                truth_claim=False,
                official_source_candidate=official_candidate,
                operator_review_required=True,
                notes=NOTES_HUMAN_REVIEW,
            ))
        return out
    except Exception as error:
        logger.warning(
            "[artifact_evidence_linker] error while linking: %s: %s",
            type(error).__name__, error,
        )
        return []
