from __future__ import annotations

import unittest
from types import SimpleNamespace

import offline_game_vault.composition as composition


class GenericUmuPrefixOperationTests(unittest.TestCase):
    def test_direct_wine_prefix_topology_is_rendered(self) -> None:
        playable = {
            "prefix_operations": [
                {
                    "type": "mkdir",
                    "path": "prefix/dosdevices",
                },
                {
                    "type": "symlink",
                    "path": "prefix/dosdevices/c:",
                    "target": "../drive_c",
                },
            ],
        }

        rendered = composition._generic_umu_prefix_setup(
            playable,
            "prefix",
        )

        self.assertIn(
            'OGV_PREFIX_OP_PATH="$PREFIX"/dosdevices',
            rendered,
        )
        self.assertIn(
            'OGV_PREFIX_OP_PATH="$PREFIX"/dosdevices/c:',
            rendered,
        )
        self.assertIn(
            "OGV_PREFIX_OP_TARGET=../drive_c",
            rendered,
        )
        self.assertIn(
            'ln -s -- "$OGV_PREFIX_OP_TARGET" "$OGV_PREFIX_OP_PATH"',
            rendered,
        )

    def test_neutral_game_payload_link_may_leave_prefix_but_not_root(
        self,
    ) -> None:
        playable = {
            "prefix_operations": [
                {
                    "type": "symlink",
                    "path": (
                        "source/payload/prefix-template/"
                        "drive_c/Games/Test"
                    ),
                    "target": "../../../game",
                },
            ],
        }

        rendered = composition._generic_umu_prefix_setup(
            playable,
            "source/payload/prefix-template",
        )

        self.assertIn(
            "OGV_PREFIX_OP_TARGET=../../../game",
            rendered,
        )

    def test_operation_path_must_remain_below_prefix(self) -> None:
        with self.assertRaisesRegex(
            composition.CompositionError,
            "must remain below the declared prefix",
        ):
            composition._generic_umu_prefix_setup(
                {
                    "prefix_operations": [
                        {
                            "type": "mkdir",
                            "path": "outside",
                        },
                    ],
                },
                "prefix",
            )

    def test_symlink_target_must_not_escape_materialization(self) -> None:
        with self.assertRaisesRegex(
            composition.CompositionError,
            "escapes the materialization",
        ):
            composition._generic_umu_prefix_setup(
                {
                    "prefix_operations": [
                        {
                            "type": "symlink",
                            "path": "prefix/link",
                            "target": "../../outside",
                        },
                    ],
                },
                "prefix",
            )

    def test_generic_launcher_runs_prefix_setup_before_umu(self) -> None:
        profile = {
            "playable": {
                "paths": {
                    "prefix": "prefix",
                },
                "prefix_operations": [
                    {
                        "type": "mkdir",
                        "path": "prefix/dosdevices",
                    },
                    {
                        "type": "symlink",
                        "path": "prefix/dosdevices/c:",
                        "target": "../drive_c",
                    },
                ],
            },
            "launch": {
                "entrypoint": "prefix/drive_c/Games/Test/game.exe",
                "working_directory": "prefix/drive_c/Games/Test",
                "arguments": [],
            },
        }
        runner = SimpleNamespace(
            source_root="Proton-9.0-203",
        )
        runtime = SimpleNamespace(
            backend_entrypoint="engine/umu-portable/umu-run-fully-local",
            backend_entrypoint_arguments=(),
            backend_pythonpath=None,
        )

        launcher = composition._generic_umu_launcher(
            capsule={
                "game": {
                    "appid": 123,
                    "source_store": "steam",
                },
            },
            profile=profile,
            runner=runner,
            runtime=runtime,
        ).decode("utf-8")

        setup_index = launcher.index("OGV_PREFIX_OP_PATH")
        exec_index = launcher.index('exec "$UMU_ENTRYPOINT"')
        self.assertLess(setup_index, exec_index)
        self.assertIn(
            'OGV_PREFIX_OP_PATH="$PREFIX"/dosdevices/c:',
            launcher,
        )


if __name__ == "__main__":
    unittest.main()
