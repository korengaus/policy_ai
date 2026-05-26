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
        # M13.1c — per-provider breakdown. Caller-level top-level
        # fields stay as the SUM across providers (preserves M13.1b
        # observability test assertions). Operators can drill into
        # `by_provider[<provider>]` to compare e.g. anthropic vs
        # openai cost / latency.
        "by_provider": {},
    }


def _empty_provider_metrics() -> dict[str, Any]:
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
    provider: str = "unknown",
) -> None:
    """Push one call's metrics into the aggregator.

    ``caller`` is the short label that appears in the snapshot
    (``"llm_judge"`` / ``"ai_reasoner"``). Multiple callers may push
    concurrently — the implementation is thread-safe.

    ``provider`` (M13.1c) is the LLM provider that served the call
    (``"anthropic"`` / ``"openai"`` / ``"unknown"``). The same metric
    shape is tracked twice: at the caller level (sum across
    providers, preserving M13.1b semantics) and inside a
    ``by_provider`` sub-dict keyed by provider name. The kwarg has
    a default of ``"unknown"`` so legacy callers that don't pass it
    still work — their metrics aggregate under ``by_provider["unknown"]``.

    Failed calls still bump ``total_calls`` (at both caller and
    provider levels) but do not contribute to successful-call
    counters / token totals / cost / latency distribution. This keeps
    the success-only stats honest while still surfacing total attempt
    rate.

    NEVER raises. A broken aggregator silently degrades to no-op
    metrics rather than breaking the pipeline.
    """
    try:
        caller_key = str(caller or "unknown")
        provider_key = str(provider or "unknown")
        with _LOCK:
            caller_metrics = _STATE.setdefault(
                caller_key, _empty_caller_metrics(),
            )
            # M13.1c: backfill `by_provider` for any pre-existing state
            # row that was created before this milestone (defence in
            # depth against reset_metrics_for_tests / module reload
            # race).
            if "by_provider" not in caller_metrics:
                caller_metrics["by_provider"] = {}
            provider_metrics = caller_metrics["by_provider"].setdefault(
                provider_key, _empty_provider_metrics(),
            )

            caller_metrics["total_calls"] += 1
            provider_metrics["total_calls"] += 1
            if not success:
                return

            caller_metrics["successful_calls"] += 1
            provider_metrics["successful_calls"] += 1

            input_int = int(input_tokens or 0)
            output_int = int(output_tokens or 0)
            caller_metrics["total_input_tokens"] += input_int
            caller_metrics["total_output_tokens"] += output_int
            provider_metrics["total_input_tokens"] += input_int
            provider_metrics["total_output_tokens"] += output_int

            if estimated_cost_usd is not None:
                cost_float = float(estimated_cost_usd)
                caller_metrics["total_estimated_cost_usd"] = round(
                    float(caller_metrics["total_estimated_cost_usd"])
                    + cost_float,
                    6,
                )
                provider_metrics["total_estimated_cost_usd"] = round(
                    float(provider_metrics["total_estimated_cost_usd"])
                    + cost_float,
                    6,
                )

            latency_int = int(latency_ms or 0)
            for bucket in (
                caller_metrics["latencies_ms"],
                provider_metrics["latencies_ms"],
            ):
                bucket.append(latency_int)
                overflow = len(bucket) - _LATENCY_HISTORY_CAP
                if overflow > 0:
                    # ring-buffer trim — drop the oldest entries.
                    del bucket[:overflow]
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
    def _copy_metrics_row(row: dict[str, Any]) -> dict[str, Any]:
        """Snapshot helper — copies one caller-or-provider metrics
        row including its latency ring-buffer."""
        return {
            "total_calls": int(row["total_calls"]),
            "successful_calls": int(row["successful_calls"]),
            "total_input_tokens": int(row["total_input_tokens"]),
            "total_output_tokens": int(row["total_output_tokens"]),
            "total_estimated_cost_usd": float(
                row["total_estimated_cost_usd"]
            ),
            "_latencies_ms": list(row["latencies_ms"]),
        }

    # Step 1: copy state under lock.
    with _LOCK:
        copied: dict[str, dict[str, Any]] = {}
        for caller_key, metrics in _STATE.items():
            caller_row = _copy_metrics_row(metrics)
            providers_copy: dict[str, dict[str, Any]] = {}
            for provider_key, prov_row in (
                metrics.get("by_provider") or {}
            ).items():
                providers_copy[provider_key] = _copy_metrics_row(prov_row)
            caller_row["_by_provider_raw"] = providers_copy
            copied[caller_key] = caller_row

    # Step 2: compute statistics outside the lock.
    def _finalise_row(row: dict[str, Any]) -> dict[str, Any]:
        latencies = row.pop("_latencies_ms")
        if latencies:
            row["avg_latency_ms"] = int(
                round(sum(latencies) / len(latencies))
            )
            row["p50_latency_ms"] = _percentile(latencies, 0.50)
            row["p95_latency_ms"] = _percentile(latencies, 0.95)
        else:
            row["avg_latency_ms"] = 0
            row["p50_latency_ms"] = 0
            row["p95_latency_ms"] = 0
        row["latency_sample_count"] = len(latencies)
        row["total_estimated_cost_usd"] = round(
            row["total_estimated_cost_usd"], 6,
        )
        return row

    snapshot: dict[str, dict[str, Any]] = {}
    for caller_key, caller_row in copied.items():
        providers_raw = caller_row.pop("_by_provider_raw") or {}
        _finalise_row(caller_row)
        # M13.1c: per-provider breakdown shaped identically to the
        # caller-level row so operators can drill in. The dict key is
        # always "by_provider" so consumers can do
        # snapshot["llm_judge"]["by_provider"]["anthropic"]["total_calls"].
        caller_row["by_provider"] = {
            provider_key: _finalise_row(prov_row)
            for provider_key, prov_row in providers_raw.items()
        }
        snapshot[caller_key] = caller_row
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
