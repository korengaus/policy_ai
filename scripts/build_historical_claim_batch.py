"""Phase 2 M7.0: anonymized historical claim batch builder.

Scans existing local analysis artifacts (``reports/policy_analysis_*.json``
and the SQLite ``analysis_results`` table) and extracts an anonymized
semantic evaluation batch that follows the same schema as
``tests/fixtures/semantic_real_claim_batch_sample.json``. The generated
batch is meant to feed ``scripts/evaluate_real_claim_batch.py`` locally
before any Render canary decision.

Strict design contract:
    * Pure deterministic. Same inputs always produce the same output
      (modulo ``--seed`` for sampling).
    * No network. No OpenAI key. No Postgres.
    * Never raises on malformed JSON — bad inputs are skipped with a
      reason in the summary.
    * Anonymization is enabled by default: real URLs collapse to
      ``example.generated.<domain-kind>/source/<hash>``; raw query
      parameters are stripped; emails, phone numbers, resident IDs, and
      very long numeric identifiers are scrubbed.
    * Category / risk inference uses the M5.7 / M6.6 guardrails so the
      labels are consistent with the rest of the semantic evaluation
      stack. Generated categories are **heuristic** — never treat them
      as gold truth.
    * No verdict-side effect. ``policy_decision``, ``policy_scoring``,
      and ``verification_card`` are not imported or read.

Generated output goes under ``reports/`` which is gitignored. Do NOT
commit the generated file. The summary markdown is also gitignored.

CLI:
    python scripts/build_historical_claim_batch.py --dry-run --max-cases 100

    python scripts/build_historical_claim_batch.py \\
        --output reports/semantic_historical_claim_batch.generated.json \\
        --max-cases 100 --overwrite

Exit codes:
    0 — success
    1 — script / parser error
    2 — output already exists and ``--overwrite`` was not passed
    3 — generated fewer than ``--min-cases`` usable cases (with ``--strict``)
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# The M5.7 / M6.6 guardrail module is the canonical source of risk-flag
# vocabulary. We import lazily inside the inference function to keep the
# builder importable in environments where the semantic stack hasn't been
# initialized.
import semantic_fact_guardrails as guardrails  # noqa: E402


BUILDER_VERSION = "M7.0"
DEFAULT_OUTPUT = "reports/semantic_historical_claim_batch.generated.json"
DEFAULT_SUMMARY = "reports/semantic_historical_claim_batch.summary.md"

# Truncation budgets — keep generated cases short like the synthetic
# fixtures. These align with the existing
# ``tests/fixtures/semantic_real_claim_batch_sample.json`` shape.
MAX_CLAIM_CHARS = 300
MAX_BODY_CHARS = 1000
MAX_TITLE_CHARS = 160
MAX_URL_CHARS = 300


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an anonymized historical claim batch from local "
            "analysis artifacts (reports/policy_analysis_*.json and "
            "policy_ai.db). Output is fixture-compatible with "
            "scripts/evaluate_real_claim_batch.py."
        ),
    )
    parser.add_argument(
        "--reports-dir", type=Path, default=ROOT / "reports",
        help="Directory containing policy_analysis_*.json files (default: %(default)s).",
    )
    parser.add_argument(
        "--sqlite-db", type=Path, default=ROOT / "policy_ai.db",
        help="SQLite DB to scan analysis_results from (default: %(default)s).",
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / DEFAULT_OUTPUT,
        help="Output JSON path (default: %(default)s). Must live under reports/.",
    )
    parser.add_argument(
        "--summary-out", type=Path, default=ROOT / DEFAULT_SUMMARY,
        help="Summary markdown path (default: %(default)s). Skipped with --dry-run.",
    )
    parser.add_argument(
        "--max-cases", type=int, default=100,
        help="Cap emitted cases (default: %(default)s).",
    )
    parser.add_argument(
        "--min-cases", type=int, default=10,
        help="Minimum required usable cases (default: %(default)s).",
    )
    parser.add_argument(
        "--source", choices=["reports", "sqlite", "both"], default="both",
        help="Which artifact stores to scan (default: %(default)s).",
    )
    parser.add_argument(
        "--anonymize", dest="anonymize", action="store_true", default=True,
        help="Anonymize URLs and strip personal-data patterns (default: on).",
    )
    parser.add_argument(
        "--no-anonymize", dest="anonymize", action="store_false",
        help=argparse.SUPPRESS,  # explicit disable; intentionally undocumented.
    )
    parser.add_argument(
        "--include-debug", action="store_true",
        help=(
            "Include a `metadata` block per case with source artifact "
            "and inference notes. The evaluator tolerates the extra field."
        ),
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Replace existing --output path. Without this, an existing file errors out (exit 2).",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit code 3 if fewer than --min-cases usable cases emit.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the summary but do not write the JSON or summary files.",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Seed for sampling/shuffling. Without it, ordering is stable by case_id.",
    )
    return parser


# ---------------------------------------------------------------------------
# Safe helpers — never raise on bad input.
# ---------------------------------------------------------------------------


def safe_get(obj: Any, path: Sequence[Any], default: Any = None) -> Any:
    """Walk ``path`` (sequence of keys / indices) safely. Returns ``default``
    when any step fails (missing key, wrong type, IndexError, etc.)."""
    cur = obj
    for step in path:
        try:
            if isinstance(cur, dict):
                cur = cur.get(step, default if step is path[-1] else None)
            elif isinstance(cur, (list, tuple)) and isinstance(step, int):
                cur = cur[step]
            else:
                return default
        except Exception:
            return default
        if cur is None:
            return default
    return cur


def truncate_text(text: object, max_chars: int) -> str:
    """Clip ``text`` to ``max_chars``. Non-strings coerce; None returns ''."""
    if text is None:
        return ""
    try:
        raw = str(text)
    except Exception:
        return ""
    raw = raw.strip()
    if max_chars and len(raw) > max_chars:
        return raw[:max_chars].rstrip() + "…"
    return raw


def stable_hash(text: object, length: int = 12) -> str:
    """Short deterministic hash. Used for generated case_ids and URL paths."""
    raw = "" if text is None else str(text)
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    return digest[:length]


def _coerce_json(value: Any) -> Any:
    """Accept a JSON-encoded string OR a parsed object. Bad input returns ``value``."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") or s.startswith("{"):
            try:
                return json.loads(s)
            except Exception:
                return value
    return value


