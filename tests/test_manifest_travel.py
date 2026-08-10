from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from offline_game_vault.manifest_travel import (
    GENERATED_FILES_MANIFEST,
    MANIFESTS_SUBTREE,
    ManifestTravelError,
    copy_manifests_to_materialization,
    validate_manifests_present_for,
    write_generated_files_manifest,
    write_receipt_sidecar,
)
from offline_game_vault.object_manifest import (
    detect_source_root,
    generate_object_manifest,
    manifest_path,
    manifest_sidecar_path,
    read_manifest,
    write_manifest_atomically,
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _make_tar_gz(path: Path, root: str, files: dict[str, bytes]) -> str:
    """Build a small tar.gz with a single top-level dir; return sha256:hex."""
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(root)
        member.type = tarfile.DIRTYPE
        member.mode = 0o755
        archive.addfile(member)
        for name, payload in files.items():
            entry = tarfile.TarInfo(f"{root}/{name}")
            entry.size = len(payload)
            entry.mode = 0o644
            import io
            archive.addfile(entry, io.BytesIO(payload))
    return "sha256:" + _sha256_bytes(path.read_bytes())


class ManifestTravelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.vault = self.root / "vault"
        self.destination = self.root / "materialization"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _ingest_and_manifest(self, name: str) -> str:
        archive = self.root / f"{name}.tar.gz"
        digest = _make_tar_gz(archive, name, {"file.txt": b"content\n"})
        # Place the archive at the vault's canonical CAS path.
        hex_digest = digest.removeprefix("sha256:")
        cas = (
            self.vault / "objects" / "sha256" / hex_digest[:2]
            / hex_digest[2:4] / hex_digest
        )
        cas.parent.mkdir(parents=True, exist_ok=True)
        cas.write_bytes(archive.read_bytes())
        # Generate the manifest.
        manifest = generate_object_manifest(
            archive=cas,
            archive_format="tar.gz",
            source_root=detect_source_root(cas, "tar.gz"),
            object_digest=digest,
            object_size=cas.stat().st_size,
        )
        write_manifest_atomically(
            manifest, manifest_path(self.vault, digest)
        )
        return digest

    # ------------------------------------------------ validate_manifests

    def test_validate_manifests_passes_when_present(self) -> None:
        digest = self._ingest_and_manifest("obj-a")
        validate_manifests_present_for(
            vault_root=self.vault, digests=[digest]
        )  # no raise

    def test_validate_manifests_fails_when_missing(self) -> None:
        with self.assertRaises(ManifestTravelError) as ctx:
            validate_manifests_present_for(
                vault_root=self.vault,
                digests=["sha256:" + "0" * 64],
            )
        self.assertIn("generate-missing-manifests", str(ctx.exception))

    def test_validate_manifests_fails_on_malformed_digest(self) -> None:
        with self.assertRaises(ManifestTravelError):
            validate_manifests_present_for(
                vault_root=self.vault, digests=["not-a-digest"]
            )

    # -------------------------------------------------------- copy

    def test_copy_manifests_to_materialization(self) -> None:
        digest = self._ingest_and_manifest("obj-a")
        self.destination.mkdir()
        written = copy_manifests_to_materialization(
            vault_root=self.vault,
            destination=self.destination,
            digests=[digest],
        )
        self.assertEqual(len(written), 2)
        # Round-trip: read_manifest should validate sidecar and parse.
        hex_digest = digest.removeprefix("sha256:")
        local_manifest = (
            self.destination / MANIFESTS_SUBTREE / "sha256"
            / hex_digest[:2] / hex_digest[2:4] / hex_digest
        )
        self.assertTrue(local_manifest.is_file())
        self.assertTrue(
            manifest_sidecar_path(local_manifest).is_file()
        )
        parsed = read_manifest(local_manifest)
        self.assertEqual(parsed.object_digest, digest)

    def test_copy_fails_when_vault_manifest_disappeared(self) -> None:
        digest = self._ingest_and_manifest("obj-a")
        # Remove the sidecar to simulate corruption between validate and
        # copy — the check inside copy must fire.
        manifest_sidecar_path(manifest_path(self.vault, digest)).unlink()
        self.destination.mkdir()
        with self.assertRaises(ManifestTravelError):
            copy_manifests_to_materialization(
                vault_root=self.vault,
                destination=self.destination,
                digests=[digest],
            )

    # ------------------------------------------- generated-files manifest

    def _write_fake_object_manifest(
        self,
        entries: list[tuple[str, bytes]],
        *,
        object_digest: str | None = None,
    ) -> "Path":
        """Write a canonical object manifest under metadata/manifests/.

        ``entries`` is a list of ``(relative_path_in_object, content)``.
        The hash of each content is used for the entry. Returns the
        manifest path so the test can pass it to
        ``write_generated_files_manifest``.
        """
        from pathlib import Path
        if object_digest is None:
            object_digest = "sha256:" + "0" * 63 + "a"
        hex_digest = object_digest.removeprefix("sha256:")
        target = (
            self.destination / MANIFESTS_SUBTREE / "sha256"
            / hex_digest[:2] / hex_digest[2:4] / hex_digest
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        header_lines = [
            "manifest_schema:0",
            f"object_digest:{object_digest}",
            "object_size:0",
            "generated_at:2026-01-01T00:00:00Z",
            "generator:test",
            f"file_count:{len(entries)}",
            f"total_bytes:{sum(len(c) for _, c in entries)}",
        ]
        body_lines = [
            f"{_sha256_bytes(content)} {len(content)} {path}"
            for path, content in entries
        ]
        text = "\n".join(header_lines) + "\n\n" + "\n".join(body_lines)
        if body_lines:
            text += "\n"
        payload = text.encode("utf-8")
        target.write_bytes(payload)
        target.with_name(target.name + ".sha256").write_text(
            f"{_sha256_bytes(payload)}  {target.name}\n",
            encoding="utf-8",
        )
        return target

    def test_generated_files_excludes_object_content_by_hash(self) -> None:
        """Object content is classified by (size, hash), not by path.

        A file whose bytes match a manifest entry is excluded from
        generated-files.json even if its destination path bears no
        resemblance to the path declared in the manifest. This is the
        UMU case in miniature: the materializer rebases and mixes
        object content, and the path in the manifest does not survive.
        """
        self.destination.mkdir()
        content = b"payload content\n"
        # Manifest declares the file at payload/game/thing.bin.
        manifest_path = self._write_fake_object_manifest(
            [("payload/game/thing.bin", content)]
        )
        # Destination writes it at prefix/drive_c/Games/thing.bin (rebased).
        (self.destination / "prefix/drive_c/Games").mkdir(parents=True)
        (self.destination / "prefix/drive_c/Games/thing.bin").write_bytes(
            content
        )
        # Plus a real generated file at root.
        (self.destination / "JUGAR.sh").write_bytes(b"#!/bin/sh\n")
        manifest_file = write_generated_files_manifest(
            destination=self.destination,
            object_manifest_paths=[manifest_path],
        )
        paths = [
            e["path"]
            for e in json.loads(manifest_file.read_text())["files"]
        ]
        self.assertIn("JUGAR.sh", paths)
        # Object content is NOT in generated-files, despite its path
        # having no top-level match against the manifest declaration.
        self.assertNotIn(
            "prefix/drive_c/Games/thing.bin", paths
        )

    def test_generated_files_excludes_manifests_subtree(self) -> None:
        self.destination.mkdir()
        (self.destination / MANIFESTS_SUBTREE).mkdir(parents=True)
        (self.destination / MANIFESTS_SUBTREE / "dummy").write_bytes(b"x")
        (self.destination / "JUGAR.sh").write_bytes(b"#!/bin/sh\n")
        manifest_file = write_generated_files_manifest(
            destination=self.destination,
            object_manifest_paths=[],
        )
        paths = [
            e["path"]
            for e in json.loads(manifest_file.read_text())["files"]
        ]
        self.assertIn("JUGAR.sh", paths)
        for p in paths:
            self.assertFalse(p.startswith("metadata/manifests/"))

    def test_generated_files_captures_unexpected_files(self) -> None:
        """Files a user drops in by hand are hashed, not rejected."""
        self.destination.mkdir()
        (self.destination / "surprise.md").write_bytes(b"my notes\n")
        (self.destination / "JUGAR.sh").write_bytes(b"#!/bin/sh\n")
        manifest_file = write_generated_files_manifest(
            destination=self.destination,
            object_manifest_paths=[],
        )
        paths = [
            e["path"]
            for e in json.loads(manifest_file.read_text())["files"]
        ]
        self.assertIn("surprise.md", paths)
        self.assertIn("JUGAR.sh", paths)

    def test_generated_files_hashes_are_correct(self) -> None:
        self.destination.mkdir()
        payload = b"deterministic\n"
        (self.destination / "JUGAR.sh").write_bytes(payload)
        manifest_file = write_generated_files_manifest(
            destination=self.destination,
            object_manifest_paths=[],
        )
        document = json.loads(manifest_file.read_text(encoding="utf-8"))
        entry = next(
            e for e in document["files"] if e["path"] == "JUGAR.sh"
        )
        self.assertEqual(entry["sha256"], _sha256_bytes(payload))
        self.assertEqual(entry["bytes"], len(payload))

    def test_generated_files_manifest_excludes_itself(self) -> None:
        self.destination.mkdir()
        (self.destination / "JUGAR.sh").write_bytes(b"#!/bin/sh\n")
        manifest_file = write_generated_files_manifest(
            destination=self.destination,
            object_manifest_paths=[],
        )
        paths = [
            e["path"]
            for e in json.loads(manifest_file.read_text())["files"]
        ]
        self.assertNotIn(GENERATED_FILES_MANIFEST, paths)

    def test_sidecar_paths_in_input_are_ignored(self) -> None:
        """Callers may pass the raw list from copy_manifests_to_materialization
        which interleaves manifest + sidecar; the sidecars are filtered out
        of the catalogue build so no spurious matches happen."""
        self.destination.mkdir()
        content = b"payload content\n"
        manifest_path = self._write_fake_object_manifest(
            [("payload/thing.bin", content)]
        )
        sidecar_path = manifest_path.with_name(
            manifest_path.name + ".sha256"
        )
        (self.destination / "thing.bin").write_bytes(content)
        manifest_file = write_generated_files_manifest(
            destination=self.destination,
            object_manifest_paths=[manifest_path, sidecar_path],
        )
        paths = [
            e["path"]
            for e in json.loads(manifest_file.read_text())["files"]
        ]
        self.assertNotIn("thing.bin", paths)

    # ----------------------------------------------- receipt sidecar

    def test_receipt_sidecar_matches_content(self) -> None:
        self.destination.mkdir()
        receipt = self.destination / "playable-materialization.json"
        payload = b'{"schema": 0}\n'
        receipt.write_bytes(payload)
        sidecar = write_receipt_sidecar(receipt)
        self.assertEqual(sidecar, receipt.with_name(receipt.name + ".sha256"))
        content = sidecar.read_text(encoding="utf-8").strip()
        expected_hex = _sha256_bytes(payload)
        self.assertTrue(content.startswith(expected_hex))
        self.assertIn(receipt.name, content)

    def test_receipt_sidecar_fails_when_receipt_missing(self) -> None:
        with self.assertRaises(ManifestTravelError):
            write_receipt_sidecar(self.destination / "missing.json")


if __name__ == "__main__":
    unittest.main()
