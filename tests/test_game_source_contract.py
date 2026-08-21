from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from offline_game_vault import composition, composition_profile


class CanonicalGameSourceContractTests(unittest.TestCase):
    CONTRACT = "ogv-game-source-v1"

    def _capsule(self, root: Path) -> Path:
        contracts = root / "host-contracts"
        contracts.mkdir(parents=True)
        (contracts / "game-source.json").write_text(
            json.dumps(
                {
                    "schema": 0,
                    "contract": self.CONTRACT,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        capsule = root / "capsule.json"
        capsule.write_text(
            json.dumps(
                {
                    "schema": 0,
                    "capsule_id": "canonical-game-source-test",
                    "objects": [],
                    "profiles": [
                        {
                            "id": "game-source",
                            "platform": "linux",
                            "adapter": "wine",
                            "dependencies": [],
                            "host_contract": "host-contracts/game-source.json",
                            "launch": {
                                "entrypoint": "prefix/drive_c/Games/Test/Game.exe",
                                "working_directory": "prefix/drive_c/Games/Test",
                                "arguments": [],
                                "network": "host_default",
                            },
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return capsule

    def test_contract_is_registered_in_both_neutral_surfaces(self) -> None:
        self.assertIn(self.CONTRACT, composition._NEUTRAL_CONTRACTS)
        self.assertIn(self.CONTRACT, composition_profile._NEUTRAL_CONTRACTS)

    def test_source_kind_recognizes_canonical_game_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            capsule = self._capsule(Path(temporary))
            profile = json.loads(capsule.read_text(encoding="utf-8"))[
                "profiles"
            ][0]
            self.assertEqual(
                composition._source_kind(capsule, profile),
                self.CONTRACT,
            )

    def test_auto_source_selection_uses_one_source_for_all_backends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            capsule = self._capsule(Path(temporary))
            for backend in ("direct-wine", "bottles", "umu"):
                with self.subTest(backend=backend):
                    self.assertEqual(
                        composition._select_source_profile(
                            capsule,
                            backend=backend,
                            profile_id=None,
                        ),
                        "game-source",
                    )

    def test_legacy_source_priorities_remain_stable(self) -> None:
        priorities = composition._SOURCE_PRIORITIES
        self.assertEqual(
            priorities["direct-wine"],
            {
                "playable-wine": 0,
                "ogv-direct-wine-neutral-v1": 1,
                "ogv-bottles-neutral-v1": 2,
                "ogv-umu-neutral-v1": 3,
                self.CONTRACT: 4,
            },
        )
        self.assertEqual(
            priorities["bottles"],
            {
                "ogv-bottles-neutral-v1": 0,
                "ogv-direct-wine-neutral-v1": 1,
                "ogv-umu-neutral-v1": 2,
                self.CONTRACT: 3,
            },
        )
        self.assertEqual(
            priorities["umu"],
            {
                "umu-native": 0,
                "ogv-umu-neutral-v1": 1,
                "ogv-direct-wine-neutral-v1": 2,
                "ogv-bottles-neutral-v1": 3,
                "playable-wine": 4,
                self.CONTRACT: 5,
            },
        )

    def test_legacy_neutral_contracts_remain_registered(self) -> None:
        expected = {
            "ogv-bottles-neutral-v1",
            "ogv-direct-wine-neutral-v1",
            "ogv-umu-neutral-v1",
        }
        self.assertTrue(expected.issubset(composition._NEUTRAL_CONTRACTS))
        self.assertTrue(expected.issubset(composition_profile._NEUTRAL_CONTRACTS))


if __name__ == "__main__":
    unittest.main()
