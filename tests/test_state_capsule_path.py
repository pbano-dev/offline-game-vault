from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from offline_game_vault import composition
from offline_game_vault.bottles_adapter import (
    BottlesAdapterError,
    deploy_bottles_profile,
)
from offline_game_vault.composition_state import (
    CompositionStateError,
)
from offline_game_vault.playable import (
    PlayableError,
    materialize_playable_profile,
)
from offline_game_vault.state_manager import StateError
from offline_game_vault.umu_adapter import (
    UmuAdapterError,
    materialize_umu_profile,
)


class StateCapsulePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        document = {
            "capsule_id": "synthetic-capsule",
            "persistent_state": [
                {
                    "id": "save",
                    "path": "save.dat",
                    "backup": True,
                }
            ],
        }
        self.original = self.root / "original-capsule.json"
        self.original.write_text(
            json.dumps(document),
            encoding="utf-8",
        )
        self.overlay = self.root / "overlay-capsule.json"
        self.overlay.write_text(
            json.dumps(document),
            encoding="utf-8",
        )
        self.backup = self.root / "backup"

    def test_direct_wine_routes_state_to_original_capsule(self) -> None:
        vault = self.root / "vault"
        vault.mkdir()
        destination = self.root / "direct-wine"
        capsule = {
            "capsule_id": "synthetic-capsule",
            "persistent_state": [],
        }

        with (
            patch(
                "offline_game_vault.playable._profile_and_contract",
                return_value=(capsule, {}, object()),
            ),
            patch(
                "offline_game_vault.playable.verify_state_backup",
                side_effect=StateError("sentinel"),
            ) as verify,
        ):
            with self.assertRaisesRegex(
                PlayableError,
                "sentinel",
            ):
                materialize_playable_profile(
                    capsule_path=self.overlay,
                    profile_id="derived",
                    vault_root=vault,
                    destination=destination,
                    state_backup=self.backup,
                    state_capsule_path=self.original,
                )

        self.assertEqual(
            verify.call_args.kwargs["capsule_path"],
            self.original.absolute(),
        )

    def test_bottles_routes_state_to_original_capsule(self) -> None:
        materialization = self.root / "materialization"
        materialization.mkdir()
        bottles = self.root / "bottles"
        bottles.mkdir()

        with (
            patch(
                "offline_game_vault.bottles_adapter."
                "require_bottles_managed_path",
                return_value=bottles.resolve(),
            ),
            patch(
                "offline_game_vault.bottles_adapter."
                "_load_capsule_profile",
                return_value=(
                    {"capsule_id": "synthetic-capsule"},
                    {},
                    {},
                    [],
                ),
            ),
            patch(
                "offline_game_vault.bottles_adapter."
                "prepare_composition_state",
                side_effect=CompositionStateError("sentinel"),
            ) as prepare,
        ):
            with self.assertRaisesRegex(
                BottlesAdapterError,
                "sentinel",
            ):
                deploy_bottles_profile(
                    capsule_path=self.overlay,
                    profile_id="derived",
                    materialization=materialization,
                    bottles_path=bottles,
                    bottle_name="synthetic",
                    state_backup=self.backup,
                    state_capsule_path=self.original,
                )

        self.assertEqual(
            prepare.call_args.kwargs["capsule_path"],
            self.original.absolute(),
        )

    def test_umu_routes_state_to_original_capsule(self) -> None:
        vault = self.root / "vault"
        vault.mkdir()
        destination = self.root / "umu"

        with (
            patch(
                "offline_game_vault.umu_adapter._profile_contract",
                return_value=(
                    {"capsule_id": "synthetic-capsule"},
                    {},
                    {},
                ),
            ),
            patch(
                "offline_game_vault.umu_adapter."
                "prepare_composition_state",
                side_effect=CompositionStateError("sentinel"),
            ) as prepare,
        ):
            with self.assertRaisesRegex(
                UmuAdapterError,
                "sentinel",
            ):
                materialize_umu_profile(
                    capsule_path=self.overlay,
                    profile_id="derived",
                    vault_root=vault,
                    destination=destination,
                    state_backup=self.backup,
                    state_capsule_path=self.original,
                )

        self.assertEqual(
            prepare.call_args.kwargs["capsule_path"],
            self.original.absolute(),
        )

    def test_public_composition_routes_original_capsule(self) -> None:
        for function in (
            composition.compose_wine,
            composition.compose_bottles,
            composition.compose_umu,
        ):
            with self.subTest(function=function.__name__):
                self.assertIn(
                    "state_capsule_path=capsule_path",
                    inspect.getsource(function),
                )


if __name__ == "__main__":
    unittest.main()
