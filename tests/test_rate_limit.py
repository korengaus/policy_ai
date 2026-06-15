"""RATE-LIMIT tests — offline, fakeredis-backed.

Exercises rate_limit.analyze_rate_limiter (the FastAPI dependency) directly with a
minimal fake Request, so no httpx/TestClient and no pipeline/LLM are touched. The
dependency only reads request.headers["x-forwarded-for"] and request.client.host,
so the fake below is sufficient and faithful.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi import HTTPException

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import rate_limit  # noqa: E402


class _FakeRequest:
    """Mimics the subset of starlette.Request the limiter uses."""

    def __init__(self, xff=None, peer="10.0.0.1"):
        headers = {}
        if xff is not None:
            headers["x-forwarded-for"] = xff
        self.headers = headers  # dict.get matches the limiter's .get("x-forwarded-for")
        self.client = SimpleNamespace(host=peer) if peer else None


@pytest.fixture
def fake_redis(monkeypatch):
    """Point the limiter at a fresh in-memory fakeredis for each test."""
    client = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(rate_limit.job_queue, "get_redis_connection", lambda: client)
    # Default window/limit env clean per test; reset the once-only warn flag.
    monkeypatch.delenv("RATE_LIMIT_ANALYZE_MAX", raising=False)
    monkeypatch.delenv("RATE_LIMIT_ANALYZE_WINDOW_SECONDS", raising=False)
    rate_limit._failopen_warned = False
    return client


def _call(xff="1.1.1.1"):
    rate_limit.analyze_rate_limiter(_FakeRequest(xff=xff))


def test_default_three_allowed_then_429(fake_redis):
    # Code default is 3/60s. First three pass, the fourth is blocked.
    for _ in range(3):
        _call()
    with pytest.raises(HTTPException) as exc:
        _call()
    assert exc.value.status_code == 429


def test_429_body_and_retry_after(fake_redis):
    for _ in range(3):
        _call()
    with pytest.raises(HTTPException) as exc:
        _call()
    err = exc.value
    assert err.detail == "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."
    retry_after = err.headers.get("Retry-After")
    assert retry_after is not None
    assert int(retry_after) > 0  # seconds remaining in the window


def test_separate_ips_have_separate_buckets(fake_redis):
    # Exhaust IP A.
    for _ in range(3):
        _call(xff="1.1.1.1")
    with pytest.raises(HTTPException):
        _call(xff="1.1.1.1")
    # IP B is untouched — first request still allowed.
    _call(xff="2.2.2.2")  # must not raise


def test_shared_bucket_across_endpoints_uses_leftmost_xff(fake_redis):
    # The left-most XFF entry is the client; trailing proxy hops are ignored, so
    # these two requests share ONE bucket (same client 1.1.1.1).
    _call(xff="1.1.1.1, 2.2.2.2, 10.0.0.9")
    _call(xff="1.1.1.1, 9.9.9.9")
    _call(xff="1.1.1.1")
    with pytest.raises(HTTPException):
        _call(xff="1.1.1.1, 7.7.7.7")  # 4th for client 1.1.1.1 -> blocked


def test_fail_open_when_redis_unavailable(monkeypatch):
    # get_redis_connection() -> None must ALLOW every request (never raise).
    monkeypatch.setattr(rate_limit.job_queue, "get_redis_connection", lambda: None)
    rate_limit._failopen_warned = False
    for _ in range(50):
        _call()  # no HTTPException, ever


def test_env_override_max(monkeypatch, fake_redis):
    monkeypatch.setenv("RATE_LIMIT_ANALYZE_MAX", "1")
    _call()  # first allowed
    with pytest.raises(HTTPException) as exc:
        _call()  # second blocked under the tighter limit
    assert exc.value.status_code == 429


def test_falls_back_to_peer_ip_without_xff(fake_redis):
    # No X-Forwarded-For -> key on the socket peer; still enforces the limit.
    req = _FakeRequest(xff=None, peer="203.0.113.5")
    for _ in range(3):
        rate_limit.analyze_rate_limiter(req)
    with pytest.raises(HTTPException):
        rate_limit.analyze_rate_limiter(req)
