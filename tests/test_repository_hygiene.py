from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from check_repository_hygiene import (
    filesystem_paths,
    reasons_for_path,
    scan,
)


class RepositoryHygieneTests(unittest.TestCase):
    def test_allows_normal_source_and_documentation(self) -> None:
        for path in (
            "src/offline_game_vault/storage.py",
            "docs/generated-file-hygiene.md",
            "fixtures/dark-souls-remastered/capsule.json",
            ".github/workflows/validate.yml",
        ):
            self.assertEqual(reasons_for_path(path), [])

    def test_preserves_leading_dot_during_normalization(self) -> None:
        self.assertTrue(reasons_for_path(".coverage"))
        self.assertTrue(reasons_for_path("./.DS_Store"))
        self.assertTrue(reasons_for_path(".incoming-object"))

    def test_rejects_python_generated_artifacts(self) -> None:
        forbidden = {
            "src/pkg/__pycache__/module.cpython-313.pyc",
            "src/offline_game_vault.egg-info/PKG-INFO",
            "tests/.pytest_cache/v/cache/nodeids",
            "module.pyo",
        }
        for path in forbidden:
            with self.subTest(path=path):
                self.assertTrue(reasons_for_path(path))

    def test_rejects_temporary_files(self) -> None:
        forbidden = {
            "file.tmp",
            "file.temp",
            "file.bak",
            "file.orig",
            "file.rej",
            "notes.md~",
            ".incoming-object-123",
            ".#document.md",
            "#document.md#",
            ".coverage",
            ".coverage.worker",
            ".DS_Store",
            "Thumbs.db",
        }
        for path in forbidden:
            with self.subTest(path=path):
                self.assertTrue(reasons_for_path(path))

    def test_rejects_generated_top_level_directories(self) -> None:
        self.assertTrue(reasons_for_path("build/lib/package.py"))
        self.assertTrue(reasons_for_path("dist/project.whl"))
        self.assertEqual(reasons_for_path("docs/build-process.md"), [])

    def test_filesystem_scan_excludes_dot_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git/objects").mkdir(parents=True)
            (root / ".git/objects/internal.tmp").write_text(
                "git internals",
                encoding="utf-8",
            )
            (root / "src").mkdir()
            (root / "src/module.py").write_text(
                "pass\n",
                encoding="utf-8",
            )

            paths = filesystem_paths(root)

            self.assertIn("src/module.py", paths)
            self.assertFalse(
                any(path.startswith(".git/") for path in paths)
            )

    def test_scan_reports_each_forbidden_path_once(self) -> None:
        violations = scan(
            [
                "src/pkg/__pycache__/module.pyc",
                "src/pkg/__pycache__/module.pyc",
            ]
        )
        self.assertEqual(len(violations), 1)


if __name__ == "__main__":
    unittest.main()
