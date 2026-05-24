"""M11.5c — pin that the dead `fetch_official_page` function stays gone.

Discovered as a side-effect of M11.7's exception-handling audit
(``docs/EXCEPTION_HANDLING_AUDIT.md``, Site 5a): the function had zero
callers anywhere in the repo. M11.5c deletes it as a dead-code cleanup,
not as an exception-handling change.

These pins catch:
  (1) Direct re-introduction of the definition.
  (2) Any new code reference (import, call site, getattr string)
      anywhere in the repo's ``.py`` files.
  (3) Module-import regression (deletion must not have broken
      ``import official_crawler``).
  (4) Removal of a function that IS in use elsewhere — by asserting the
      known live public surface still exists.

Doc-prose mentions of the function name inside
``docs/EXCEPTION_HANDLING_AUDIT.md`` are explicitly allowed; the M11.5c
spec instructs to append a "Resolution" section to that doc rather than
rewriting history.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_CRAWLER_PATH = _PROJECT_ROOT / "official_crawler.py"
_THIS_FILE_NAME = Path(__file__).name


class FetchOfficialPageRemovedTests(unittest.TestCase):
    def setUp(self):
        self.crawler_text = _CRAWLER_PATH.read_text(encoding="utf-8")

    def test_fetch_official_page_function_removed(self):
        """The function definition must not reappear in official_crawler.py.
        Counted via raw substring so a future PR cannot sneak it back in
        under a decorator or different formatting."""
        occurrences = self.crawler_text.count("def fetch_official_page")
        self.assertEqual(
            occurrences, 0,
            f"M11.5c removed `fetch_official_page` from "
            f"official_crawler.py; found {occurrences} definition(s) now. "
            "If you legitimately need a fetch-single-page helper, build "
            "it deliberately with a documented caller — don't reintroduce "
            "the dead one.",
        )

    def test_no_repo_references_to_fetch_official_page(self):
        """No `.py` file anywhere in the repo may reference
        `fetch_official_page` AS CODE — neither call, import,
        attribute access, function/class name, nor `getattr` /
        `hasattr` string literal targeting it. Mentions inside
        comments, docstrings, and unrelated string literals are
        allowed (this test, the M11.7 audit doc's prose, and the
        validate.py wiring comment all legitimately quote the
        name).

        Implementation: parse each file with ast and walk for
        Name / Attribute / Import / ImportFrom / FunctionDef /
        ClassDef nodes whose identifier equals the dead name,
        plus Call nodes whose first arg is the dead name as a
        string literal (for the getattr/hasattr/setattr family).
        Comments and docstrings are NOT in the AST so they are
        ignored automatically."""
        skip_dirs = {
            ".git", ".venv", "venv", "__pycache__", "node_modules",
            ".pytest_cache", ".mypy_cache",
        }
        attr_lookup_funcs = {"getattr", "hasattr", "setattr", "delattr"}
        dead_name = "fetch_official_page"
        offenders = []
        for dirpath, dirnames, filenames in os.walk(_PROJECT_ROOT):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                if filename == _THIS_FILE_NAME:
                    continue
                path = Path(dirpath) / filename
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                if dead_name not in text:
                    continue
                try:
                    tree = ast.parse(text, filename=str(path))
                except SyntaxError:
                    # Best-effort: if a file fails to parse, fall
                    # back to substring match so we don't silently
                    # miss it.
                    offenders.append(
                        f"{path.relative_to(_PROJECT_ROOT)} (unparseable; substring match)"
                    )
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and node.id == dead_name:
                        offenders.append(
                            f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno} Name"
                        )
                    elif isinstance(node, ast.Attribute) and node.attr == dead_name:
                        offenders.append(
                            f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno} Attribute"
                        )
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == dead_name:
                        offenders.append(
                            f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno} {type(node).__name__}"
                        )
                    elif isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            if alias.name == dead_name or alias.asname == dead_name:
                                offenders.append(
                                    f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno} ImportFrom"
                                )
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name == dead_name or alias.asname == dead_name:
                                offenders.append(
                                    f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno} Import"
                                )
                    elif (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id in attr_lookup_funcs
                        and node.args
                        and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value == dead_name
                    ):
                        offenders.append(
                            f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno} {node.func.id}(...,'{dead_name}')"
                        )
        self.assertEqual(
            offenders, [],
            "Found CODE references to fetch_official_page in: "
            + "; ".join(offenders)
            + ". M11.5c removed this dead function; any new code "
            "reference is a re-introduction.",
        )

    def test_official_crawler_still_imports_cleanly(self):
        """Deletion must not have broken module import. Reload to pick
        up the post-deletion file state even if another test imported
        the module first."""
        import importlib
        import official_crawler

        reloaded = importlib.reload(official_crawler)
        self.assertFalse(
            hasattr(reloaded, "fetch_official_page"),
            "official_crawler.fetch_official_page must not be a module "
            "attribute after M11.5c. If it is, the deletion was "
            "incomplete or someone re-added it.",
        )

    def test_official_crawler_public_api_intact(self):
        """The functions / constants that are actually imported by
        other modules must still exist. Discovered via grep for
        `from official_crawler import` at HEAD:
          * main.py uses `fetch_official_evidence`,
            `print_official_evidence_results`.
          * official_source_body.py uses `GOV_CACHE_ALLOWED_DOMAINS`.
          * tests/test_mojibake_cleanup.py uses
            `fetch_best_official_document`.
        Each must still be present on the module post-deletion."""
        import importlib
        import official_crawler

        module = importlib.reload(official_crawler)
        for name in (
            "fetch_official_evidence",
            "print_official_evidence_results",
            "GOV_CACHE_ALLOWED_DOMAINS",
            "fetch_best_official_document",
        ):
            self.assertTrue(
                hasattr(module, name),
                f"official_crawler.{name} is imported elsewhere in the "
                "repo and must still be defined after M11.5c. The "
                "deletion overshot.",
            )


if __name__ == "__main__":
    unittest.main()
