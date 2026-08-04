from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from offline_game_vault.composition import (
    CompositionError,
    list_shared_umu_runtimes,
    compose_bottles,
    compose_umu,
    compose_wine,
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


class CompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._composition_path_patch = patch(
            "offline_game_vault.composition.require_bottles_managed_path",
            side_effect=lambda requested=None: Path(requested).resolve(),
        )
        self._adapter_path_patch = patch(
            "offline_game_vault.bottles_adapter.require_bottles_managed_path",
            side_effect=lambda requested=None: Path(requested).resolve(),
        )
        self._register_patch = patch(
            "offline_game_vault.bottles_adapter.assert_bottle_registered"
        )
        self._composition_path_patch.start()
        self._adapter_path_patch.start()
        self._register_patch.start()
        self.addCleanup(self._composition_path_patch.stop)
        self.addCleanup(self._adapter_path_patch.stop)
        self.addCleanup(self._register_patch.stop)
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
                "ge-proton/toolmanifest.vdf": (
                    b'"manifest"\n{\n'
                    b'  "require_tool_appid" "4183110"\n'
                    b'}\n',
                    0o644,
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
                },
                {
                    "id": "linux-bottles",
                    "platform": "linux",
                    "adapter": "bottles",
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

    def _add_umu_backend(
        self,
        *,
        family: str = "steamrt4",
        platform_prefix: str = "steamrt4",
    ) -> None:
        backend_archive = self.root / "umu-backend.tar.gz"
        _make_tar_gz(
            backend_archive,
            directories=(
                "engine",
                "engine/python-portable",
                "engine/python-portable/bin",
                "engine/umu-portable",
                "engine/umu-portable/bin",
            ),
            entries={
                "engine/python-portable/umu-run-fully-local": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
                "engine/umu-portable/bin/umu-run-portable": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
            },
        )

        platform_name = f"{platform_prefix}_platform_test"
        runtime_archive = self.root / f"{family}-runtime.tar.gz"
        _make_tar_gz(
            runtime_archive,
            directories=(
                family,
                f"{family}/var",
                f"{family}/pressure-vessel",
                f"{family}/pressure-vessel/bin",
                f"{family}/{platform_name}",
                f"{family}/{platform_name}/files",
            ),
            entries={
                f"{family}/VERSIONS.txt": (
                    f"{family}\ttest\n".encode("utf-8"),
                    0o644,
                ),
                f"{family}/_v2-entry-point": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
                f"{family}/mtree.txt.gz": (b"mtree", 0o644),
                f"{family}/pressure-vessel/bin/pv-verify": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
                f"{family}/{platform_name}/files/runtime.bin": (
                    b"runtime",
                    0o644,
                ),
                f"{family}/var/runtime-state": (
                    b"preserved runtime data",
                    0o644,
                ),
            },
        )

        self.backend_object = self._ingest(
            object_id="umu-backend",
            archive=backend_archive,
            roles=["backend", "tool"],
            shared=True,
        )
        self.runtime_object = self._ingest(
            object_id=f"{family}-runtime",
            archive=runtime_archive,
            roles=["runtime"],
            shared=True,
        )
        self._write_inventory()

        inventory_path = self.immutable / "VAULT_INVENTORY.json"
        inventory = json.loads(
            inventory_path.read_text(encoding="utf-8")
        )
        inventory_objects = inventory.get("objects")
        self.assertIsInstance(inventory_objects, list)

        for declaration in (
            self.backend_object,
            self.runtime_object,
        ):
            digest = declaration["digest"]
            inventory_objects[:] = [
                item
                for item in inventory_objects
                if not (
                    isinstance(item, dict)
                    and item.get("digest") == digest
                )
            ]
            inventory_objects.append(
                {
                    "bytes": declaration["size"],
                    "digest": declaration["digest"],
                    "path": declaration["archive_path"],
                }
            )

        inventory_path.write_text(
            json.dumps(inventory),
            encoding="utf-8",
        )

        index_path = self.collection / "INDEX.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        objects = index.get("objects")
        self.assertIsInstance(objects, list)
        objects.extend(
            [
                {
                    "capsule_object_id": self.backend_object["id"],
                    "label": backend_archive.name,
                    "path": self.backend_object["archive_path"],
                    "role": "shared-umu-stack",
                    "sha256": self.backend_object[
                        "digest"
                    ].removeprefix("sha256:"),
                    "size": self.backend_object["size"],
                },
                {
                    "archive_root": family,
                    "component_id": self.runtime_object["id"],
                    "label": runtime_archive.name,
                    "path": self.runtime_object["archive_path"],
                    "role": "shared-umu-runtime",
                    "runtime_family": family,
                    "runtime_version": "test",
                    "sha256": self.runtime_object[
                        "digest"
                    ].removeprefix("sha256:"),
                    "size": self.runtime_object["size"],
                },
            ]
        )
        index_path.write_text(
            json.dumps(index),
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

    def test_direct_wine_materializes_from_neutral_source(self) -> None:
        destination = self.root / "direct-wine"
        result = compose_wine(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            destination=destination,
        )

        self.assertTrue(result.materialized)
        self.assertEqual(result.backend, "direct-wine")
        for name in ("JUGAR.sh", "VERIFICAR.sh", "DESINSTALAR.sh"):
            path = destination / name
            self.assertTrue(path.is_file())
            self.assertTrue(path.stat().st_mode & 0o100)
        verified_script = subprocess.run(
            [str(destination / "VERIFICAR.sh"), "--json"],
            cwd=destination,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(verified_script.returncode, 0, verified_script.stderr)
        played_script = subprocess.run(
            [str(destination / "JUGAR.sh"), "--json"],
            cwd=destination,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(played_script.returncode, 0, played_script.stderr)
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
        self.assertNotIn("composition_composition", receipt)
        self.assertNotIn("profile_status", receipt)

    def test_bottles_installs_and_reuses_only_verified_vault_runner(self) -> None:
        bottles = self.root / "components/bottles"
        bottles.mkdir(parents=True)
        first = compose_bottles(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            bottles_path=bottles,
            bottle_name="First",
        )
        self.assertTrue(first.materialized)
        first_root = bottles / "First"
        for name in ("JUGAR.sh", "VERIFICAR.sh", "DESINSTALAR.sh"):
            path = first_root / name
            self.assertTrue(path.is_file())
            self.assertTrue(path.stat().st_mode & 0o100)
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        flatpak = fake_bin / "flatpak"
        flatpak.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *\"info bottles-path\"*) "
            f"printf '%s\\n' '{bottles}' ;;\n"
            "  *\"list bottles\"*) "
            "printf '%s\\n' '{\"bottles\":[\"First\"]}' ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        flatpak.chmod(0o755)
        environment = dict(os.environ)
        environment["PATH"] = str(fake_bin) + os.pathsep + environment.get(
            "PATH", ""
        )
        verified_script = subprocess.run(
            [str(first_root / "VERIFICAR.sh"), "--json"],
            cwd=first_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(verified_script.returncode, 0, verified_script.stderr)
        played_script = subprocess.run(
            [str(first_root / "JUGAR.sh"), "--json"],
            cwd=first_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(played_script.returncode, 0, played_script.stderr)
        runner_root = self.root / "components/runners/ge-proton"
        self.assertTrue(
            (runner_root / ".ogv-preserved-runner.json").is_file()
        )

        second = compose_bottles(
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
            CompositionError,
            "differs from the preserved Vault object",
        ):
            compose_bottles(
                collection_root=self.collection,
                capsule_path=self.capsule_path,
                runner_id="ge-proton",
                bottles_path=bottles,
                bottle_name="Third",
            )

    def test_bottles_heavy_workspace_is_created_under_selected_path(self) -> None:
        bottles = self.root / "components/bottles"
        bottles.mkdir(parents=True)
        real_temporary_directory = tempfile.TemporaryDirectory
        parents: list[Path | None] = []

        def tracked(*args: object, **kwargs: object):
            raw = kwargs.get("dir")
            parents.append(Path(raw) if raw is not None else None)
            return real_temporary_directory(*args, **kwargs)

        with patch(
            "offline_game_vault.composition.tempfile.TemporaryDirectory",
            side_effect=tracked,
        ):
            result = compose_bottles(
                collection_root=self.collection,
                capsule_path=self.capsule_path,
                runner_id="ge-proton",
                bottles_path=bottles,
                bottle_name="StagingTest",
            )

        self.assertTrue(result.materialized)
        self.assertEqual(parents, [bottles.resolve()])
        self.assertFalse(
            any(path.name.startswith(".ogv-work-") for path in bottles.iterdir())
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
        result = compose_bottles(
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
        self.assertNotIn("composition_composition", receipt)

    def test_direct_wine_synthesizes_from_bottles_neutral_source(self) -> None:
        self.capsule["profiles"] = [
            profile
            for profile in self.capsule["profiles"]
            if profile["id"] == "linux-bottles"
        ]
        self._write_capsule()

        destination = self.root / "direct-from-bottles"
        result = compose_wine(
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
        result = compose_umu(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            destination=destination,
        )

        self.assertTrue(result.materialized)
        self.assertTrue(
            (destination / "engine/proton/ge-proton/proton").is_file()
        )

    def test_umu_runtime_catalog_rejects_unregistered_backend(
        self,
    ) -> None:
        self._add_umu_backend()
        index_path = self.collection / "INDEX.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for item in index["objects"]:
            if item.get("role") == "shared-umu-stack":
                item["role"] = "backend"
        index_path.write_text(
            json.dumps(index),
            encoding="utf-8",
        )

        self.assertEqual(
            list_shared_umu_runtimes(self.collection),
            (),
        )
        with self.assertRaisesRegex(
            CompositionError,
            "no matching global UMU component composition",
        ):
            compose_umu(
                collection_root=self.collection,
                capsule_path=self.capsule_path,
                runner_id="ge-proton",
                destination=self.root / "umu-rejected",
            )
    def test_umu_runner_rejects_a_complete_but_wrong_runtime_family(self) -> None:
        self._add_umu_backend(
            family="steamrt3",
            platform_prefix="sniper",
        )
        runtimes = list_shared_umu_runtimes(self.collection)
        self.assertEqual(len(runtimes), 1)
        self.assertEqual(runtimes[0].runtime_family, "steamrt3")
        with self.assertRaisesRegex(
            CompositionError,
            r"requires steamrt4 .*no matching global UMU component composition",
        ):
            compose_umu(
                collection_root=self.collection,
                capsule_path=self.capsule_path,
                runner_id="ge-proton",
                destination=self.root / "umu-wrong-family",
            )

    def test_umu_materializes_from_preserved_backend_and_proton(
        self,
    ) -> None:
        self._add_umu_backend()

        component_sets = list_shared_umu_runtimes(
            self.collection
        )

        self.assertEqual(
            len(component_sets),
            1,
        )

        component_set = component_sets[0]

        self.assertTrue(
            component_set.component_set_id.startswith(
                "umu-component-set-"
            )
        )

        self.assertEqual(
            component_set.backend_object_id,
            "umu-backend",
        )

        self.assertEqual(
            component_set.runtime_object_id,
            "steamrt4-runtime",
        )

        self.assertEqual(
            component_set.backend_entrypoint,
            (
                "engine/python-portable/"
                "umu-run-fully-local"
            ),
        )

        self.assertEqual(
            component_set.backend_entrypoint_arguments,
            (),
        )

        self.assertIsNone(
            component_set.backend_pythonpath
        )

        self.assertEqual(
            component_set.runtime_var,
            "engine/xdg-data/umu/steamrt4/var",
        )

        destination = self.root / "umu"

        result = compose_umu(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton",
            destination=destination,
        )

        self.assertTrue(
            result.materialized
        )

        runtime_state = (
            destination
            / "engine"
            / "xdg-data"
            / "umu"
            / "steamrt4"
            / "var"
            / "runtime-state"
        )

        self.assertEqual(
            runtime_state.read_bytes(),
            b"preserved runtime data",
        )

        for name in (
            "JUGAR.sh",
            "VERIFICAR.sh",
            "DESINSTALAR.sh",
        ):
            path = destination / name

            self.assertTrue(
                path.is_file()
            )

            self.assertTrue(
                path.stat().st_mode & 0o100
            )

        self.assertTrue(
            (
                destination
                / "engine"
                / "proton"
                / "ge-proton"
                / "proton"
            ).is_file()
        )

        launcher = (
            destination
            / "launchers"
            / "JUGAR_UMU.sh"
        )

        self.assertTrue(
            launcher.is_file()
        )

        launcher_text = launcher.read_text(
            encoding="utf-8"
        )

        self.assertIn(
            (
                'UMU_ENTRYPOINT="$ROOT/'
                'engine/python-portable/'
                'umu-run-fully-local"'
            ),
            launcher_text,
        )

        self.assertNotIn(
            "find ",
            launcher_text,
        )

        self.assertNotIn(
            "composition composition",
            launcher_text,
        )

        launched = subprocess.run(
            [
                str(launcher),
            ],
            cwd=destination,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(
            launched.returncode,
            0,
            launched.stderr,
        )

        mutable_marker = (
            destination
            / "engine"
            / "proton"
            / "ge-proton"
            / "files"
            / "steampipe_fixups_mtime"
        )

        mutable_marker.write_text(
            "generated\n",
            encoding="utf-8",
        )

        sanitized = subprocess.run(
            [
                str(
                    destination
                    / "launchers"
                    / "sanear_umu.sh"
                ),
            ],
            cwd=destination,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(
            sanitized.returncode,
            0,
            sanitized.stderr,
        )

        self.assertFalse(
            mutable_marker.exists()
        )

        self.assertEqual(
            runtime_state.read_bytes(),
            b"preserved runtime data",
        )

        verified = subprocess.run(
            [
                str(
                    destination
                    / "VERIFICAR.sh"
                ),
            ],
            cwd=destination,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(
            verified.returncode,
            0,
            verified.stderr,
        )

        self.assertEqual(
            runtime_state.read_bytes(),
            b"preserved runtime data",
        )

        backend_result = result.backend_result

        self.assertEqual(
            backend_result[
                "component_set_id"
            ],
            component_set.component_set_id,
        )

        self.assertEqual(
            backend_result[
                "backend_component_id"
            ],
            "umu-backend",
        )

        self.assertEqual(
            backend_result[
                "runtime_component_id"
            ],
            "steamrt4-runtime",
        )

        self.assertEqual(
            backend_result[
                "backend_entrypoint"
            ],
            (
                "engine/python-portable/"
                "umu-run-fully-local"
            ),
        )

        self.assertNotIn(
            "shared_runtime_id",
            backend_result,
        )

        receipt = json.loads(
            (
                destination
                / "umu-materialization.json"
            ).read_text(
                encoding="utf-8"
            )
        )

        self.assertNotIn(
            "composition_composition",
            receipt,
        )

        self.assertTrue(
            receipt[
                "initial_verification"
            ][
                "runtime_var_preserved"
            ]
        )

        self.assertNotIn(
            "runtime_var_empty",
            receipt[
                "initial_verification"
            ],
        )
if __name__ == "__main__":
    unittest.main()
