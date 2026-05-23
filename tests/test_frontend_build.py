"""Tests for the M13.2a frontend build pipeline.

Run with: python tests/test_frontend_build.py

Synthetic tests use temp directories with a private copy of the build
module loaded via importlib so we can rebind its global paths without
mutating the repo-level module state. The single ``RepoLevelIntegrationTest``
case runs against the real ``frontend/`` + ``web/index.html`` and is
the canonical "no drift" check.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_BUILD_PATH = _PROJECT_ROOT / "frontend" / "build_index.py"


def _load_build_module():
    """Import the build script as a fresh module each time so the
    test can override its module-level path globals without leaking
    state across cases."""
    spec = importlib.util.spec_from_file_location(
        "build_index_under_test", str(_BUILD_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_synthetic(
    tmp_dir: Path,
    template_bytes: bytes,
    css_bytes: bytes,
    served_bytes: bytes,
):
    """Lay out a synthetic frontend/ + served-HTML tree under ``tmp_dir``
    and return a configured build module with paths rebound to that
    tree. Always returns a module whose ``cmd_*`` functions operate on
    the synthetic tree."""
    frontend = tmp_dir / "frontend"
    (frontend / "styles").mkdir(parents=True, exist_ok=True)
    (frontend / "template.html").write_bytes(template_bytes)
    (frontend / "styles" / "main.css").write_bytes(css_bytes)
    served = tmp_dir / "web"
    served.mkdir(parents=True, exist_ok=True)
    (served / "index.html").write_bytes(served_bytes)

    module = _load_build_module()
    module.FRONTEND_DIR = frontend
    module.REPO_ROOT = tmp_dir
    module.TEMPLATE_PATH = frontend / "template.html"
    module.CSS_PATH = frontend / "styles" / "main.css"
    module.CHECKSUM_PATH = frontend / "dist_checksum.txt"
    module.SERVED_HTML_PATH = served / "index.html"
    return module


def _capture(callable_, *args, **kwargs):
    """Run a function with stdout/stderr captured. Returns
    ``(return_value, stdout_text, stderr_text)``."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        result = callable_(*args, **kwargs)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return result, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Build-core invariants on synthetic fixtures
# ---------------------------------------------------------------------------


_SYNTH_TEMPLATE = (
    "<!doctype html>\n"
    "<html><head><meta charset=\"utf-8\">\n"
    "<!-- CSS_INJECT -->\n"
    "</head><body>안녕하세요</body></html>\n"
).encode("utf-8")
_SYNTH_CSS = (
    "body { background: #fff; }\n"
    ".korean-class { color: red; }\n"
).encode("utf-8")


def _expected_synth_output() -> bytes:
    return _SYNTH_TEMPLATE.replace(
        b"<!-- CSS_INJECT -->",
        b"<style>" + _SYNTH_CSS + b"</style>",
        1,
    )


