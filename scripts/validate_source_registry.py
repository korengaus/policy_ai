"""Phase 2 M10.0: source-registry validator CLI.

Offline validator for ``data/source_registry.json`` (or any path
passed via ``--registry-path``). Loads the JSON, runs the
:mod:`source_registry` semantic validator, and prints a human
summary plus a stable JSON summary tail (or pure JSON with
``--json``).

Hard contract:
    * No HTTP, no browser automation, no OpenAI.
    * No subprocess / git calls.
    * Does not modify the registry file.

Exit codes:
    0 — registry is valid
    1 — validation errors detected
    2 — CLI usage error (bad path, bad flag)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import source_registry as registry_mod  # noqa: E402


def _build_summary(
    normalized: Dict[str, Any], errors: List[str], warnings: List[str],
    *, source_path: Path,
) -> Dict[str, Any]:
    sources: List[Dict[str, Any]] = normalized.get("sources") or []
    enabled = sum(1 for s in sources if s.get("default_enabled"))
    disabled = len(sources) - enabled
    source_types: Dict[str, int] = {}
    for s in sources:
        st = s.get("source_type")
        if st:
            source_types[st] = source_types.get(st, 0) + 1
    browser_required = sum(
        1 for s in sources
        if s.get("capture_method") == "browser_required"
        or s.get("browser_automation") == "required"
    )
    return {
        "passed": not errors,
        "schema_version": normalized.get("schema_version"),
        "registry_name": normalized.get("registry_name"),
        "source_path": str(source_path),
        "sources_count": len(sources),
        "enabled_count": enabled,
        "disabled_count": disabled,
        "source_types": source_types,
        "browser_required_count": browser_required,
        "issues": list(errors),
        "warnings": list(warnings),
    }


def _print_human_summary(summary: Dict[str, Any]) -> None:
    print(f"[source-registry] source_path={summary['source_path']}")
    print(
        f"[source-registry] schema_version={summary['schema_version']} "
        f"registry_name={summary['registry_name']!r}"
    )
    print(
        f"[source-registry] sources_count={summary['sources_count']} "
        f"enabled={summary['enabled_count']} disabled={summary['disabled_count']} "
        f"browser_required={summary['browser_required_count']}"
    )
    types = summary.get("source_types") or {}
    if types:
        type_line = ", ".join(f"{k}={v}" for k, v in sorted(types.items()))
        print(f"[source-registry] source_types: {type_line}")
    for w in summary.get("warnings") or []:
        print(f"[source-registry] warn: {w}", file=sys.stderr)
    for issue in summary.get("issues") or []:
        print(f"[source-registry] error: {issue}", file=sys.stderr)
    print(f"[source-registry] passed={summary['passed']}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline validator for the M10.0 source registry "
            "(data/source_registry.json by default). Never fetches, "
            "scrapes, or calls any external service."
        ),
    )
    parser.add_argument(
        "--registry-path", default=None,
        help=(
            "Path to the registry JSON. Defaults to "
            "data/source_registry.json under the repo root."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Suppress the human summary; only print the JSON payload.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        path = registry_mod.normalize_registry_path(args.registry_path)
    except Exception as error:
        print(f"[source-registry] could not resolve path: {error}",
              file=sys.stderr)
        return 2
    if not path.exists():
        print(
            f"[source-registry] registry not found at {path}",
            file=sys.stderr,
        )
        return 2
    try:
        raw = registry_mod.load_source_registry(path)
    except registry_mod.SourceRegistryError as error:
        # Surface as a validation issue rather than a CLI usage error
        # so the operator gets a JSON payload describing the failure.
        normalized = {
            "schema_version": None, "registry_name": None, "sources": [],
        }
        summary = _build_summary(
            normalized, [f"{error.reason}: {error}"], [],
            source_path=path,
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            _print_human_summary(summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1

    normalized, errors, warnings = registry_mod.validate_source_registry(raw)
    summary = _build_summary(normalized, errors, warnings, source_path=path)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human_summary(summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
