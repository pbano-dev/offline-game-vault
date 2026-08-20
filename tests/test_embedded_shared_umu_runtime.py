from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import offline_game_vault.composition as composition


class EmbeddedSharedUmuRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.collection = Path(self.temporary.name)
        self.stack_digest = "sha256:" + "a" * 64
        hexdigest = self.stack_digest.removeprefix("sha256:")
        self.manifest = (
            self.collection
            / "01_IMMUTABLE_VAULT"
            / "manifests"
            / "sha256"
            / hexdigest[:2]
            / hexdigest[2:4]
            / hexdigest
        )
        self.manifest.parent.mkdir(parents=True)
        self.stack_declaration = {
            "id": "shared-stack",
            "digest": self.stack_digest,
            "archive_path": "objects/shared-stack",
            "format": "tar.zst",
            "roles": ["backend", "tool"],
            "required": True,
            "shared": True,
        }
        self.stack_index = {"role": "shared-umu-stack"}
        self.stack_physical = self.collection / "stack.tar.zst"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self, *, family: str = "steamrt3") -> None:
        prefix = f"engine/xdg-data/umu/{family}"
        self.manifest.write_text(
            "\n".join(
                [
                    "manifest_schema:0",
                    f"{'1' * 64} 1 {prefix}/VERSIONS.txt",
                    f"{'2' * 64} 1 {prefix}/_v2-entry-point",
                    f"{'3' * 64} 1 {prefix}/mtree.txt.gz",
                    (
                        f"{'4' * 64} 1 "
                        f"{prefix}/pressure-vessel/bin/pv-verify"
                    ),
                    (
                        f"{'5' * 64} 1 "
                        f"{prefix}/sniper_platform_test/files/bin/sh"
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def registered_components(
        self,
        _root: Path,
        *,
        role: str,
        roles: tuple[str, ...],
        kind: str,
    ):
        del roles, kind
        if role == "shared-umu-stack":
            return (
                (
                    self.stack_index,
                    self.stack_declaration,
                    self.stack_physical,
                ),
            )
        if role == "shared-umu-runtime":
            return ()
        raise AssertionError(role)

    @staticmethod
    def validate_backend(_declaration, _physical):
        return (
            "engine/umu-portable/umu-run-fully-local",
            (),
            "engine/python-portable",
        )

    @staticmethod
    def validate_runtime(index_item, _declaration, _physical):
        if (
            index_item.get("runtime_family") == "steamrt3"
            and index_item.get("archive_root")
            == "engine/xdg-data/umu/steamrt3"
        ):
            return (
                "steamrt3",
                "sniper",
                "sniper_platform_test",
                "engine/xdg-data/umu/steamrt3/var",
            )
        raise composition.CompositionError("not a complete runtime")

    def test_complete_runtime_inside_shared_stack_is_reusable(self) -> None:
        self.write_manifest()

        with (
            patch.object(
                composition,
                "_registered_global_components",
                side_effect=self.registered_components,
            ),
            patch.object(
                composition,
                "_validate_global_umu_backend",
                side_effect=self.validate_backend,
            ),
            patch.object(
                composition,
                "_validate_global_steam_runtime",
                side_effect=self.validate_runtime,
            ),
        ):
            runtimes = composition._scan_shared_umu_runtimes(
                self.collection
            )

        self.assertEqual(len(runtimes), 1)
        runtime = runtimes[0]
        self.assertEqual(runtime.runtime_family, "steamrt3")
        self.assertEqual(
            runtime.runtime_source,
            "engine/xdg-data/umu/steamrt3",
        )
        self.assertEqual(runtime.backend_object_id, "shared-stack")
        self.assertEqual(
            runtime.runtime_object_id,
            "shared-stack-embedded-steamrt3",
        )
        self.assertEqual(
            runtime.runtime_object["digest"],
            self.stack_digest,
        )
        self.assertIn("runtime", runtime.runtime_object["roles"])

        raw_digest = self.stack_digest.removeprefix("sha256:")
        legacy_identity = hashlib.sha256(
            f"{raw_digest}:{raw_digest}".encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(
            runtime.component_set_digest,
            legacy_identity,
        )

    def test_manifest_candidate_still_requires_runtime_validation(self) -> None:
        self.write_manifest()

        with (
            patch.object(
                composition,
                "_registered_global_components",
                side_effect=self.registered_components,
            ),
            patch.object(
                composition,
                "_validate_global_umu_backend",
                side_effect=self.validate_backend,
            ),
            patch.object(
                composition,
                "_validate_global_steam_runtime",
                side_effect=composition.CompositionError(
                    "incomplete embedded runtime"
                ),
            ),
        ):
            runtimes = composition._scan_shared_umu_runtimes(
                self.collection
            )

        self.assertEqual(runtimes, ())

    def test_unadvertised_manifest_subtree_is_not_invented(self) -> None:
        self.manifest.write_text(
            f"{'1' * 64} 1 engine/other/VERSIONS.txt\n",
            encoding="utf-8",
        )

        with (
            patch.object(
                composition,
                "_registered_global_components",
                side_effect=self.registered_components,
            ),
            patch.object(
                composition,
                "_validate_global_umu_backend",
                side_effect=self.validate_backend,
            ),
            patch.object(
                composition,
                "_validate_global_steam_runtime",
            ) as validate_runtime,
        ):
            runtimes = composition._scan_shared_umu_runtimes(
                self.collection
            )

        self.assertEqual(runtimes, ())
        validate_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
