"""RATE-LIMIT — IP-keyed fixed-window throttle for the public analyze surface.

HTTP-layer abuse + LLM-cost guard. Used as a FastAPI dependency on the three
POST analyze endpoints (/analyze, /jobs/analyze, /v2/analyze), which all trigger
the full pipeline + paid LLM calls. The dependency runs BEFORE the route body, so
an over-limit request gets a clean HTTP 429 and the pipeline / LLM never runs.

Design contract
===============
* **Verdict-isolated.** Pure HTTP gate. Never imports the pipeline / scoring /
  verification-card code; never touches verdict_label / policy_alert_level /
  disagreement_signal. It can only return 429 or pass through.
* **Reuses our existing Redis.** Backing store is the same Redis the job queue
  uses, via ``job_queue.get_redis_connection()`` (lazy, graceful). No new
  dependency; no client constructed at import time.
* **Fail-open.** If Redis is unavailable (REDIS_URL unset / unreachable), the
  limiter ALLOWS the request (logging once) — a Redis hiccup must never take down
  /analyze. Availability is preferred over strict enforcement for a cost guard.
* **Shared bucket.** All three analyze endpoints share ONE per-IP counter so they
  cannot be combined to bypass the limit.
* **Correct client IP behind Render's proxy.** ``request.client.host`` is the
  Render proxy; the real client is the LEFT-MOST entry of ``X-Forwarded-For``.
  (XFF is client-settable; acceptable for a cost/abuse guard, not auth.)

Window semantics: fixed window via ``INCR`` + ``EXPIRE`` on first hit. The first
request in a window sets the TTL; the (MAX+1)-th within the same window is
rejected with ``Retry-After`` = remaining TTL seconds.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Request

import job_queue


log = logging.getLogger("policy_ai.rate_limit")


# Defaults (code-level). Both overridable via env so we can tune without a code
# change. Read lazily per request so an env change takes effect on restart.
_DEFAULT_MAX = 3
_DEFAULT_WINDOW_SECONDS = 60

# Shared Redis key prefix — the three analyze endpoints intentionally share ONE
# bucket per IP so they can't be combined to bypass the limit.
_KEY_PREFIX = "ratelimit:analyze:"

# Korean-friendly 429 body (matches the approved copy).
_RATE_LIMIT_MESSAGE = "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."

# Emit the fail-open warning at most once per process to avoid log spam when
# Redis is down for a sustained period.
_failopen_warned = False


def _int_env(name: str, default: int) -> int:
    """Parse a positive int from env; fall back to ``default`` on missing/invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _max_requests() -> int:
    return _int_env("RATE_LIMIT_ANALYZE_MAX", _DEFAULT_MAX)


def _window_seconds() -> int:
    return _int_env("RATE_LIMIT_ANALYZE_WINDOW_SECONDS", _DEFAULT_WINDOW_SECONDS)


def client_ip(request: Request) -> str:
    """Best-effort original client IP.

    Behind Render's proxy the direct peer is the proxy, so prefer the LEFT-MOST
    ``X-Forwarded-For`` entry (the original client). Falls back to the socket
    peer, then to ``"unknown"`` so a missing client never crashes the limiter.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _check(ip: str) -> None:
    """Increment the IP's window counter and raise 429 if over the limit.

    Fail-open: if Redis is unavailable, return (allow). Any unexpected Redis error
    is also swallowed as allow — the guard must never break /analyze.
    """
    global _failopen_warned
    redis_client = job_queue.get_redis_connection()
    if redis_client is None:
        if not _failopen_warned:
            log.warning(
                "rate_limit.fail_open: Redis unavailable; analyze rate limiting "
                "is disabled until Redis recovers."
            )
            _failopen_warned = True
        return

    max_requests = _max_requests()
    window = _window_seconds()
    key = _KEY_PREFIX + ip
    try:
        count = redis_client.incr(key)
        if count == 1:
            # First hit in this window — start the TTL.
            redis_client.expire(key, window)
        over_limit = count > max_requests
    except Exception as error:  # noqa: BLE001 — fail-open on any Redis error
        if not _failopen_warned:
            log.warning(
                "rate_limit.fail_open: Redis error (%s); analyze rate limiting "
                "temporarily disabled.",
                type(error).__name__,
            )
            _failopen_warned = True
        return

    if over_limit:
        try:
            ttl = int(redis_client.ttl(key))
        except Exception:  # noqa: BLE001
            ttl = window
        retry_after = ttl if ttl and ttl > 0 else window
        # Log at info — over-limit is expected operation, not an error.
        log.info(
            "rate_limit.blocked: ip=%s count=%s max=%s window=%ss",
            ip, count, max_requests, window,
        )
        raise HTTPException(
            status_code=429,
            detail=_RATE_LIMIT_MESSAGE,
            headers={"Retry-After": str(retry_after)},
        )


def analyze_rate_limiter(request: Request) -> None:
    """FastAPI dependency for the public analyze endpoints.

    Add as ``dependencies=[Depends(analyze_rate_limiter)]`` (or a parameter
    ``_: None = Depends(analyze_rate_limiter)``) on each POST analyze route.
    Raises HTTP 429 BEFORE the route body runs when the per-IP shared bucket is
    exhausted; otherwise returns ``None`` and the request proceeds.
    """
    _check(client_ip(request))
