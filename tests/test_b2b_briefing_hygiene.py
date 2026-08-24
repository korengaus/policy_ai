"""B2B-BRIEFING source hygiene — MIRROR of the endpoint source-scan pattern
(tests/test_cluster_sizes_endpoint.py::ClusterSizesHonestyTests and its
siblings): the briefing generator's CODE must never touch a verdict, score,
or confidence column.

Two differences from the endpoint tests, both forced by what is being scanned:

* The generator is a SCRIPT, not an importable endpoint function, and this
  test must not import it (importing pulls the weekly engine). The scan
  therefore reads the file and walks its AST instead of inspect.getsource.
* The generator's comments and docstrings legitimately NAME the excluded
  columns to state the exclusion ("no verdict_label, no policy_confidence…").
  Prose about the moat is not a breach of it, so the scan strips docstrings
  and re-serializes via ast.unparse (which drops comments): the assertion is
  about executable code, never about the prose that documents the guarantee.

Scan 1 (column names, the endpoint tests' exact list): verdict_label,
policy_confidence, truth_claim, operator_review_required,
has_genuine_official_support — absent from code and code-level strings alike.
Scan 2 (identifier vocabulary): no identifier in the generator contains
"verdict", "score", or "confidence". ("label" is deliberately not scanned at
the identifier level: the honesty guard constant FORBIDDEN_LABEL_VOCAB — the
guard itself — carries it.)
"""
import ast
import unittest
from pathlib import Path

GENERATOR = (Path(__file__).resolve().parent.parent
             / "scripts" / "b2b_briefing.py")

FORBIDDEN_COLUMNS = (
    "verdict_label", "policy_confidence", "truth_claim",
    "operator_review_required", "has_genuine_official_support",
)
FORBIDDEN_IDENTIFIER_VOCAB = ("verdict", "score", "confidence")


def _strip_docstrings(tree: ast.Module) -> ast.Module:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _identifiers(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, ast.arg):
            yield node.arg
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            yield node.name
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                yield alias.name
                if alias.asname:
                    yield alias.asname


class B2BBriefingSourceHygieneTests(unittest.TestCase):
    def setUp(self):
        self.tree = _strip_docstrings(
            ast.parse(GENERATOR.read_text(encoding="utf-8")))

    def test_no_verdict_column_in_generator_code(self):
        code = ast.unparse(self.tree).lower()
        for column in FORBIDDEN_COLUMNS:
            self.assertNotIn(
                column, code,
                "generator code (docstrings/comments stripped) carries %r"
                % column)

    def test_no_verdict_score_confidence_identifier(self):
        for name in _identifiers(self.tree):
            lowered = name.lower()
            for word in FORBIDDEN_IDENTIFIER_VOCAB:
                self.assertNotIn(
                    word, lowered,
                    "generator identifier %r carries %r" % (name, word))


if __name__ == "__main__":
    unittest.main()