class BuildCoreInvariantTests(unittest.TestCase):
    def test_build_is_idempotent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, b"",
            )
            first = module.build_html_bytes()
            second = module.build_html_bytes()
            self.assertEqual(first, second)

    def test_marker_missing_raises(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            template = b"<!doctype html><html><body></body></html>"
            module = _seed_synthetic(
                Path(tmp), template, _SYNTH_CSS, b"",
            )
            with self.assertRaises(RuntimeError) as ctx:
                module.build_html_bytes()
            self.assertIn(
                "missing required marker", str(ctx.exception).lower(),
            )

    def test_multiple_markers_raises(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            template = (
                b"<head>\n<!-- CSS_INJECT -->\n"
                b"<!-- CSS_INJECT -->\n</head>"
            )
            module = _seed_synthetic(
                Path(tmp), template, _SYNTH_CSS, b"",
            )
            with self.assertRaises(RuntimeError) as ctx:
                module.build_html_bytes()
            self.assertIn("exactly one", str(ctx.exception).lower())

    def test_missing_css_file_raises(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, b"",
            )
            module.CSS_PATH.unlink()
            with self.assertRaises(RuntimeError) as ctx:
                module.build_html_bytes()
            self.assertIn("css missing", str(ctx.exception).lower())

    def test_missing_template_raises(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, b"",
            )
            module.TEMPLATE_PATH.unlink()
            with self.assertRaises(RuntimeError) as ctx:
                module.build_html_bytes()
            self.assertIn("template missing", str(ctx.exception).lower())

    def test_round_trip_byte_identical(self):
        """Synthetic template + CSS reassembles to the expected
        original bytes."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, b"",
            )
            output = module.build_html_bytes()
            self.assertEqual(output, _expected_synth_output())

    def test_korean_text_preserved_in_template_body(self):
        """Korean content outside the CSS region must survive the
        build unchanged."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, b"",
            )
            output = module.build_html_bytes()
            self.assertIn("안녕하세요".encode("utf-8"), output)

    def test_korean_text_preserved_in_css(self):
        """CSS can carry Korean text in comments / content rules; the
        build must not transcode."""
        css = (
            "/* 한국어 코멘트 */\n"
            ".korean { content: '\\ud55c\\uad6d\\uc5b4'; }\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, css, b"",
            )
            output = module.build_html_bytes()
            self.assertIn("한국어 코멘트".encode("utf-8"), output)


# ---------------------------------------------------------------------------
# --check / --status / cmd_write behaviour
# ---------------------------------------------------------------------------


class CheckAndStatusBehaviourTests(unittest.TestCase):
    def test_check_passes_when_synced(self):
        expected = _expected_synth_output()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, expected,
            )
            rc, out, _ = _capture(module.cmd_check)
            self.assertEqual(rc, 0)
            self.assertIn("matches build output exactly", out)

    def test_check_fails_when_served_modified(self):
        expected = _expected_synth_output()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS,
                expected + b"<!-- stray byte -->",
            )
            rc, _, err = _capture(module.cmd_check)
            self.assertEqual(rc, 1)
            self.assertIn("does not match", err)
            self.assertIn("served hash", err)
            self.assertIn("expected hash", err)

    def test_check_fails_when_css_modified_but_artifact_stale(self):
        served = _expected_synth_output()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, served,
            )
            # Modify CSS without rebuilding.
            module.CSS_PATH.write_bytes(
                _SYNTH_CSS + b"\n/* extra rule */\n",
            )
            rc, _, err = _capture(module.cmd_check)
            self.assertEqual(rc, 1)
            self.assertIn("First diff at byte", err)

    def test_check_fails_when_served_missing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, b"",
            )
            module.SERVED_HTML_PATH.unlink()
            rc, _, err = _capture(module.cmd_check)
            self.assertEqual(rc, 1)
            self.assertIn("does not exist", err)

    def test_check_does_not_write_any_file(self):
        served = _expected_synth_output()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, served,
            )
            before = _snapshot_tree(Path(tmp))
            _capture(module.cmd_check)
            after = _snapshot_tree(Path(tmp))
            self.assertEqual(before, after)

    def test_status_does_not_write_any_file(self):
        served = _expected_synth_output()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, served,
            )
            before = _snapshot_tree(Path(tmp))
            _capture(module.cmd_status)
            after = _snapshot_tree(Path(tmp))
            self.assertEqual(before, after)

    def test_status_output_includes_expected_fields(self):
        served = _expected_synth_output()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, served,
            )
            _, out, _ = _capture(module.cmd_status)
            for needle in (
                "Template:", "CSS:", "Served HTML:",
                "Checksum", "Served bytes", "Served hash",
            ):
                self.assertIn(needle, out)

    def test_write_then_check_passes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            module = _seed_synthetic(
                Path(tmp), _SYNTH_TEMPLATE, _SYNTH_CSS, b"",
            )
            rc_write, _, _ = _capture(module.cmd_write)
            self.assertEqual(rc_write, 0)
            rc_check, _, _ = _capture(module.cmd_check)
            self.assertEqual(rc_check, 0)
            # Checksum file was written.
            self.assertTrue(module.CHECKSUM_PATH.exists())
            stored = module.CHECKSUM_PATH.read_bytes().decode("ascii").strip()
            expected_hash = hashlib.sha256(
                _expected_synth_output(),
            ).hexdigest()
            self.assertEqual(stored, expected_hash)


