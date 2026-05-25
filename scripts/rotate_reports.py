"""M12.2 — Reports directory rotation script.

Moves stale ``reports/*.json`` artifacts into ``reports/archive/YYYY-MM/``
where ``YYYY-MM`` derives from each file's mtime (UTC). Never deletes by
default; ``--delete-after-days N`` opt-in permanently removes files
already inside ``reports/archive/`` that are older than N days.

Design notes (see ``docs/REPORTS_ROTATION.md`` for the operator runbook):

* Scans top-level files only — subdirectories (including the archive
  itself) are skipped.
* Excludes ``operational_check_*.json`` by default; ``--exclude-pattern``
  can be repeated to add more glob excludes.
* Idempotent: re-running on the same state is a no-op. Destination
  collisions are skipped and counted.
* Per-file errors are isolated — one bad file does not halt the run.
  Exit code 1 if any error occurred, 0 otherwise.
* ``--dry-run`` previews the would-be actions without touching the
  filesystem.
* No SQLite or Postgres queries — ``analysis_results`` does not
  reference report filenames (verified during M12.2 Phase 1 §4).
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_REPORTS_DIR = "reports"
DEFAULT_ARCHIVE_SUBDIR = "archive"
DEFAULT_DAYS = 30
DEFAULT_EXCLUDES = ("operational_check_*.json",)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Move stale reports/*.json files into "
            "reports/archive/YYYY-MM/. Idempotent; per-file errors do "
            "not halt the run."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"Move files older than N days (default: {DEFAULT_DAYS}).",
    )
    parser.add_argument(
        "--reports-dir", default=DEFAULT_REPORTS_DIR,
        help=f"Reports directory (default: {DEFAULT_REPORTS_DIR}).",
    )
    parser.add_argument(
        "--archive-subdir", default=DEFAULT_ARCHIVE_SUBDIR,
        help=f"Archive subdirectory under reports-dir (default: {DEFAULT_ARCHIVE_SUBDIR}).",
    )
    parser.add_argument(
        "--exclude-pattern", action="append", default=None,
        help=(
            "Glob pattern to exclude (repeatable). Defaults to "
            "operational_check_*.json. Pass at least one to override."
        ),
    )
    parser.add_argument(
        "--compress", action="store_true",
        help="Gzip moved files (adds .gz suffix).",
    )
    parser.add_argument(
        "--delete-after-days", type=int, default=None,
        help=(
            "Permanently delete files inside reports/archive/ older "
            "than N days. Disabled by default."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview actions without touching the filesystem.",
    )
    parser.add_argument(
        "--json-log", action="store_true",
        help="Emit one JSON line per action (stdout).",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-file logs (summary still printed).",
    )
    return parser


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit(record: dict, *, json_log: bool, quiet: bool) -> None:
    if quiet:
        return
    if json_log:
        try:
            print(json.dumps(record, ensure_ascii=False))
        except (TypeError, ValueError):
            print(record)
    else:
        action = record.get("action", "?")
        src = record.get("src", "")
        dst = record.get("dst", "")
        extra = ""
        if action == "summary":
            extra = json.dumps(
                {k: v for k, v in record.items() if k not in ("action", "ts")},
                ensure_ascii=False,
            )
            print(f"[rotate_reports] summary {extra}")
            return
        if dst:
            print(f"[rotate_reports] {action}: {src} -> {dst}")
        else:
            print(f"[rotate_reports] {action}: {src}")


def _is_excluded(filename: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(filename, pattern) for pattern in patterns)


def _archive_subdir_for(mtime: float, archive_root: Path) -> Path:
    bucket = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m")
    return archive_root / bucket


def _iter_top_level_json(reports_dir: Path) -> Iterable[Path]:
    for child in sorted(reports_dir.iterdir()):
        if child.is_file() and child.suffix == ".json":
            yield child


def _move_one(
    src: Path,
    archive_root: Path,
    *,
    compress: bool,
    dry_run: bool,
) -> tuple[str, Path, int]:
    """Return ``(status, destination, bytes_moved)``.

    Status is one of ``moved``, ``already_archived``, ``would_move``.
    """
    stat = src.stat()
    dest_dir = _archive_subdir_for(stat.st_mtime, archive_root)
    suffix = ".gz" if compress else ""
    dest = dest_dir / (src.name + suffix)

    if dest.exists():
        return ("already_archived", dest, 0)

    if dry_run:
        return ("would_move", dest, stat.st_size)

    dest_dir.mkdir(parents=True, exist_ok=True)

    if compress:
        with open(src, "rb") as src_file, gzip.open(dest, "wb", compresslevel=6) as dst_file:
            shutil.copyfileobj(src_file, dst_file)
        src.unlink()
    else:
        shutil.move(str(src), str(dest))

    return ("moved", dest, stat.st_size)


def _delete_old_archive(
    archive_root: Path,
    *,
    days: int,
    dry_run: bool,
    json_log: bool,
    quiet: bool,
) -> tuple[int, int, int]:
    """Permanently delete archive files older than ``days``.

    Returns ``(deleted, would_delete, errors)``.
    """
    if not archive_root.exists():
        return (0, 0, 0)
    cutoff = time.time() - (days * 86400)
    deleted = 0
    would = 0
    errors = 0
    for path in sorted(archive_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            errors += 1
            continue
        if dry_run:
            would += 1
            _emit(
                {"ts": _now_iso(), "action": "would_delete", "src": str(path)},
                json_log=json_log, quiet=quiet,
            )
            continue
        try:
            path.unlink()
            deleted += 1
            _emit(
                {"ts": _now_iso(), "action": "delete", "src": str(path)},
                json_log=json_log, quiet=quiet,
            )
        except OSError as exc:
            errors += 1
            _emit(
                {
                    "ts": _now_iso(),
                    "action": "delete_error",
                    "src": str(path),
                    "error": str(exc)[:500],
                },
                json_log=json_log, quiet=quiet,
            )
    return (deleted, would, errors)


def run(args: argparse.Namespace) -> int:
    reports_dir = Path(args.reports_dir).resolve()
    archive_root = (reports_dir / args.archive_subdir).resolve()
    excludes = tuple(args.exclude_pattern) if args.exclude_pattern else DEFAULT_EXCLUDES

    if not reports_dir.exists() or not reports_dir.is_dir():
        _emit(
            {
                "ts": _now_iso(),
                "action": "summary",
                "scanned": 0,
                "excluded": 0,
                "eligible": 0,
                "moved": 0,
                "would_move": 0,
                "already_archived": 0,
                "errors": 1,
                "bytes_moved": 0,
                "dry_run": args.dry_run,
                "note": f"reports_dir not found: {reports_dir}",
            },
            json_log=args.json_log, quiet=False,
        )
        return 1

    cutoff = time.time() - (args.days * 86400)

    scanned = 0
    excluded = 0
    eligible = 0
    moved = 0
    would_move = 0
    already = 0
    errors = 0
    bytes_moved = 0

    for src in _iter_top_level_json(reports_dir):
        scanned += 1
        if _is_excluded(src.name, excludes):
            excluded += 1
            continue
        try:
            mtime = src.stat().st_mtime
        except OSError as exc:
            errors += 1
            _emit(
                {
                    "ts": _now_iso(),
                    "action": "stat_error",
                    "src": str(src),
                    "error": str(exc)[:500],
                },
                json_log=args.json_log, quiet=args.quiet,
            )
            continue
        if mtime >= cutoff:
            continue
        eligible += 1
        try:
            status, dest, size = _move_one(
                src, archive_root, compress=args.compress, dry_run=args.dry_run,
            )
        except OSError as exc:
            errors += 1
            _emit(
                {
                    "ts": _now_iso(),
                    "action": "move_error",
                    "src": str(src),
                    "error": str(exc)[:500],
                },
                json_log=args.json_log, quiet=args.quiet,
            )
            continue

        if status == "moved":
            moved += 1
            bytes_moved += size
        elif status == "would_move":
            would_move += 1
            bytes_moved += size
        elif status == "already_archived":
            already += 1

        _emit(
            {
                "ts": _now_iso(),
                "action": status,
                "src": str(src),
                "dst": str(dest),
                "bytes": size,
                "compressed": args.compress,
            },
            json_log=args.json_log, quiet=args.quiet,
        )

    deleted = 0
    would_delete = 0
    if args.delete_after_days is not None:
        deleted, would_delete, delete_errors = _delete_old_archive(
            archive_root,
            days=args.delete_after_days,
            dry_run=args.dry_run,
            json_log=args.json_log,
            quiet=args.quiet,
        )
        errors += delete_errors

    summary = {
        "ts": _now_iso(),
        "action": "summary",
        "scanned": scanned,
        "excluded": excluded,
        "eligible": eligible,
        "moved": moved,
        "would_move": would_move,
        "already_archived": already,
        "errors": errors,
        "bytes_moved": bytes_moved,
        "dry_run": args.dry_run,
    }
    if args.delete_after_days is not None:
        summary["deleted"] = deleted
        summary["would_delete"] = would_delete
        summary["delete_after_days"] = args.delete_after_days
    _emit(summary, json_log=args.json_log, quiet=False)

    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
