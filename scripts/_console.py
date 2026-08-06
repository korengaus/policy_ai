"""CONSOLE — the one print helper for scripts/, and nothing else.

WHY THIS EXISTS. ``p`` was copied into 29 scripts as three different bodies:
11 printed with ``flush=True`` and NO encoding guard, 17 carried the guard but
did not flush, and exactly one had both. The unguarded copies are the defect.
The operator's console is cp949 and every one of these tools prints Korean, so
a single character the console cannot encode — ``✓``, ``—``, an emoji —
raised ``UnicodeEncodeError`` mid-report, killed the script, discarded every
line after it, and exited non-zero, which a wrapper reads as a failed check.
That is the ``notify`` shape: many copies, one carrying the fix.

DEGRADE, NEVER RAISE, NEVER DROP. Three steps, in order:

  1. Print the text exactly as given. This is the only path a healthy console
     ever takes, so migrating a script cannot change what it prints.
  2. If the console cannot encode it, re-render THROUGH THE CONSOLE'S OWN
     encoding with ``backslashreplace``. On cp949 that keeps the Korean —
     cp949 encodes Hangul perfectly well — and escapes only the characters
     that actually failed, so ``정책 — 확인`` degrades to ``정책 — 확인``
     rather than losing the sentence. This is deliberately better than the
     older guard's ``.encode("ascii", ...)``, which escaped the Korean too and
     turned a readable Korean line into a wall of escapes.
  3. If even that fails (an exotic or missing stdout encoding), fall back to
     ASCII with ``backslashreplace``, which cannot fail.

The line is always emitted. Nothing is silently swallowed: an unencodable
character becomes a visible ``\\uXXXX`` escape, so the reader can see both that
something was there and exactly what it was.

FLUSH. Every path flushes. These scripts are long-running and their output is
routinely piped or watched live; without it a crash or a pipe loses whatever
sat in the buffer.

LEAF MODULE, DELIBERATELY. Pure stdlib, no logging, no config, no I/O at
import beyond ``import sys``. It is imported by tools that themselves do a
guarded stdout reconfigure at import; a leaf inherits nothing from its
importers, and this one gives them nothing to inherit. No ``log.*`` call site
here or anywhere it is imported — scripts/ is pin-OUT for 331/16 and this
module does not move it.
"""

from __future__ import annotations

import sys


def p(line: str = "") -> None:
    """Print one line to stdout, flushed, surviving an unencodable character."""
    text = "" if line is None else str(line)
    try:
        print(text, flush=True)
        return
    except UnicodeEncodeError:
        pass
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        print(text.encode(encoding, "backslashreplace").decode(encoding, "replace"),
              flush=True)
        return
    except (UnicodeEncodeError, LookupError):
        pass
    print(text.encode("ascii", "backslashreplace").decode("ascii"), flush=True)