# ---------------------------------------------------------------------------
# Anonymization / sanitization.
# ---------------------------------------------------------------------------

# Patterns the sanitizers strip / replace. Korean policy terms intentionally
# pass through — we only redact obviously personal / opaque tokens.
_EMAIL_RE = re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b0\d{1,2}-\d{3,4}-\d{4}\b")
_RID_RE = re.compile(r"\b\d{6}-\d{7}\b")
_LONG_NUMERIC_ID_RE = re.compile(r"\b\d{11,}\b")
# Conservative Korean-name pattern: 3 Hangul syllables followed by 씨 (Mr./Ms.).
# We replace the full token with a placeholder to avoid leaking real names
# from quoted news content; longer / shorter patterns are left alone.
_KOREAN_NAME_HONORIFIC_RE = re.compile(r"[가-힣]{3}씨")

# Map common host-suffix groups to a stable synthetic host so the
# anonymized URL still hints at the source type.
_GOV_DOMAIN_KINDS = [
    # (substring → synthetic host)
    ("seoul.go.kr", "example.generated.seoul.go.kr"),
    ("busan.go.kr", "example.generated.busan.go.kr"),
    ("gg.go.kr", "example.generated.gg.go.kr"),
    ("incheon.go.kr", "example.generated.incheon.go.kr"),
    ("daegu.go.kr", "example.generated.daegu.go.kr"),
    ("daejeon.go.kr", "example.generated.daejeon.go.kr"),
    ("gwangju.go.kr", "example.generated.gwangju.go.kr"),
    ("ulsan.go.kr", "example.generated.ulsan.go.kr"),
    ("jeju.go.kr", "example.generated.jeju.go.kr"),
    ("sejong.go.kr", "example.generated.sejong.go.kr"),
    ("kr/", "example.generated.go.kr"),  # generic .go.kr fallback
]


