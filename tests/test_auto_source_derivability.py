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
        self.runner = SimpleNamespace(
            runner_id="proton-test",
            digest=(
                "sha256:"
                + "c" * 64
            ),
        )

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
                    "objects": [
                        {
                            "id": "native-runner-object",
                            "digest": (
                                "sha256:"
                                + "c" * 64
                            ),
                            "roles": ["runner"],
                        },
                    ],
                    "profiles": [
                        {
                            "id": "native",
                            "platform": "linux",
                            "adapter": "umu",
                            "dependencies": [
                                "native-runner-object",
                            ],
                            "umu": {
                                "schema": 0,
                                "layout": [
                                    {
                                        "object": "native-runner-object",
                                        "source": "native-runner-object",
                                        "destination": (
                                            "engine/proton/"
                                            "native-runner-object"
                                        ),
                                    },
                                ],
                            },
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


class UmuNativeRunnerBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_capsule(
        self,
        *,
        include_fallback: bool = True,
    ) -> Path:
        profiles = [
            {
                "id": "native-ge",
                "platform": "linux",
                "adapter": "umu",
                "dependencies": ["game", "ge-runner"],
                "umu": {
                    "schema": 0,
                    "layout": [
                        {
                            "object": "ge-runner",
                            "source": "ge-runner",
                            "destination": "engine/proton/ge-runner",
                        }
                    ],
                },
            },
        ]
        if include_fallback:
            profiles.append(
                {
                    "id": "fallback-wine",
                    "platform": "linux",
                    "adapter": "wine",
                    "dependencies": ["game"],
                    "playable": {
                        "schema": 0,
                        "backend": "wine",
                    },
                }
            )
        capsule = self.root / "native-capsule.json"
        capsule.write_text(
            json.dumps(
                {
                    "capsule_id": "native-runner-binding",
                    "objects": [
                        {
                            "id": "game",
                            "roles": ["game_payload"],
                        },
                        {
                            "id": "ge-runner",
                            "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "roles": ["runner"],
                        },
                    ],
                    "profiles": profiles,
                }
            ),
            encoding="utf-8",
        )
        return capsule

    def runner(self, runner_id: str) -> object:
        digest = (
            "sha256:"
            + (
                "a" * 64
                if runner_id == "ge-runner"
                else "b" * 64
            )
        )
        return SimpleNamespace(
            runner_id=runner_id,
            digest=digest,
            source_root=runner_id,
        )

    def test_auto_uses_native_when_runner_binding_matches(self) -> None:
        capsule = self.write_capsule()

        with patch(
            "offline_game_vault.composition.build_derived_capsule"
        ) as derive:
            selected = _select_source_profile(
                capsule,
                backend="umu",
                profile_id=None,
                runner=self.runner("ge-runner"),  # type: ignore[arg-type]
            )

        self.assertEqual(selected, "native-ge")
        derive.assert_not_called()

    def test_auto_skips_native_bound_to_another_runner(self) -> None:
        capsule = self.write_capsule()

        with patch(
            "offline_game_vault.composition.build_derived_capsule",
            return_value=object(),
        ) as derive:
            selected = _select_source_profile(
                capsule,
                backend="umu",
                profile_id=None,
                runner=self.runner("proton-9"),  # type: ignore[arg-type]
            )

        self.assertEqual(selected, "fallback-wine")
        derive.assert_called_once()

    def test_explicit_mismatched_native_runner_is_rejected(self) -> None:
        capsule = self.write_capsule()

        with self.assertRaisesRegex(
            CompositionError,
            "not bound exclusively to requested runner",
        ):
            _select_source_profile(
                capsule,
                backend="umu",
                profile_id="native-ge",
                runner=self.runner("proton-9"),  # type: ignore[arg-type]
            )

    def test_auto_mismatch_without_fallback_is_explicit(self) -> None:
        capsule = self.write_capsule(include_fallback=False)

        with self.assertRaisesRegex(
            CompositionError,
            "No Linux source profile can satisfy the requested UMU runner",
        ):
            _select_source_profile(
                capsule,
                backend="umu",
                profile_id=None,
                runner=self.runner("proton-9"),  # type: ignore[arg-type]
            )

    def write_manifest(
        self,
        collection: Path,
        digest: str,
        entries: list[tuple[str, int, str]],
    ) -> None:
        hexdigest = digest.removeprefix("sha256:")
        path = (
            collection
            / "01_IMMUTABLE_VAULT"
            / "manifests"
            / "sha256"
            / hexdigest[:2]
            / hexdigest[2:4]
            / hexdigest
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["manifest_schema:0"]
        lines.extend(
            f"{file_digest} {size} {relative}"
            for file_digest, size, relative in entries
        )
        path.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    def write_embedded_capsule(self) -> tuple[Path, object, object]:
        collection = self.root / "collection"
        capsule_dir = (
            collection
            / "02_CAPSULES"
            / "embedded-native"
        )
        capsule_dir.mkdir(parents=True, exist_ok=True)
        capsule = capsule_dir / "capsule.json"

        selected_digest = "sha256:" + "d" * 64
        other_digest = "sha256:" + "e" * 64
        stack_digest = "sha256:" + "f" * 64
        file_a = "1" * 64
        file_b = "2" * 64

        self.write_manifest(
            collection,
            selected_digest,
            [
                (file_a, 10, "proton"),
                (file_b, 20, "files/bin/wine"),
            ],
        )
        self.write_manifest(
            collection,
            other_digest,
            [
                (file_a, 10, "proton"),
                ("3" * 64, 20, "files/bin/wine"),
            ],
        )
        self.write_manifest(
            collection,
            stack_digest,
            [
                (
                    file_a,
                    10,
                    "engine/proton/Proton-Test/proton",
                ),
                (
                    file_b,
                    20,
                    "engine/proton/Proton-Test/files/bin/wine",
                ),
                ("4" * 64, 5, "engine/umu/tool"),
            ],
        )

        capsule.write_text(
            json.dumps(
                {
                    "capsule_id": "embedded-native",
                    "objects": [
                        {
                            "id": "game",
                            "roles": ["game_payload"],
                        },
                        {
                            "id": "stack",
                            "digest": stack_digest,
                            "roles": ["runner", "runtime", "tool"],
                        },
                    ],
                    "profiles": [
                        {
                            "id": "native-embedded",
                            "platform": "linux",
                            "adapter": "umu",
                            "dependencies": ["game", "stack"],
                            "umu": {
                                "schema": 0,
                                "layout": [
                                    {
                                        "object": "stack",
                                        "source": "engine",
                                        "destination": "engine",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        selected = SimpleNamespace(
            runner_id="Proton-Test",
            digest=selected_digest,
            source_root="Proton-Test",
        )
        other = SimpleNamespace(
            runner_id="Other-Proton",
            digest=other_digest,
            source_root="Proton-Test",
        )
        return capsule, selected, other

    def test_auto_accepts_byte_identical_embedded_runner(self) -> None:
        capsule, selected, _ = self.write_embedded_capsule()

        with patch(
            "offline_game_vault.composition.build_derived_capsule"
        ) as derive:
            profile = _select_source_profile(
                capsule,
                backend="umu",
                profile_id=None,
                runner=selected,  # type: ignore[arg-type]
            )

        self.assertEqual(profile, "native-embedded")
        derive.assert_not_called()

    def test_auto_rejects_different_embedded_runner(self) -> None:
        capsule, _, other = self.write_embedded_capsule()

        with self.assertRaisesRegex(
            CompositionError,
            "No Linux source profile can satisfy the requested UMU runner",
        ):
            _select_source_profile(
                capsule,
                backend="umu",
                profile_id=None,
                runner=other,  # type: ignore[arg-type]
            )

    def test_direct_binding_overrides_embedded_identity(self) -> None:
        capsule, embedded_runner, _ = self.write_embedded_capsule()
        payload = json.loads(capsule.read_text(encoding="utf-8"))

        direct_digest = "sha256:" + "9" * 64
        payload["objects"].append(
            {
                "id": "direct-runner",
                "digest": direct_digest,
                "roles": ["runner"],
            }
        )

        native = payload["profiles"][0]
        native["dependencies"].append("direct-runner")
        native["umu"]["layout"].append(
            {
                "object": "direct-runner",
                "source": "direct-runner",
                "destination": "engine/proton/Direct-Proton",
            }
        )

        payload["profiles"].append(
            {
                "id": "fallback-wine",
                "platform": "linux",
                "adapter": "wine",
                "dependencies": ["game"],
                "playable": {
                    "schema": 0,
                    "backend": "wine",
                },
            }
        )
        capsule.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        with patch(
            "offline_game_vault.composition.build_derived_capsule",
            return_value=object(),
        ) as derive:
            selected = _select_source_profile(
                capsule,
                backend="umu",
                profile_id=None,
                runner=embedded_runner,  # type: ignore[arg-type]
            )

        self.assertEqual(selected, "fallback-wine")
        derive.assert_called_once()


if __name__ == "__main__":
    unittest.main()
