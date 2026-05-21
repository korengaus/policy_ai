"""Phase 2 M10.0: official source registry foundation.

Pure-stdlib helpers for loading + validating an *operator-curated*
source registry. The registry is a deliberately conservative
foundation that future ingestion layers (HTTP fetchers, browser
automation, n8n / OpenClaw / browser-use orchestration) will consume
*before* they touch any external network.

Hard contract:
    * No HTTP. No browser. No DB. No FastAPI.
    * No ``openai`` / ``anthropic`` / ``requests`` / ``httpx`` /
      ``playwright`` / ``browser_use`` / ``openclaw`` imports.
    * ``truth_claim`` must never be true on any source — the registry
      only marks *candidates* for official evidence, not truth.
    * ``operator_review_required`` defaults to true; setting it to
      false requires an explicit per-source justification field.
    * Capture plans are *plans only* — no function in this module
      ever fetches, scrapes, or otherwise touches a URL.

Public surface (stable, pinned by tests):

    SOURCE_REGISTRY_SCHEMA_VERSION
    SourceRegistryError
    KNOWN_SOURCE_TYPES
    KNOWN_CAPTURE_METHODS
    KNOWN_BROWSER_AUTOMATION

    normalize_registry_path(path)
    normalize_domain(value)
    normalize_url(value)
    load_source_registry(path=None)
    validate_source_record(record)
    validate_source_registry(registry)
    list_sources(registry, source_type=None, enabled=None)
    get_source_by_id(registry, source_id)
    find_sources_by_domain(registry, domain)
    is_url_allowed_for_source(source, url)
    classify_url_against_registry(registry, url)
    build_source_capture_plan(source, url=None)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


SOURCE_REGISTRY_SCHEMA_VERSION = 1

# Default location is ``<repo>/data/source_registry.json``. Resolved
# lazily so the module is import-safe even if the file is missing.
_THIS_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY_PATH = _THIS_DIR / "data" / "source_registry.json"

REGISTRY_NAME = "policy_ai_source_registry"


KNOWN_SOURCE_TYPES = (
    "government_policy",
    "government_press",
    "law_or_regulation",
    "parliament",
    "local_government",
    "public_agency",
    "news",
    "fact_check",
    "demo",
)

KNOWN_CAPTURE_METHODS = (
    "manual_or_http",
    "rss",
    "api",
    "html",
    "pdf",
    "browser_required",
    "unknown",
)

KNOWN_BROWSER_AUTOMATION = (
    "not_required",
    "maybe_required",
    "required",
    "unknown",
)

# Allowed source_id format: lowercase ASCII letters, digits, underscores,
# starting with a letter. 3–80 characters.
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,79}$")

# Allowed bare-hostname format: lowercase ASCII letters, digits, dots,
# hyphens. No scheme, no path, no port, no credentials. 4–253 chars.
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,251}[a-z0-9])$")

# Token-shaped literals we never want to see in metadata. Anything that
# *looks* like a secret in a notes/display_name/tag field is treated
# as a validation error so a careless commit cannot leak it.
_HEX_TOKEN_RE = re.compile(r"[0-9a-fA-F]{32,}")
_SDK_KEY_PREFIX_RE = re.compile(r"sk-[A-Za-z0-9]{16,}")


# Fields that may carry localhost or 127.0.0.1 URLs for testing /
# demo. Any source flagged with ``demo`` type can use http:// for its
# base_url; anything else must be https:// only.
_DEMO_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class SourceRegistryError(ValueError):
    """Raised by every public helper when validation fails. The
    ``reason`` attribute gives a stable machine-readable tag tests
    can pin without depending on the human message wording."""

    def __init__(self, message: str, *, reason: str = "invalid"):
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


def normalize_registry_path(path: Optional[object]) -> Path:
    """Resolve ``path`` to an absolute Path. ``None`` returns the
    documented default under ``data/source_registry.json``."""
    if path is None:
        return DEFAULT_REGISTRY_PATH.resolve()
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        p = (_THIS_DIR / p).resolve()
    else:
        p = p.resolve()
    return p


def normalize_domain(value: object) -> str:
    """Strip whitespace and lowercase a domain string. Does NOT validate
    syntax — callers use the validators below for that. Returns empty
    string for ``None`` / non-string input."""
    if value is None:
        return ""
    try:
        s = str(value).strip().lower()
    except Exception:
        return ""
    return s


def normalize_url(value: object) -> str:
    """Strip whitespace around a URL string. Does NOT validate; the
    URL safety checks live in :func:`is_url_allowed_for_source`."""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_source_id(value: object) -> Tuple[Optional[str], List[str]]:
    if value is None or value == "":
        return None, ["source_id is required"]
    try:
        s = str(value)
    except Exception:
        return None, ["source_id must be a string"]
    if not _SOURCE_ID_RE.match(s):
        return None, [
            f"source_id {s!r} must match ^[a-z][a-z0-9_]{{2,79}}$ "
            "(lowercase ASCII, digits, underscores; start with letter)"
        ]
    return s, []


def _validate_enum(value: object, allowed: Iterable[str], *, field: str,
                   required: bool = True) -> Tuple[Optional[str], List[str]]:
    if value is None or value == "":
        if required:
            return None, [f"{field} is required"]
        return None, []
    try:
        s = str(value).strip()
    except Exception:
        return None, [f"{field} must be a string"]
    if s not in allowed:
        return None, [
            f"{field} {s!r} must be one of: {', '.join(allowed)}"
        ]
    return s, []


def _validate_https_url(value: object, *, allow_local_demo: bool,
                        field: str) -> Tuple[Optional[str], List[str]]:
    """https:// is required everywhere. Demo sources may also use
    http:// against localhost/127.0.0.1/::1 for fixture tests."""
    if value is None or value == "":
        return None, [f"{field} is required"]
    s = normalize_url(value)
    parsed = urlparse(s)
    if not parsed.scheme or not parsed.netloc:
        return None, [f"{field} {s!r} must be an absolute URL"]
    if parsed.username or parsed.password:
        return None, [
            f"{field} {s!r} must not embed credentials (no user:pass@host)"
        ]
    if "?" in s or "#" in s:
        return None, [
            f"{field} {s!r} must not carry a query string or fragment"
        ]
    scheme = parsed.scheme.lower()
    if scheme == "https":
        pass
    elif scheme == "http" and allow_local_demo:
        host = (parsed.hostname or "").lower()
        if host not in _DEMO_LOCAL_HOSTS:
            return None, [
                f"{field} {s!r} may use http only for localhost/127.0.0.1/::1"
            ]
    else:
        return None, [f"{field} {s!r} must use https:// (got {scheme!r})"]
    return s, []


