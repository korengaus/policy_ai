"""Single-shot HTTP cache activation check (M13.3c).

After enabling ``HTTP_CACHE_ENABLED=true`` and
``OFFICIAL_CRAWLER_CACHE_ENABLED=true`` on Render, an operator runs
this script to verify the cache is actually being hit. Two
back-to-back smoke runs against the same query: if the cache is
working, the second run's elapsed time will be substantially shorter
than the first.

Usage::

    python scripts/check_cache_activation.py --help
    python scripts/check_cache_activation.py \\
        --base-url https://policy-ai-q5ax.onrender.com \\
        --query 금융위

Verdict thresholds (warm / cold ratio):

* ``< 0.75``  -> "OK Cache appears effective" (>=25% speedup)
* ``> 0.85``  -> "WARN Cache may not be active" (<15% speedup)
* otherwise   -> "AMBIGUOUS; recommend measure_cache_impact.py --runs 3"

Exit codes::

    0 -- check completed (any verdict)
    1 -- smoke runs failed (infrastructure)
    2 -- CLI usage error
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# Reuse the parser from measure_cache_impact for consistency. The
# script lives in scripts/ which we add to sys.path above so the
# import works whether the CLI is launched directly or imported as a
# module under unittest.
import measure_cache_impact as _measure  # type: ignore[no-redef]  # noqa: E402


# Verdict thresholds — explicit constants so tests can pin them.
RATIO_EFFECTIVE_THRESHOLD = 0.75
RATIO_INEFFECTIVE_THRESHOLD = 0.85


def _run_smoke_once_real(args) -> dict:
    cmd = [
        sys.executable,
        str(_PROJECT_ROOT / "scripts" / "smoke_async_job.py"),
        "--base-url", args.base_url,
        "--query", args.query,
        "--max-news", str(args.max_news),
        "--timeout-seconds", str(int(args.timeout_seconds)),
        "--poll-interval", str(args.poll_interval),
    ]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=args.timeout_seconds + 60,
        )
        return _measure.parse_smoke_output(
            completed.stdout, completed.stderr, completed.returncode,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "fail",
            "elapsed_seconds": None,
            "final_status": None,
            "exit_code": -1,
            "error": "subprocess timeout",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "fail",
            "elapsed_seconds": None,
            "final_status": None,
            "exit_code": -1,
            "error": str(exc),
        }


def _run_smoke_once_simulated(args, run_index: int) -> dict:
    """Synthetic 2-run pair. Cold = 122.3s, warm = 67.1s -> ratio 0.55,
    yielding the OK verdict. ``--simulate-ineffective`` flips to a
    pair that produces a WARN."""
    if args.simulate_ineffective:
        elapsed = (122.3, 110.5)[run_index]
    elif args.simulate_ambiguous:
        elapsed = (120.0, 95.0)[run_index]  # ratio ~0.79
    else:
        elapsed = (122.3, 67.1)[run_index]
    return {
        "status": "pass",
        "elapsed_seconds": elapsed,
        "final_status": "completed",
        "exit_code": 0,
        "simulated": True,
    }


def _classify(cold_elapsed, warm_elapsed) -> dict:
    """Compute the ratio and map to a verdict label. Tolerant of
    None / zero / negative cold values."""
    if (
        cold_elapsed is None or warm_elapsed is None
        or cold_elapsed <= 0
    ):
        return {
            "verdict": "insufficient_data",
            "verdict_label": "INSUFFICIENT_DATA",
            "ratio": None,
            "speedup_percent": None,
            "reason": "Missing elapsed time(s); cannot compare.",
        }
    ratio = warm_elapsed / cold_elapsed
    speedup_pct = round((1.0 - ratio) * 100.0, 2)
    if ratio < RATIO_EFFECTIVE_THRESHOLD:
        return {
            "verdict": "ok",
            "verdict_label": "OK",
            "ratio": round(ratio, 3),
            "speedup_percent": speedup_pct,
            "reason": (
                f"Second-run speedup {speedup_pct}% "
                f"(ratio {ratio:.3f} < {RATIO_EFFECTIVE_THRESHOLD}). "
                "Cache appears effective."
            ),
        }
    if ratio > RATIO_INEFFECTIVE_THRESHOLD:
        return {
            "verdict": "warn",
            "verdict_label": "WARN",
            "ratio": round(ratio, 3),
            "speedup_percent": speedup_pct,
            "reason": (
                f"Second-run speedup only {speedup_pct}% "
                f"(ratio {ratio:.3f} > {RATIO_INEFFECTIVE_THRESHOLD}). "
                "Cache may not be active."
            ),
        }
    return {
        "verdict": "ambiguous",
        "verdict_label": "AMBIGUOUS",
        "ratio": round(ratio, 3),
        "speedup_percent": speedup_pct,
        "reason": (
            f"Second-run speedup {speedup_pct}% "
            f"(ratio {ratio:.3f} in "
            f"[{RATIO_EFFECTIVE_THRESHOLD}, "
            f"{RATIO_INEFFECTIVE_THRESHOLD}]). "
            "Recommend statistical confirmation."
        ),
    }


def _render_human(args, cold: dict, warm: dict, verdict: dict) -> str:
    lines = ["=== Cache Activation Check ===", ""]
    lines.append(f"Base URL: {args.base_url}")
    lines.append(f"Query: {args.query}")
    lines.append("")
    lines.append(
        f"Run 1 (cold): {cold.get('elapsed_seconds')}s "
        f"status={cold.get('status')}"
    )
    if warm is not None:
        lines.append(
            f"Run 2 (warm): {warm.get('elapsed_seconds')}s "
            f"status={warm.get('status')}"
        )
    if verdict.get("ratio") is not None:
        lines.append(
            f"Speedup: {verdict.get('speedup_percent')}% "
            f"({warm.get('elapsed_seconds')}s / "
            f"{cold.get('elapsed_seconds')}s = "
            f"{verdict.get('ratio')})"
        )
    lines.append("")

    label = verdict.get("verdict_label", "?")
    reason = verdict.get("reason", "")
    if label == "OK":
        lines.append(f"[OK] {reason}")
        lines.append(
            "[Tip] Check Render logs for "
            "\"official_crawler_cache_event\" with cache_hit=True"
        )
        lines.append(
            "[Tip] For statistical confidence, run: "
            "python scripts/measure_cache_impact.py --runs 3"
        )
    elif label == "WARN":
        lines.append(f"[WARN] {reason}")
        lines.append("[Tip] Verify on Render:")
        lines.append("  - HTTP_CACHE_ENABLED=true is set")
        lines.append("  - OFFICIAL_CRAWLER_CACHE_ENABLED=true is set")
        lines.append("  - The deployed code includes M13.3b commit")
        lines.append(
            "[Tip] Check Render logs for "
            "\"official_crawler_cache_event\" entries."
        )
        lines.append(
            "[Tip] If both flags are set but no cache events appear, "
            "the query may not involve government domains in the "
            "allow-list."
        )
    elif label == "AMBIGUOUS":
        lines.append(f"[AMBIGUOUS] {reason}")
        lines.append(
            "[Tip] For statistical confidence, run: "
            "python scripts/measure_cache_impact.py --runs 3"
        )
    else:
        lines.append(f"[{label}] {reason}")
    lines.append("")
    lines.append(
        "[Safety] This script does NOT toggle Render env vars. "
        "See docs/CACHE_ACTIVATION_GUIDE.md for full activation steps."
    )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_cache_activation",
        description=(
            "Run smoke_async_job twice back-to-back and infer whether "
            "the M13.3b HTTP cache is currently active on the target "
            "deployment."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 -- check completed (any verdict)\n"
            "  1 -- smoke run failed (infra issue)\n"
            "  2 -- CLI usage error\n\n"
            "Safety: this script does NOT toggle Render env vars."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", required=True,
                        help="Target deployment URL")
    parser.add_argument("--query", required=True,
                        help="Verification query (pick one that involves "
                             "government URLs for best signal)")
    parser.add_argument("--max-news", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON.")
    parser.add_argument(
        "--simulate", action="store_true",
        help="Use deterministic synthetic timings; no real smoke calls.",
    )
    parser.add_argument(
        "--simulate-ineffective", action="store_true",
        help="(simulate variant) Force a WARN-verdict pair.",
    )
    parser.add_argument(
        "--simulate-ambiguous", action="store_true",
        help="(simulate variant) Force an AMBIGUOUS-verdict pair.",
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.simulate_ineffective or args.simulate_ambiguous:
        args.simulate = True

    # Cold run.
    if args.simulate:
        cold = _run_smoke_once_simulated(args, 0)
    else:
        cold = _run_smoke_once_real(args)
    if cold.get("status") != "pass":
        message = (
            "Cold smoke run failed; cannot evaluate cache activation."
        )
        if args.json:
            print(json.dumps({
                "verdict": "error",
                "verdict_label": "ERROR",
                "reason": message,
                "cold": cold,
                "warm": None,
            }, ensure_ascii=False, indent=2))
        else:
            print(f"[ERROR] {message}", file=sys.stderr)
            print(
                f"  cold.exit_code={cold.get('exit_code')} "
                f"error={cold.get('error')}",
                file=sys.stderr,
            )
        return 1

    # Brief documented "sleep 5 seconds" between runs; that's a real
    # delay, not a simulate one. Synthetic mode skips it.
    if not args.simulate:
        import time
        time.sleep(5)

    # Warm run.
    if args.simulate:
        warm = _run_smoke_once_simulated(args, 1)
    else:
        warm = _run_smoke_once_real(args)
    if warm.get("status") != "pass":
        message = (
            "Warm smoke run failed; partial result reported."
        )
        if args.json:
            print(json.dumps({
                "verdict": "error",
                "verdict_label": "ERROR",
                "reason": message,
                "cold": cold,
                "warm": warm,
            }, ensure_ascii=False, indent=2))
        else:
            print(f"[ERROR] {message}", file=sys.stderr)
            print(
                f"  warm.exit_code={warm.get('exit_code')} "
                f"error={warm.get('error')}",
                file=sys.stderr,
            )
        return 1

    verdict = _classify(
        cold.get("elapsed_seconds"),
        warm.get("elapsed_seconds"),
    )

    payload = {
        "checked_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        ),
        "base_url": args.base_url,
        "query": args.query,
        "cold": cold,
        "warm": warm,
        "verdict": verdict,
        "thresholds": {
            "effective_ratio_below": RATIO_EFFECTIVE_THRESHOLD,
            "ineffective_ratio_above": RATIO_INEFFECTIVE_THRESHOLD,
        },
        "simulated": bool(args.simulate),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render_human(args, cold, warm, verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
