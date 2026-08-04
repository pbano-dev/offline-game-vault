from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from offline_game_vault import __version__


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class VersionContractTests(unittest.TestCase):
    def test_project_package_and_changelog_versions_match(
        self,
    ) -> None:
        project = tomllib.loads(
            (
                REPOSITORY_ROOT
                / "pyproject.toml"
            ).read_text(
                encoding="utf-8"
            )
        )

        project_version = project[
            "project"
        ][
            "version"
        ]

        changelog = (
            REPOSITORY_ROOT
            / "CHANGELOG.md"
        ).read_text(
            encoding="utf-8"
        )

        published = re.search(
            r"^## ([0-9]+\.[0-9]+\.[0-9]+) — ",
            changelog,
            flags=re.MULTILINE,
        )

        self.assertIsNotNone(
            published
        )

        assert published is not None

        self.assertEqual(
            __version__,
            "0.11.4",
        )

        self.assertEqual(
            project_version,
            __version__,
        )

        self.assertEqual(
            published.group(1),
            __version__,
        )


if __name__ == "__main__":
    unittest.main()