def _validate_domain_list(values: object, *, field: str) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    if values is None:
        return [], [f"{field} is required (non-empty list)"]
    if not isinstance(values, list):
        return [], [f"{field} must be a list"]
    if not values:
        return [], [f"{field} must be non-empty"]
    out: List[str] = []
    seen = set()
    for raw in values:
        d = normalize_domain(raw)
        if not d:
            errors.append(f"{field} entry is empty / non-string: {raw!r}")
            continue
        # Reject obvious wildcards. Sub-domain handling is per-source
        # via ``allow_subdomains: true``, not via "*.example.com".
        if "*" in d:
            errors.append(f"{field} entry {d!r} must not contain '*'")
            continue
        # Reject embedded scheme / path / port / userinfo.
        for forbidden_char in ("://", "/", "?", "#", ":", "@"):
            if forbidden_char in d:
                errors.append(
                    f"{field} entry {d!r} must be a bare hostname "
                    f"(no {forbidden_char!r})"
                )
                break
        else:
            if not _DOMAIN_RE.match(d):
                errors.append(
                    f"{field} entry {d!r} is not a valid ASCII hostname"
                )
                continue
            if d in seen:
                errors.append(f"{field} entry {d!r} duplicated")
                continue
            seen.add(d)
            out.append(d)
    return out, errors


