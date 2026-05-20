"""Phase 2 M8.0: review-API safety gate.

A deliberately minimal env-var-driven gate that keeps the reviewer
endpoints off by default. This is **not** a real auth system — it's a
temporary fence so the M8.0 review surface cannot be reached
accidentally from a public Render deploy. A proper auth + admin layer
is a future milestone.

Gate logic:
    * ``REVIEW_API_ENABLED`` (env)
        - unset / "false" / "0" / "no" / "off" → endpoint returns 503
        - "true" / "1" / "yes" / "on"          → token check runs
    * ``REVIEW_API_TOKEN`` (env)
        - required when REVIEW_API_ENABLED is true
        - missing → 503 (configuration error, surface to operator)
    * request header ``X-Review-Token``
        - missing            → 403
        - present but wrong  → 403
        - present and matches REVIEW_API_TOKEN → request proceeds

Constants the API layer can reuse for status-code consistency. The
helper is pure-stdlib + FastAPI's ``HTTPException`` so tests can
exercise it without a running server.
"""

from __future__ import annotations

import os
from typing import Optional

try:
    # FastAPI is only an import-time dependency for type / exception
    # shapes — the helper is also callable from non-FastAPI code (tests)
    # via the lower-level ``check_review_request`` function.
    from fastapi import Header, HTTPException
except Exception:  # pragma: no cover - FastAPI is required in this repo
    Header = None  # type: ignore[assignment]
    HTTPException = Exception  # type: ignore[assignment]


REVIEW_API_ENABLED_ENV = "REVIEW_API_ENABLED"
REVIEW_API_TOKEN_ENV = "REVIEW_API_TOKEN"
REVIEW_TOKEN_HEADER = "X-Review-Token"

# HTTP status codes the gate returns. Kept distinct so the API layer
# can map them onto specific error messages without leaking the token.
DISABLED_STATUS_CODE = 503
FORBIDDEN_STATUS_CODE = 403


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes", "on"}


def review_api_enabled() -> bool:
    """True when the env explicitly opts in. Default: False."""
    return _truthy(os.environ.get(REVIEW_API_ENABLED_ENV))


def configured_token() -> Optional[str]:
    """Return the configured token value (never log this)."""
    return os.environ.get(REVIEW_API_TOKEN_ENV) or None


def check_review_request(provided_token: Optional[str]) -> None:
    """Raise ``HTTPException`` when the request should be refused. The
    function is intentionally side-effect-free otherwise so the API
    layer can wrap it as a FastAPI dependency.

    The function never prints or returns the configured token. Error
    messages identify *why* the request was refused (disabled, missing
    token, wrong token) but never echo any token value.
    """
    if not review_api_enabled():
        raise HTTPException(
            status_code=DISABLED_STATUS_CODE,
            detail=(
                "Review API is disabled. Set REVIEW_API_ENABLED=true and "
                "REVIEW_API_TOKEN to enable. See docs/REVIEW_WORKFLOW.md."
            ),
        )
    expected = configured_token()
    if not expected:
        raise HTTPException(
            status_code=DISABLED_STATUS_CODE,
            detail=(
                "Review API is enabled but REVIEW_API_TOKEN is not configured. "
                "Set REVIEW_API_TOKEN to a non-empty secret."
            ),
        )
    if not provided_token or provided_token != expected:
        raise HTTPException(
            status_code=FORBIDDEN_STATUS_CODE,
            detail="Missing or invalid X-Review-Token header.",
        )


def require_review_token(x_review_token: Optional[str] = None):
    """FastAPI dependency entry point. Use as::

        @app.get("/review/tasks")
        def list_tasks(_: None = Depends(require_review_token)):
            ...

    FastAPI injects the ``X-Review-Token`` header via the ``Header``
    type alias defined below; this function calls ``check_review_request``
    and returns ``None`` on success.
    """
    check_review_request(x_review_token)
    return None


# FastAPI ``Header`` default. Kept here so the API layer can write
# ``x_review_token: Optional[str] = Header(default=None, alias=REVIEW_TOKEN_HEADER)``
# without re-importing or re-spelling the alias.
if Header is not None:
    review_token_header_param = Header(default=None, alias=REVIEW_TOKEN_HEADER)
else:  # pragma: no cover - FastAPI absent
    review_token_header_param = None
