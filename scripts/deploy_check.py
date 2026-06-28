"""DEPLOY-CHECK Phase 1 — READ-ONLY: is DISPLAY-ALIGN actually live + correct?

We've been ASSUMING the DISPLAY-ALIGN fix deployed (push + CI green) without
confirming the RUNNING server has the new code. This probe proves it two ways,
with no screen, no new analysis, no matching variance:

  PART 1 — the deployed source actually contains the edit:
    * inspect.getsource(summarize_source_reliability) and check the DISPLAY-ALIGN
      markers are present: "box_driving_matches", "extract_primary_document_match",
      "PRIMARY_DOCUMENT_STRONG_CLASSIFICATION".
    * print `git rev-parse --short HEAD` on the Worker (compare to 94ddb7b).

  PART 2 — exercise the selection on a STORED row (no re-analysis):
    * SELECT id=500's stored source_candidates.
    * call summarize_source_reliability(source_candidates) directly — a pure
      in-memory recompute of ONLY the display selection over the SAME candidates
      (no internet, deterministic).
    * print the NEWLY-computed top_official_institution / top_official_detail_title
      beside the OLD stored slim value, so the difference the fix makes is visible.
      Expected if live+correct: 금융위원회 + the real 청년미래적금 release, NOT
      "Korea Policy Briefing" / "상단주요뉴스".

STRICTLY SELECT / READ-ONLY for the DB. Calling summarize_source_reliability is a
pure in-memory recompute over already-stored candidates (no writes, no internet, no
re-analysis). Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell:
    git log --oneline -1
    PYTHONPATH=. python scripts/deploy_check.py
    PYTHONPATH=. python scripts/deploy_check.py --id 500
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

DISPLAY_ALIGN_MARKERS = (
    "box_driving_matches",
    "extract_primary_document_match",
    "PRIMARY_DOCUMENT_STRONG_CLASSIFICATION",
)
EXPECTED_COMMIT = "94ddb7b"

# A specific Korean government issuer usually ends in one of these morphemes.
_MINISTRY_TAIL = re.compile(r"(부|처|청|위원회|원|실|공사|공단|은행|진흥원|관리원|연구원)$")
GENERIC_PLATFORM = {"korea policy briefing", "정책브리핑", "대한민국 정책브리핑", "korea.kr", ""}
_HANGUL = re.compile(r"[가-힣]")


def _get_engine():
    import sqlalchemy as sa

    raw = os.environ.get("DATABASE_URL")
    if raw:
        url = raw.replace("postgresql+psycopg://", "postgresql://")
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        try:
            engine = sa.create_engine(url)
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001 — never leak the URL
            print(f"NOTE: direct DATABASE_URL engine unavailable ({type(exc).__name__}); "
                  "falling back to postgres_storage.get_engine().", file=sys.stderr)
    try:
        import postgres_storage
        return postgres_storage.get_engine()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no engine available ({type(exc).__name__}).", file=sys.stderr)
        return None


def _parse_json(value):
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _trunc(v, n=100) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _is_specific_ministry(inst) -> bool:
    raw = str(inst or "").strip()
    if not raw or raw.lower() in GENERIC_PLATFORM:
        return False
    return bool(_HANGUL.search(raw) and _MINISTRY_TAIL.search(raw))


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_PROJECT_ROOT), capture_output=True, text=True, timeout=10,
        )
        return (out.stdout or out.stderr or "").strip() or "(unknown)"
    except Exception as exc:  # noqa: BLE001
        return f"(git unavailable: {type(exc).__name__})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="deploy_check")
    parser.add_argument("--id", type=int, default=500, help="stored row id to recompute")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    # ---- PART 1: is the DISPLAY-ALIGN edit in the running code? ----
    print("=" * 100)
    print("PART 1 — deployed-code check")
    print("=" * 100)
    import source_reliability_agent

    try:
        src = inspect.getsource(source_reliability_agent.summarize_source_reliability)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not read summarize_source_reliability source ({type(exc).__name__}).")
        return 1

    marker_results = {m: (m in src) for m in DISPLAY_ALIGN_MARKERS}
    for m, ok in marker_results.items():
        print(f"  marker {'PASS' if ok else 'FAIL'} : {m}")
    all_markers = all(marker_results.values())
    # Confirm the import/constant is actually resolvable in the running module too.
    has_import = hasattr(source_reliability_agent, "extract_primary_document_match") \
        and hasattr(source_reliability_agent, "PRIMARY_DOCUMENT_STRONG_CLASSIFICATION")
    print(f"  module-level import resolvable : {'PASS' if has_import else 'FAIL'} "
          "(extract_primary_document_match + PRIMARY_DOCUMENT_STRONG_CLASSIFICATION)")

    head = _git_head()
    print(f"\n  server git HEAD : {head}   (expected: {EXPECTED_COMMIT})")
    commit_match = head.startswith(EXPECTED_COMMIT) or EXPECTED_COMMIT.startswith(head.split()[0]) \
        if head and "(" not in head else False
    print(f"  commit matches expected : {'PASS' if commit_match else 'note — compare manually'}")

    # ---- PART 2: exercise the selection on stored candidates ----
    print("\n" + "=" * 100)
    print(f"PART 2 — live recompute over stored candidates (id={args.id}, no re-analysis)")
    print("=" * 100)

    import sqlalchemy as sa
    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT id, title, source_reliability_summary, source_candidates "
            "FROM analysis_results WHERE id = :rid"
        ), {"rid": args.id}).mappings().first()

    if row is None:
        print(f"NO ROW with id={args.id}")
        return 0

    stored_summary = _parse_json(row.get("source_reliability_summary"))
    stored_summary = stored_summary if isinstance(stored_summary, dict) else {}
    cands = _parse_json(row.get("source_candidates"))
    cands = cands if isinstance(cands, list) else []

    old_inst = stored_summary.get("top_official_institution")
    old_doc = stored_summary.get("top_official_detail_title")

    # Pure in-memory recompute of the display selection (no writes, no network).
    recomputed = source_reliability_agent.summarize_source_reliability(cands)
    new_inst = recomputed.get("top_official_institution")
    new_doc = recomputed.get("top_official_detail_title")

    print(f"  stored candidates count : {len(cands)}")
    print(f"\n  STORED SLIM (old code at analysis time):")
    print(f"     institution : {_trunc(old_inst, 40)!r}")
    print(f"     doc title   : {_trunc(old_doc, 50)!r}")
    print(f"\n  FUNCTION NOW PICKS (recomputed with running code):")
    print(f"     institution : {_trunc(new_inst, 40)!r}")
    print(f"     doc title   : {_trunc(new_doc, 50)!r}")

    new_is_specific = _is_specific_ministry(new_inst)
    changed = (str(old_inst or "") != str(new_inst or "")) or (str(old_doc or "") != str(new_doc or ""))
    print(f"\n  recomputed institution is a SPECIFIC ministry : {new_is_specific}")
    print(f"  recomputed value DIFFERS from stored slim      : {changed}")

    # ---- Final verdict ----
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"  DEPLOYED (3 markers present)   : {'yes' if all_markers and has_import else 'NO'}")
    print(f"  server commit                  : {head}  (expected {EXPECTED_COMMIT})")
    print(f"  FUNCTION NOW PICKS             : {_trunc(new_inst, 30)} / {_trunc(new_doc, 40)}")
    print(f"  STORED SLIM (old)              : {_trunc(old_inst, 30)} / {_trunc(old_doc, 40)}")
    if all_markers and has_import and new_is_specific:
        verdict = "FIX-LIVE-AND-CORRECT"
    elif not (all_markers and has_import):
        verdict = "NOT-DEPLOYED (markers missing — running an old build)"
    elif not new_is_specific:
        verdict = ("MARKERS-PRESENT-BUT-PICK-NOT-SPECIFIC "
                   f"(recomputed institution={_trunc(new_inst, 30)!r} — inspect candidates; "
                   "the row may genuinely lack a strong/primary ministry candidate)")
    else:
        verdict = "UNCLEAR"
    print(f"  VERDICT                        : {verdict}")
    print("\nDONE (read-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