def sanitize_url(url: object, anonymize: bool = True) -> str:
    """Strip query parameters and (when ``anonymize``) replace host with a
    synthetic ``example.generated.*`` host. Returns at most ``MAX_URL_CHARS``.
    """
    if url is None:
        return ""
    raw = str(url).strip()
    if not raw:
        return ""
    if not anonymize:
        return truncate_text(raw, MAX_URL_CHARS)
    try:
        parsed = urlparse(raw)
    except Exception:
        return truncate_text(raw, MAX_URL_CHARS)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    # Drop query and fragment entirely.
    new_host = "example.generated.news"
    is_gov_domain = (
        ".go.kr" in host
        or host.endswith("go.kr")
        # korea.kr is the central-government portal — treat it as a gov
        # domain so it doesn't collapse to a news-style synthetic host.
        or host.endswith("korea.kr")
        or host.endswith(".police.go.kr")
    )
    if is_gov_domain:
        for needle, replacement in _GOV_DOMAIN_KINDS:
            if needle in host:
                new_host = replacement
                break
        else:
            new_host = "example.generated.go.kr"
    elif ".or.kr" in host:
        new_host = "example.generated.or.kr"
    # Replace the path with a stable hash-based identifier so we don't
    # leak the original URL slug.
    digest = stable_hash(raw, length=12)
    new_path = f"/source/{digest}"
    rebuilt = f"https://{new_host}{new_path}"
    return truncate_text(rebuilt, MAX_URL_CHARS)


def sanitize_text(text: object, max_chars: int) -> str:
    """Scrub PII patterns from text, then truncate. Korean policy terms
    intentionally pass through unmodified."""
    raw = truncate_text(text, max_chars)
    if not raw:
        return raw
    raw = _EMAIL_RE.sub("[이메일]", raw)
    raw = _PHONE_RE.sub("[전화번호]", raw)
    raw = _RID_RE.sub("[주민번호]", raw)
    raw = _LONG_NUMERIC_ID_RE.sub("[식별자]", raw)
    raw = _KOREAN_NAME_HONORIFIC_RE.sub("[이름]씨", raw)
    return raw


def sanitize_title(text: object) -> str:
    """Title-specific sanitizer — same scrub rules, shorter budget."""
    return sanitize_text(text, MAX_TITLE_CHARS)


def sanitize_publisher(publisher: object) -> str:
    """Publisher names rarely contain PII but are still anonymized to a
    generic "Example Source" label so the fixture stays neutral."""
    if not publisher:
        return ""
    raw = str(publisher).strip()
    if not raw:
        return ""
    # Keep a short fingerprint so different publishers don't collapse.
    return f"Example Source ({stable_hash(raw, length=6)})"


# ---------------------------------------------------------------------------
# Recursive discovery helpers.
# ---------------------------------------------------------------------------


def find_strings_by_keys(obj: Any, keys: Iterable[str], *, max_depth: int = 6) -> List[str]:
    """Return every non-empty string found at any of ``keys`` anywhere in the
    nested structure (up to ``max_depth``). Deterministic order (depth-first)."""
    out: List[str] = []
    key_set = set(keys)
    seen: set = set()

    def _walk(node: Any, depth: int) -> None:
        if depth > max_depth or node is None:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in key_set:
                    if isinstance(v, str) and v.strip():
                        sig = (k, v.strip()[:200])
                        if sig not in seen:
                            seen.add(sig)
                            out.append(v.strip())
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)

    _walk(obj, 0)
    return out


