"""M13.1b-obs (2026-05-26) — in-process aggregator for live OpenAI API
usage across the verdict pipeline.

Two production call sites push into this aggregator:

    * ``llm_judge.py`` — post-P2 judge, gated by ``LLM_JUDGE_ENABLED=true``
    * ``ai_reasoner.py`` — Phase B reasoner, always-on when
      ``OPENAI_API_KEY`` is set

For each call the aggregator records:

    * total_calls / successful_calls (per caller)
    * total_input_tokens / total_output_tokens
    * total_estimated_cost_usd (using ``llm_judge.estimate_cost_usd``)
    * latency_ms (in a bounded ring-buffer for p50 / p95)

The latency history is capped at :data:`_LATENCY_HISTORY_CAP` per
caller so a long-running Render Worker cannot accumulate millions
of entries. Bounded memory: ~16 KB per caller for the int list.

Safety
------

* NEVER raises. The public ``record_llm_call`` swallows any exception
  silently so the pipeline never breaks because of broken
  observability. (The same contract M11.7a-2 / M11.7c established for
  the rest of the pipeline.)
* Logs no prompt or response content. Token counts, cost, latency,
  and provider names only.
* No network I/O. No SDK imports.
* Thread-safe: M15.0d's per-news-item ``ThreadPoolExecutor`` means
  two callers can land here concurrently. A single module-level
  ``threading.Lock`` guards the state-mutation path. The accessor
  copies the state out under lock and computes p50 / p95 outside
  the critical section.
"""

from __future__ import annotations

import statistics
import threading
from typing import Any, Optional

from llm_judge import LLM_COST_PER_1K, estimate_cost_usd


# Per-caller latency ring buffer cap. p95 stays stable at 1000
# samples; raise only with operator review.
_LATENCY_HISTORY_CAP = 1000


# Module-level state. Keys are caller labels (e.g. "llm_judge",
# "ai_reasoner"); values are the metrics dict.
_LOCK = threading.Lock()
_STATE: dict[str, dict[str, Any]] = {}


def _empty_caller_metrics() -> dict[str, Any]:
    return {
        "total_calls": 0,
        "successful_calls": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_estimated_cost_usd": 0.0,
        "latencies_ms": [],
    }


def record_llm_call(
    *,
    caller: str,
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: Optional[float],
    latency_ms: int,
    success: bool,
) -> None:
    """Push one call's metrics into the aggregator.

    ``caller`` is the short label that appears in the snapshot
    (``"llm_judge"`` / ``"ai_reasoner"``). Multiple callers may push
    concurrently — the implementation is thread-safe.

    Failed calls still bump ``total_calls`` but do not contribute to
    successful-call counters / token totals / cost / latency
    distribution. This keeps the success-only stats honest while
    still surfacing total attempt rate.

    NEVER raises. A broken aggregator silently degrades to no-op
    metrics rather than breaking the pipeline.
    """
    try:
        caller_key = str(caller or "unknown")
        with _LOCK:
            metrics = _STATE.setdefault(caller_key, _empty_caller_metrics())
            metrics["total_calls"] += 1
            if not success:
                return
            metrics["successful_calls"] += 1
            metrics["total_input_tokens"] += int(input_tokens or 0)
            metrics["total_output_tokens"] += int(output_tokens or 0)
            if estimated_cost_usd is not None:
                metrics["total_estimated_cost_usd"] = round(
                    float(metrics["total_estimated_cost_usd"])
                    + float(estimated_cost_usd),
                    6,
                )
            latencies = metrics["latencies_ms"]
            latencies.append(int(latency_ms or 0))
            overflow = len(latencies) - _LATENCY_HISTORY_CAP
            if overflow > 0:
                # ring-buffer trim — drop the oldest entries.
                del latencies[:overflow]
    except Exception:  # noqa: BLE001 — never break the pipeline
        return


def _percentile(values: list[int], p: float) -> int:
    """Linear-interpolation percentile. Returns 0 on empty input."""
    if not values:
        return 0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return int(sorted_values[0])
    # statistics.quantiles uses n-1 cuts; convert p∈[0,1] to a cut.
    # For p95 we want the 95th percentile of the empirical
    # distribution; using ``inclusive`` matches NumPy's default.
    try:
        cuts = statistics.quantiles(
            sorted_values, n=100, method="inclusive",
        )
        # cuts is 99 boundaries (1% to 99%); index 94 = 95th percentile.
        index = max(0, min(98, int(round(p * 100)) - 1))
        return int(round(cuts[index]))
    except statistics.StatisticsError:
        # Fallback for n < 2 (shouldn't happen here — we early-return
        # on len==1 above).
        return int(sorted_values[-1])


def get_metrics_snapshot() -> dict[str, dict[str, Any]]:
    """Return a deep-copy snapshot of aggregator state, with derived
    averages and p50 / p95 latency computed at read time.

    The snapshot is computed under lock for the copy, then the
    statistics are computed outside the critical section so concurrent
    ``record_llm_call`` paths are not blocked on the (potentially
    slow) percentile computation.

    Shape:

        {
          "<caller>": {
            "total_calls": int,
            "successful_calls": int,
            "total_input_tokens": int,
            "total_output_tokens": int,
            "total_estimated_cost_usd": float,
            "avg_latency_ms": int,
            "p50_latency_ms": int,
            "p95_latency_ms": int,
            "latency_sample_count": int,
          },
          ...
        }
    """
    # Step 1: copy state under lock.
    with _LOCK:
        copied: dict[str, dict[str, Any]] = {}
        for caller_key, metrics in _STATE.items():
            copied[caller_key] = {
                "total_calls": int(metrics["total_calls"]),
                "successful_calls": int(metrics["successful_calls"]),
                "total_input_tokens": int(metrics["total_input_tokens"]),
                "total_output_tokens": int(metrics["total_output_tokens"]),
                "total_estimated_cost_usd": float(
                    metrics["total_estimated_cost_usd"]
                ),
                "_latencies_ms": list(metrics["latencies_ms"]),
            }

    # Step 2: compute statistics outside the lock.
    snapshot: dict[str, dict[str, Any]] = {}
    for caller_key, metrics in copied.items():
        latencies = metrics.pop("_latencies_ms")
        if latencies:
            avg_latency_ms = int(round(sum(latencies) / len(latencies)))
            p50_latency_ms = _percentile(latencies, 0.50)
            p95_latency_ms = _percentile(latencies, 0.95)
        else:
            avg_latency_ms = 0
            p50_latency_ms = 0
            p95_latency_ms = 0
        metrics["avg_latency_ms"] = avg_latency_ms
        metrics["p50_latency_ms"] = p50_latency_ms
        metrics["p95_latency_ms"] = p95_latency_ms
        metrics["latency_sample_count"] = len(latencies)
        # Round total cost to 6 decimal places for display determinism.
        metrics["total_estimated_cost_usd"] = round(
            metrics["total_estimated_cost_usd"], 6,
        )
        snapshot[caller_key] = metrics
    return snapshot


def reset_metrics_for_tests() -> None:
    """Clear all caller state. Used by tests; not intended for
    production code."""
    with _LOCK:
        _STATE.clear()


# Re-exports so ai_reasoner.py doesn't need a separate llm_judge
# import for cost helpers. The pricing dict + estimator continue to
# live in llm_judge.py (the M13.1a/b decision; see
# docs/MAGIC_THRESHOLDS.md and llm_judge.py:87 verification comment).
__all__ = (
    "record_llm_call",
    "get_metrics_snapshot",
    "reset_metrics_for_tests",
    "estimate_cost_usd",
    "LLM_COST_PER_1K",
)
