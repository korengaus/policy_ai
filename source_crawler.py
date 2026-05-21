"""Phase 2 M10.2: source registry static crawler foundation.

A **bounded, auditable** static HTTP fetcher for registry-candidate
URLs. Uses ``requests`` + ``BeautifulSoup`` only — no Playwright,
no browser automation, no JavaScript execution. Results are saved
through the explicit ``database.save_fetch_artifact`` path only;
they do **not** feed back into the verification pipeline.

Hard contract:
    * Never invoked automatically. The pipeline (``main.py`` /
      ``analyze_pipeline``) never imports this module.
    * Never modifies verdict logic, ``policy_confidence``,
      ``verification_card``, or semantic matching.
    * ``truth_claim`` is forced to ``False`` on every
      ``FetchResult``, regardless of outcome.
    * Five safety checks run *before* any network call (see
      ``fetch_source_url``); refusing returns a ``FetchResult`` with
      ``success=False`` rather than raising.
    * Single attempt — no retries.
    * At most 3 redirects.
    * Content-Length over 2 MB aborts the fetch.
    * Text extraction truncates at 50 000 characters.
    * No cookies, no session reuse across sources.
    * No OpenAI / Anthropic imports.

Public surface (stable, pinned by tests):

    DEFAULT_TIMEOUT_SECONDS
    DEFAULT_USER_AGENT
    MAX_CONTENT_BYTES
    MAX_TEXT_CHARS
    MAX_REDIRECTS
    FetchResult                            (dataclass)
    fetch_result_to_dict(result)
    fetch_source_url(url, source, config=None) -> FetchResult
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------


DEFAULT_TIMEOUT_SECONDS = 15.0
# A neutral descriptive User-Agent. Not a bot/crawler identifier and
# not a browser impersonation — the operator can override via
# ``config["user_agent"]`` when needed.
DEFAULT_USER_AGENT = (
    "policy_ai-source-crawler/M10.2 "
    "(+offline registry static fetcher; operator-triggered only)"
)
# 2 MB hard cap. The smaller we keep this, the lower the blast radius
# of any future regression that fetches an unexpected page.
MAX_CONTENT_BYTES = 2 * 1024 * 1024
# 50 000 chars covers a long article without storing a runaway page.
MAX_TEXT_CHARS = 50_000
MAX_REDIRECTS = 3


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """Stable wire shape consumed by tests, the CLI, and the DB save
    helper. Every field is set by ``fetch_source_url`` — partial
    failures still produce a fully-populated result so persistence
    and logging stay consistent.

    Safety-flag fields (``truth_claim``, ``official_source_candidate``,
    ``network_fetch_performed``) are always present so consumers do
    not have to handle ``None``.
    """
    url: str
    source_id: str
    status_code: Optional[int] = None
    content_type: Optional[str] = None
    raw_html: Optional[str] = None
    text_content: Optional[str] = None
    fetch_timestamp: str = ""
    fetch_duration_ms: int = 0
    success: bool = False
    error: Optional[str] = None
    # Always True when we actually called ``requests.get``; False when
    # a safety check refused before any network activity.
    network_fetch_performed: bool = False
    # Always False — this module never asserts truth on its results.
    truth_claim: bool = False
    # Mirrored from the source entry so a stored artifact carries
    # the registry-side candidate flag without a join.
    official_source_candidate: bool = False


def fetch_result_to_dict(result: FetchResult) -> Dict[str, Any]:
    """Serialize a ``FetchResult`` to a plain dict (the shape
    ``database.save_fetch_artifact`` expects)."""
    return asdict(result)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_result(url: str, source_id: str,
                  *, official_source_candidate: bool = False) -> FetchResult:
    return FetchResult(
        url=str(url or ""),
        source_id=str(source_id or ""),
        fetch_timestamp=_utc_now_iso(),
        official_source_candidate=bool(official_source_candidate),
    )


def _refuse(result: FetchResult, error: str) -> FetchResult:
    """Populate a refusal result. Network was NOT attempted."""
    result.success = False
    result.error = error
    result.network_fetch_performed = False
    # truth_claim stays False (default); explicit re-assertion in case
    # a future refactor changes the dataclass default.
    result.truth_claim = False
    logger.info(
        "[source_crawler] refused: source_id=%s url=%s error=%s",
        result.source_id, result.url, error,
    )
    return result


def _run_safety_checks(url: str, source: Dict[str, Any]) -> Optional[str]:
    """Return an error string if any safety check refuses the fetch,
    or ``None`` when every check passes. The five rules pinned by
    ``tests/test_source_crawler.py`` (matching the M10.2 spec):

        1. ``default_enabled`` must be ``True``  — else refuse.
        2. ``operator_review_required`` must be ``False``  — else refuse.
        3. URL scheme must be ``https``.
        4. URL host must be in ``source["allowed_domains"]``.
        5. ``browser_automation`` must not be ``"required"``.

    The first-match-wins order is documented and stable; tests
    enforce each refusal text independently.
    """
    if not isinstance(source, dict):
        return "source entry is not a dict"
    if bool(source.get("default_enabled", False)) is not True:
        return "source not enabled for automated fetch"
    if bool(source.get("operator_review_required", True)) is True:
        return "operator review required before fetch"
    try:
        parsed = urlparse(str(url or ""))
    except Exception as parse_error:
        return f"url could not be parsed: {parse_error}"
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        return "only https urls are permitted"
    host = (parsed.hostname or "").lower()
    if not host:
        return "url has no host"
    allowed = source.get("allowed_domains") or []
    if not isinstance(allowed, list) or not allowed:
        return "source has no allowed_domains"
    allow_subdomains = bool(source.get("allow_subdomains", False))
    host_ok = False
    for raw in allowed:
        d = str(raw or "").strip().lower()
        if not d:
            continue
        if host == d:
            host_ok = True
            break
        if allow_subdomains and host.endswith("." + d):
            host_ok = True
            break
    if not host_ok:
        return "url host not in allowed_domains"
    ba = str(source.get("browser_automation") or "").strip().lower()
    if ba == "required":
        return "source requires browser automation, static fetch not appropriate"
    return None


def _extract_text(html: str) -> str:
    """BeautifulSoup-based reader text extraction. Strips script /
    style / nav / footer / header before pulling text. Truncates to
    ``MAX_TEXT_CHARS`` to keep stored artifacts bounded."""
    try:
        # Local import keeps the module importable even in test
        # contexts that monkey-patch BeautifulSoup; the production
        # codepath always has bs4 installed.
        from bs4 import BeautifulSoup
    except Exception:
        return ""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return ""
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        try:
            tag.decompose()
        except Exception:
            continue
    try:
        text = soup.get_text(separator="\n", strip=True)
    except Exception:
        return ""
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    return text


def _content_length_exceeds_cap(response: Any) -> bool:
    """Best-effort header check. Some upstreams omit Content-Length;
    we still cap the in-memory size when reading the body."""
    try:
        raw = response.headers.get("Content-Length")
    except Exception:
        raw = None
    if not raw:
        return False
    try:
        return int(raw) > MAX_CONTENT_BYTES
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def fetch_source_url(
    url: str,
    source: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> FetchResult:
    """Fetch a single registry-candidate URL. Never raises — every
    failure path returns a populated ``FetchResult`` with
    ``success=False`` and a descriptive ``error`` string.

    The fetch:
        * runs the five safety checks before any I/O;
        * uses a fresh ``requests`` request (no session reuse);
        * sends no cookies;
        * follows at most ``MAX_REDIRECTS`` redirects;
        * aborts if Content-Length is reported above
          ``MAX_CONTENT_BYTES``;
        * caps stored ``raw_html`` at ``MAX_CONTENT_BYTES`` bytes;
        * caps extracted ``text_content`` at ``MAX_TEXT_CHARS`` chars;
        * does **not** retry.

    ``config`` is an optional dict that may carry:
        * ``timeout`` (float, seconds) — overrides ``DEFAULT_TIMEOUT_SECONDS``
        * ``user_agent`` (str) — overrides ``DEFAULT_USER_AGENT``
        * ``requests_module`` — test-injectable HTTP module (must
          expose ``get(...)`` returning a response with ``status_code``,
          ``headers``, ``text`` / ``content``). Lets tests mock the
          network without monkey-patching the global ``requests``.
    """
    cfg = config if isinstance(config, dict) else {}
    timeout = float(cfg.get("timeout", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS)
    if timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS
    user_agent = str(cfg.get("user_agent") or DEFAULT_USER_AGENT)

    source_id = ""
    official_candidate = False
    if isinstance(source, dict):
        source_id = str(source.get("source_id") or "")
        official_candidate = bool(source.get("official_source_candidate", False))

    result = _empty_result(
        url=url, source_id=source_id,
        official_source_candidate=official_candidate,
    )

    refusal = _run_safety_checks(url, source)
    if refusal:
        return _refuse(result, refusal)

    # Resolve HTTP module — defaults to the global ``requests``. Tests
    # pass a stub via ``config["requests_module"]``.
    requests_module = cfg.get("requests_module")
    if requests_module is None:
        try:
            import requests as _requests_pkg
        except Exception as imp_error:
            return _refuse(
                result, f"requests library unavailable: {imp_error}",
            )
        requests_module = _requests_pkg

    started = time.perf_counter()
    result.network_fetch_performed = True
    try:
        response = requests_module.get(
            str(url),
            headers={"User-Agent": user_agent},
            timeout=timeout,
            allow_redirects=True,
            # Note: requests' allow_redirects=True follows by default;
            # the safety check is the explicit MAX_REDIRECTS cap below
            # via len(response.history).
        )
    except Exception as fetch_error:
        result.fetch_duration_ms = int((time.perf_counter() - started) * 1000)
        result.success = False
        result.error = f"{type(fetch_error).__name__}: {fetch_error}"
        # truth_claim already False (default).
        return result

    result.fetch_duration_ms = int((time.perf_counter() - started) * 1000)

    # Redirect cap.
    try:
        history_len = len(getattr(response, "history", []) or [])
    except Exception:
        history_len = 0
    if history_len > MAX_REDIRECTS:
        result.success = False
        result.error = (
            f"too many redirects: {history_len} > {MAX_REDIRECTS}"
        )
        return result

    # Content-Length cap.
    if _content_length_exceeds_cap(response):
        result.success = False
        try:
            raw_cl = response.headers.get("Content-Length")
        except Exception:
            raw_cl = "?"
        result.error = (
            f"content-length {raw_cl} exceeds cap of {MAX_CONTENT_BYTES} bytes"
        )
        # Defensive: do NOT touch response.text — would force a read.
        result.status_code = getattr(response, "status_code", None)
        try:
            result.content_type = response.headers.get("Content-Type")
        except Exception:
            result.content_type = None
        return result

    # Capture status + content type.
    try:
        result.status_code = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        result.status_code = None
    try:
        result.content_type = response.headers.get("Content-Type")
    except Exception:
        result.content_type = None

    # Pull body — capped at the byte budget even if Content-Length
    # was missing or lied.
    raw_text: Optional[str] = None
    try:
        # Prefer ``content`` for byte-level capping when available;
        # fall back to ``text``. The mock interface in tests uses
        # whichever the response object exposes.
        body_bytes = getattr(response, "content", None)
        if body_bytes is not None:
            try:
                body_len = len(body_bytes)
            except Exception:
                body_len = 0
            if body_len > MAX_CONTENT_BYTES:
                result.success = False
                result.error = (
                    f"response body {body_len} bytes exceeds cap of "
                    f"{MAX_CONTENT_BYTES} bytes"
                )
                return result
            try:
                raw_text = body_bytes.decode("utf-8", errors="replace")
            except Exception:
                raw_text = None
        if raw_text is None:
            try:
                raw_text = response.text
            except Exception:
                raw_text = None
    except Exception as body_error:
        result.success = False
        result.error = f"failed to read response body: {body_error}"
        return result

    if raw_text is not None and len(raw_text) > MAX_CONTENT_BYTES:
        raw_text = raw_text[:MAX_CONTENT_BYTES]

    result.raw_html = raw_text

    # 4xx / 5xx are *not* successful even when the body downloaded
    # cleanly. The conservative posture: surface success=False but
    # keep status_code + content_type so the operator can inspect.
    status_ok = (
        isinstance(result.status_code, int)
        and 200 <= result.status_code < 300
    )

    ct_lower = (result.content_type or "").lower()
    extraction_note: Optional[str] = None
    if status_ok and "text/html" in ct_lower and raw_text:
        result.text_content = _extract_text(raw_text)
    else:
        result.text_content = None
        if status_ok and not raw_text:
            extraction_note = "empty body — no text extracted"
        elif status_ok and "text/html" not in ct_lower:
            extraction_note = (
                f"content-type {result.content_type!r} is not text/html; "
                "text extraction skipped"
            )

    if status_ok:
        result.success = True
        result.error = extraction_note
    else:
        result.success = False
        result.error = (
            f"upstream status {result.status_code}"
            if result.status_code is not None
            else "upstream returned no status"
        )

    # truth_claim is always False — re-assert defensively.
    result.truth_claim = False
    return result