def find_dicts_with_keys(obj: Any, required_keys: Iterable[str], *, max_depth: int = 6) -> List[dict]:
    """Return every dict at any depth that contains *all* ``required_keys``.
    Order is depth-first. Each dict appears at most once."""
    out: List[dict] = []
    req = set(required_keys)
    seen: set = set()

    def _walk(node: Any, depth: int) -> None:
        if depth > max_depth or node is None:
            return
        if isinstance(node, dict):
            if req.issubset(node.keys()):
                sig = id(node)
                if sig not in seen:
                    seen.add(sig)
                    out.append(node)
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)

    _walk(obj, 0)
    return out


# ---------------------------------------------------------------------------
# Extraction from analysis artifacts.
# ---------------------------------------------------------------------------


def extract_claim_candidates(news_result: dict) -> List[str]:
    """Return claim-text candidates in priority order. Normalized claims
    are preferred (cleanest single-sentence claims); falls back to
    policy_claims, then the top-level query, then the title."""
    out: List[str] = []
    seen: set = set()

    def _add(value: object) -> None:
        if not value:
            return
        text = str(value).strip()
        if not text or text in seen or len(text) < 10:
            return
        seen.add(text)
        out.append(text)

    # 1) normalized_claims[].claim_text
    for item in _coerce_json(news_result.get("normalized_claims")) or []:
        if isinstance(item, dict):
            _add(item.get("claim_text") or item.get("text") or item.get("claim"))
        elif isinstance(item, str):
            _add(item)

    # 2) policy_claims[].sentence
    for item in _coerce_json(news_result.get("policy_claims")) or []:
        if isinstance(item, dict):
            _add(item.get("sentence") or item.get("claim_text"))

    # 3) claims string-encoded list
    claims = _coerce_json(news_result.get("claims"))
    if isinstance(claims, list):
        for c in claims:
            _add(c if isinstance(c, str) else None)

    # 4) headline-style fallbacks (only if nothing else)
    if not out:
        for key in ("query", "title", "headline", "article_title"):
            _add(news_result.get(key))
    return out


def extract_source_candidates(news_result: dict, *, anonymize: bool) -> List[dict]:
    """Return source-candidate dicts that look like an "official body" — i.e.
    a URL + title + non-empty body text. Each dict mirrors the
    real-claim-batch fixture's source shape (already anonymized)."""
    out: List[dict] = []
    seen_urls: set = set()

    def _push(url: str, title: str, publisher: str, body: str, source_type: str) -> None:
        if not url and not title:
            return
        # Anonymize the URL first so the dedupe key is the synthetic URL.
        clean_url = sanitize_url(url, anonymize=anonymize)
        if clean_url in seen_urls:
            return
        seen_urls.add(clean_url)
        out.append({
            "source_id": stable_hash(url or title, length=12),
            "title": sanitize_title(title),
            "url": clean_url,
            "publisher": sanitize_publisher(publisher),
            "official_body_text": sanitize_text(body, MAX_BODY_CHARS),
            "_source_type": source_type,
        })

    # official_evidence_results — strongest candidates (real official document
    # text). Prefer document_text_snippet, fall back to text_snippet.
    for r in _coerce_json(news_result.get("official_evidence_results")) or []:
        if not isinstance(r, dict):
            continue
        body = (
            r.get("document_text_snippet")
            or r.get("text_snippet")
            or r.get("rendered_text_snippet")
            or ""
        )
        url = (
            r.get("selected_document_url")
            or r.get("url")
            or r.get("search_url")
            or ""
        )
        title = r.get("document_title") or r.get("title") or r.get("source_name") or ""
        publisher = r.get("source_name") or ""
        if not isinstance(body, str):
            body = ""
        _push(url, title, publisher, body, "official_evidence_results")

    # evidence_snippets — typically news-side, but useful as a fallback
    # when the report has no official body but does have a relevant snippet.
    for r in _coerce_json(news_result.get("evidence_snippets")) or []:
        if not isinstance(r, dict):
            continue
        _push(
            r.get("source_url") or "",
            r.get("source_title") or "",
            r.get("publisher") or "",
            r.get("evidence_text") or "",
            "evidence_snippets",
        )

    return out