def _scan_for_token_literals(record: Dict[str, Any]) -> List[str]:
    """Reject token-shaped literals in string-valued metadata fields.
    Hex 32+ runs and SDK-key prefixes (sk-XXXX…) both flag."""
    issues: List[str] = []

    def _scan(value: object, *, path: str) -> None:
        if isinstance(value, str):
            if _HEX_TOKEN_RE.search(value):
                issues.append(
                    f"{path}: contains a 32+ hex literal that resembles a token"
                )
            if _SDK_KEY_PREFIX_RE.search(value):
                issues.append(
                    f"{path}: contains an SDK-key prefix (sk-…) literal"
                )
        elif isinstance(value, list):
            for i, item in enumerate(value):
                _scan(item, path=f"{path}[{i}]")
        elif isinstance(value, dict):
            for k, v in value.items():
                _scan(v, path=f"{path}.{k}")

    for k, v in record.items():
        _scan(v, path=str(k))
    return issues


def validate_source_record(record: object) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Validate one source dict. Returns (normalized_record, errors,
    warnings). Never raises — callers decide whether errors are
    fatal. Sets safe defaults for missing optional fields so callers
    can rely on a stable shape.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(record, dict):
        return ({}, ["source record must be a dict"], [])

    source_id, e = _validate_source_id(record.get("source_id"))
    errors.extend(e)

    source_type, e = _validate_enum(
        record.get("source_type"), KNOWN_SOURCE_TYPES, field="source_type",
    )
    errors.extend(e)

    capture_method, e = _validate_enum(
        record.get("capture_method"), KNOWN_CAPTURE_METHODS,
        field="capture_method",
    )
    errors.extend(e)

    browser_automation, e = _validate_enum(
        record.get("browser_automation"), KNOWN_BROWSER_AUTOMATION,
        field="browser_automation",
    )
    errors.extend(e)

    # Per-source flag controlling http-vs-https demo exception.
    is_demo = (source_type == "demo")

    base_url, e = _validate_https_url(
        record.get("base_url"), allow_local_demo=is_demo, field="base_url",
    )
    errors.extend(e)

    allowed_domains, e = _validate_domain_list(
        record.get("allowed_domains"), field="allowed_domains",
    )
    errors.extend(e)

    # truth_claim must be False (defaults to False if missing).
    truth_claim = bool(record.get("truth_claim", False))
    if truth_claim:
        errors.append(
            "truth_claim must be false — the registry never asserts truth"
        )

    # operator_review_required defaults to True. Setting it to False
    # without an explicit justification field is an error.
    operator_review_required_raw = record.get("operator_review_required", True)
    operator_review_required = bool(operator_review_required_raw)
    if not operator_review_required:
        justification = record.get("operator_review_required_justification")
        if not (isinstance(justification, str) and justification.strip()):
            errors.append(
                "operator_review_required=False requires "
                "'operator_review_required_justification' (non-empty string)"
            )

    # official_source_candidate optional; defaults to False.
    official_source_candidate = bool(
        record.get("official_source_candidate", False)
    )

    # default_enabled optional; defaults to False.
    default_enabled = bool(record.get("default_enabled", False))

    # allow_subdomains optional; defaults to False (exact-match only).
    allow_subdomains = bool(record.get("allow_subdomains", False))

    # semantic_debug_only optional; defaults to False (no debug exposure).
    semantic_debug_only = bool(record.get("semantic_debug_only", False))

    # jurisdiction / display_name / notes / tags are all optional strings/
    # lists. We do not constrain values here beyond the token-literal
    # scan below.
    jurisdiction = record.get("jurisdiction")
    display_name = record.get("display_name")
    notes = record.get("notes")
    tags = record.get("tags")

    if jurisdiction is not None and not isinstance(jurisdiction, str):
        errors.append("jurisdiction must be a string when present")
    if display_name is not None and not isinstance(display_name, str):
        errors.append("display_name must be a string when present")
    if notes is not None and not isinstance(notes, str):
        errors.append("notes must be a string when present")
    if tags is not None:
        if not isinstance(tags, list) or not all(
            isinstance(t, str) for t in tags
        ):
            errors.append("tags must be a list[str] when present")

    # capture_method=browser_required without browser_automation>=maybe_required
    # is a contradiction.
    if (capture_method == "browser_required"
            and browser_automation == "not_required"):
        errors.append(
            "capture_method='browser_required' contradicts "
            "browser_automation='not_required'"
        )

    # No token-shaped literals anywhere in metadata.
    errors.extend(_scan_for_token_literals(record))

    normalized: Dict[str, Any] = {
        "source_id": source_id,
        "display_name": display_name,
        "source_type": source_type,
        "jurisdiction": jurisdiction,
        "base_url": base_url,
        "allowed_domains": allowed_domains,
        "allow_subdomains": allow_subdomains,
        "default_enabled": default_enabled,
        "capture_method": capture_method,
        "browser_automation": browser_automation,
        "operator_review_required": operator_review_required,
        "official_source_candidate": official_source_candidate,
        "truth_claim": False,            # forced — registry never asserts truth
        "semantic_debug_only": semantic_debug_only,
        "notes": notes,
        "tags": list(tags) if isinstance(tags, list) else [],
    }
    # Preserve the justification field when present (it's only
    # meaningful when operator_review_required=False).
    if "operator_review_required_justification" in record:
        normalized["operator_review_required_justification"] = (
            record.get("operator_review_required_justification")
        )
    return normalized, errors, warnings


