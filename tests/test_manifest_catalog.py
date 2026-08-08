"""The manifest catalog decides which objects need a manifest and drives it.

These tests cover the properties that matter for a batch run: it never writes
false evidence, it is idempotent when nothing has changed, it surfaces
partial-vault situations instead of hiding them, and it honors the operator's
"limit" and "dry-run" toggles.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from offline_game_vault.manifest_catalog import (
    BatchResult,
    ManifestCatalogError,
    ObjectRecord,
    generate_missing_manifests,
    manifest_is_current,
    read_vault_inventory,
    scan_vault,
)
from offline_game_vault.object_manifest import (
    manifest_path,
    manifest_sidecar_path,
    read_manifest,
)
from offline_game_vault.storage import (
    ObjectSpec,
    canonical_object_path,
    ingest_object,
)


def _tar_archive(destination: Path, root: str, files: dict[str, bytes]) -> None:
    directories: set[str] = {root}
    for name in files:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            directories.add("/".join(parts[:depth]))
    with tarfile.open(destination, "w:gz") as tf:
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tf.addfile(info)
        for name, payload in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(payload))


class VaultBuilder:
    """Small helper that assembles a fake Vault matching the on-disk layout."""

    def __init__(self, root: Path) -> None:
        self.collection = root / "collection"
        self.immutable = self.collection / "01_IMMUTABLE_VAULT"
        self.capsules = self.collection / "02_CAPSULES"
        self.immutable.mkdir(parents=True)
        self.capsules.mkdir(parents=True)
        self._inventory: list[dict[str, object]] = []

    def add_object(
        self,
        *,
        object_id: str,
        payload: bytes,
        source_root: str,
        files: dict[str, bytes],
        format_name: str = "tar.gz",
        capsule_id: str | None = None,
    ) -> tuple[str, int]:
        # Build the archive on disk.
        staging = self.immutable.parent / f".stage-{object_id}.tar.gz"
        _tar_archive(staging, source_root, files)
        raw = staging.read_bytes()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        hex_digest = digest.removeprefix("sha256:")
        archive_relative = (
            f"objects/sha256/{hex_digest[:2]}/{hex_digest[2:4]}/{hex_digest}"
        )
        ingest_object(
            source=staging,
            destination_spec=ObjectSpec(
                object_id=object_id,
                path=canonical_object_path(self.immutable, digest),
                expected_digest=digest,
                expected_size=len(raw),
                vault_root=self.immutable.resolve(),
            ),
        )
        self._inventory.append({
            "digest": digest,
            "path": archive_relative,
            "bytes": len(raw),
        })
        if capsule_id is not None:
            self._add_capsule(capsule_id, object_id, digest, len(raw),
                              archive_relative, format_name)
        return digest, len(raw)

    def _add_capsule(
        self,
        capsule_id: str,
        object_id: str,
        digest: str,
        size: int,
        archive_relative: str,
        format_name: str,
    ) -> None:
        capsule_dir = self.capsules / capsule_id
        capsule_dir.mkdir(exist_ok=True)
        (capsule_dir / "capsule.json").write_text(
            json.dumps({
                "schema": 0,
                "capsule_id": capsule_id,
                "objects": [{
                    "id": object_id,
                    "digest": digest,
                    "roles": ["game_payload"],
                    "format": format_name,
                    "required": True,
                    "archive_path": archive_relative,
                    "shared": False,
                    "size": size,
                }],
                "profiles": [],
            }),
            encoding="utf-8",
        )

    def write_inventory(self) -> None:
        (self.immutable / "VAULT_INVENTORY.json").write_text(
            json.dumps({"schema": 0, "objects": self._inventory}),
            encoding="utf-8",
        )


class InventoryReadingTest(unittest.TestCase):
    """The catalog fails loudly on a broken inventory."""

    def test_missing_inventory_is_reported_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ManifestCatalogError):
                read_vault_inventory(Path(tmp))

    def test_malformed_inventory_is_reported_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VAULT_INVENTORY.json").write_text("not json",
                                                      encoding="utf-8")
            with self.assertRaises(ManifestCatalogError):
                read_vault_inventory(root)

    def test_inventory_without_objects_is_reported_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VAULT_INVENTORY.json").write_text(
                json.dumps({"schema": 0}), encoding="utf-8"
            )
            with self.assertRaises(ManifestCatalogError):
                read_vault_inventory(root)


class ScanTest(unittest.TestCase):
    """scan_vault reflects the vault as it is on disk, not as we wish."""

    def _build(self, tmp: Path) -> VaultBuilder:
        builder = VaultBuilder(tmp)
        builder.add_object(
            object_id="alpha",
            payload=b"a",
            source_root="alpha",
            files={"alpha/readme.txt": b"hi\n"},
            capsule_id="game-a",
        )
        builder.add_object(
            object_id="beta",
            payload=b"b",
            source_root="beta",
            files={"beta/data.bin": b"\x00\x01"},
            capsule_id="game-b",
        )
        builder.write_inventory()
        return builder

    def test_records_carry_format_and_declaring_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = self._build(Path(tmp))
            records = scan_vault(builder.collection)
            by_id = {r.declared_by[0]: r for r in records}
            self.assertEqual(len(records), 2)
            self.assertEqual(by_id["game-a"].format, "tar.gz")
            self.assertEqual(by_id["game-b"].format, "tar.gz")

    def test_undeclared_objects_appear_with_format_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = VaultBuilder(Path(tmp))
            builder.add_object(
                object_id="orphan",
                payload=b"o",
                source_root="orphan",
                files={"orphan/marker": b"x"},
                capsule_id=None,  # no capsule declares this one
            )
            builder.write_inventory()
            records = scan_vault(builder.collection)
            self.assertEqual(len(records), 1)
            self.assertIsNone(records[0].format)
            self.assertEqual(records[0].declared_by, ())

    def test_index_json_labels_rescue_shared_components(self) -> None:
        """A shared runner declared only in INDEX.json is still processable.

        The paso 27b diagnosis showed the real vault has shared-runner
        entries whose format lives in the ``label`` field (``soda-9.0-1.tar.gz``,
        ``Proton-9.0-203.tar.gz``, ``bottles-flatpak-*.tar.zst``). Those are
        legitimate objects, not orphans, and must not be skipped as "no
        capsule declares this object's format".
        """
        with tempfile.TemporaryDirectory() as tmp:
            builder = VaultBuilder(Path(tmp))
            digest, size = builder.add_object(
                object_id="soda",
                payload=b"s",
                source_root="soda-9.0-1",
                files={"soda-9.0-1/bin/wine": b"binary"},
                capsule_id=None,  # only INDEX.json will declare it
            )
            builder.write_inventory()
            # Now write an INDEX.json that declares it as a shared-runner.
            hex_digest = digest.removeprefix("sha256:")
            archive_relative = (
                f"objects/sha256/{hex_digest[:2]}/{hex_digest[2:4]}/{hex_digest}"
            )
            (builder.collection / "INDEX.json").write_text(
                json.dumps({
                    "schema": 0,
                    "objects": [{
                        "label": "soda-9.0-1.tar.gz",
                        "sha256": hex_digest,
                        "size": size,
                        "role": "shared-runner",
                        "path": archive_relative,
                    }],
                }),
                encoding="utf-8",
            )
            records = scan_vault(builder.collection)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].format, "tar.gz")
            self.assertEqual(
                records[0].declared_by, ("INDEX.json:shared-runner",)
            )

    def test_conflicting_declarations_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = VaultBuilder(Path(tmp))
            digest, size = builder.add_object(
                object_id="shared",
                payload=b"s",
                source_root="shared",
                files={"shared/x": b"1"},
                capsule_id="game-c",
                format_name="tar.gz",
            )
            hex_digest = digest.removeprefix("sha256:")
            archive_relative = (
                f"objects/sha256/{hex_digest[:2]}/{hex_digest[2:4]}/{hex_digest}"
            )
            (builder.capsules / "game-d").mkdir()
            (builder.capsules / "game-d" / "capsule.json").write_text(
                json.dumps({
                    "schema": 0,
                    "capsule_id": "game-d",
                    "objects": [{
                        "id": "shared",
                        "digest": digest,
                        "roles": [],
                        "format": "tar.zst",  # conflicting format
                        "required": True,
                        "archive_path": archive_relative,
                        "shared": True,
                        "size": size,
                    }],
                    "profiles": [],
                }),
                encoding="utf-8",
            )
            builder.write_inventory()
            with self.assertRaises(ManifestCatalogError):
                scan_vault(builder.collection)


class BatchTest(unittest.TestCase):
    """The batch runner respects existing manifests and reports its work."""

    def _prepare(self, tmp: Path) -> VaultBuilder:
        builder = VaultBuilder(tmp)
        builder.add_object(
            object_id="alpha",
            payload=b"a",
            source_root="alpha",
            files={
                "alpha/readme.txt": b"hi\n",
                "alpha/inner/x.bin": b"\x00\x01\x02",
            },
            capsule_id="game-a",
        )
        builder.add_object(
            object_id="beta",
            payload=b"b",
            source_root="beta",
            files={"beta/data.bin": b"payload"},
            capsule_id="game-b",
        )
        builder.write_inventory()
        return builder

    def test_first_run_generates_every_valid_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = self._prepare(Path(tmp))
            result = generate_missing_manifests(
                collection_root=builder.collection
            )
            self.assertEqual(len(result.generated), 2)
            self.assertEqual(result.already_present, [])
            self.assertFalse(result.has_failures)
            for record in scan_vault(builder.collection):
                target = manifest_path(builder.immutable, record.digest)
                self.assertTrue(target.is_file())
                self.assertTrue(manifest_sidecar_path(target).is_file())

    def test_second_run_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = self._prepare(Path(tmp))
            generate_missing_manifests(collection_root=builder.collection)
            targets = [
                manifest_path(builder.immutable, r.digest)
                for r in scan_vault(builder.collection)
            ]
            before = {p: p.stat().st_mtime_ns for p in targets}

            result = generate_missing_manifests(
                collection_root=builder.collection
            )
            self.assertEqual(len(result.generated), 0)
            self.assertEqual(len(result.already_present), 2)
            for path, mtime in before.items():
                self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_objects_without_format_are_skipped_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = VaultBuilder(Path(tmp))
            builder.add_object(
                object_id="orphan",
                payload=b"o",
                source_root="orphan",
                files={"orphan/marker": b"x"},
                capsule_id=None,
            )
            builder.write_inventory()
            result = generate_missing_manifests(
                collection_root=builder.collection
            )
            self.assertEqual(result.generated, [])
            self.assertEqual(len(result.skipped), 1)
            self.assertIn("format", result.skipped[0][1])
            self.assertFalse(result.has_failures)

    def test_limit_caps_only_the_work_actually_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = self._prepare(Path(tmp))
            result = generate_missing_manifests(
                collection_root=builder.collection, limit=1
            )
            self.assertEqual(len(result.generated), 1)
            # The other object is skipped with a "limit" reason, not failed.
            self.assertEqual(len(result.skipped), 1)
            self.assertIn("limit", result.skipped[0][1])

    def test_a_corrupted_existing_manifest_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = self._prepare(Path(tmp))
            generate_missing_manifests(collection_root=builder.collection)
            # Poison one manifest without touching its sidecar.
            first = manifest_path(
                builder.immutable, scan_vault(builder.collection)[0].digest
            )
            first.write_bytes(b"garbage\n")

            result = generate_missing_manifests(
                collection_root=builder.collection
            )
            self.assertEqual(len(result.generated), 1)
            self.assertEqual(len(result.already_present), 1)
            # And the regenerated one now reads cleanly.
            read_manifest(first)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = self._prepare(Path(tmp))
            result = generate_missing_manifests(
                collection_root=builder.collection, dry_run=True
            )
            self.assertEqual(len(result.generated), 2)
            for record in scan_vault(builder.collection):
                target = manifest_path(builder.immutable, record.digest)
                self.assertFalse(target.exists(), f"leaked: {target}")


if __name__ == "__main__":
    unittest.main()