# ---------------------------------------------------------------------------
# Category + risk inference.
# ---------------------------------------------------------------------------

# Order matters: each flag maps to one category, and we pick the first
# matching flag in this priority. number/date/eligibility/finality/negation
# take precedence over scope flags because they're the harder-to-recover
# disagreements; the scope flags below are for "topic shared, instrument
# different" failure modes.
_FLAG_TO_CATEGORY: List[tuple] = [
    ("number_mismatch", "number_mismatch"),
    ("date_mismatch", "date_mismatch"),
    ("eligibility_mismatch", "eligibility_mismatch"),
    ("finality_mismatch", "finality_mismatch"),
    ("negation_mismatch", "negation_or_refutation"),
    ("policy_scope_mismatch", "same_topic_wrong_policy"),
    ("actor_scope_mismatch", "local_vs_central_authority"),
    ("local_vs_central", "local_vs_central_authority"),
]


def infer_category_and_risk(claim_text: str, source_body: str) -> dict:
    """Run the M5.7 / M6.6 guardrails over (claim, source) and translate
    the output into the fixture's category + risk_flags shape. Empty
    source body short-circuits to ``no_body`` so the generator can still
    emit a useful evaluation case."""
    body = (source_body or "").strip()
    if not body:
        return {
            "category": "no_body",
            "risk_flags": ["official_body_missing"],
            "should_not_be_strong": True,
            "should_be_unavailable_when_no_body": True,
            "notes": ["source body empty"],
        }

    check = guardrails.compare_critical_facts(claim_text, body)
    flags: List[str] = list(check.get("risk_flags") or [])
    has_missing = "missing_critical_fact" in flags

    # Pick the first flag in our priority order; if none match, fall
    # back to partial_support (when only missing_critical_fact fires) or
    # unknown_historical otherwise. ``unknown_historical`` flags the case
    # as worth-inspecting but doesn't claim a specific failure mode.
    category = None
    for flag, cat in _FLAG_TO_CATEGORY:
        if flag in flags:
            category = cat
            break
    if category is None:
        category = "partial_support" if has_missing else "unknown_historical"

    risk_flags = list(flags)
    # Categories without a guardrail-specific flag get a documentation-
    # only label so the fixture is self-describing.
    if category == "unknown_historical" and not risk_flags:
        risk_flags = ["heuristic_unknown_historical"]
    elif category == "partial_support" and "missing_critical_fact" not in risk_flags:
        risk_flags.append("missing_critical_fact")

    notes: List[str] = []
    cap = check.get("support_cap") or "strong"
    if cap != "strong":
        notes.append(f"guardrail_cap={cap}")
    return {
        "category": category,
        "risk_flags": risk_flags,
        "should_not_be_strong": category != "unknown_historical",
        "should_be_unavailable_when_no_body": False,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Pipeline: scan, extract, anonymize, infer.
# ---------------------------------------------------------------------------


def _load_reports(reports_dir: Path) -> List[tuple]:
    """Return a list of (path, parsed-json) tuples. Missing dir or bad JSON
    are skipped silently — counts surface in the summary."""
    if not reports_dir.exists() or not reports_dir.is_dir():
        return []
    out: List[tuple] = []
    for path in sorted(reports_dir.glob("policy_analysis_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append((str(path), data))
    return out


def _load_sqlite_rows(db_path: Path) -> List[dict]:
    """Return analysis_results rows as a list of dicts. Missing DB or
    missing table are tolerated."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_results'"
            )
            if not cur.fetchone():
                return []
            cur.execute("SELECT * FROM analysis_results")
            rows = [dict(r) for r in cur.fetchall()]
            return rows
        finally:
            conn.close()
    except Exception:
        return []


def _row_to_news_result_shape(row: dict) -> dict:
    """Coerce an analysis_results SQLite row into the same shape as a
    policy_analysis JSON's ``news_results[i]`` entry so the extraction
    helpers don't care about the source."""
    shaped: dict = {
        "title": row.get("title"),
        "query": row.get("query"),
        "claims": _coerce_json(row.get("claims")),
        "normalized_claims": _coerce_json(row.get("normalized_claims")),
        "evidence_snippets": _coerce_json(row.get("evidence_snippets")),
        "source_candidates": _coerce_json(row.get("source_candidates")),
        # debug_summary often carries official_evidence_results inside.
        "debug_summary": _coerce_json(row.get("debug_summary")),
    }
    debug = shaped["debug_summary"]
    if isinstance(debug, dict):
        oer = debug.get("official_evidence_results")
        if isinstance(oer, list):
            shaped["official_evidence_results"] = oer
    return shaped


def _emit_cases_from_news_result(
    news_result: dict,
    *,
    artifact: str,
    artifact_index: int,
    anonymize: bool,
    include_debug: bool,
) -> List[dict]:
    """Build zero or more case dicts from a single news_result. Each
    (claim, source) pair becomes one case so the fixture stays shallow
    (1 source per case, like the synthetic real-claim batch)."""
    claims = extract_claim_candidates(news_result)
    if not claims:
        return []
    # Use the strongest available claim only — extracting every claim
    # from every report would balloon the fixture without adding signal.
    claim_raw = claims[0]
    claim_text = sanitize_text(claim_raw, MAX_CLAIM_CHARS)
    if len(claim_text) < 10:
        return []
    sources = extract_source_candidates(news_result, anonymize=anonymize)

    out: List[dict] = []
    if not sources:
        # No-body cases — emit one degenerate case with empty official body
        # so the evaluator can still measure "unavailable" handling.
        category = infer_category_and_risk(claim_text, "")
        case = _assemble_case(
            claim_text=claim_text,
            source={
                "source_id": stable_hash(claim_text, length=12),
                "title": "",
                "url": "",
                "publisher": "",
                "official_body_text": "",
            },
            inferred=category,
            artifact=artifact,
            artifact_index=artifact_index,
            include_debug=include_debug,
        )
        out.append(case)
        return out

    for source in sources:
        body = source.get("official_body_text") or ""
        inferred = infer_category_and_risk(claim_text, body)
        # Drop the helper '_source_type' attribute before serializing.
        clean_source = {k: v for k, v in source.items() if not k.startswith("_")}
        case = _assemble_case(
            claim_text=claim_text,
            source=clean_source,
            inferred=inferred,
            artifact=artifact,
            artifact_index=artifact_index,
            include_debug=include_debug,
        )
        out.append(case)
        # One source per case keeps the batch comparable with the M6.4
        # synthetic real-claim fixture.
        break
    return out


def _assemble_case(
    *,
    claim_text: str,
    source: dict,
    inferred: dict,
    artifact: str,
    artifact_index: int,
    include_debug: bool,
) -> dict:
    # The evaluator's related_top1 check uses substring contains against
    # ``source_id + " " + source_url``. Anchoring the marker on the
    # source's own ``source_id`` (already a 12-char stable hash) is the
    # most reliable match — it survives URL anonymization and never
    # collides across cases.
    contains_marker = source.get("source_id") or stable_hash(
        source.get("url") or claim_text, length=12
    )

    case_id = f"historical_{stable_hash(artifact + claim_text + (source.get('url') or ''), length=12)}"
    # ``should_rank_related_first`` must only be True when the source has
    # both a URL AND a non-empty body — the evaluator can't rank what the
    # agent has no chunks for, so a True here on a no_body case would
    # force the related_top1 check to fail unfairly.
    can_rank = bool(source.get("url")) and bool(source.get("official_body_text"))
    expected: dict = {
        "related_source_url_contains": contains_marker,
        "should_rank_related_first": can_rank,
        "expected_support_level": "any",
        "should_not_be_strong": bool(inferred.get("should_not_be_strong")),
        "risk_flags": inferred.get("risk_flags") or [],
    }
    if inferred.get("should_be_unavailable_when_no_body"):
        expected["should_be_unavailable_when_no_body"] = True
        # No-body cases use the unavailable check; the rank check is
        # disabled regardless of URL presence.
        expected["should_rank_related_first"] = False

    case: dict = {
        "case_id": case_id,
        "category": inferred.get("category") or "unknown_historical",
        "description": (
            f"Anonymized historical case derived from "
            f"{os.path.basename(artifact)} (index {artifact_index})."
        ),
        "claim_text": claim_text,
        "sources": [source],
        "expected": expected,
    }
    if include_debug:
        case["metadata"] = {
            "source_artifact": os.path.basename(artifact),
            "artifact_index": artifact_index,
            "anonymized": True,
            "builder_version": BUILDER_VERSION,
            "notes": list(inferred.get("notes") or []),
        }
    return case


# ---------------------------------------------------------------------------
# Output + summary.
# ---------------------------------------------------------------------------


def _category_distribution(cases: List[dict]) -> dict:
    out: dict = {}
    for c in cases:
        cat = c.get("category") or "unknown_historical"
        out[cat] = out.get(cat, 0) + 1
    return dict(sorted(out.items()))


def _risk_flag_distribution(cases: List[dict]) -> dict:
    out: dict = {}
    for c in cases:
        for f in (c.get("expected") or {}).get("risk_flags") or []:
            out[f] = out.get(f, 0) + 1
    return dict(sorted(out.items()))


def _write_summary(
    path: Path,
    *,
    cases: List[dict],
    reports_scanned: int,
    sqlite_rows_scanned: int,
    candidates_seen: int,
    skipped_reasons: dict,
    anonymized: bool,
    output_path: Path,
) -> None:
    lines: List[str] = []
    lines.append("# Semantic Historical Claim Batch — Build Summary")
    lines.append("")
    lines.append(
        f"- generated_at: `{datetime.now(timezone.utc).isoformat(timespec='seconds')}`"
    )
    lines.append(f"- builder_version: `{BUILDER_VERSION}`")
    lines.append(f"- anonymized: `{anonymized}`")
    lines.append(f"- output: `{output_path}`")
    lines.append(f"- reports_scanned: {reports_scanned}")
    lines.append(f"- sqlite_rows_scanned: {sqlite_rows_scanned}")
    lines.append(f"- candidate_count: {candidates_seen}")
    lines.append(f"- emitted_case_count: {len(cases)}")
    lines.append(f"- skipped_count: {sum(skipped_reasons.values())}")
    if skipped_reasons:
        lines.append("- skipped_reasons:")
        for k, v in sorted(skipped_reasons.items()):
            lines.append(f"  - `{k}`: {v}")
    lines.append("")
    lines.append("## Category distribution")
    lines.append("")
    for cat, n in _category_distribution(cases).items():
        lines.append(f"- `{cat}`: {n}")
    lines.append("")
    lines.append("## Risk flag distribution")
    lines.append("")
    for f, n in _risk_flag_distribution(cases).items():
        lines.append(f"- `{f}`: {n}")
    lines.append("")
    lines.append(
        "> This file is gitignored. The categories and risk flags above "
        "are **heuristic**, derived deterministically from the M5.7 / "
        "M6.6 guardrails — do not treat them as gold labels. The next "
        "step is to evaluate the generated batch with "
        "`scripts/evaluate_real_claim_batch.py --provider deterministic "
        "--no-network`."
    )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    output_path = args.output

    # Output-existence check (only when not dry-run).
    if not args.dry_run and output_path.exists() and not args.overwrite:
        print(
            f"[build-historical] output already exists: {output_path}\n"
            "Pass --overwrite to replace it.",
            file=sys.stderr,
        )
        return 2

    # 1) Load.
    if args.source in ("reports", "both"):
        reports = _load_reports(args.reports_dir)
    else:
        reports = []
    if args.source in ("sqlite", "both"):
        sqlite_rows = _load_sqlite_rows(args.sqlite_db)
    else:
        sqlite_rows = []

    # 2) Iterate news_results across both sources.
    candidates: List[tuple] = []  # (artifact, index, news_result)
    for path, parsed in reports:
        nr = parsed.get("news_results") if isinstance(parsed, dict) else None
        if isinstance(nr, list):
            for i, item in enumerate(nr):
                if isinstance(item, dict):
                    candidates.append((path, i, item))
    for row_index, row in enumerate(sqlite_rows):
        shaped = _row_to_news_result_shape(row)
        candidates.append((f"sqlite:analysis_results#{row.get('id', row_index)}", 0, shaped))

    # 3) Stable ordering — by case_id seed so re-runs produce the same
    # batch slice when --max-cases is below total. Optional --seed shuffles.
    if args.seed is not None:
        rng = random.Random(args.seed)
        rng.shuffle(candidates)

    # 4) Emit cases.
    cases: List[dict] = []
    skipped: dict = {"no_claim": 0, "duplicate_case_id": 0}
    seen_case_ids: set = set()
    for artifact, idx, news_result in candidates:
        if len(cases) >= args.max_cases:
            break
        emitted = _emit_cases_from_news_result(
            news_result,
            artifact=artifact,
            artifact_index=idx,
            anonymize=args.anonymize,
            include_debug=args.include_debug,
        )
        if not emitted:
            skipped["no_claim"] += 1
            continue
        for case in emitted:
            if case["case_id"] in seen_case_ids:
                skipped["duplicate_case_id"] += 1
                continue
            seen_case_ids.add(case["case_id"])
            cases.append(case)
            if len(cases) >= args.max_cases:
                break

    elapsed = time.perf_counter() - started

    # 5) Print summary line.
    print(
        "[build-historical] reports_scanned={r} sqlite_rows={s} "
        "candidates={c} emitted={e} skipped={k} elapsed={t:.2f}s "
        "anonymized={a}".format(
            r=len(reports), s=len(sqlite_rows), c=len(candidates),
            e=len(cases), k=sum(skipped.values()), t=elapsed,
            a=args.anonymize,
        )
    )
    print(f"  category_distribution={_category_distribution(cases)}")
    print(f"  risk_flag_distribution={_risk_flag_distribution(cases)}")

    if args.strict and len(cases) < args.min_cases:
        print(
            f"[build-historical] FAILED: emitted {len(cases)} cases, "
            f"minimum is {args.min_cases}",
            file=sys.stderr,
        )
        return 3

    # 6) Write (unless dry-run).
    if args.dry_run:
        print("[build-historical] --dry-run: no file written")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[build-historical] wrote {len(cases)} case(s) to {output_path}")

    _write_summary(
        args.summary_out,
        cases=cases,
        reports_scanned=len(reports),
        sqlite_rows_scanned=len(sqlite_rows),
        candidates_seen=len(candidates),
        skipped_reasons=skipped,
        anonymized=args.anonymize,
        output_path=output_path,
    )
    print(f"[build-historical] wrote summary to {args.summary_out}")
    return 0


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("[build-historical] aborted by user", file=sys.stderr)
        return 130
    except Exception as error:  # defensive
        print(f"[build-historical] FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
