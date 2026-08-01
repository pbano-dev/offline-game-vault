from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.experimental import (
    ExperimentalVariantError,
    list_umu_templates,
    materialize_experimental_bottles,
    materialize_experimental_umu,
    materialize_experimental_wine,
)
from offline_game_vault.preserved_runners import (
    RunnerCatalogError,
    scan_runners,
)
from offline_game_vault.storage import (
    canonical_object_path,
    ingest_object,
)
from offline_game_vault.verifier import ObjectSpec


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive_path(digest: str) -> str:
    value = digest.removeprefix("sha256:")
    return f"objects/sha256/{value[:2]}/{value[2:4]}/{value}"


def _make_tar_gz(
    path: Path,
    *,
    entries: dict[str, tuple[bytes, int]],
    directories: tuple[str, ...] = (),
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for directory in directories:
            member = tarfile.TarInfo(directory)
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        for name, (payload, mode) in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = mode
            archive.addfile(member, io.BytesIO(payload))


class ExperimentalVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.collection = self.root / "collection"
        self.immutable = self.collection / "01_IMMUTABLE_VAULT"
        self.immutable.mkdir(parents=True)
        self.capsule_root = self.collection / "02_CAPSULES/game"
        self.capsule_root.mkdir(parents=True)
        (self.collection / "04_RECEIPTS").mkdir()

        self.game_payload = b"synthetic-game"
        self.game_archive = self.root / "game.tar.gz"
        _make_tar_gz(
            self.game_archive,
            directories=(
                "neutral-object",
                "neutral-object/payload",
                "neutral-object/payload/game",
                "neutral-object/payload/game/EP9",
                "neutral-object/payload/game/EP9/TSBin",
                "neutral-object/payload/prefix-template",
            ),
            entries={
                "neutral-object/payload/game/EP9/TSBin/game.exe": (
                    self.game_payload,
                    0o644,
                )
            },
        )

        self.runner_archive = self.root / "ge-proton.tar.gz"
        _make_tar_gz(
            self.runner_archive,
            directories=(
                "ge-proton",
                "ge-proton/files",
                "ge-proton/files/bin",
            ),
            entries={
                "ge-proton/files/bin/wine": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
                "ge-proton/files/bin/wineserver": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
                "ge-proton/proton": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
            },
        )

        self.game_object = self._ingest(
            object_id="game-baseline",
            archive=self.game_archive,
            roles=["game_payload", "prefix_baseline"],
            shared=False,
        )
        self.runner_object = self._ingest(
            object_id="ge-proton",
            archive=self.runner_archive,
            roles=["runner"],
            shared=True,
        )
        self._write_inventory()
        self._write_index()

        contracts = self.capsule_root / "host-contracts"
        contracts.mkdir()
        common = {
            "schema": 0,
            "source_object": "game-baseline",
            "neutral_root": "neutral-object",
            "prefix_source": "neutral-object/payload/prefix-template",
            "game_source": "neutral-object/payload/game",
            "game_destination_in_prefix": "drive_c/Games/game",
            "entrypoint_relative_to_game": "EP9/TSBin/game.exe",
            "working_directory_in_prefix":
                "drive_c/Games/game/EP9/TSBin",
            "baseline_state": "clean",
            "runner_binding": "select-at-materialization",
            "preferred_runner": None,
        }
        protected = [
            {
                "path": (
                    "prefix/drive_c/Games/game/"
                    "EP9/TSBin/game.exe"
                ),
                "digest": (
                    "sha256:"
                    + hashlib.sha256(self.game_payload).hexdigest()
                ),
                "size": len(self.game_payload),
            }
        ]
        (contracts / "linux-direct-wine.json").write_text(
            json.dumps(
                {
                    **common,
                    "contract": "ogv-direct-wine-neutral-v1",
                    "runtime_directory": "runtime",
                    "launcher": "JUGAR.sh",
                    "uninstaller": "RETIRAR.sh",
                    "protected_files": protected,
                    "network": "host_default",
                }
            ),
            encoding="utf-8",
        )
        (contracts / "linux-bottles.json").write_text(
            json.dumps(
                {
                    **common,
                    "contract": "ogv-bottles-neutral-v1",
                    "flatpak_app": "com.usebottles.bottles",
                    "bottle_yml_policy": "template-or-generate-derived",
                    "bottle_yml_template":
                        "evidence/source-bottles/bottle.yml",
                    "network": "isolated",
                }
            ),
            encoding="utf-8",
        )
        evidence = self.capsule_root / "evidence"
        evidence.mkdir()
        (evidence / "protected-files.json").write_text(
            json.dumps({"schema": 0, "items": protected}),
            encoding="utf-8",
        )

        self.capsule = {
            "schema": 0,
            "capsule_id": "game",
            "sanitized_fixture": False,
            "game": {
                "title": "Synthetic",
                "source_store": "Steam",
                "preserved_version": "1",
            },
            "documents": {},
            "objects": [self.game_object],
            "persistent_state": [],
            "profiles": [
                {
                    "id": "linux-direct-wine",
                    "platform": "linux",
                    "adapter": "wine",
                    "status": "unavailable",
                    "dependencies": ["game-baseline"],
                    "host_contract":
                        "host-contracts/linux-direct-wine.json",
                    "launch": {
                        "entrypoint": (
                            "prefix/drive_c/Games/game/"
                            "EP9/TSBin/game.exe"
                        ),
                        "working_directory": (
                            "prefix/drive_c/Games/game/EP9/TSBin"
                        ),
                        "arguments": [],
                        "network": "host_default",
                    },
                    "notes": "Descriptive status only.",
                },
                {
                    "id": "linux-bottles",
                    "platform": "linux",
                    "adapter": "bottles",
                    "status": "not_tested",
                    "dependencies": ["game-baseline"],
                    "host_contract":
                        "host-contracts/linux-bottles.json",
                    "launch": {
                        "entrypoint": (
                            "drive_c/Games/game/EP9/TSBin/game.exe"
                        ),
                        "working_directory": (
                            "drive_c/Games/game/EP9/TSBin"
                        ),
                        "arguments": [],
                        "network": "isolated",
                    },
                    "notes": "Descriptive status only.",
                },
            ],
        }
        self.capsule_path = self.capsule_root / "capsule.json"
        self._write_capsule()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest(
        self,
        *,
        object_id: str,
        archive: Path,
        roles: list[str],
        shared: bool,
    ) -> dict[str, object]:
        digest = "sha256:" + _sha256(archive)
        destination = canonical_object_path(self.immutable, digest)
        ingest_object(
            source=archive,
            destination_spec=ObjectSpec(
                object_id=object_id,
                path=destination,
                expected_digest=digest,
                expected_size=archive.stat().st_size,
                vault_root=self.immutable.resolve(),
            ),
        )
        return {
            "id": object_id,
            "digest": digest,
            "roles": roles,
            "format": "tar.gz",
            "required": True,
            "archive_path": _archive_path(digest),
            "shared": shared,
            "size": archive.stat().st_size,
        }

    def _write_inventory(self) -> None:
        objects = [self.game_object, self.runner_object]
        if hasattr(self, "backend_object"):
            objects.append(self.backend_object)
        (self.immutable / "VAULT_INVENTORY.json").write_text(
            json.dumps(
                {
                    "schema": 0,
                    "objects": [
                        {
                            "digest": item["digest"],
                            "path": item["archive_path"],
                            "bytes": item["size"],
                        }
                        for item in objects
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _write_index(self) -> None:
        (self.collection / "INDEX.json").write_text(
            json.dumps(
                {
                    "schema": 0,
                    "objects": [
                        {
                            "label": "ge-proton.tar.gz",
                            "path": self.runner_object["archive_path"],
                            "role": "shared-runner",
                            "sha256": str(
                                self.runner_object["digest"]
                            ).removeprefix("sha256:"),
                            "size": self.runner_object["size"],
                        }
                    ],
                    "capsules": [],
                }
            ),
            encoding="utf-8",
        )

    def _write_capsule(self) -> None:
        self.capsule_path.write_text(
            json.dumps(self.capsule),
            encoding="utf-8",
        )

    def _add_umu_backend(self) -> None:
        archive = self.root / "umu-backend.tar.gz"
        _make_tar_gz(
            archive,
            directories=(
                "engine",
                "engine/python-portable",
                "engine/python-portable/bin",
                "engine/umu-portable",
                "engine/umu-portable/bin",
                "engine/xdg-data",
                "engine/xdg-data/SteamLinuxRuntime_sniper",
                "engine/xdg-data/SteamLinuxRuntime_sniper/var",
            ),
            entries={
                "engine/python-portable/bin/python3": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
                "engine/umu-portable/bin/umu-run": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
                "engine/xdg-data/SteamLinuxRuntime_sniper/tool": (
                    b"runtime",
                    0o644,
                ),
            },
        )
        self.backend_object = self._ingest(
            object_id="umu-stack",
            archive=archive,
            roles=["runtime", "backend", "tool"],
            shared=True,
        )
        self._write_inventory()

        template = self.collection / "02_CAPSULES/umu-template"
        template.mkdir()
        (template / "host.json").write_text("{}\n", encoding="utf-8")
        (template / "capsule.json").write_text(
            json.dumps(
                {
                    "schema": 0,
                    "capsule_id": "umu-template",
                    "sanitized_fixture": False,
                    "game": {
                        "title": "UMU backend template",
                        "source_store": "Test",
                        "preserved_version": "1",
                    },
                    "documents": {},
                    "objects": [self.backend_object],
                    "persistent_state": [],
                    "profiles": [
                        {
                            "id": "linux-umu",
                            "platform": "linux",
                            "adapter": "umu",
                            "status": "verified",
                            "dependencies": ["umu-stack"],
                            "host_contract": "host.json",
                            "launch": {
                                "entrypoint": "unused",
                                "network": "host_default",
                            },
                            "umu": {
                                "schema": 0,
                                "layout": [
                                    {
                                        "object": "umu-stack",
                                        "source": "engine",
                                        "destination": "engine",
                                    }
                                ],
                                "allowed_absolute_symlinks": [],
                                "nested_archives": [],
                                "state_archives": [],
                                "launchers": [],
                                "protected_manifests": [],
                                "symlink_manifests": [],
                                "mutable_paths": [],
                                "paths": {
                                    "launcher": "unused",
                                    "sanitizer": "unused",
                                    "runtime_var": (
                                        "engine/xdg-data/"
                                        "SteamLinuxRuntime_sniper/var"
                                    ),
                                },
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_runner_catalog_hashes_and_classifies_proton(self) -> None:
        runners, warnings = scan_runners(self.collection)
        self.assertEqual(warnings, ())
        self.assertEqual(len(runners), 1)
        self.assertEqual(runners[0].runner_id, "ge-proton")
        self.assertEqual(runners[0].kind, "proton")
        self.assertEqual(
            runners[0].compatible_backends,
            ("direct-wine", "bottles", "umu"),
        )

    def test_runner_catalog_rejects_same_size_tampering(self) -> None:
        physical = self.immutable / self.runner_object["archive_path"]
        payload = bytearray(physical.read_bytes())
        payload[-1] ^= 1
        physical.write_bytes(payload)

        with self.assertRaisesRegex(
            RunnerCatalogError,
            "physical SHA-256 differs",
        ):
            scan_runners(self.collection)

    def test_direct_wine_materializes_even_when_source_is_unavailable(self) -> None:
        destination = self.root / "direct-wine"
        result = materialize_experimental_wine(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            destination=destination,
        )

        self.assertTrue(result.materialized)
        self.assertFalse(result.acceptance_inherited)
        self.assertEqual(result.backend, "direct-wine")
        self.assertEqual(
            (
                destination
                / "source/payload/game/EP9/TSBin/game.exe"
            ).read_bytes(),
            self.game_payload,
        )
        receipt = json.loads(
            (destination / "playable-materialization.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            receipt["experimental_variant"]["kind"],
            "experimental",
        )
        self.assertFalse(
            receipt["experimental_variant"]["acceptance_inherited"]
        )

    def test_bottles_installs_and_reuses_only_verified_vault_runner(self) -> None:
        bottles = self.root / "components/bottles"
        bottles.mkdir(parents=True)
        first = materialize_experimental_bottles(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            bottles_path=bottles,
            bottle_name="First",
        )
        self.assertTrue(first.materialized)
        runner_root = self.root / "components/runners/ge-proton"
        self.assertTrue(
            (runner_root / ".ogv-preserved-runner.json").is_file()
        )

        second = materialize_experimental_bottles(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            bottles_path=bottles,
            bottle_name="Second",
        )
        self.assertTrue(second.materialized)
        self.assertFalse(second.backend_result["runner_installed"])

        wine = runner_root / "files/bin/wine"
        wine.write_bytes(wine.read_bytes() + b"tampered")
        with self.assertRaisesRegex(
            ExperimentalVariantError,
            "differs from the preserved Vault object",
        ):
            materialize_experimental_bottles(
                collection_root=self.collection,
                capsule_path=self.capsule_path,
                runner_id="ge-proton",
                bottles_path=bottles,
                bottle_name="Third",
            )

    def test_bottles_synthesizes_from_direct_wine_neutral_source(self) -> None:
        self.capsule["profiles"] = [
            profile
            for profile in self.capsule["profiles"]
            if profile["id"] == "linux-direct-wine"
        ]
        self._write_capsule()

        bottles = self.root / "components/bottles"
        bottles.mkdir(parents=True)
        result = materialize_experimental_bottles(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            bottles_path=bottles,
            bottle_name="Synthesized",
        )

        self.assertTrue(result.materialized)
        receipt = json.loads(
            (
                bottles
                / "Synthesized"
                / ".ogv-bottles-deployment.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            receipt["experimental_variant"]["source_profile_id"],
            "linux-direct-wine",
        )

    def test_direct_wine_synthesizes_from_bottles_neutral_source(self) -> None:
        self.capsule["profiles"] = [
            profile
            for profile in self.capsule["profiles"]
            if profile["id"] == "linux-bottles"
        ]
        self._write_capsule()

        destination = self.root / "direct-from-bottles"
        result = materialize_experimental_wine(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            destination=destination,
        )

        self.assertTrue(result.materialized)
        self.assertEqual(
            (
                destination
                / "source/payload/game/EP9/TSBin/game.exe"
            ).read_bytes(),
            self.game_payload,
        )

    def test_umu_synthesizes_from_bottles_neutral_source(self) -> None:
        self.capsule["profiles"] = [
            profile
            for profile in self.capsule["profiles"]
            if profile["id"] == "linux-bottles"
        ]
        self._write_capsule()
        self._add_umu_backend()

        destination = self.root / "umu-from-bottles"
        result = materialize_experimental_umu(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            destination=destination,
            backend_template_id="umu-template/linux-umu",
        )

        self.assertTrue(result.materialized)
        self.assertTrue(
            (destination / "engine/proton/ge-proton/proton").is_file()
        )

    def test_umu_materializes_from_preserved_backend_and_proton(self) -> None:
        self._add_umu_backend()
        templates = list_umu_templates(self.collection)
        self.assertEqual(
            [item.template_id for item in templates],
            ["umu-template/linux-umu"],
        )

        destination = self.root / "umu"
        result = materialize_experimental_umu(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            destination=destination,
            backend_template_id="umu-template/linux-umu",
        )

        self.assertTrue(result.materialized)
        self.assertFalse(result.acceptance_inherited)
        self.assertTrue(
            (destination / "engine/proton/ge-proton/proton").is_file()
        )
        self.assertTrue(
            (
                destination
                / "launchers/JUGAR_UMU_EXPERIMENTAL.sh"
            ).is_file()
        )
        receipt = json.loads(
            (destination / "umu-materialization.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            receipt["experimental_variant"]["backend"],
            "umu",
        )


if __name__ == "__main__":
    unittest.main()