def validate_source_registry(registry: object) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Validate the top-level registry. Returns (normalized_registry,
    errors, warnings). Defensive against malformed input — never raises."""
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(registry, dict):
        return ({}, ["registry must be a dict"], [])

    schema_version = registry.get("schema_version")
    if schema_version != SOURCE_REGISTRY_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SOURCE_REGISTRY_SCHEMA_VERSION} "
            f"(got {schema_version!r})"
        )

    name = registry.get("registry_name")
    if name != REGISTRY_NAME:
        warnings.append(
            f"registry_name {name!r} differs from documented "
            f"{REGISTRY_NAME!r}"
        )

    sources_raw = registry.get("sources")
    if not isinstance(sources_raw, list):
        errors.append("sources must be a list")
        sources_raw = []

    normalized_sources: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for i, raw in enumerate(sources_raw):
        normalized, src_errors, src_warnings = validate_source_record(raw)
        # Surface source-specific errors with row context so the
        # validator CLI can point at the offending entry.
        for msg in src_errors:
            errors.append(f"sources[{i}]: {msg}")
        for msg in src_warnings:
            warnings.append(f"sources[{i}]: {msg}")
        sid = normalized.get("source_id")
        if sid:
            if sid in seen_ids:
                errors.append(f"sources[{i}]: duplicate source_id {sid!r}")
            else:
                seen_ids.add(sid)
        normalized_sources.append(normalized)

    normalized_registry = {
        "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
        "registry_name": REGISTRY_NAME,
        "sources": normalized_sources,
    }
    return normalized_registry, errors, warnings


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_source_registry(path: Optional[object] = None) -> Dict[str, Any]:
    """Load + parse the JSON registry. Raises ``SourceRegistryError``
    on file or JSON errors. Does NOT validate semantically — callers
    should run :func:`validate_source_registry` on the returned dict.
    """
    resolved = normalize_registry_path(path)
    if not resolved.exists():
        raise SourceRegistryError(
            f"source registry not found at {resolved}",
            reason="file_not_found",
        )
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as error:
        raise SourceRegistryError(
            f"could not read source registry at {resolved}: {error}",
            reason="io_error",
        ) from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SourceRegistryError(
            f"source registry at {resolved} is not valid JSON: {error}",
            reason="json_decode_error",
        ) from error
    if not isinstance(data, dict):
        raise SourceRegistryError(
            f"source registry at {resolved} must be a JSON object at top level",
            reason="top_level_not_object",
        )
    return data


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def list_sources(
    registry: Dict[str, Any],
    *,
    source_type: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Return registry sources optionally filtered by ``source_type``
    and/or ``default_enabled``."""
    sources = registry.get("sources") if isinstance(registry, dict) else None
    if not isinstance(sources, list):
        return []
    out: List[Dict[str, Any]] = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        if source_type is not None and s.get("source_type") != source_type:
            continue
        if enabled is not None and bool(s.get("default_enabled")) != enabled:
            continue
        out.append(s)
    return out


