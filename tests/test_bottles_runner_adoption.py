"""Selecting a valid runner must not fail because of a hand-installed one.

Bottles users install runners themselves. A foreign directory occupying the
name of a preserved runner previously made composition impossible, which
blocked a technically valid user choice for a reason that is not a fact about
the objects. The Vault now adopts a byte-identical tree and otherwise installs
its verified copy beside the user's, never overwriting it.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from offline_game_vault.preserved_runners import scan_runners
from offline_game_vault.runner_deployment import (
    RunnerDeploymentError,
    _MARKER_NAME,
    ensure_bottles_runner,
)
from offline_game_vault.storage import (
    ObjectSpec,
    canonical_object_path,
    ingest_object,
)


def _make_runner_archive(path: Path, *, root: str, payload: bytes) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name in (root, f"{root}/files", f"{root}/files/bin"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, data, mode in (
            (f"{root}/files/bin/wine", b"#!/bin/sh\nexit 0\n", 0o755),
            (f"{root}/files/bin/wineserver", b"#!/bin/sh\nexit 0\n", 0o755),
            (f"{root}/marca.txt", payload, 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))


class BottlesRunnerAdoptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

        self.collection = self.root / "collection"
        self.immutable = self.collection / "01_IMMUTABLE_VAULT"
        self.immutable.mkdir(parents=True)

        self.archive = self.root / "soda-9.0-1.tar.gz"
        _make_runner_archive(
            self.archive,
            root="soda-9.0-1",
            payload=b"preserved-runner-payload",
        )
        digest = "sha256:" + hashlib.sha256(
            self.archive.read_bytes()
        ).hexdigest()
        raw = digest.removeprefix("sha256:")
        ingest_object(
            source=self.archive,
            destination_spec=ObjectSpec(
                object_id="soda-9.0-1",
                path=canonical_object_path(self.immutable, digest),
                expected_digest=digest,
                expected_size=self.archive.stat().st_size,
                vault_root=self.immutable.resolve(),
            ),
        )
        archive_path = f"objects/sha256/{raw[:2]}/{raw[2:4]}/{raw}"

        (self.immutable / "VAULT_INVENTORY.json").write_text(
            json.dumps(
                {
                    "schema": 0,
                    "objects": [
                        {
                            "digest": digest,
                            "path": archive_path,
                            "bytes": self.archive.stat().st_size,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.collection / "INDEX.json").write_text(
            json.dumps(
                {
                    "schema": 0,
                    "capsules": [],
                    "objects": [
                        {
                            "label": "soda-9.0-1.tar.gz",
                            "path": archive_path,
                            "role": "shared-runner",
                            "sha256": raw,
                            "size": self.archive.stat().st_size,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        runners, _avisos = scan_runners(self.collection)
        self.runner = next(
            item for item in runners if item.runner_id == "soda-9.0-1"
        )

        componentes = self.root / "componentes"
        self.bottles = componentes / "bottles"
        self.bottles.mkdir(parents=True)
        self.runners_dir = componentes / "runners"

    def _instalar(self):
        return ensure_bottles_runner(
            self.collection,
            self.bottles,
            self.runner,
        )

    def _extraer_ajeno(self, *, identico: bool) -> Path:
        """Simulate a runner the user installed by hand."""
        self.runners_dir.mkdir(parents=True, exist_ok=True)
        destino = self.runners_dir / "soda-9.0-1"
        with tarfile.open(self.archive, "r:*") as archivo:
            archivo.extractall(self.runners_dir, filter="tar")
        self.assertTrue(destino.is_dir())
        if not identico:
            (destino / "modificado-por-el-usuario.txt").write_text(
                "no coincide", encoding="utf-8"
            )
        return destino

    def test_first_install_uses_the_vault_runner_id(self) -> None:
        resultado = self._instalar()
        self.assertTrue(resultado.created)
        self.assertFalse(resultado.adopted)
        self.assertEqual(resultado.name, "soda-9.0-1")
        self.assertTrue((resultado.path / _MARKER_NAME).is_file())

    def test_second_call_reuses_without_reinstalling(self) -> None:
        primero = self._instalar()
        segundo = self._instalar()
        self.assertFalse(segundo.created)
        self.assertFalse(segundo.adopted)
        self.assertEqual(segundo.name, "soda-9.0-1")
        self.assertEqual(segundo.path, primero.path)

    def test_identical_foreign_runner_is_adopted_not_duplicated(self) -> None:
        ajeno = self._extraer_ajeno(identico=True)
        self.assertFalse((ajeno / _MARKER_NAME).exists())

        resultado = self._instalar()

        self.assertTrue(resultado.adopted)
        self.assertFalse(resultado.created)
        self.assertEqual(resultado.name, "soda-9.0-1")
        self.assertEqual(resultado.path, ajeno.resolve())
        # The marker now records the verified fact.
        self.assertTrue((ajeno / _MARKER_NAME).is_file())
        marcador = json.loads(
            (ajeno / _MARKER_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(marcador["runner_id"], "soda-9.0-1")
        self.assertEqual(marcador["archive_digest"], self.runner.digest)
        # No sibling copy was created.
        hermanos = [
            item.name
            for item in self.runners_dir.iterdir()
            if item.is_dir()
        ]
        self.assertEqual(hermanos, ["soda-9.0-1"])

    def test_adopted_runner_is_reused_on_the_next_composition(self) -> None:
        self._extraer_ajeno(identico=True)
        primero = self._instalar()
        segundo = self._instalar()
        self.assertTrue(primero.adopted)
        self.assertFalse(segundo.adopted)
        self.assertFalse(segundo.created)
        self.assertEqual(segundo.name, "soda-9.0-1")

    def test_different_foreign_runner_installs_beside_it(self) -> None:
        ajeno = self._extraer_ajeno(identico=False)
        contenido_previo = sorted(
            item.name for item in ajeno.iterdir()
        )

        resultado = self._instalar()

        self.assertTrue(resultado.created)
        self.assertFalse(resultado.adopted)
        self.assertNotEqual(resultado.name, "soda-9.0-1")
        self.assertTrue(resultado.name.startswith("soda-9.0-1-ogv-"))
        self.assertTrue((resultado.path / _MARKER_NAME).is_file())

        # The user's directory is untouched, marker included.
        self.assertFalse((ajeno / _MARKER_NAME).exists())
        self.assertEqual(
            sorted(item.name for item in ajeno.iterdir()),
            contenido_previo,
        )

    def test_namespaced_runner_is_reused_and_is_deterministic(self) -> None:
        self._extraer_ajeno(identico=False)
        primero = self._instalar()
        segundo = self._instalar()
        self.assertEqual(primero.name, segundo.name)
        self.assertTrue(primero.created)
        self.assertFalse(segundo.created)

    def test_a_tampered_vault_runner_is_refused_not_worked_around(self) -> None:
        """Integrity failures must be reported, never routed around.

        A directory the Vault installed carries a marker. If its tree stops
        matching, that is tampering or corruption, not a name collision, and
        silently installing a fresh copy beside it would hide the evidence.
        """
        instalado = self._instalar()
        self.assertTrue(instalado.created)

        wine = instalado.path / "files/bin/wine"
        wine.write_bytes(wine.read_bytes() + b"manipulado")

        with self.assertRaisesRegex(
            RunnerDeploymentError,
            "differs from the preserved Vault object",
        ):
            self._instalar()

        # And no sibling copy was quietly created.
        hermanos = sorted(
            item.name for item in self.runners_dir.iterdir() if item.is_dir()
        )
        self.assertEqual(hermanos, ["soda-9.0-1"])

    def test_a_corrupt_marker_is_refused(self) -> None:
        instalado = self._instalar()
        (instalado.path / _MARKER_NAME).write_text("no es json", encoding="utf-8")
        with self.assertRaises(RunnerDeploymentError):
            self._instalar()

    def test_a_symlinked_destination_is_still_refused(self) -> None:
        self.runners_dir.mkdir(parents=True, exist_ok=True)
        otro = self.root / "otro-sitio"
        otro.mkdir()
        (self.runners_dir / "soda-9.0-1").symlink_to(otro)
        with self.assertRaises(RunnerDeploymentError):
            self._instalar()


if __name__ == "__main__":
    unittest.main()
