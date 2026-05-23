"""Frontend build pipeline (M13.2a).

Reads ``frontend/styles/main.css`` and the template at
``frontend/template.html``, injects the CSS at the ``<!-- CSS_INJECT -->``
marker, and writes the result to ``web/index.html``. Pure Python, no
Node bundler, stdlib-only.

All file I/O is BYTE-ORIENTED (``read_bytes`` / ``write_bytes``). The
M13.2a invariant is that the served HTML must be byte-identical to the
pre-extraction version, and Windows ``open(..., encoding="utf-8")``
defaults to universal newline translation (``\\n`` → ``\\r\\n`` on
write) which would silently violate that guarantee. Operating on bytes
sidesteps that entirely.

Run before deploying any frontend change::

    python frontend/build_index.py             # rewrite served HTML
    python frontend/build_index.py --check     # verify (used by validate.py)
    python frontend/build_index.py --status    # paths + checksums
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


FRONTEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = FRONTEND_DIR.parent
TEMPLATE_PATH = FRONTEND_DIR / "template.html"
CSS_PATH = FRONTEND_DIR / "styles" / "main.css"
CHECKSUM_PATH = FRONTEND_DIR / "dist_checksum.txt"

# Discovered by reading ``api_server.py`` at:
#   @app.get("/")
#   def root():
#       return FileResponse("web/index.html")
SERVED_HTML_PATH = REPO_ROOT / "web" / "index.html"

# Bytes literal — never a Python string. Avoids any UTF-8 encode/decode
# round trip during build.
CSS_MARKER = b"<!-- CSS_INJECT -->"


# ---------------------------------------------------------------------------
# Core build
# ---------------------------------------------------------------------------


def build_html_bytes() -> bytes:
    """Reads template + CSS and returns the assembled HTML bytes.

    Raises ``RuntimeError`` (never silently produces a degraded build)
    when:

    * The template file is missing.
    * The CSS file is missing.
    * The template does not contain the CSS marker.
    * The template contains the marker more than once (M13.2a supports
      exactly one CSS injection point).
    """
    if not TEMPLATE_PATH.exists():
        raise RuntimeError(f"Template missing: {TEMPLATE_PATH}")
    if not CSS_PATH.exists():
        raise RuntimeError(f"CSS missing: {CSS_PATH}")

    template = TEMPLATE_PATH.read_bytes()
    css = CSS_PATH.read_bytes()

    marker_count = template.count(CSS_MARKER)
    if marker_count == 0:
        raise RuntimeError(
            f"Template missing required marker {CSS_MARKER!r}. "
            "Refusing to build."
        )
    if marker_count > 1:
        raise RuntimeError(
            f"Template contains {marker_count} markers; M13.2a "
            "supports exactly one CSS injection point."
        )

    css_block = b"<style>" + css + b"</style>"
    return template.replace(CSS_MARKER, css_block, 1)


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_write() -> int:
    """Default mode — write the assembled HTML to the served path and
    refresh ``dist_checksum.txt``."""
    output = build_html_bytes()
    SERVED_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERVED_HTML_PATH.write_bytes(output)
    checksum = sha256_of_bytes(output)
    CHECKSUM_PATH.write_bytes((checksum + "\n").encode("ascii"))
    print(f"Wrote {SERVED_HTML_PATH} ({len(output)} bytes)")
    print(f"Checksum: {checksum}")
    return 0


def cmd_check() -> int:
    """Verify the served HTML matches what the build would produce.

    Returns exit 0 when byte-identical, exit 1 otherwise. ``--check``
    NEVER writes — it is safe to run repeatedly in CI without side
    effects.
    """
    if not SERVED_HTML_PATH.exists():
        print(f"FAIL: {SERVED_HTML_PATH} does not exist", file=sys.stderr)
        return 1
    served = SERVED_HTML_PATH.read_bytes()
    try:
        expected = build_html_bytes()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if served == expected:
        print(f"OK: {SERVED_HTML_PATH} matches build output exactly")
        return 0

    served_hash = sha256_of_bytes(served)
    expected_hash = sha256_of_bytes(expected)
    print("FAIL: served HTML does not match build output", file=sys.stderr)
    print(f"  served hash:    {served_hash}", file=sys.stderr)
    print(f"  expected hash:  {expected_hash}", file=sys.stderr)
    print(f"  served bytes:   {len(served)}", file=sys.stderr)
    print(f"  expected bytes: {len(expected)}", file=sys.stderr)

    shared = min(len(served), len(expected))
    diff_index = None
    for i in range(shared):
        if served[i] != expected[i]:
            diff_index = i
            break
    if diff_index is None:
        print(
            "  Files differ in length only; one is a prefix of the other.",
            file=sys.stderr,
        )
    else:
        ctx_start = max(0, diff_index - 50)
        ctx_end = diff_index + 50
        print(f"  First diff at byte {diff_index}:", file=sys.stderr)
        print(
            f"  served (...):   {served[ctx_start:ctx_end]!r}",
            file=sys.stderr,
        )
        print(
            f"  expected (...): {expected[ctx_start:ctx_end]!r}",
            file=sys.stderr,
        )
    print(
        "\nTo update: python frontend/build_index.py",
        file=sys.stderr,
    )
    return 1


def cmd_status() -> int:
    """Print paths, byte counts, and checksums for debugging. Never
    writes any file."""
    print(f"Template:      {TEMPLATE_PATH} (exists={TEMPLATE_PATH.exists()})")
    print(f"CSS:           {CSS_PATH} (exists={CSS_PATH.exists()})")
    print(f"Served HTML:   {SERVED_HTML_PATH} (exists={SERVED_HTML_PATH.exists()})")
    print(f"Checksum file: {CHECKSUM_PATH} (exists={CHECKSUM_PATH.exists()})")
    if SERVED_HTML_PATH.exists():
        served = SERVED_HTML_PATH.read_bytes()
        print(f"Served bytes:  {len(served)}")
        print(f"Served hash:   {sha256_of_bytes(served)}")
    if CHECKSUM_PATH.exists():
        stored = CHECKSUM_PATH.read_bytes().decode("ascii", errors="replace").strip()
        print(f"Stored hash:   {stored}")
    return 0


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_index",
        description=(
            "Assemble web/index.html from frontend/template.html + "
            "frontend/styles/main.css. Pure Python, no bundler. "
            "M13.2a invariant: built output is byte-identical to the "
            "pre-extraction served HTML."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 -- success (write or check passed)\n"
            "  1 -- check failed (drift detected) or build error\n"
            "  2 -- CLI usage error\n\n"
            "Safety: --check and --status NEVER write any file. "
            "Only the default (no-flag) invocation rewrites web/index.html."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check", action="store_true",
        help="Verify served HTML matches build output (no writes).",
    )
    group.add_argument(
        "--status", action="store_true",
        help="Print paths and checksum diagnostics (no writes).",
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.check:
        return cmd_check()
    if args.status:
        return cmd_status()
    return cmd_write()


if __name__ == "__main__":
    sys.exit(main())
