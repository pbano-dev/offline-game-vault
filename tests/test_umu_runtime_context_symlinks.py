from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from offline_game_vault.umu_adapter import (
    UmuAdapterError,
    _runtime_context_unresolved_prefixes,
    _verify_no_broken_symlinks,
)


RUNTIME_VAR = PurePosixPath(
    "engine/xdg-data/umu/steamrt4/var"
)


class RuntimeContextSymlinkTests(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime_var = self.root.joinpath(
            *RUNTIME_VAR.parts
        )
        self.runtime_var.mkdir(
            parents=True
        )

    def test_pressure_vessel_relative_symlink_is_preserved(
        self,
    ) -> None:
        link = (
            self.runtime_var
            / "tmp-DREGT3"
            / "usr"
            / "etc"
            / "os-release"
        )
        link.parent.mkdir(
            parents=True
        )
        link.symlink_to(
            "../usr/lib/os-release"
        )

        prefixes = (
            _runtime_context_unresolved_prefixes(
                self.root,
                RUNTIME_VAR,
            )
        )

        self.assertEqual(
            prefixes,
            {
                (
                    "engine/xdg-data/umu/steamrt4/"
                    "var/tmp-DREGT3"
                )
            },
        )
        self.assertTrue(
            link.is_symlink()
        )
        self.assertEqual(
            os.readlink(link),
            "../usr/lib/os-release",
        )
        self.assertFalse(
            link.exists()
        )

        _verify_no_broken_symlinks(
            self.root,
            allowed_unresolved_prefixes=prefixes,
        )

    def test_pressure_vessel_absolute_symlink_is_preserved(
        self,
    ) -> None:
        link = (
            self.runtime_var
            / "tmp-DREGT3"
            / "usr"
            / "bin"
            / "getconf"
        )
        link.parent.mkdir(
            parents=True
        )
        link.symlink_to(
            "/run/host/usr/sbin/getconf"
        )

        prefixes = (
            _runtime_context_unresolved_prefixes(
                self.root,
                RUNTIME_VAR,
            )
        )

        self.assertTrue(
            link.is_symlink()
        )
        self.assertEqual(
            os.readlink(link),
            "/run/host/usr/sbin/getconf",
        )

        _verify_no_broken_symlinks(
            self.root,
            allowed_unresolved_prefixes=prefixes,
        )

    def test_broken_symlink_outside_runtime_tmp_is_rejected(
        self,
    ) -> None:
        (
            self.runtime_var
            / "tmp-DREGT3"
        ).mkdir()

        outside = (
            self.root
            / "engine"
            / "broken-runtime-link"
        )
        outside.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        outside.symlink_to(
            "missing-target"
        )

        prefixes = (
            _runtime_context_unresolved_prefixes(
                self.root,
                RUNTIME_VAR,
            )
        )

        with self.assertRaisesRegex(
            UmuAdapterError,
            (
                "Broken symlink: "
                "engine/broken-runtime-link"
            ),
        ):
            _verify_no_broken_symlinks(
                self.root,
                allowed_unresolved_prefixes=prefixes,
            )

    def test_non_directory_tmp_entry_is_rejected(
        self,
    ) -> None:
        (
            self.runtime_var
            / "tmp-not-a-directory"
        ).write_text(
            "not a directory\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            UmuAdapterError,
            (
                "Runtime temporary entry is not "
                "a regular directory"
            ),
        ):
            _runtime_context_unresolved_prefixes(
                self.root,
                RUNTIME_VAR,
            )

    def test_non_tmp_runtime_directory_is_not_exempt(
        self,
    ) -> None:
        link = (
            self.runtime_var
            / "cache"
            / "broken-link"
        )
        link.parent.mkdir(
            parents=True
        )
        link.symlink_to(
            "missing-target"
        )

        prefixes = (
            _runtime_context_unresolved_prefixes(
                self.root,
                RUNTIME_VAR,
            )
        )

        self.assertEqual(
            prefixes,
            set(),
        )

        with self.assertRaisesRegex(
            UmuAdapterError,
            (
                "Broken symlink: "
                "engine/xdg-data/umu/steamrt4/"
                "var/cache/broken-link"
            ),
        ):
            _verify_no_broken_symlinks(
                self.root,
                allowed_unresolved_prefixes=prefixes,
            )


if __name__ == "__main__":
    unittest.main()
