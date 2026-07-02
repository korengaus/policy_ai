"""SUMMARY-FLEX-PROBE — READ-ONLY measurement of whether the flex-`<summary>` rule
violation flagged in the Day5 design audit (main.css:3973 `.adv-cand-summary`) is a
REAL break or a benign rule-violation.

MEASUREMENT ONLY. No source edit, no build, no git, no DB. Reads the three frontend
files and reports to the terminal. scripts/ is pin-OUT (no 331/16 or 38-pin impact).

WHY
---
The hard rule (documented in the DESIGN-DETAIL-5f comment, main.css:~3790): a `<summary>`
whose `display` is changed away from the default `list-item` (to flex/grid/contents/…)
can, in some Chromium versions, stop being recognized as the `<details>` disclosure
control — so a CLOSED `<details>` renders at zero height and is not even Ctrl+F-findable.
The 5e regression proved this on the operator's browser; the 5f fix was "do NOT touch
`display` on a summary." `.adv-cand-summary` sets `display:flex` — same pattern. Visual
eyeballing was inconclusive (the live toggles opened), so measure before fixing.

WHAT THIS MEASURES
------------------
  STATIC (authoritative for the rule — it is a display-based rule, not a layout measure):
    Enumerate EVERY `<summary>` (static in template.html + rendered by main.js), resolve
    the CSS rule(s) that target it, compute the effective `display`, and classify
    OK (list-item/block/default) vs VIOLATION (flex/grid/contents/inline-flex/inline-grid).
  RUNTIME:
    (a) Attempt a jsdom check; if jsdom is absent (it is not a repo dependency) report so.
    (b) Regardless, run the STRUCTURAL runtime-precondition check that actually decides
        the 5f break: for each VIOLATION summary, is it the FIRST element child of a
        `<details>` (the disclosure-control position — only there does the display
        override compromise the control), and is that `<details>` rendered CLOSED (no
        `open` attr — the state in which the collapse manifests)?
    jsdom CAVEAT: jsdom does not lay out `<details>`/`<summary>` and returns offsetHeight
    0 for everything, so it CANNOT measure the visual collapse — the CSS `display` value
    + the structural position are the authoritative signals.

SAFETY: read-only; ASCII-guarded prints. Usage:
    PYTHONPATH=. python scripts/summary_flex_probe.py
Exit 0 always (diagnostic).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FRONTEND = _PROJECT_ROOT / "frontend"
_CSS = _FRONTEND / "styles" / "main.css"
_TEMPLATE = _FRONTEND / "template.html"
_MAINJS = _FRONTEND / "scripts" / "main.js"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# display values that, on a first-child <summary>, put it in the 5f break class.
BREAK_DISPLAYS = {"flex", "grid", "contents", "inline-flex", "inline-grid"}
# display values that are safe (the disclosure control keeps working).
OK_DISPLAYS = {"list-item", "block", "", "(default)"}


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        p(f"[error] cannot read {path}: {exc}")
        return ""


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


# ---------------------------------------------------------------------------
# CSS: parse selector -> declarations, keep only rules that carry a `display`.
# We only need rules whose selector's SUBJECT (last simple selector) is `summary`
# or a `.class` (so we can match a class applied to a summary).
# ---------------------------------------------------------------------------
def parse_css_display_rules(css: str) -> list[dict]:
    # strip comments so a `display:` inside a /* ... */ note is never parsed.
    css_nc = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    rules = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css_nc):
        selector = " ".join(match.group(1).split())
        body = match.group(2)
        disp = None
        for decl in body.split(";"):
            if ":" in decl:
                prop, _, value = decl.partition(":")
                if prop.strip().lower() == "display":
                    disp = value.strip().lower()
        if disp is None:
            continue
        # subject = last space-separated token, stripped of combinators.
        subject = selector.split()[-1] if selector.split() else selector
        subject = subject.split(">")[-1].split("+")[-1].split("~")[-1]
        # A `display` on a PSEUDO-ELEMENT (`::marker`, `::after`, …) targets that
        # pseudo, NOT the element — e.g. `summary::-webkit-details-marker{display:none}`
        # hides the triangle, it does NOT set the summary's own display. Skip those so
        # they never masquerade as the element's display.
        if "::" in subject:
            continue
        subject = subject.split(":")[0]  # drop pseudo-CLASSES (:hover) — same subject
        rules.append({"selector": selector, "subject": subject, "display": disp})
    return rules


def display_for_summary(classes: list[str], css_rules: list[dict]) -> tuple[str, str]:
    """Effective display for a <summary> carrying `classes`. Returns (display, source
    selector). Later matching rules win (source order). Default is list-item."""
    display = "(default)"
    source = "(UA default: list-item)"
    for rule in css_rules:  # file order == cascade order for equal specificity
        subj = rule["subject"]
        matched = False
        if subj == "summary":
            matched = True  # bare-summary rule applies to any summary
        elif subj.startswith("."):
            cls = subj[1:]
            if cls in classes:
                matched = True
        if matched:
            display = rule["display"]
            source = rule["selector"]
    return display, source


# ---------------------------------------------------------------------------
# Enumerate <summary> occurrences (static + rendered) + their enclosing <details>.
# ---------------------------------------------------------------------------
def _classes_of(tag_text: str) -> list[str]:
    m = re.search(r'class\s*=\s*"([^"]*)"', tag_text)
    if not m:
        return []
    # drop template-literal interpolations; keep literal class tokens.
    raw = re.sub(r"\$\{[^}]*\}", " ", m.group(1))
    return [c for c in raw.split() if c and "${" not in c]


def find_summaries(text: str, origin: str) -> list[dict]:
    """Every `<summary ...>` in `text`, with its classes, line, and whether it is the
    FIRST element child of its nearest enclosing `<details>` which is rendered CLOSED."""
    out = []
    for m in re.finditer(r"<summary\b[^>]*>", text):
        tag = m.group(0)
        idx = m.start()
        classes = _classes_of(tag)
        # nearest preceding <details ...> open tag.
        details_open = None
        for dm in re.finditer(r"<details\b[^>]*>", text):
            if dm.start() < idx:
                details_open = dm
            else:
                break
        details_tag = details_open.group(0) if details_open else ""
        # first element child? -> only whitespace between the <details ...> and <summary.
        first_child = False
        rendered_closed = None
        if details_open:
            between = text[details_open.end():idx]
            first_child = between.strip() == ""
            rendered_closed = not re.search(r"\bopen\b", details_tag)
        out.append({
            "origin": origin,
            "line": _line_of(text, idx),
            "classes": classes,
            "first_child_of_details": first_child,
            "details_tag": details_tag.strip(),
            "rendered_closed": rendered_closed,
        })
    return out


# ---------------------------------------------------------------------------
# Runtime half — attempt jsdom (absent in this repo), then report.
# ---------------------------------------------------------------------------
def try_jsdom() -> str:
    node = None
    for exe in ("node", "node.exe"):
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                node = exe
                break
        except Exception:  # noqa: BLE001
            continue
    if node is None:
        return "node not found — jsdom runtime check unavailable."
    probe = "try{require.resolve('jsdom');console.log('JSDOM_PRESENT');}catch(e){console.log('JSDOM_ABSENT');}"
    try:
        r = subprocess.run([node, "-e", probe], capture_output=True, text=True,
                           timeout=20, cwd=str(_PROJECT_ROOT))
        out = (r.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"node present but jsdom probe failed ({exc})."
    if "JSDOM_PRESENT" in out:
        # Even present, jsdom cannot lay out <details> — note it and defer to structural.
        return ("jsdom PRESENT, but jsdom does not render <details>/<summary> and returns "
                "offsetHeight 0 for all nodes -> cannot measure the visual collapse; "
                "CSS display + structural position are authoritative.")
    return ("jsdom ABSENT (not a repo dependency; the repo's JS tests use bare vm+regex). "
            "Not installed (would need network + node_modules write). Falling back to the "
            "structural runtime-precondition check below.")


def classify(display: str) -> str:
    if display in BREAK_DISPLAYS:
        return "VIOLATION"
    if display in OK_DISPLAYS:
        return "OK"
    return "REVIEW"


def main() -> int:
    css = _read(_CSS)
    template = _read(_TEMPLATE)
    mainjs = _read(_MAINJS)
    css_rules = parse_css_display_rules(css)

    summaries = (
        find_summaries(template, "template.html")
        + find_summaries(mainjs, "main.js")
    )

    p("=== SUMMARY-FLEX-PROBE (READ-ONLY) ===")
    p(f"summary-targeting CSS display rules found: {len(css_rules)}")
    p("")

    # ---- STATIC TABLE -------------------------------------------------------
    p("=== STATIC TABLE — every <summary>, its class(es), effective display, verdict ===")
    p("origin:line | classes | effective display | source selector | classify")
    rows = []
    for s in summaries:
        display, source = display_for_summary(s["classes"], css_rules)
        verdict = classify(display)
        s["display"] = display
        s["source"] = source
        s["verdict"] = verdict
        rows.append(s)
        cls = ",".join(s["classes"]) or "(none)"
        p(f"{s['origin']}:{s['line']} | {cls} | {display} | {source} | {verdict}")

    violations = [r for r in rows if r["verdict"] == "VIOLATION"]
    reviews = [r for r in rows if r["verdict"] == "REVIEW"]

    p("")
    p(f"total summaries: {len(rows)}  |  VIOLATION: {len(violations)}  |  "
      f"REVIEW: {len(reviews)}  |  OK: {len(rows) - len(violations) - len(reviews)}")

    # ---- RUNTIME HALF -------------------------------------------------------
    p("")
    p("=== RUNTIME — jsdom attempt + structural precondition of the 5f break ===")
    p(f"jsdom: {try_jsdom()}")
    p("")
    if not violations:
        p("No display-VIOLATION summary found — nothing to runtime-check.")
    for s in violations:
        cls = ",".join(s["classes"]) or "(none)"
        # The 5f break manifests ONLY when the flex/grid summary IS the first-child
        # disclosure control AND the <details> is rendered closed.
        control = s["first_child_of_details"]
        closed = s["rendered_closed"]
        at_risk = bool(control and closed)
        p(f"[{s['origin']}:{s['line']}] .{cls}")
        p(f"    effective display        : {s['display']}  (rule: {s['source']})")
        p(f"    first-child of <details> : {control}   (the disclosure-control position)")
        p(f"    <details> rendered closed: {closed}   (the state the collapse shows in)")
        p(f"    details tag              : {s['details_tag'][:70]}")
        p(f"    5f-break preconditions   : {'ALL MET (at-risk)' if at_risk else 'NOT all met'}")
    p("    jsdom CAVEAT: jsdom cannot lay out <details>; offsetHeight is always 0, so it")
    p("    cannot confirm/deny the Chromium collapse. CSS display (above) is authoritative")
    p("    for the RULE; a live Chromium render is the only thing that proves the VISUAL")
    p("    collapse, and the Day5 audit observed the live toggles OPENING.")

    # ---- VERDICT ------------------------------------------------------------
    p("")
    p("=== VERDICT ===")
    advcand = [r for r in rows if "adv-cand-summary" in r["classes"]]
    if advcand:
        s = advcand[0]
        at_risk = bool(s["first_child_of_details"] and s["rendered_closed"])
        p(f".adv-cand-summary: display={s['display']}, first-child-of-details="
          f"{s['first_child_of_details']}, closed={s['rendered_closed']}.")
        if s["display"] in BREAK_DISPLAYS and at_risk:
            p("-> REAL RULE VIOLATION, CONFIRMED-AT-RISK but NOT a proven runtime break:")
            p("   the display IS flex AND the summary IS the closed first-child disclosure")
            p("   control — the exact 5f pattern. Whether it actually collapses is")
            p("   Chromium-version-dependent (modern Chromium >=89 keeps a flex summary")
            p("   functional; the 5f collapse was an older-Chromium failure). It could NOT")
            p("   be reproduced offline (no Chromium/jsdom layout), and the live site shows")
            p("   the toggles opening -> BENIGN IN THE TESTED BROWSER, but a latent,")
            p("   documented-failure-pattern risk. Fix = drop `display` (use inner-span")
            p("   layout, the 5f pattern); rebuild-only, pin-safe.")
        elif s["display"] in BREAK_DISPLAYS:
            p("-> Rule-violating display but NOT in the at-risk structural position.")
        else:
            p("-> display is not a break-class value; no violation.")
    else:
        p(".adv-cand-summary not found in the rendered markup (unexpected).")

    other = [r for r in violations if "adv-cand-summary" not in r["classes"]]
    p("")
    if other:
        p("OTHER summary display-VIOLATIONS found:")
        for s in other:
            p(f"    {s['origin']}:{s['line']} .{','.join(s['classes'])} display={s['display']}")
    else:
        p("OTHER summary display-VIOLATIONS: none — .adv-cand-summary is the only one.")

    p("")
    p("[Safety] READ-ONLY probe — no files written, no build, no git, no DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