def _snapshot_tree(root: Path) -> dict:
    """Snapshot of the directory: path -> (size, sha256). Used to
    confirm a CLI mode is truly read-only."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            out[str(path.relative_to(root))] = (
                len(data), hashlib.sha256(data).hexdigest(),
            )
    return out


# ---------------------------------------------------------------------------
# Static module shape — stdlib-only, no bundler imports.
# ---------------------------------------------------------------------------


class ModuleLevelStaticChecks(unittest.TestCase):
    def setUp(self):
        self.source = _BUILD_PATH.read_text(encoding="utf-8")

    def test_no_third_party_imports(self):
        """The build script must depend on stdlib only — no npm
        package, no bundler, no requests/yaml/etc."""
        forbidden_modules = (
            "yaml", "requests", "httpx", "click", "rich",
            "webpack", "vite", "rollup", "parcel", "esbuild",
            "anthropic", "openai", "fastapi", "sqlalchemy",
        )
        for module_name in forbidden_modules:
            pattern = re.compile(
                rf"^(?:from\s+{module_name}\b|import\s+{module_name}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=f"build_index.py must not import {module_name}",
            )

    def test_only_stdlib_imports(self):
        """Top-level imports must be drawn from a curated stdlib
        allowlist. A new import here forces a deliberate review."""
        allowlist = {
            "__future__",
            "argparse",
            "hashlib",
            "sys",
            "pathlib",
        }
        # Match top-level imports (no leading whitespace).
        import_pattern = re.compile(
            r"^(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))",
            re.MULTILINE,
        )
        for match in import_pattern.finditer(self.source):
            module = (match.group(1) or match.group(2)).split(".")[0]
            self.assertIn(
                module, allowlist,
                msg=(
                    f"build_index.py imports {module!r}; not in the "
                    f"stdlib allowlist {sorted(allowlist)}"
                ),
            )

    def test_uses_bytes_io(self):
        """Build script must use read_bytes/write_bytes (not
        read_text/write_text) so Windows newline translation cannot
        violate byte-identicality."""
        self.assertIn("read_bytes", self.source)
        self.assertIn("write_bytes", self.source)


# ---------------------------------------------------------------------------
# Canonical repo-level integration — the build of the real files must
# match the real served HTML byte-for-byte. This is the test that
# guarantees M13.2a's invariant.
# ---------------------------------------------------------------------------


class RepoLevelIntegrationTest(unittest.TestCase):
    def test_real_build_matches_real_served_html(self):
        """The single most important test in this file. If this fails,
        web/index.html and frontend/ have drifted and the operator
        must rebuild before commit."""
        # Reuse the actual build module (not the helper rebound one).
        module = _load_build_module()
        served = module.SERVED_HTML_PATH.read_bytes()
        expected = module.build_html_bytes()
        self.assertEqual(
            hashlib.sha256(served).hexdigest(),
            hashlib.sha256(expected).hexdigest(),
            msg=(
                "web/index.html and frontend/ have drifted. "
                "Run `python frontend/build_index.py` and commit "
                "the rebuilt artifact."
            ),
        )
        # Belt and braces — also compare bytes directly.
        self.assertEqual(served, expected)

    def test_dist_checksum_matches_served_html(self):
        """When committed, dist_checksum.txt should match the served
        artifact. Drift here flags an operator-side rebuild issue."""
        module = _load_build_module()
        if not module.CHECKSUM_PATH.exists():
            self.skipTest("dist_checksum.txt not committed yet")
        served_hash = hashlib.sha256(
            module.SERVED_HTML_PATH.read_bytes(),
        ).hexdigest()
        stored = module.CHECKSUM_PATH.read_bytes().decode("ascii").strip()
        self.assertEqual(
            stored, served_hash,
            msg=(
                "dist_checksum.txt is stale. Run "
                "`python frontend/build_index.py` to refresh."
            ),
        )


if __name__ == "__main__":
    unittest.main()
