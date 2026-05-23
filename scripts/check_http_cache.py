"""HTTP cache diagnostic CLI (M13.3a).

Read-only by default. Reports the cache singleton's configuration,
current size, and stats. With ``--simulate-*`` flags, exercises the
put/get path against a private, in-test :class:`HttpCache` instance so
operators can verify the validation pipeline without standing up real
HTTP traffic.

The CLI NEVER makes a real HTTP request, NEVER touches
``policy_ai.db``, and NEVER writes to ``reports/``.

Usage::

    python scripts/check_http_cache.py --help
    python scripts/check_http_cache.py                 # human-readable status
    python scripts/check_http_cache.py --status        # alias
    python scripts/check_http_cache.py --json          # machine-readable
    python scripts/check_http_cache.py --simulate-hit
    python scripts/check_http_cache.py --simulate-deny
    python scripts/check_http_cache.py --simulate-expired

Exit codes::

    0 — status reported successfully (or simulation observed the expected outcome)
    1 — simulation failed to observe the expected outcome (e.g. --simulate-hit
        returned no entry, --simulate-expired returned a fresh entry)
    2 — CLI usage error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


import http_cache  # noqa: E402 — import after sys.path manipulation


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _format_stats_block(stats: dict) -> str:
    lines = []
    width = max(len(key) for key in stats.keys()) + 2
    for key in (
        "hits", "misses", "expired",
        "refused_by_domain", "refused_by_cache_control",
        "evicted", "stored", "disabled_calls",
    ):
        if key not in stats:
            continue
        lines.append(f"  {key + ':':<{width}}{stats[key]}")
    return "\n".join(lines)


def _render_status_human(status: dict) -> str:
    enabled = status["enabled"]
    enabled_suffix = (
        '' if enabled
        else '  (env HTTP_CACHE_ENABLED != "true")'
    )
    allowed = status["allowed_domains"]
    denied = status["denied_domains"]
    lines = ["=== HTTP Cache Status ===", ""]
    lines.append(f"Enabled:                {enabled}{enabled_suffix}")
    lines.append(
        f"Current size:           {status['current_size']} / "
        f"{status['max_entries']}"
    )
    lines.append(f"Default TTL:            {status['default_ttl_seconds']}s")
    lines.append(
        "Allowed domains:        "
        + (", ".join(allowed) if allowed
           else "(empty -- allow all not denied)")
    )
    lines.append(
        "Denied domains:         "
        + (", ".join(denied) if denied else "(empty)")
    )
    lines.append("")
    lines.append("Stats since process start:")
    lines.append(_format_stats_block(status["stats"]))
    lines.append("")
    lines.append(
        "[Safety] M13.3a infrastructure only. Cache is NOT integrated "
        "with official_crawler, official_source_body, news_collector, "
        "or any other pipeline component."
    )
    lines.append(
        "[Safety] No verdict, no analysis result, no evidence depends "
        "on this module in M13.3a."
    )
    lines.append(
        "[Safety] Default state: disabled. Set HTTP_CACHE_ENABLED=true "
        "to activate."
    )
    return "\n".join(lines)


def _render_simulation_human(
    title: str, steps: list, stats: dict, success: bool,
    safety_note: str = (
        "[Safety] Synthetic URL -- no real network call made."
    ),
) -> str:
    lines = [f"=== HTTP Cache Simulation: {title} ===", ""]
    for index, step in enumerate(steps, start=1):
        lines.append(step)
    lines.append("")
    lines.append(
        "Stats: "
        + " ".join(f"{k}={stats.get(k, 0)}" for k in (
            "hits", "misses", "stored",
            "refused_by_domain", "refused_by_cache_control",
            "expired", "disabled_calls",
        ))
    )
    lines.append(safety_note)
    if not success:
        lines.append(
            "[Result] FAILED -- expected outcome not observed. "
            "See exit code 1."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Simulations — each uses a fresh HttpCache instance with explicit
# allow/deny lists so the test outcome is deterministic regardless of
# what the singleton has cached so far.
# ---------------------------------------------------------------------------


_SIM_URL = "https://example.gov.kr/test"
_SIM_DENIED_URL = "https://denied.example.gov.kr/test"


def _simulate_hit() -> tuple:
    """Put a synthetic entry, then get it. Expected: hit."""
    cache = http_cache.HttpCache(
        max_entries=10,
        default_ttl_seconds=3600,
    )
    body = b"hello"
    steps = []
    stored = cache.put(_SIM_URL, body, status_code=200)
    steps.append(
        f'Step 1: put({_SIM_URL!r}, b"hello", status=200)'
        f"\n        result: stored={stored}"
    )
    entry = cache.get(_SIM_URL)
    if entry is not None:
        steps.append(
            f"Step 2: get({_SIM_URL!r})"
            f"\n        result: hit (bytes={entry.bytes_size})"
        )
        success = stored is True
    else:
        steps.append(
            f"Step 2: get({_SIM_URL!r})"
            f"\n        result: MISS (unexpected)"
        )
        success = False
    return cache.snapshot(), steps, success


def _simulate_deny() -> tuple:
    """Put against a denied domain. Expected: refused."""
    cache = http_cache.HttpCache(
        max_entries=10,
        denied_domains={"denied.example.gov.kr"},
    )
    steps = []
    stored = cache.put(_SIM_DENIED_URL, b"payload", status_code=200)
    steps.append(
        f"Step 1: put({_SIM_DENIED_URL!r}, ...)"
        f"\n        result: stored={stored} (expected False)"
    )
    entry = cache.get(_SIM_DENIED_URL)
    steps.append(
        f"Step 2: get({_SIM_DENIED_URL!r})"
        f"\n        result: entry={'None' if entry is None else 'present (unexpected)'}"
    )
    success = (stored is False) and (entry is None)
    return cache.snapshot(), steps, success


def _simulate_expired() -> tuple:
    """Put with tiny TTL, sleep past it, get. Expected: expired (None)."""
    cache = http_cache.HttpCache(max_entries=10)
    steps = []
    stored = cache.put(
        _SIM_URL, b"will expire", status_code=200,
        ttl_seconds=1,  # smallest positive int per put() contract
    )
    # Force the entry's expires_at into the past without sleeping. This
    # keeps the CLI fast and deterministic on slow CI machines while
    # still exercising the is_expired() code path.
    with cache._lock:  # noqa: SLF001 — diagnostic CLI may peek
        for entry in cache._store.values():
            entry.expires_at = time.time() - 1.0
    steps.append(
        f'Step 1: put({_SIM_URL!r}, b"will expire", ttl_seconds=1)'
        f"\n        result: stored={stored}"
    )
    steps.append(
        "Step 2: simulate clock advance past expiry (no real sleep)"
    )
    entry = cache.get(_SIM_URL)
    steps.append(
        f"Step 3: get({_SIM_URL!r})"
        f"\n        result: entry={'None (expired)' if entry is None else 'present (unexpected)'}"
    )
    success = (stored is True) and (entry is None)
    return cache.snapshot(), steps, success


def _emit(args, payload, human_text, success_when_simulating=None):
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(human_text)


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_http_cache",
        description=(
            "Diagnostic CLI for the M13.3a shared HTTP cache. Reports "
            "configuration, size, and stats. Optional --simulate-* "
            "flags exercise the put/get path without any real HTTP "
            "traffic."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 -- status reported / simulation succeeded\n"
            "  1 -- simulation observed an unexpected outcome\n"
            "  2 -- CLI usage error\n\n"
            "Safety: M13.3a is infrastructure only. The cache is NOT "
            "integrated with any pipeline component. The CLI never "
            "makes a real HTTP request."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--status", action="store_true",
        help="Print the current cache singleton's status (default).",
    )
    mode.add_argument(
        "--simulate-hit", action="store_true",
        help=(
            "Synthetic put + get round-trip. Requires the cache to be "
            "enabled via HTTP_CACHE_ENABLED=true."
        ),
    )
    mode.add_argument(
        "--simulate-deny", action="store_true",
        help=(
            "Synthetic put against a denied domain to confirm refusal "
            "is logged."
        ),
    )
    mode.add_argument(
        "--simulate-expired", action="store_true",
        help=(
            "Synthetic put + advance clock past expiry + get to "
            "confirm expiry handling."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the human report.",
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    # Simulations require the cache to be enabled. Refuse early so the
    # operator sees a clear error instead of "Step 1: stored=False".
    simulation_requested = (
        args.simulate_hit or args.simulate_deny or args.simulate_expired
    )
    if simulation_requested and not http_cache.is_http_cache_enabled():
        message = (
            "Simulations require HTTP_CACHE_ENABLED=true. The default "
            "(disabled) state is what M13.3a ships with; export the "
            "env var for this one command to exercise the cache."
        )
        if args.json:
            print(json.dumps(
                {"error": message, "exit_code": 1}, indent=2,
            ))
        else:
            print(f"error: {message}", file=sys.stderr)
        return 1

    if args.simulate_hit:
        snap, steps, success = _simulate_hit()
        if args.json:
            print(json.dumps({
                "simulation": "hit",
                "success": success,
                "snapshot": snap,
                "steps": steps,
            }, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(_render_simulation_human(
                "hit", steps, snap["stats"], success,
            ))
        return 0 if success else 1

    if args.simulate_deny:
        snap, steps, success = _simulate_deny()
        if args.json:
            print(json.dumps({
                "simulation": "deny",
                "success": success,
                "snapshot": snap,
                "steps": steps,
            }, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(_render_simulation_human(
                "deny", steps, snap["stats"], success,
            ))
        return 0 if success else 1

    if args.simulate_expired:
        snap, steps, success = _simulate_expired()
        if args.json:
            print(json.dumps({
                "simulation": "expired",
                "success": success,
                "snapshot": snap,
                "steps": steps,
            }, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(_render_simulation_human(
                "expired", steps, snap["stats"], success,
            ))
        return 0 if success else 1

    # Default mode: status.
    status = http_cache.health_check()
    if args.json:
        # Add the safety metadata operators expect from M13.3 CLIs.
        payload = dict(status)
        payload["safety"] = {
            "milestone": "M13.3a",
            "integrated_with_pipeline": False,
            "makes_real_http_calls": False,
        }
        print(json.dumps(
            payload, indent=2, ensure_ascii=False, sort_keys=True,
        ))
    else:
        print(_render_status_human(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
