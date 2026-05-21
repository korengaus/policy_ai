"""Phase 2 M9.3: local launcher for the reviewer UI demo.

A tiny wrapper that monkey-patches ``database.DB_PATH`` to the demo
SQLite file *before* importing ``api_server``, then starts uvicorn in
the foreground. The project's DB path is a module-level constant (no
env-var indirection), so this launcher is the safe way to point the
FastAPI app at a non-default DB without editing tracked source.

Hard contract:
    * Refuses any ``--db-path`` outside ``reports/`` (defensive — the
      demo helper only writes there).
    * Refuses to launch if the DB file doesn't already exist (the
      operator should run ``prepare_review_ui_local_demo.py`` first).
    * Never modifies Render env.
    * Never calls OpenAI / Render / external network.
    * Reads REVIEW_API_ENABLED / REVIEW_API_TOKEN from the environment
      so the operator's PowerShell ``$env:REVIEW_API_TOKEN=...`` works
      as expected — but never prints either value.

Usage (from the repo root, after running ``prepare_review_ui_local_demo.py``):

    $env:REVIEW_API_ENABLED = "true"
    $env:REVIEW_API_TOKEN = "local-review-demo-token"
    python scripts\\serve_review_ui_local_demo.py \\
        --db-path reports\\review_ui_local_demo.sqlite

Stop with Ctrl+C.

Exit codes:
    0 — uvicorn exited cleanly (Ctrl+C)
    1 — refused (DB missing, unsafe path)
    2 — bad CLI usage
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


REPORTS_DIR_NAME = "reports"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _resolve_db_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    else:
        p = p.resolve()
    return p


def _path_is_under_reports(p: Path) -> bool:
    try:
        p.resolve().relative_to((ROOT / REPORTS_DIR_NAME).resolve())
        return True
    except ValueError:
        return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Start uvicorn against a local demo SQLite database. Used "
            "together with scripts/prepare_review_ui_local_demo.py to "
            "exercise the M9.2 audit-packet UI viewer locally without "
            "modifying policy_ai.db or Render env."
        ),
    )
    parser.add_argument(
        "--db-path", required=True,
        help=(
            "Path to the prepared demo SQLite DB. Must live under "
            "reports/. The launcher refuses any other location."
        ),
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Bind host (default {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Bind port (default {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--reload", action="store_true",
        help="Pass --reload to uvicorn (live reload for development).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    db_path = _resolve_db_path(args.db_path)
    if not _path_is_under_reports(db_path):
        print(
            f"[serve-demo] refusing to use --db-path outside reports/: {db_path}",
            file=sys.stderr,
        )
        return 1
    if not db_path.exists():
        print(
            f"[serve-demo] demo DB not found at {db_path}. "
            f"Run: python scripts/prepare_review_ui_local_demo.py --reset",
            file=sys.stderr,
        )
        return 1

    # Override the module-level DB constant BEFORE any code imports
    # ``api_server`` (api_server itself imports database). Doing this
    # via env var would require touching database.py; instead we keep
    # database.py unchanged and patch its DB_PATH for the launcher's
    # lifetime only.
    import database
    database.DB_PATH = db_path

    # Defensive: never print the token. We deliberately do not even
    # echo its presence; the operator already set it themselves.
    print(f"[serve-demo] starting uvicorn with demo DB: {db_path}")
    print(f"[serve-demo] http://{args.host}:{args.port}/")

    try:
        import uvicorn
    except ImportError as error:
        print(
            f"[serve-demo] uvicorn is required to run the local demo: {error}",
            file=sys.stderr,
        )
        return 1

    try:
        uvicorn.run(
            "api_server:app",
            host=args.host,
            port=args.port,
            reload=bool(args.reload),
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
