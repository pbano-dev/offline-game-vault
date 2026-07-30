from __future__ import annotations
import os, subprocess, tempfile, unittest
from pathlib import Path
from check_public_privacy import scan

class PrivacyTests(unittest.TestCase):
    def repo(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        return tmp, root

    def track(self, root, name, text):
        path = root / name
        path.write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", name], cwd=root, check=True)

    def test_placeholders_allowed(self):
        tmp, root = self.repo()
        with tmp:
            self.track(root, "README.md", "$HOME /run/user/<UID> <VAULT>")
            self.assertEqual(scan(root), [])

    def test_var_home_rejected(self):
        tmp, root = self.repo()
        with tmp:
            self.track(
                root,
                "leak.md",
                "/" + "var" + "/" + "home" + "/" + "private" + "/file",
            )
            self.assertTrue(scan(root))

    def test_run_media_rejected(self):
        tmp, root = self.repo()
        with tmp:
            self.track(root, "leak.md", "/run" + "/media/private/disk")
            self.assertTrue(scan(root))

    def test_absolute_symlink_rejected(self):
        tmp, root = self.repo()
        with tmp:
            os.symlink("/home" + "/private/file", root / "leak")
            subprocess.run(["git", "add", "leak"], cwd=root, check=True)
            self.assertTrue(scan(root))

if __name__ == "__main__":
    unittest.main()
