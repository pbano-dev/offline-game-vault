"""Bottles must accept historical playable-wine capsules.

Sekiro, Dark Souls III and Dark Souls Remastered are preserved as full Bottles
archives whose single top-level directory is the Wine prefix, with the game
already installed inside it. Those capsules declare a playable Wine profile and
no neutral host contract.

Direct-Wine and UMU already read that recipe. Bottles refused it, which is a
composition restriction based on the shape of historical evidence rather than
on a technical fact, and therefore contradicts ADR 0015 and ADR 0016.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from offline_game_vault.composition import (
    CompositionError,
    _neutral_fields_from_playable,
    _select_source_profile,
    compose_bottles,
)
from offline_game_vault.storage import ObjectSpec, canonical_object_path, ingest_object
from offline_game_vault.object_manifest import (
    detect_source_root,
    generate_object_manifest,
    manifest_path,
    write_manifest_atomically,
)


GAME_PAYLOAD = b"synthetic-sekiro-executable"
SAVE_PAYLOAD = b"synthetic-preserved-registry"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_tar_gz(
    path: Path,
    *,
    directories: tuple[str, ...],
    entries: dict[str, tuple[bytes, int]],
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name in directories:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, (payload, mode) in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = mode
            import io

            archive.addfile(info, io.BytesIO(payload))


class HistoricalPlayableBottlesTest(unittest.TestCase):
    """End-to-end composition from a legacy full-prefix archive."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

        for target in (
            "offline_game_vault.composition.require_bottles_managed_path",
            "offline_game_vault.bottles_adapter.require_bottles_managed_path",
        ):
            patcher = patch(
                target,
                side_effect=lambda requested=None: Path(requested).resolve(),
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        registered = patch(
            "offline_game_vault.bottles_adapter.assert_bottle_registered"
        )
        registered.start()
        self.addCleanup(registered.stop)

        self.collection = self.root / "collection"
        self.immutable = self.collection / "01_IMMUTABLE_VAULT"
        self.immutable.mkdir(parents=True)
        (self.collection / "04_RECEIPTS").mkdir()
        self.capsule_root = self.collection / "02_CAPSULES/sekiro"
        self.capsule_root.mkdir(parents=True)
        self.capsule_path = self.capsule_root / "capsule.json"

        # A historical Bottles Full Archive: the archive root IS the prefix and
        # the game already sits at drive_c/Games/Sekiro inside it.
        game_archive = self.root / "sekiro-bottle-baseline.tar.gz"
        _make_tar_gz(
            game_archive,
            directories=(
                "Sekiro",
                "Sekiro/drive_c",
                "Sekiro/drive_c/users",
                "Sekiro/drive_c/Games",
                "Sekiro/drive_c/Games/Sekiro",
            ),
            entries={
                "Sekiro/drive_c/Games/Sekiro/sekiro.exe.unpacked.exe": (
                    GAME_PAYLOAD,
                    0o755,
                ),
                "Sekiro/system.reg": (SAVE_PAYLOAD, 0o644),
            },
        )
        runner_archive = self.root / "ge-proton11-1.tar.gz"
        _make_tar_gz(
            runner_archive,
            directories=(
                "ge-proton11-1",
                "ge-proton11-1/files",
                "ge-proton11-1/files/bin",
            ),
            entries={
                "ge-proton11-1/files/bin/wine": (b"#!/bin/sh\nexit 0\n", 0o755),
                "ge-proton11-1/files/bin/wineserver": (
                    b"#!/bin/sh\nexit 0\n",
                    0o755,
                ),
            },
        )

        self.game_object = self._ingest(
            object_id="sekiro-bottle-baseline",
            archive=game_archive,
            roles=["game_payload", "prefix_baseline"],
            shared=False,
        )
        self.runner_object = self._ingest(
            object_id="ge-proton11-1",
            archive=runner_archive,
            roles=["runner"],
            shared=True,
        )
        self._write_inventory()
        self._write_index()
        self._write_capsule()

    def _ingest(
        self,
        *,
        object_id: str,
        archive: Path,
        roles: list[str],
        shared: bool,
    ) -> dict[str, object]:
        digest = "sha256:" + _sha256(archive)
        raw = digest.removeprefix("sha256:")
        ingest_object(
            source=archive,
            destination_spec=ObjectSpec(
                object_id=object_id,
                path=canonical_object_path(self.immutable, digest),
                expected_digest=digest,
                expected_size=archive.stat().st_size,
                vault_root=self.immutable.resolve(),
            ),
        )
        # Fase 4 pre-flight requires a valid manifest for every object
        # in the operational capsule; test fixtures generate it here.
        _manifest = generate_object_manifest(
            archive=canonical_object_path(self.immutable, digest),
            archive_format="tar.gz",
            source_root=detect_source_root(
                canonical_object_path(self.immutable, digest),
                "tar.gz",
            ),
            object_digest=digest,
            object_size=archive.stat().st_size,
        )
        write_manifest_atomically(
            _manifest,
            manifest_path(self.immutable, digest),
        )
        return {
            "id": object_id,
            "digest": digest,
            "roles": roles,
            "format": "tar.gz",
            "required": True,
            "archive_path": f"objects/sha256/{raw[:2]}/{raw[2:4]}/{raw}",
            "shared": shared,
            "size": archive.stat().st_size,
        }

    def _write_inventory(self) -> None:
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
                        for item in (self.game_object, self.runner_object)
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
                    "capsules": [],
                    "objects": [
                        {
                            "label": "ge-proton11-1.tar.gz",
                            "path": self.runner_object["archive_path"],
                            "role": "shared-runner",
                            "sha256": str(
                                self.runner_object["digest"]
                            ).removeprefix("sha256:"),
                            "size": self.runner_object["size"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _write_capsule(self) -> None:
        # Mirrors fixtures/sekiro-shadows-die-twice: a Bottles profile with a
        # legacy host contract, and a Direct-Wine profile carrying the playable
        # layout. No neutral contract exists anywhere in the capsule.
        (self.capsule_root / "host-contract.linux-bottles.json").write_text(
            json.dumps(
                {
                    "schema": 0,
                    "contract_id": "linux-x86_64-bottles-flatpak-sekiro",
                    "platform": "linux",
                }
            ),
            encoding="utf-8",
        )
        (self.capsule_root / "host-contract.linux-direct-wine.json").write_text(
            json.dumps(
                {
                    "schema": 0,
                    "contract_id": "linux-x86_64-direct-wine-sekiro-verified",
                    "platform": "linux",
                }
            ),
            encoding="utf-8",
        )
        self.capsule_path.write_text(
            json.dumps(
                {
                    "schema": 0,
                    "capsule_id": "steam-814380-sekiro-shadows-die-twice-1.06",
                    "objects": [self.game_object, self.runner_object],
                    "profiles": [
                        {
                            "id": "linux-bottles-flatpak",
                            "platform": "linux",
                            "adapter": "bottles",
                            "dependencies": [
                                "sekiro-bottle-baseline",
                                "ge-proton11-1",
                            ],
                            "host_contract": "host-contract.linux-bottles.json",
                            "launch": {
                                "entrypoint": (
                                    "drive_c/Games/Sekiro/"
                                    "sekiro.exe.unpacked.exe"
                                ),
                                "working_directory": "drive_c/Games/Sekiro",
                                "arguments": [],
                                "network": "isolated",
                            },
                        },
                        {
                            "id": "linux-direct-wine",
                            "platform": "linux",
                            "adapter": "wine",
                            "dependencies": [
                                "sekiro-bottle-baseline",
                                "ge-proton11-1",
                            ],
                            "host_contract": (
                                "host-contract.linux-direct-wine.json"
                            ),
                            "launch": {
                                "entrypoint": (
                                    "prefix/drive_c/Games/Sekiro/"
                                    "sekiro.exe.unpacked.exe"
                                ),
                                "working_directory": (
                                    "prefix/drive_c/Games/Sekiro"
                                ),
                                "arguments": [],
                                "environment": {"WINEDEBUG": "-all"},
                                "network": "host_default",
                            },
                            "playable": {
                                "schema": 0,
                                "backend": "wine",
                                "layout": [
                                    {
                                        "object": "sekiro-bottle-baseline",
                                        "source": "Sekiro",
                                        "destination": "prefix",
                                    },
                                    {
                                        "object": "ge-proton11-1",
                                        "source": "ge-proton11-1",
                                        "destination": "runner/ge-proton11-1",
                                    },
                                ],
                                "paths": {
                                    "prefix": "prefix",
                                    "runner": "runner/ge-proton11-1",
                                    "runtime": "runtime",
                                    "launcher": "jugar_sekiro.sh",
                                    "uninstaller": "desinstalar_sekiro.sh",
                                    "wine": (
                                        "runner/ge-proton11-1/files/bin/wine"
                                    ),
                                    "wineserver": (
                                        "runner/ge-proton11-1/files/bin/"
                                        "wineserver"
                                    ),
                                },
                                "protected_files": [
                                    {
                                        "path": (
                                            "prefix/drive_c/Games/Sekiro/"
                                            "sekiro.exe.unpacked.exe"
                                        ),
                                        "digest": "sha256:"
                                        + hashlib.sha256(
                                            GAME_PAYLOAD
                                        ).hexdigest(),
                                        "size": len(GAME_PAYLOAD),
                                    }
                                ],
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_bottles_selects_the_historical_playable_source(self) -> None:
        selected = _select_source_profile(
            self.capsule_path,
            backend="bottles",
            profile_id=None,
        )
        self.assertEqual(selected, "linux-direct-wine")

    def test_neutral_fields_are_derived_without_new_evidence(self) -> None:
        capsule = json.loads(self.capsule_path.read_text(encoding="utf-8"))
        profile = next(
            item
            for item in capsule["profiles"]
            if item["id"] == "linux-direct-wine"
        )
        self.assertEqual(
            _neutral_fields_from_playable(profile),
            {
                "source_object": "sekiro-bottle-baseline",
                "neutral_root": "Sekiro",
                "prefix_source": "Sekiro",
                "game_source": "Sekiro/drive_c/Games/Sekiro",
                "game_destination_in_prefix": "drive_c/Games/Sekiro",
                "entrypoint_relative_to_game": "sekiro.exe.unpacked.exe",
                "working_directory_in_prefix": "drive_c/Games/Sekiro",
            },
        )

    def _componer(self, etiqueta: str):
        """Compose into an external destination outside the collection."""
        bottles = self.root / f"bottles-{etiqueta}"
        bottles.mkdir()
        externo = self.root / f"externo-{etiqueta}"
        externo.mkdir()
        destino = externo / "Sekiro"
        argumentos = dict(
            collection_root=self.collection,
            capsule_path=self.capsule_path,
            runner_id="ge-proton11-1",
            bottles_path=bottles,
            bottle_name="Sekiro",
        )
        # The external-destination refactor added a required `destination`.
        # This test is about source selection, not about where the adapter
        # publishes, so it adapts to whichever signature is present.
        firma = inspect.signature(compose_bottles)
        if "destination" in firma.parameters:
            argumentos["destination"] = destino
            raiz = destino
        else:
            raiz = bottles / "Sekiro"
        resultado = compose_bottles(**argumentos)
        return resultado, raiz, bottles

    def test_bottles_composition_completes_from_a_legacy_capsule(self) -> None:
        resultado, destino, _bottles = self._componer("completa")
        self.assertTrue(resultado.materialized)
        self.assertEqual(resultado.backend, "bottles")

        # Layout-agnostic on purpose: the fix under test is that Bottles
        # accepts the historical capsule at all, not where the adapter
        # publishes the tree.
        ejecutables = [
            path
            for path in destino.rglob("sekiro.exe.unpacked.exe")
            if path.is_file() and not path.is_symlink()
        ]
        self.assertEqual(len(ejecutables), 1, f"encontrados: {ejecutables}")
        self.assertEqual(ejecutables[0].read_bytes(), GAME_PAYLOAD)

        # The rest of the archived prefix survives the conversion.
        registros = [
            path
            for path in destino.rglob("system.reg")
            if path.is_file() and not path.is_symlink()
        ]
        self.assertTrue(registros, "system.reg no ha sobrevivido")
        self.assertEqual(registros[0].read_bytes(), SAVE_PAYLOAD)

        self.assertTrue(
            any(destino.rglob("bottle.yml")), "no se ha generado bottle.yml"
        )

    def test_nested_game_is_not_duplicated(self) -> None:
        _resultado, destino, _bottles = self._componer("sin-duplicar")
        copias = [
            path
            for path in destino.rglob("sekiro.exe.unpacked.exe")
            if path.is_file() and not path.is_symlink()
        ]
        self.assertEqual(len(copias), 1, f"duplicado en: {copias}")

    def test_a_truly_missing_source_is_still_blocked(self) -> None:
        capsule = json.loads(self.capsule_path.read_text(encoding="utf-8"))
        capsule["profiles"] = [
            item
            for item in capsule["profiles"]
            if item["id"] != "linux-direct-wine"
        ]
        self.capsule_path.write_text(json.dumps(capsule), encoding="utf-8")
        with self.assertRaises(CompositionError):
            _select_source_profile(
                self.capsule_path,
                backend="bottles",
                profile_id=None,
            )


if __name__ == "__main__":
    unittest.main()
