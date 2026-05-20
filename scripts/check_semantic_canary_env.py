"""Phase 2 M7.2: semantic canary env readiness checker.

Prints whether the local shell is configured to run a semantic debug
canary against a uvicorn instance — without printing or persisting the
API key value. Render env is **never** modified by this script; the
only thing it does is read what's in ``os.environ`` and report.

Exit codes:
    0 — env is fully ready for a local canary
    1 — env is missing a required variable (printed; key value is never)
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional


REQUIRED_FOR_LOCAL_CANARY = [
    "SEMANTIC_MATCHING_ENABLED",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Print semantic canary env readiness. Reads env only — never "
            "prints the API key value, never writes anything to disk, never "
            "modifies Render."
        ),
    )
    parser.add_argument(
        "--require-openai", action="store_true",
        help="Require EMBEDDING_PROVIDER=openai for a 'ready' verdict.",
    )
    return parser


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)

    semantic_enabled = (os.environ.get("SEMANTIC_MATCHING_ENABLED") or "").strip().lower()
    provider = (os.environ.get("EMBEDDING_PROVIDER") or "").strip()
    model = (os.environ.get("EMBEDDING_MODEL") or "").strip()
    cache_enabled = (os.environ.get("EMBEDDING_CACHE_ENABLED") or "").strip()
    key_value = os.environ.get("OPENAI_API_KEY") or ""
    key_present = bool(key_value)
    key_length = len(key_value)

    print("[canary-env] semantic readiness check")
    print(f"  SEMANTIC_MATCHING_ENABLED: {semantic_enabled or '(unset)'}")
    print(f"  EMBEDDING_PROVIDER: {provider or '(unset)'}")
    print(f"  EMBEDDING_MODEL: {model or '(unset)'}")
    print(f"  EMBEDDING_CACHE_ENABLED: {cache_enabled or '(unset, defaults to enabled)'}")
    print(f"  OPENAI_API_KEY present: {key_present}")
    if key_present:
        # Show length only — never the value. Helps the operator confirm
        # the key wasn't truncated when they pasted it into the shell.
        print(f"  OPENAI_API_KEY length: {key_length}")

    missing: List[str] = []
    for var in REQUIRED_FOR_LOCAL_CANARY:
        if not (os.environ.get(var) or "").strip():
            missing.append(var)

    needs_openai = args.require_openai or provider.lower() == "openai"
    if needs_openai and not key_present:
        missing.append("OPENAI_API_KEY")

    if semantic_enabled not in {"true", "1", "yes", "on"}:
        # Even when the env is configured the kill-switch must be on.
        if "SEMANTIC_MATCHING_ENABLED" not in missing:
            missing.append("SEMANTIC_MATCHING_ENABLED")

    ready = not missing
    print()
    print(f"  ready_for_local_canary: {ready}")
    if missing:
        print(f"  missing: {sorted(set(missing))}")
        print(
            "  hint: set the missing vars in this shell. Do NOT paste the key "
            "into chat or commit it anywhere. Example for PowerShell:"
        )
        print("    $env:SEMANTIC_MATCHING_ENABLED='true'")
        print("    $env:EMBEDDING_PROVIDER='openai'")
        print("    $env:EMBEDDING_MODEL='text-embedding-3-small'")
        print("    $env:OPENAI_API_KEY='<your-key>'")
    print()
    print(
        "  reminder: this script does not modify Render. Render env stays "
        "SEMANTIC_MATCHING_ENABLED=false / EMBEDDING_PROVIDER=disabled until "
        "an operator changes it manually via the Render dashboard."
    )

    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
