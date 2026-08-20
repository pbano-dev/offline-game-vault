from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from offline_game_vault.composition import (
    CompositionError,
    _select_source_profile,
)
from offline_game_vault.composition_profile import RunnerOverrideError


class AutoSourceDerivabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runner = SimpleNamespace(runner_id="proton-test")

    def write_synthesized_capsule(self) -> Path:
        capsule = self.root / "capsule.json"
        contract = self.root / "neutral.json"
        contract.write_text(
            json.dumps(
                {
                    "schema": 0,
                    "contract": "ogv-bottles-neutral-v1",
                }
            ),
            encoding="utf-8",
        )
        capsule.write_text(
            json.dumps(
                {
                    "capsule_id": "auto-source-test",
                    "profiles": [
                        {
                            "id": "preferred-neutral",
                            "platform": "linux",
                            "adapter": "bottles",
                            "host_contract": "neutral.json",
                        },
                        {
                            "id": "fallback-wine",
                            "platform": "linux",
                            "adapter": "wine",
                            "playable": {
                                "schema": 0,
                                "backend": "wine",
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return capsule

    def test_auto_falls_back_when_preferred_source_cannot_derive(
        self,
    ) -> None:
        capsule = self.write_synthesized_capsule()
        calls: list[str] = []

        def derive(
            _capsule: Path,
            profile_id: str,
            _runner: object,
        ) -> object:
            calls.append(profile_id)
            if profile_id == "preferred-neutral":
                raise RunnerOverrideError(
                    "The neutral source has no protected-file evidence"
                )
            return object()

        with patch(
            "offline_game_vault.composition.build_derived_capsule",
            side_effect=derive,
        ):
            selected = _select_source_profile(
                capsule,
                backend="umu",
                profile_id=None,
                runner=self.runner,  # type: ignore[arg-type]
            )

        self.assertEqual(selected, "fallback-wine")
        self.assertEqual(
            calls,
            ["preferred-neutral", "fallback-wine"],
        )

    def test_explicit_source_never_falls_back(self) -> None:
        capsule = self.write_synthesized_capsule()

        with patch(
            "offline_game_vault.composition.build_derived_capsule"
        ) as derive:
            selected = _select_source_profile(
                capsule,
                backend="umu",
                profile_id="preferred-neutral",
                runner=self.runner,  # type: ignore[arg-type]
            )

        self.assertEqual(selected, "preferred-neutral")
        derive.assert_not_called()

    def test_auto_reports_all_failed_synthesized_candidates(
        self,
    ) -> None:
        capsule = self.write_synthesized_capsule()

        with patch(
            "offline_game_vault.composition.build_derived_capsule",
            side_effect=RunnerOverrideError("cannot derive"),
        ):
            with self.assertRaisesRegex(
                CompositionError,
                "No automatically selected Linux source profile",
            ) as raised:
                _select_source_profile(
                    capsule,
                    backend="umu",
                    profile_id=None,
                    runner=self.runner,  # type: ignore[arg-type]
                )

        message = str(raised.exception)
        self.assertIn("preferred-neutral", message)
        self.assertIn("fallback-wine", message)

    def test_umu_native_is_not_preflighted_as_wine_synthesis(
        self,
    ) -> None:
        capsule = self.root / "native-capsule.json"
        capsule.write_text(
            json.dumps(
                {
                    "capsule_id": "native-source-test",
                    "profiles": [
                        {
                            "id": "native",
                            "platform": "linux",
                            "adapter": "umu",
                            "umu": {"schema": 0},
                        },
                        {
                            "id": "fallback-wine",
                            "platform": "linux",
                            "adapter": "wine",
                            "playable": {
                                "schema": 0,
                                "backend": "wine",
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        with patch(
            "offline_game_vault.composition.build_derived_capsule"
        ) as derive:
            selected = _select_source_profile(
                capsule,
                backend="umu",
                profile_id=None,
                runner=self.runner,  # type: ignore[arg-type]
            )

        self.assertEqual(selected, "native")
        derive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