def get_source_by_id(
    registry: Dict[str, Any], source_id: object,
) -> Optional[Dict[str, Any]]:
    if not isinstance(registry, dict):
        return None
    target = str(source_id or "").strip()
    if not target:
        return None
    for s in registry.get("sources") or []:
        if isinstance(s, dict) and s.get("source_id") == target:
            return s
    return None


def find_sources_by_domain(
    registry: Dict[str, Any], domain: object,
) -> List[Dict[str, Any]]:
    """Sources whose ``allowed_domains`` exactly match the given
    (normalized) domain. Subdomain lookups are out of scope for this
    helper — use :func:`classify_url_against_registry` for URL-aware
    matching."""
    d = normalize_domain(domain)
    if not d:
        return []
    out: List[Dict[str, Any]] = []
    for s in (registry.get("sources") or []) if isinstance(registry, dict) else []:
        if not isinstance(s, dict):
            continue
        allowed = s.get("allowed_domains") or []
        if isinstance(allowed, list) and any(
            normalize_domain(a) == d for a in allowed
        ):
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# URL safety + classification
# ---------------------------------------------------------------------------


def _url_is_safe_shape(url: str, *, allow_local_demo: bool) -> Tuple[bool, str]:
    """Quick safety check the URL is parseable + https + no credentials.
    Returns (ok, reason)."""
    s = normalize_url(url)
    if not s:
        return False, "empty_url"
    parsed = urlparse(s)
    if not parsed.scheme or not parsed.netloc:
        return False, "missing_scheme_or_host"
    if parsed.username or parsed.password:
        return False, "credentials_in_url"
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return True, ""
    if scheme == "http" and allow_local_demo:
        host = (parsed.hostname or "").lower()
        if host in _DEMO_LOCAL_HOSTS:
            return True, ""
    return False, f"non_https_scheme:{scheme}"


def is_url_allowed_for_source(source: Dict[str, Any], url: object) -> bool:
    """True iff ``url`` is https (or http to localhost when the source
    is a ``demo`` type) AND its hostname matches the source's
    ``allowed_domains`` (exact match, or a strict subdomain when
    ``allow_subdomains=true``).

    Never fetches the URL.
    """
    if not isinstance(source, dict):
        return False
    is_demo = (source.get("source_type") == "demo")
    ok, _reason = _url_is_safe_shape(str(url or ""), allow_local_demo=is_demo)
    if not ok:
        return False
    parsed = urlparse(normalize_url(url))
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # Reject non-ASCII / IDN-shaped hostnames (xn--…). Future milestones
    # can extend this with an allowlist, but the v1 registry refuses
    # confusable shapes by default.
    if not _DOMAIN_RE.match(host):
        return False
    allowed = source.get("allowed_domains") or []
    if not isinstance(allowed, list):
        return False
    allow_subdomains = bool(source.get("allow_subdomains", False))
    for raw in allowed:
        d = normalize_domain(raw)
        if not d:
            continue
        if host == d:
            return True
        if allow_subdomains and host.endswith("." + d):
            # Strict subdomain match: host must end with ``.<allowed>``,
            # never just contain it (prevents foo.example.com.evil).
            return True
    return False


