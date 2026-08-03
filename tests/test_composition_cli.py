from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from offline_game_vault.cli import (
    build_parser,
    main,
)


def _component_set() -> SimpleNamespace:
    return SimpleNamespace(
        component_set_id=(
            "umu-component-set-test"
        ),
        component_set_digest="a" * 64,
        backend_object_id="umu-backend",
        runtime_object_id="steamrt4-runtime",
        backend_entrypoint=(
            "engine/python-portable/"
            "umu-run-fully-local"
        ),
        runtime_var=(
            "engine/xdg-data/umu/"
            "steamrt4/var"
        ),
        runtime_family="steamrt4",
        platform_prefix="steamrt4",
        platform_directory=(
            "steamrt4_platform_test"
        ),
    )


class CompositionCliTests(
    unittest.TestCase
):
    def test_commands_are_registered(
        self,
    ) -> None:
        parser = build_parser()

        runners = parser.parse_args(
            [
                "list-preserved-runners",
                "--collection-root",
                "vault",
                "--json",
            ]
        )

        self.assertEqual(
            runners.command,
            "list-preserved-runners",
        )

        self.assertEqual(
            runners.collection_root,
            Path("vault"),
        )

        component_sets = parser.parse_args(
            [
                "list-shared-umu-runtimes",
                "--collection-root",
                "vault",
            ]
        )

        self.assertEqual(
            component_sets.command,
            "list-shared-umu-runtimes",
        )

        materialize = parser.parse_args(
            [
                "compose",
                "--collection-root",
                "vault",
                "--capsule",
                "capsule.json",
                "--backend",
                "direct-wine",
                "--runner",
                "runner",
                "--destination",
                "output",
                "--play",
                "--",
                "-windowed",
            ]
        )

        self.assertEqual(
            materialize.command,
            "compose",
        )

        self.assertEqual(
            materialize.backend,
            "direct-wine",
        )

        self.assertTrue(
            materialize.play
        )

        self.assertEqual(
            materialize.arguments,
            [
                "--",
                "-windowed",
            ],
        )

    def test_missing_collection_is_reported_before_materialization(
        self,
    ) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(
            stderr
        ):
            returncode = main(
                [
                    "list-preserved-runners",
                    "--collection-root",
                    "missing-vault",
                ]
            )

        self.assertEqual(
            returncode,
            2,
        )

        self.assertIn(
            "collection is not a regular directory",
            stderr.getvalue(),
        )

    def test_shared_umu_component_sets_json_contract(
        self,
    ) -> None:
        stdout = io.StringIO()

        with patch(
            (
                "offline_game_vault.cli."
                "list_shared_umu_runtimes"
            ),
            return_value=(
                _component_set(),
            ),
        ):
            with contextlib.redirect_stdout(
                stdout
            ):
                returncode = main(
                    [
                        "list-shared-umu-runtimes",
                        "--collection-root",
                        "vault",
                        "--json",
                    ]
                )

        self.assertEqual(
            returncode,
            0,
        )

        document = json.loads(
            stdout.getvalue()
        )

        self.assertEqual(
            document["schema"],
            0,
        )

        self.assertEqual(
            len(
                document["component_sets"]
            ),
            1,
        )

        item = document[
            "component_sets"
        ][0]

        self.assertEqual(
            item["component_set_id"],
            "umu-component-set-test",
        )

        self.assertEqual(
            item["backend_component_id"],
            "umu-backend",
        )

        self.assertEqual(
            item["runtime_component_id"],
            "steamrt4-runtime",
        )

        self.assertEqual(
            item["backend_entrypoint"],
            (
                "engine/python-portable/"
                "umu-run-fully-local"
            ),
        )

        for retired in (
            "runtime_id",
            "composite_object_id",
            "source_capsule_id",
            "source_profile_id",
        ):
            self.assertNotIn(
                retired,
                item,
            )

    def test_shared_umu_component_sets_text_contract(
        self,
    ) -> None:
        stdout = io.StringIO()

        with patch(
            (
                "offline_game_vault.cli."
                "list_shared_umu_runtimes"
            ),
            return_value=(
                _component_set(),
            ),
        ):
            with contextlib.redirect_stdout(
                stdout
            ):
                returncode = main(
                    [
                        "list-shared-umu-runtimes",
                        "--collection-root",
                        "vault",
                    ]
                )

        self.assertEqual(
            returncode,
            0,
        )

        output = stdout.getvalue()

        self.assertIn(
            "umu-component-set-test",
            output,
        )

        self.assertIn(
            "backend=umu-backend",
            output,
        )

        self.assertIn(
            "runtime=steamrt4-runtime",
            output,
        )

        self.assertIn(
            (
                "entrypoint="
                "engine/python-portable/"
                "umu-run-fully-local"
            ),
            output,
        )


if __name__ == "__main__":
    unittest.main()
