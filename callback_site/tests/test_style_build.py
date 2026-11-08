"""Sass build reproducibility and failure behavior, independent of Flask."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from callback_site.build_styles import APP_DIR, GENERATED_HEADER, build_styles


class StyleBuildTests(unittest.TestCase):
    def test_committed_assets_match_sass_sources(self) -> None:
        self.assertEqual(build_styles(APP_DIR / "styles", APP_DIR / "static", check=True), 0)
        entries = {path.stem for path in (APP_DIR / "styles").glob("*.scss")}
        outputs = {path.stem for path in (APP_DIR / "static").glob("*.css")}
        self.assertEqual(entries, outputs)

    def test_build_and_check_detect_missing_and_stale_css(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources, output = root / "styles", root / "static"
            sources.mkdir()
            (sources / "_palette.scss").write_text("$ink: #123456;\n")
            entry = sources / "page.scss"
            entry.write_text('@use "palette";\n.page { color: palette.$ink; }\n')
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(build_styles(sources, output, check=True), 1)
            self.assertIn("page.css", errors.getvalue())
            self.assertFalse(output.exists())
            self.assertEqual(build_styles(sources, output), 0)
            css = output / "page.css"
            self.assertTrue(css.read_text().startswith(GENERATED_HEADER))
            self.assertIn("#123456", css.read_text())
            self.assertFalse((output / "_palette.css").exists())
            self.assertFalse(list(output.glob("*.map")))
            self.assertEqual(build_styles(sources, output, check=True), 0)
            modified = css.stat().st_mtime_ns
            self.assertEqual(build_styles(sources, output), 0)
            self.assertEqual(css.stat().st_mtime_ns, modified)
            (sources / "_palette.scss").write_text("$ink: #654321;\n")
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(build_styles(sources, output, check=True), 1)
            self.assertIn("Stale generated CSS", errors.getvalue())
            self.assertIn("#123456", css.read_text())
            self.assertEqual(build_styles(sources, output), 0)
            self.assertIn("#654321", css.read_text())

    def test_compile_failure_does_not_publish_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources, output = root / "styles", root / "static"
            sources.mkdir()
            output.mkdir()
            (sources / "a.scss").write_text(".valid { color: red; }\n")
            (sources / "z.scss").write_text('@use "missing";\n')
            (output / "a.css").write_text("existing css\n")
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(build_styles(sources, output), 1)
            self.assertIn("Sass compilation failed for z.scss", errors.getvalue())
            self.assertEqual((output / "a.css").read_text(), "existing css\n")
            self.assertFalse((output / "z.css").exists())

    def test_empty_source_directory_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(build_styles(Path(tmp), Path(tmp) / "static"), 1)
        self.assertIn("No Sass entrypoints", errors.getvalue())

    def test_shared_mixins_emit_only_when_used_and_reject_unknown_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources, output = root / "styles", root / "static"
            shutil.copytree(APP_DIR / "styles/abstracts", sources / "abstracts")
            entry = sources / "state.scss"
            entry.write_text('@use "abstracts/mixins";\n')
            self.assertEqual(build_styles(sources, output), 0)
            self.assertEqual((output / "state.css").read_text().strip(), GENERATED_HEADER.strip())
            entry.write_text('@use "abstracts/mixins";\n.badge { @include mixins.state(success); }\n')
            self.assertEqual(build_styles(sources, output), 0)
            generated = (output / "state.css").read_text()
            self.assertIn("background: #dcfce7", generated)
            self.assertIn("color: #166534", generated)
            entry.write_text('@use "abstracts/mixins";\n.badge { @include mixins.state(unknown); }\n')
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(build_styles(sources, output), 1)
            self.assertIn("Unknown UI state", errors.getvalue())
            self.assertEqual((output / "state.css").read_text(), generated)

    def test_missing_build_dependency_has_install_guidance(self) -> None:
        with patch.dict("sys.modules", {"sass_embedded": None}), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(build_styles(APP_DIR / "styles", APP_DIR / "static"), 1)
        self.assertIn("requirements-styles.txt", errors.getvalue())

    def test_sass_modules_stay_small_and_shared_selectors_have_one_owner(self) -> None:
        for path in (APP_DIR / "styles").rglob("*.scss"):
            with self.subTest(source=path.relative_to(APP_DIR)):
                text = path.read_text()
                self.assertLessEqual(len(text.splitlines()), 300)
                self.assertNotIn("@import", text)
                self.assertNotIn("@extend", text)
                if "pages" in path.parts:
                    self.assertNotRegex(text, r"(?m)^(?:body|\.sidebar|\.wrap|\.panel|\.table-wrap|:root)\s*\{")


if __name__ == "__main__":
    unittest.main()
