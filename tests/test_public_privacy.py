from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from check_public_privacy import scan_paths


class PrivacyTests(unittest.TestCase):
    def write(
        self,
        root: Path,
        name: str,
        text: str,
    ) -> Path:
        path = root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_placeholders_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.write(
                root,
                "README.md",
                "$HOME /run/user/<UID> <VAULT>",
            )
            self.assertEqual(
                scan_paths(root, [path]),
                [],
            )

    def test_var_home_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.write(
                root,
                "leak.md",
                "/" + "var" + "/" + "home" + "/" + "private" + "/file",
            )
            self.assertTrue(
                scan_paths(root, [path])
            )

    def test_run_media_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.write(
                root,
                "leak.md",
                "/" + "run" + "/" + "media" + "/" + "private" + "/disk",
            )
            self.assertTrue(
                scan_paths(root, [path])
            )

    def test_absolute_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "leak"
            os.symlink(
                "/" + "home" + "/" + "private" + "/file",
                path,
            )
            self.assertTrue(
                scan_paths(root, [path])
            )


if __name__ == "__main__":
    unittest.main()
