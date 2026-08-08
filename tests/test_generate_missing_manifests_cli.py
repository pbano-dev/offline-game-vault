"""End-to-end tests for the batch manifest command.

The unit tests already cover the catalog logic. These tests fix the surface
the operator actually types: what happens with --dry-run, --limit, --json,
and how failures are reported.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from offline_game_vault import cli
from offline_game_vault.object_manifest import (
    manifest_path,
    manifest_sidecar_path,
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


class GenerateMissingCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.collection = self.root / "collection"
        self.immutable = self.collection / "01_IMMUTABLE_VAULT"
        self.capsules = self.collection / "02_CAPSULES"
        self.immutable.mkdir(parents=True)
        self.capsules.mkdir(parents=True)
        self._inventory: list[dict[str, object]] = []
        self._add(
            "alpha",
            {"alpha/readme.txt": b"hi\n", "alpha/inner/x.bin": b"\x00\x01"},
            "alpha",
            capsule_id="game-a",
        )
        self._add(
            "beta",
            {"beta/data.bin": b"payload"},
            "beta",
            capsule_id="game-b",
        )
        (self.immutable / "VAULT_INVENTORY.json").write_text(
            json.dumps({"schema": 0, "objects": self._inventory}),
            encoding="utf-8",
        )

    def _add(
        self,
        object_id: str,
        files: dict[str, bytes],
        source_root: str,
        *,
        capsule_id: str,
    ) -> str:
        staging = self.root / f".stage-{object_id}.tar.gz"
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
        capsule_dir = self.capsules / capsule_id
        capsule_dir.mkdir(exist_ok=True)
        (capsule_dir / "capsule.json").write_text(
            json.dumps({
                "schema": 0,
                "capsule_id": capsule_id,
                "objects": [{
                    "id": object_id,
                    "digest": digest,
                    "roles": [],
                    "format": "tar.gz",
                    "required": True,
                    "archive_path": archive_relative,
                    "shared": False,
                    "size": len(raw),
                }],
                "profiles": [],
            }),
            encoding="utf-8",
        )
        return digest

    def _run(self, *extra: str) -> tuple[int, str]:
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            code = cli.main([
                "generate-missing-manifests",
                "--collection-root", str(self.collection),
                *extra,
            ])
        return code, capture.getvalue()

    def test_first_run_generates_and_reports_counts(self) -> None:
        code, output = self._run("--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["generated_count"], 2)
        self.assertEqual(payload["already_present_count"], 0)
        for digest in payload["generated"]:
            target = manifest_path(self.immutable, digest)
            self.assertTrue(target.is_file())
            self.assertTrue(manifest_sidecar_path(target).is_file())

    def test_second_run_reports_everything_as_already_present(self) -> None:
        self._run()
        code, output = self._run("--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["generated_count"], 0)
        self.assertEqual(payload["already_present_count"], 2)

    def test_dry_run_writes_nothing(self) -> None:
        code, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        for digest in [item["digest"] for item in self._inventory]:
            self.assertFalse(
                manifest_path(self.immutable, digest).exists()
            )

    def test_limit_caps_the_amount_of_work(self) -> None:
        code, output = self._run("--limit", "1", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["generated_count"], 1)
        self.assertEqual(payload["skipped_count"], 1)


if __name__ == "__main__":
    unittest.main()
