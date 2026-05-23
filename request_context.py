"""Per-request context for structured logging (M14.3a, extended M14.3b).

Currently holds a single value, ``request_id``, propagated via a
``contextvars.ContextVar``. Async tasks and threads inherit the
current value automatically — no manual plumbing through call sites.

Design contract
---------------

* The default value is ``None`` — meaning *no request context*. The
  M14.0a ``JsonFormatter`` (extended in M14.3a) omits the
  ``request_id`` key entirely in that case, so log lines emitted from
  scripts / CLI tools / unit tests look IDENTICAL to pre-M14.3a output.
* ``set_request_id`` returns a token that must be passed to
  ``reset_request_id`` to restore the prior value. Most callers
  should prefer the ``request_id_scope`` context manager, which
  handles the reset automatically (including on exception).
* ``new_request_id`` returns a 12-character hex slice of ``uuid4().hex``
  — short enough to be readable in Render's log viewer and long
  enough to avoid collisions in any realistic operator scenario
  (2**48 ≈ 2.8e14 possibilities).

M14.3b — worker context propagation
-----------------------------------

* ``capture_context()`` returns the *current* contextvars context
  (request_id and anything else set on the context). Use this at the
  point where a job is queued / a thread is spawned.
* ``run_in_captured_context(ctx, func, *args, **kwargs)`` runs the
  callable inside the captured context, so its log calls see the
  same request_id that was set when the context was captured.

These two helpers exist because ``concurrent.futures.ThreadPoolExecutor``
does NOT propagate contextvars to its workers — submitting a task with
``executor.submit(func, ...)`` runs ``func`` in the default empty
context. By contrast, ``asyncio.create_task`` and ``asyncio.to_thread``
*do* propagate context automatically in Python 3.9+, so the api_server
hot path already inherits request_id correctly. The explicit helpers
here exist for future callers (scheduler.py, batch tools, LLM judge
worker pool in M13.1b) that may use raw executors or threading.Thread.

Safety
------

* No external dependency.
* No I/O.
* ``request_id_scope`` resets the ContextVar in a ``finally`` block,
  so exceptions inside the ``with`` block do not leak the request
  ID to subsequent unrelated work.
* ``run_in_captured_context`` re-raises the callable's exception
  faithfully — it never swallows errors. The captured context is
  also not mutated: any ``set_request_id`` inside the callable only
  affects the *local copy* of the context the runtime gives ``ctx.run``,
  never the caller's context.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from contextvars import Context, ContextVar, Token
from typing import Any, Callable, Optional


_REQUEST_ID: ContextVar[Optional[str]] = ContextVar(
    "request_id", default=None,
)


def get_request_id() -> Optional[str]:
    """Return the current request ID, or ``None`` if not set."""
    return _REQUEST_ID.get()


def set_request_id(request_id: Optional[str]) -> Token:
    """Set the request ID for the current context.

    Returns a :class:`Token` that can be passed to
    :func:`reset_request_id` to restore the previous value. Most
    callers should prefer :func:`request_id_scope`.
    """
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token) -> None:
    """Restore the request ID to its previous value using a token
    from :func:`set_request_id`."""
    _REQUEST_ID.reset(token)


def clear_request_id() -> None:
    """Set the request ID for the current context to ``None``.

    Note: this does NOT pop the ContextVar's stack like
    :func:`reset_request_id` does — it just writes ``None`` over the
    current value. Useful for scripts that want to explicitly mark
    "no request" without holding a reset token.
    """
    _REQUEST_ID.set(None)


def new_request_id() -> str:
    """Generate a fresh request ID: 12 hex characters from a UUID4.

    The 12-char width is a compromise between readability in Render's
    log viewer and collision resistance — 48 bits of entropy is more
    than enough for any realistic operator workload.
    """
    return uuid.uuid4().hex[:12]


@contextmanager
def request_id_scope(request_id: Optional[str] = None):
    """Set ``request_id`` for the duration of the ``with``-block, then
    restore. If ``request_id`` is ``None``, a fresh one is generated
    via :func:`new_request_id`.

    Usage::

        from request_context import request_id_scope

        with request_id_scope() as rid:
            log.info("doing work")
            # The JSON output for this log line includes
            # "request_id": rid.

        # On exit, the previous request ID (or None) is restored,
        # even if the block raised an exception.
    """
    rid = request_id if request_id is not None else new_request_id()
    token = _REQUEST_ID.set(rid)
    try:
        yield rid
    finally:
        _REQUEST_ID.reset(token)


# ---------------------------------------------------------------------------
# M14.3b — worker context propagation helpers
# ---------------------------------------------------------------------------


def capture_context() -> Context:
    """Capture the current contextvars context.

    Use this at the point where work is being handed off to another
    execution context (a thread pool worker, a background task created
    outside the asyncio loop, a manual ``threading.Thread`` target).

    The returned :class:`contextvars.Context` is a *snapshot*: later
    mutations to the calling context's ContextVars do not affect what
    a subsequent ``ctx.run(func, ...)`` sees. Conversely, mutations
    inside ``ctx.run`` only modify the snapshot, never the caller's
    live context.
    """
    return contextvars.copy_context()


def run_in_captured_context(
    ctx: Context,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run ``func(*args, **kwargs)`` inside ``ctx``.

    Every ``get_request_id`` call (and every log emission that
    consults it via :class:`structured_logging.JsonFormatter`) made by
    ``func`` (and anything ``func`` transitively calls on the same
    thread / coroutine) will observe the ``request_id`` that was set
    at the time ``ctx`` was captured.

    Exceptions raised by ``func`` propagate out faithfully; the
    captured context is not mutated.
    """
    return ctx.run(func, *args, **kwargs)
