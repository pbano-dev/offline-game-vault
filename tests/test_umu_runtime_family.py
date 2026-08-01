from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from offline_game_vault.portable_umu_runtime import _verify_offline_runtime
from offline_game_vault.umu_adapter import _discover_offline_environment


class UmuRuntimeFamilyTests(unittest.TestCase):
    def _make_runtime(
        self,
        root: Path,
        *,
        family: str,
        platform_prefix: str,
        version: str = "test",
    ) -> Path:
        runtime = root / "engine/xdg-data/umu" / family
        platform = runtime / f"{platform_prefix}_platform_{version}"
        (platform / "files").mkdir(parents=True)
        (runtime / "pressure-vessel/bin").mkdir(parents=True)
        (runtime / "var").mkdir(parents=True)
        (root / "engine/xdg-cache").mkdir(parents=True)
        (runtime / "VERSIONS.txt").write_text("test\n", encoding="utf-8")
        (runtime / "mtree.txt.gz").write_bytes(b"test")
        entrypoint = runtime / "_v2-entry-point"
        entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        entrypoint.chmod(0o755)
        verifier = runtime / "pressure-vessel/bin/pv-verify"
        verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        verifier.chmod(0o755)
        return runtime

    def test_steamrt3_uses_sniper_platform_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_runtime(
                root,
                family="steamrt3",
                platform_prefix="sniper",
                version="3.0.test",
            )

            offline = _discover_offline_environment(root)

            self.assertEqual(offline["runtime"]["family"], "steamrt3")
            self.assertEqual(offline["runtime"]["version"], "3.0.test")
            required = {
                item["path"] for item in offline["runtime"]["required_paths"]
            }
            self.assertIn("sniper_platform_3.0.test", required)

            receipt = {
                "offline_environment": offline,
            }
            verified = _verify_offline_runtime(root, receipt)
            self.assertEqual(
                verified["runtime_root"],
                root / "engine/xdg-data/umu/steamrt3",
            )

    def test_steamrt3_rejects_literal_steamrt3_platform_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_runtime(
                root,
                family="steamrt3",
                platform_prefix="steamrt3",
            )

            with self.assertRaisesRegex(
                Exception,
                r"sniper_platform_\*",
            ):
                _discover_offline_environment(root)


if __name__ == "__main__":
    unittest.main()
