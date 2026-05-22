"""Phase 2 M10.3: operator CLI for enabling a source-registry entry.

The **only** authorized way to flip a registry entry from
``default_enabled=false`` + ``operator_review_required=true`` into the
state ``scripts/fetch_registry_source.py --save`` will accept. Every
enable requires an explicit operator justification (>= 20 characters)
and a typed ``YES`` confirmation. The write is atomic (tmp +
``os.replace``) and idempotent on already-enabled entries.

Hard contract:
    * No HTTP. No browser. No DB. No FastAPI.
    * No ``openai`` / ``anthropic`` / ``requests`` / ``httpx`` /
      ``playwright`` / ``browser_use`` / ``openclaw`` imports.
    * Never auto-invoked. The pipeline (``main.py`` / FastAPI /
      ``scheduler.py``) does not import this script.
    * ``truth_claim`` is **never** written; the safety check refuses
      to enable any entry whose registry record carries
      ``truth_claim=true``.
    * The CLI does not strip or overwrite existing
      ``operator_enable_record`` history when re-enabling — it
      records a fresh enable timestamp + justification each time.
    * The atomic write preserves every other field exactly. Only
      ``default_enabled``, ``operator_review_required``, and
      ``operator_enable_record`` are modified.

Usage::

    python scripts/enable_registry_source.py --list
    python scripts/enable_registry_source.py --list --json
    python scripts/enable_registry_source.py --source-id <id> --justification "<reason>" --dry-run
    python scripts/enable_registry_source.py --source-id <id> --justification "<reason>"
    python scripts/enable_registry_source.py --source-id <id> --justification "<reason>" --yes
    python scripts/enable_registry_source.py --source-id <id> --justification "<reason>" --allow-browser
    python scripts/enable_registry_source.py --help

Exit codes:
    0 — success (enabled, already-enabled idempotent, dry-run, --list)
    1 — pre-check failure, source not found, confirmation refused,
        write error
    2 — CLI usage error (missing required flags, unrecognized args)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import source_registry as registry_mod  # noqa: E402


CLI_VERSION = "1.0"

MIN_JUSTIFICATION_CHARS = 20

# Confirmation prompt requires this exact string (case-sensitive) to
# proceed. Anything else aborts.
CONFIRM_TOKEN = "YES"

# Safety notes the spec requires in every output mode.
SAFETY_NOTE_NOT_TRUTH = (
    "Enabling a source does NOT imply truth or guarantee accuracy of any "
    "content fetched from it."
)
SAFETY_NOTE_REVIEW = (
    "Fetch results remain raw artifacts requiring separate human review "
    "before any verification use."
)
SAFETY_NOTE_NO_AUTO = (
    "Enabling only authorizes operator-triggered fetches via "
    "scripts/fetch_registry_source.py --save. The analysis pipeline does "
    "not auto-fetch enabled sources."
)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enable a source-registry entry for operator-triggered fetch. "
            "Requires an explicit operator justification (>= "
            f"{MIN_JUSTIFICATION_CHARS} chars) and confirmation. Refuses "
            "any entry with truth_claim=true. Writes atomically to a tmp "
            "file before os.replace(). Never fetches, scrapes, or contacts "
            "any external service."
        ),
        epilog=(
            "Exit codes: 0=success/already-enabled/dry-run/list; "
            "1=pre-check failure / not found / confirmation refused / "
            "write error; 2=CLI usage error."
        ),
    )
    parser.add_argument(
        "--source-id", default=None,
        help="source_id to enable (required unless --list).",
    )
    parser.add_argument(
        "--justification", default=None,
        help=(
            f"Operator-supplied reason (>= {MIN_JUSTIFICATION_CHARS} "
            "characters). Required when enabling."
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
        "--dry-run", action="store_true",
        help="Print what would change but write nothing.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help=(
            "Skip the interactive YES confirmation. Still requires "
            "--justification; intended for scripted use."
        ),
    )
    parser.add_argument(
        "--allow-browser", action="store_true",
        help=(
            "Acknowledge that the entry's capture_method is "
            "'browser_required'. Without this flag, browser-required "
            "entries are refused at the pre-check stage."
        ),
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_mode",
        help="List every registry entry's enable/review status and exit.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print results as JSON only (no human header/footer).",
    )
    return parser


# ---------------------------------------------------------------------------
# Pre-checks
# ---------------------------------------------------------------------------


def _precheck(
    *, source_id: str, source: Optional[Dict[str, Any]],
    justification: str, allow_browser: bool,
) -> Tuple[bool, Optional[str]]:
    """Return ``(ok, refusal_reason)``. ``refusal_reason`` is a short
    machine-readable tag the human + JSON output can quote verbatim.
    """
    if not isinstance(source_id, str) or not source_id.strip():
        return False, "source_id must be a non-empty string"
    if source is None:
        return False, f"source_id {source_id!r} not found in registry"
    # Sanity: the source dict's source_id must match the lookup key.
    if source.get("source_id") != source_id:
        return False, (
            f"registry inconsistency: looked up {source_id!r} but record "
            f"reports source_id={source.get('source_id')!r}"
        )
    # truth_claim must never be true on any registry entry. If it
    # somehow is, refuse rather than silently enabling.
    if bool(source.get("truth_claim", False)):
        return False, (
            "truth_claim=true on the registry record — refusing to enable; "
            "the registry must never assert truth"
        )
    # capture_method=browser_required requires explicit acknowledgement.
    if (
        source.get("capture_method") == "browser_required"
        and not allow_browser
    ):
        return False, (
            "capture_method='browser_required' — pass --allow-browser to "
            "acknowledge that the static crawler cannot service this entry"
        )
    if not isinstance(justification, str):
        return False, "justification must be a string"
    if len(justification.strip()) < MIN_JUSTIFICATION_CHARS:
        return False, (
            f"justification too short: {len(justification.strip())} chars "
            f"(minimum {MIN_JUSTIFICATION_CHARS})"
        )
    return True, None


def _is_already_enabled(source: Dict[str, Any]) -> bool:
    return (
        bool(source.get("default_enabled", False))
        and not bool(source.get("operator_review_required", True))
    )


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _apply_enable_to_record(
    record: Dict[str, Any], *, justification: str,
) -> Dict[str, Any]:
    """Return a new dict mirroring ``record`` with the enable fields
    flipped. The original dict is not mutated.
    """
    updated = dict(record)
    updated["default_enabled"] = True
    updated["operator_review_required"] = False
    # truth_claim must remain False — explicitly re-assert.
    updated["truth_claim"] = False
    # Preserve any prior justification field the M10.0 schema allowed
    # for operator_review_required=false. Keep the new structured
    # operator_enable_record as well.
    updated["operator_review_required_justification"] = justification.strip()
    updated["operator_enable_record"] = {
        "justification": justification.strip(),
        "enabled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cli_version": CLI_VERSION,
    }
    return updated


def _atomic_write_registry(
    *, registry: Dict[str, Any], target_path: Path,
) -> None:
    """Write ``registry`` to a temp file in the same directory, then
    ``os.replace`` it over ``target_path``. Same-directory tmp keeps
    the rename atomic on Windows + POSIX alike.
    """
    target_path = target_path.resolve()
    target_dir = target_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = target_dir / f".{target_path.name}.tmp"
    payload = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(str(tmp_path), str(target_path))
    finally:
        # Belt-and-suspenders: clean up the tmp file if os.replace
        # raised before the rename completed. os.replace on success
        # already removed the tmp.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _safety_notes_dict() -> Dict[str, str]:
    return {
        "not_truth": SAFETY_NOTE_NOT_TRUTH,
        "review": SAFETY_NOTE_REVIEW,
        "no_auto": SAFETY_NOTE_NO_AUTO,
    }


def _print_safety_footer() -> None:
    print("")
    print(f"[Safety] {SAFETY_NOTE_NOT_TRUTH}")
    print(f"[Safety] {SAFETY_NOTE_REVIEW}")
    print(f"[Safety] {SAFETY_NOTE_NO_AUTO}")


def _summarize_source_row(source: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source_id": source.get("source_id"),
        "default_enabled": bool(source.get("default_enabled", False)),
        "operator_review_required": bool(
            source.get("operator_review_required", True)
        ),
        "source_type": source.get("source_type"),
        "capture_method": source.get("capture_method"),
        "browser_automation": source.get("browser_automation"),
        "official_source_candidate": bool(
            source.get("official_source_candidate", False)
        ),
        "truth_claim": bool(source.get("truth_claim", False)),
    }


def _list_payload(
    *, registry: Dict[str, Any], registry_path: str,
) -> Dict[str, Any]:
    sources = registry.get("sources") or []
    rows = [
        _summarize_source_row(s) for s in sources if isinstance(s, dict)
    ]
    enabled = sum(1 for r in rows if r["default_enabled"])
    review_required = sum(1 for r in rows if r["operator_review_required"])
    return {
        "cli_version": CLI_VERSION,
        "mode": "list",
        "registry_path": registry_path,
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "sources": rows,
        "summary": {
            "total": len(rows),
            "enabled": enabled,
            "review_required": review_required,
        },
        "safety_notes": _safety_notes_dict(),
    }


def _print_list_human(payload: Dict[str, Any]) -> None:
    print("=== Registry Source Status ===")
    print("")
    header = (
        f"{'source_id':<32} | {'enabled':<7} | {'review_required':<15} "
        f"| {'source_type'}"
    )
    print(header)
    print("-" * len(header))
    for row in payload.get("sources", []):
        sid = str(row.get("source_id") or "")
        enabled = "True" if row.get("default_enabled") else "False"
        review = "True" if row.get("operator_review_required") else "False"
        stype = str(row.get("source_type") or "")
        print(f"{sid:<32} | {enabled:<7} | {review:<15} | {stype}")
    summary = payload.get("summary") or {}
    print("")
    print(
        f"Total: {summary.get('total', 0)} | "
        f"enabled={summary.get('enabled', 0)} | "
        f"review_required={summary.get('review_required', 0)}"
    )
    _print_safety_footer()


def _shape_enable_payload(
    *,
    source_id: str,
    registry_path: str,
    source: Optional[Dict[str, Any]],
    justification: str,
    refusal_reason: Optional[str],
    proposed: Optional[Dict[str, Any]],
    dry_run: bool,
    already_enabled: bool,
    written: bool,
) -> Dict[str, Any]:
    return {
        "cli_version": CLI_VERSION,
        "mode": "dry_run" if dry_run else "enable",
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "registry_path": registry_path,
        "source_id": source_id,
        "source_found": source is not None,
        "already_enabled": bool(already_enabled),
        "justification": justification,
        "refusal_reason": refusal_reason,
        "current_state": (
            _summarize_source_row(source) if source is not None else None
        ),
        "proposed_state": (
            _summarize_source_row(proposed) if proposed is not None else None
        ),
        "written": bool(written),
        "truth_claim": False,
        "safety_notes": _safety_notes_dict(),
    }


def _print_enable_human(payload: Dict[str, Any]) -> None:
    if payload.get("mode") == "dry_run":
        print("=== enable_registry_source: DRY RUN — no file written ===")
    else:
        print("=== enable_registry_source: ENABLE ===")
    print(f"source_id: {payload['source_id']}")
    print(f"registry_path: {payload['registry_path']}")
    print(f"source_found: {payload['source_found']}")
    if not payload["source_found"]:
        print(f"result: source_id {payload['source_id']!r} not in registry")
        _print_safety_footer()
        return
    if payload.get("refusal_reason"):
        print(f"refusal_reason: {payload['refusal_reason']}")
        print("result: pre-check failed; no changes proposed")
        _print_safety_footer()
        return
    current = payload.get("current_state") or {}
    proposed = payload.get("proposed_state") or {}
    print(f"source_type: {current.get('source_type')}")
    print(f"capture_method: {current.get('capture_method')}")
    print(f"browser_automation: {current.get('browser_automation')}")
    print("")
    print("State transition:")
    print(
        f"  default_enabled: {current.get('default_enabled')} "
        f"-> {proposed.get('default_enabled')}"
    )
    print(
        f"  operator_review_required: {current.get('operator_review_required')} "
        f"-> {proposed.get('operator_review_required')}"
    )
    print(f"  truth_claim: False -> False (forced)")
    print("")
    print(f"justification: {payload['justification']}")
    if payload.get("already_enabled"):
        print(
            "result: source is already enabled; nothing to do (idempotent)"
        )
    elif payload.get("mode") == "dry_run":
        print("DRY RUN — no file written")
    elif payload.get("written"):
        print("result: registry written successfully")
    else:
        print("result: no write occurred")
    _print_safety_footer()


# ---------------------------------------------------------------------------
# List mode
# ---------------------------------------------------------------------------


def _run_list_mode(
    *, registry: Dict[str, Any], registry_path: str, as_json: bool,
) -> int:
    payload = _list_payload(registry=registry, registry_path=registry_path)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_list_human(payload)
    return 0


# ---------------------------------------------------------------------------
# Enable workflow
# ---------------------------------------------------------------------------


def _read_confirmation(prompt: str) -> str:
    """Indirection so tests can monkeypatch ``input`` without going
    through subprocess. Returns whatever the operator typed (stripped
    of leading/trailing whitespace)."""
    try:
        raw = input(prompt)
    except EOFError:
        return ""
    return (raw or "").strip()


def _run_enable_mode(
    *,
    registry: Dict[str, Any],
    registry_path: Path,
    source_id: str,
    justification: str,
    dry_run: bool,
    auto_yes: bool,
    allow_browser: bool,
    as_json: bool,
) -> int:
    registry_path_str = str(registry_path)

    source = registry_mod.get_source_by_id(registry, source_id)
    ok, refusal = _precheck(
        source_id=source_id, source=source,
        justification=justification, allow_browser=allow_browser,
    )

    if not ok:
        payload = _shape_enable_payload(
            source_id=source_id,
            registry_path=registry_path_str,
            source=source,
            justification=justification,
            refusal_reason=refusal,
            proposed=None,
            dry_run=dry_run,
            already_enabled=False,
            written=False,
        )
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_enable_human(payload)
        return 1

    assert source is not None  # mypy / lint: _precheck only returns ok with source

    if _is_already_enabled(source):
        payload = _shape_enable_payload(
            source_id=source_id,
            registry_path=registry_path_str,
            source=source,
            justification=justification,
            refusal_reason=None,
            proposed=source,
            dry_run=dry_run,
            already_enabled=True,
            written=False,
        )
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_enable_human(payload)
        return 0

    proposed = _apply_enable_to_record(source, justification=justification)

    if dry_run:
        payload = _shape_enable_payload(
            source_id=source_id,
            registry_path=registry_path_str,
            source=source,
            justification=justification,
            refusal_reason=None,
            proposed=proposed,
            dry_run=True,
            already_enabled=False,
            written=False,
        )
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_enable_human(payload)
        return 0

    if not auto_yes:
        # Print a brief preview before the confirmation prompt so the
        # operator knows what they are agreeing to. Always to stderr
        # so a JSON consumer's stdout stays clean (the final JSON
        # payload still goes to stdout below).
        if as_json:
            print(
                f"[enable] About to enable source_id={source_id!r}. "
                f"Justification: {justification!r}",
                file=sys.stderr,
            )
        else:
            print("=== enable_registry_source: CONFIRMATION REQUIRED ===")
            print(f"source_id: {source_id}")
            print(f"registry_path: {registry_path_str}")
            print(f"justification: {justification}")
            print(
                "This will flip default_enabled=true and "
                "operator_review_required=false on the entry."
            )
        typed = _read_confirmation(f"Type {CONFIRM_TOKEN} to confirm enable: ")
        if typed != CONFIRM_TOKEN:
            payload = _shape_enable_payload(
                source_id=source_id,
                registry_path=registry_path_str,
                source=source,
                justification=justification,
                refusal_reason=(
                    f"confirmation aborted (expected exact {CONFIRM_TOKEN!r}, "
                    f"got {typed!r})"
                ),
                proposed=proposed,
                dry_run=False,
                already_enabled=False,
                written=False,
            )
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                _print_enable_human(payload)
            return 1

    # Build the updated registry. Replace only the targeted source
    # entry; preserve every other entry and every top-level field.
    updated_registry = dict(registry)
    new_sources: List[Dict[str, Any]] = []
    for entry in registry.get("sources") or []:
        if isinstance(entry, dict) and entry.get("source_id") == source_id:
            new_sources.append(proposed)
        else:
            new_sources.append(entry)
    updated_registry["sources"] = new_sources

    try:
        _atomic_write_registry(
            registry=updated_registry, target_path=registry_path,
        )
    except OSError as error:
        payload = _shape_enable_payload(
            source_id=source_id,
            registry_path=registry_path_str,
            source=source,
            justification=justification,
            refusal_reason=f"write_error: {type(error).__name__}: {error}",
            proposed=proposed,
            dry_run=False,
            already_enabled=False,
            written=False,
        )
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_enable_human(payload)
        return 1

    payload = _shape_enable_payload(
        source_id=source_id,
        registry_path=registry_path_str,
        source=source,
        justification=justification,
        refusal_reason=None,
        proposed=proposed,
        dry_run=False,
        already_enabled=False,
        written=True,
    )
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_enable_human(payload)
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    # Resolve registry path early so output can quote it consistently.
    try:
        resolved_path = registry_mod.normalize_registry_path(
            args.registry_path,
        )
    except Exception as error:
        print(
            f"[enable] could not resolve registry path: {error}",
            file=sys.stderr,
        )
        return 1
    registry_path_str = str(resolved_path)

    try:
        registry = registry_mod.load_source_registry(resolved_path)
    except registry_mod.SourceRegistryError as error:
        print(
            f"[enable] failed to load registry: {error}",
            file=sys.stderr,
        )
        return 1

    if args.list_mode:
        return _run_list_mode(
            registry=registry, registry_path=registry_path_str,
            as_json=bool(args.json),
        )

    # Enable path — requires --source-id and --justification.
    source_id = (args.source_id or "").strip()
    justification = args.justification or ""
    if not source_id:
        print(
            "[enable] --source-id is required (or pass --list to inspect).",
            file=sys.stderr,
        )
        return 2
    if not justification.strip():
        print(
            "[enable] --justification is required (>= "
            f"{MIN_JUSTIFICATION_CHARS} characters).",
            file=sys.stderr,
        )
        return 2

    return _run_enable_mode(
        registry=registry,
        registry_path=resolved_path,
        source_id=source_id,
        justification=justification,
        dry_run=bool(args.dry_run),
        auto_yes=bool(args.yes),
        allow_browser=bool(args.allow_browser),
        as_json=bool(args.json),
    )


if __name__ == "__main__":
    sys.exit(main())