def classify_url_against_registry(
    registry: Dict[str, Any], url: object,
) -> Dict[str, Any]:
    """Find the registered source (if any) that owns ``url``. Never
    fetches.

    Returns a dict with stable keys:

        * ``matched_source_id`` — str or None
        * ``allowed`` — bool
        * ``reason`` — short tag describing the outcome
        * ``host`` — extracted hostname (or empty)
    """
    out: Dict[str, Any] = {
        "matched_source_id": None, "allowed": False,
        "reason": "no_match", "host": "",
    }
    if not isinstance(registry, dict):
        out["reason"] = "registry_not_object"
        return out
    s = normalize_url(url)
    if not s:
        out["reason"] = "empty_url"
        return out
    parsed = urlparse(s)
    if not parsed.scheme or not parsed.netloc:
        out["reason"] = "missing_scheme_or_host"
        return out
    if parsed.username or parsed.password:
        out["reason"] = "credentials_in_url"
        return out
    host = (parsed.hostname or "").lower()
    out["host"] = host
    if not host or not _DOMAIN_RE.match(host):
        out["reason"] = "invalid_host"
        return out

    sources = registry.get("sources") or []
    if not isinstance(sources, list):
        out["reason"] = "registry_sources_not_list"
        return out
    for source in sources:
        if not isinstance(source, dict):
            continue
        if is_url_allowed_for_source(source, s):
            out["matched_source_id"] = source.get("source_id")
            out["allowed"] = True
            out["reason"] = "matched"
            return out
    return out


# ---------------------------------------------------------------------------
# Capture plan
# ---------------------------------------------------------------------------


def build_source_capture_plan(
    source: Dict[str, Any], url: Optional[object] = None,
) -> Dict[str, Any]:
    """Build a *plan* describing how a future ingestion layer should
    treat ``source``. Does NOT fetch, does NOT scrape, does NOT
    contact any external service.

    Returns a dict with stable keys: ``source_id``, ``capture_method``,
    ``browser_automation``, ``operator_review_required``,
    ``official_source_candidate``, ``default_enabled``, ``url``,
    ``url_allowed``, ``network_fetch_performed`` (always ``false``),
    ``notes``, ``next_step``.

    ``next_step`` is one of:
        * ``manual_review`` — operator must inspect the source first
          (default for disabled sources / unknown capture method)
        * ``http_fetch_candidate`` — an HTTP-style ingestion is the
          next reasonable step
        * ``browser_candidate`` — needs browser automation; an
          HTTP-only ingester should skip
        * ``unsupported`` — registry shape is too incomplete to plan
    """
    if not isinstance(source, dict):
        return {
            "source_id": None,
            "capture_method": None,
            "browser_automation": None,
            "operator_review_required": True,
            "official_source_candidate": False,
            "default_enabled": False,
            "url": None,
            "url_allowed": False,
            "network_fetch_performed": False,
            "notes": "source argument was not a dict",
            "next_step": "unsupported",
        }

    source_id = source.get("source_id")
    capture_method = source.get("capture_method")
    browser_automation = source.get("browser_automation")
    operator_review_required = bool(source.get("operator_review_required", True))
    official_source_candidate = bool(source.get("official_source_candidate", False))
    default_enabled = bool(source.get("default_enabled", False))

    url_str = normalize_url(url) if url is not None else None
    url_allowed = bool(url_str and is_url_allowed_for_source(source, url_str))

    # Decide next_step. The registry's default posture is conservative:
    # disabled sources and sources with unknown capture flags always
    # surface as ``manual_review``.
    if not default_enabled:
        next_step = "manual_review"
    elif capture_method == "browser_required" or browser_automation == "required":
        next_step = "browser_candidate"
    elif capture_method in ("manual_or_http", "rss", "api", "html", "pdf"):
        next_step = "http_fetch_candidate"
    elif capture_method == "unknown" or browser_automation == "unknown":
        next_step = "manual_review"
    else:
        next_step = "unsupported"

    notes = source.get("notes")
    if not isinstance(notes, str):
        notes = ""

    return {
        "source_id": source_id,
        "capture_method": capture_method,
        "browser_automation": browser_automation,
        "operator_review_required": operator_review_required,
        "official_source_candidate": official_source_candidate,
        "default_enabled": default_enabled,
        "url": url_str,
        "url_allowed": url_allowed,
        "network_fetch_performed": False,
        "notes": notes,
        "next_step": next_step,
    }
