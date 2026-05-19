"""Phase 2 M5: Korean-aware text chunking for semantic evidence matching.

The chunker is intentionally lightweight — no segmentation library, no
ML model, no network. It produces small chunks (sentence-sized when
possible, paragraph- or window-sized as fallback) so the embedding step
sees coherent passages instead of whole document bodies.

Robustness contract:
    * Never raises on bad input. Empty/None/non-string input → empty list.
    * Caps chunk count and per-chunk length so a runaway document body
      cannot blow up the embedding bill.
    * Output preserves character offsets relative to the original input so
      downstream consumers can highlight the matched passage if they wish.
"""

from __future__ import annotations

import re
from typing import List, Optional


_KOREAN_SENTENCE_TERMINATORS = re.compile(r"(?<=[.!?。！？…])\s+|(?<=[다요죠음])\s+(?=[가-힣A-Z])")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_WHITESPACE = re.compile(r"\s+")


def normalize_semantic_text(text: object) -> str:
    """Trim, collapse whitespace, strip null bytes. Never raises."""
    if text is None:
        return ""
    try:
        raw = str(text)
    except Exception:
        return ""
    raw = raw.replace("\x00", " ")
    raw = _WHITESPACE.sub(" ", raw)
    return raw.strip()


def _split_sentences(text: str) -> List[str]:
    if not text:
        return []
    # Try Korean/English sentence terminators first.
    pieces = [piece.strip() for piece in _KOREAN_SENTENCE_TERMINATORS.split(text) if piece.strip()]
    if pieces:
        return pieces
    return [text]


def _split_paragraphs(text: str) -> List[str]:
    if not text:
        return []
    parts = [part.strip() for part in _PARAGRAPH_SPLIT.split(text) if part.strip()]
    if parts:
        return parts
    return [text]


def _sliding_window(text: str, window: int) -> List[str]:
    if not text:
        return []
    if window <= 0:
        return [text]
    return [text[start : start + window] for start in range(0, len(text), window)]


def _coerce_int(value, default: int, minimum: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    if coerced < minimum:
        return minimum
    return coerced


def chunk_text_for_semantic_matching(
    text: object,
    max_chunks: int = 20,
    max_chars_per_chunk: int = 480,
    source_kind: str = "official_body_text",
    source_id: Optional[str] = None,
) -> List[dict]:
    """Split ``text`` into up to ``max_chunks`` chunks, sentence-first.

    Returns a list of ``{"chunk_id", "text", "char_start", "char_end",
    "source", "source_id"}`` dicts. Character offsets reference the ORIGINAL
    (pre-normalization) input so downstream UIs can highlight if desired.
    """
    if not isinstance(text, str) or not text.strip():
        return []

    max_chunks = _coerce_int(max_chunks, default=20, minimum=1)
    max_chars_per_chunk = _coerce_int(max_chars_per_chunk, default=480, minimum=40)
    safe_source = source_kind or "text"
    safe_source_id = str(source_id) if source_id is not None else ""

    normalized = normalize_semantic_text(text)
    if not normalized:
        return []

    candidates: List[str] = []
    sentences = _split_sentences(normalized)
    if sentences:
        candidates.extend(sentences)
    else:
        candidates.extend(_split_paragraphs(normalized))

    chunks: List[dict] = []
    cursor = 0  # offset into ``text`` we have not yet placed
    chunk_counter = 0
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        # If a single candidate is too long, slide a window across it. This
        # keeps very long official documents (laws, FAQs) from being lost.
        pieces = _sliding_window(candidate, max_chars_per_chunk) if len(candidate) > max_chars_per_chunk else [candidate]
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            # Locate the piece in the original text from the current cursor;
            # if it isn't there (e.g. whitespace collapsed), fall back to a
            # safe span starting at cursor for char_end approximation.
            located = text.find(piece, cursor)
            if located == -1:
                char_start = cursor
                char_end = min(len(text), cursor + len(piece))
            else:
                char_start = located
                char_end = located + len(piece)
                cursor = char_end
            chunks.append({
                "chunk_id": f"{safe_source}:{safe_source_id or 'src'}:{chunk_counter}",
                "text": piece,
                "char_start": char_start,
                "char_end": char_end,
                "source": safe_source,
                "source_id": safe_source_id,
            })
            chunk_counter += 1
            if len(chunks) >= max_chunks:
                return chunks
    return chunks
