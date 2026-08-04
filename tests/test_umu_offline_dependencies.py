from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None

from offline_game_vault.storage import canonical_object_path, ingest_object
from offline_game_vault.umu_adapter import (
    UmuAdapterError,
    _discover_offline_environment,
    materialize_umu_profile,
    run_umu_materialization,
    verify_umu_materialization,
)
from offline_game_vault.verifier import ObjectSpec


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_directory(archive: tarfile.TarFile, name: str, mode: int = 0o755) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    archive.addfile(info)


def add_file(
    archive: tarfile.TarFile,
    name: str,
    payload: bytes,
    mode: int = 0o644,
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    archive.addfile(info, io.BytesIO(payload))


class UmuOfflineDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("tar") is None:
            self.skipTest("GNU tar is unavailable")

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.capsule_root = self.root / "capsule"
        self.capsule_root.mkdir()
        (self.capsule_root / "launchers").mkdir()
        (self.capsule_root / "manifests").mkdir()
        self.destination = self.root / "materialized"
        self.original_path = os.environ.get("PATH", "")
        test_bin = self.root / "bin"
        test_bin.mkdir()
        systemd_run = test_bin / "systemd-run"
        self.systemd_log = self.root / "systemd-run.args"
        os.environ["OGV_TEST_SYSTEMD_RUN_LOG"] = str(self.systemd_log)
        systemd_run.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf '%s\\n' \"$@\" > \"$OGV_TEST_SYSTEMD_RUN_LOG\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"--\" ]; then\n"
            "    shift\n"
            "    exec \"$@\"\n"
            "  fi\n"
            "  shift\n"
            "done\n"
            "exit 2\n",
            encoding="utf-8",
        )
        systemd_run.chmod(0o755)
        os.environ["PATH"] = str(test_bin) + os.pathsep + self.original_path

        game_archive = self.root / "game.tar"
        with tarfile.open(game_archive, "w") as archive:
            add_directory(archive, "payload")
            add_file(archive, "payload/game.bin", b"game\n")

        backend_archive = self.root / "backend.tar"
        with tarfile.open(backend_archive, "w") as archive:
            add_directory(archive, "engine")
            add_directory(archive, "engine/runtime")
            add_directory(archive, "engine/runtime/var")

        runtime_archive = self.root / "runtime.tar"
        with tarfile.open(runtime_archive, "w") as archive:
            add_directory(archive, "steamrt4")
            add_file(archive, "steamrt4/VERSIONS.txt", b"steamrt4\t1\n")
            add_file(
                archive,
                "steamrt4/_v2-entry-point",
                b"#!/bin/sh\nexit 0\n",
                0o755,
            )
            add_directory(archive, "steamrt4/pressure-vessel")
            add_directory(archive, "steamrt4/steamrt4_platform_test")
            add_directory(
                archive,
                "steamrt4/steamrt4_platform_test/files",
            )
            add_file(
                archive,
                "steamrt4/steamrt4_platform_test/files/lib.bin",
                b"runtime\n",
            )

            hardlink = tarfile.TarInfo(
                "steamrt4/steamrt4_platform_test/files/lib-copy.bin"
            )
            hardlink.type = tarfile.LNKTYPE
            hardlink.linkname = (
                "steamrt4/steamrt4_platform_test/files/lib.bin"
            )
            hardlink.mode = 0o644
            archive.addfile(hardlink)

            absolute = tarfile.TarInfo("steamrt4/run-host")
            absolute.type = tarfile.SYMTYPE
            absolute.linkname = "/run/host"
            absolute.mode = 0o777
            archive.addfile(absolute)

            unresolved = tarfile.TarInfo("steamrt4/unresolved")
            unresolved.type = tarfile.SYMTYPE
            unresolved.linkname = "missing"
            unresolved.mode = 0o777
            archive.addfile(unresolved)

        cache_payload = b"redistributable\n"
        cache_archive = self.root / "cache.tar"
        with tarfile.open(cache_archive, "w") as archive:
            add_directory(archive, "vcrun2017")
            add_file(
                archive,
                "vcrun2017/vc_redist.x64.exe",
                cache_payload,
            )

        objects = []
        for object_id, source, roles in (
            ("game", game_archive, ["game_payload"]),
            ("backend", backend_archive, ["tool"]),
            ("runtime", runtime_archive, ["runtime"]),
            ("vcrun", cache_archive, ["configuration"]),
        ):
            declared = "sha256:" + digest_file(source)
            destination = canonical_object_path(self.vault, declared)
            ingest_object(
                source=source,
                destination_spec=ObjectSpec(
                    object_id=object_id,
                    path=destination,
                    expected_digest=declared,
                    expected_size=source.stat().st_size,
                    vault_root=self.vault.resolve(),
                ),
            )
            objects.append(
                {
                    "id": object_id,
                    "digest": declared,
                    "size": source.stat().st_size,
                    "roles": roles,
                    "format": "tar",
                    "required": True,
                    "archive_path": (
                        f"objects/sha256/{declared[7:9]}/"
                        f"{declared[9:11]}/{declared[7:]}"
                    ),
                    "shared": object_id != "game",
                }
            )

        launcher = self.capsule_root / "launchers/run.sh"
        launcher.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "{\n"
            "  printf 'XDG_DATA_HOME=%s\\n' \"$XDG_DATA_HOME\"\n"
            "  printf 'XDG_CACHE_HOME=%s\\n' \"$XDG_CACHE_HOME\"\n"
            "  printf 'UMU_RUNTIME_UPDATE=%s\\n' \"$UMU_RUNTIME_UPDATE\"\n"
            "} > payload/environment.txt\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)

        sanitizer = self.capsule_root / "launchers/sanitize.sh"
        sanitizer.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "find engine/runtime/var -mindepth 1 -maxdepth 1 "
            "-exec rm -rf -- {} +\n",
            encoding="utf-8",
        )
        sanitizer.chmod(0o755)

        symlinks = self.capsule_root / "manifests/runtime.symlinks"
        symlinks.write_text(
            "engine/xdg-data/umu/steamrt4/run-host\t/run/host\n"
            "engine/xdg-data/umu/steamrt4/unresolved\tmissing\n",
            encoding="utf-8",
        )

        hardlinks = self.capsule_root / "manifests/runtime.hardlinks.json"
        hardlinks.write_text(
            json.dumps(
                [[
                    "steamrt4_platform_test/files/lib-copy.bin",
                    "steamrt4_platform_test/files/lib.bin",
                ]],
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        self.cache_digest = digest_bytes(cache_payload)

        capsule = {
            "schema": 0,
            "capsule_id": "umu-offline-test",
            "game": {
                "title": "Synthetic",
                "source_store": "Test",
                "preserved_version": "1",
            },
            "documents": {
                "readme": "README.md",
                "game_sheet": "GAME.md",
                "credits": "CREDITS.md",
                "preserved_by": "PRESERVED.md",
            },
            "objects": objects,
            "persistent_state": [],
            "profiles": [
                {
                    "id": "linux-umu-offline",
                    "platform": "linux",
                    "adapter": "umu",
                    "dependencies": [
                        "game",
                        "backend",
                        "runtime",
                        "vcrun",
                    ],
                    "host_contract": "host.json",
                    "launch": {
                        "entrypoint": "payload/game.bin",
                        "network": "host_default",
                    },
                    "umu": {
                        "schema": 0,
                        "layout": [
                            {
                                "object": "game",
                                "source": "payload",
                                "destination": "payload",
                            },
                            {
                                "object": "backend",
                                "source": "engine",
                                "destination": "engine",
                            },
                            {
                                "object": "runtime",
                                "source": "steamrt4",
                                "destination":
                                    "engine/xdg-data/umu/steamrt4",
                                "archive_policy": {
                                    "allow_absolute_symlinks": True,
                                    "allow_hardlinks": True,
                                },
                            },
                            {
                                "object": "vcrun",
                                "source": "vcrun2017",
                                "destination":
                                    "engine/xdg-cache/winetricks/vcrun2017",
                            },
                        ],
                        "launchers": [
                            {
                                "source": "launchers/run.sh",
                                "destination": "launchers/run.sh",
                                "digest": "sha256:" + digest_file(launcher),
                                "mode": 0o755,
                            },
                            {
                                "source": "launchers/sanitize.sh",
                                "destination": "launchers/sanitize.sh",
                                "digest": "sha256:" + digest_file(sanitizer),
                                "mode": 0o755,
                            },
                        ],
                        "protected_manifests": [],
                        "symlink_manifests": [
                            {
                                "source": "manifests/runtime.symlinks",
                                "prefixes": [
                                    "engine/xdg-data/umu/steamrt4"
                                ],
                                "allow_unresolved": True,
                            }
                        ],
                        "hardlink_manifests": [
                            {
                                "source":
                                    "manifests/runtime.hardlinks.json",
                                "root":
                                    "engine/xdg-data/umu/steamrt4",
                            }
                        ],
                        "mutable_paths": [
                            "payload/environment.txt",
                        ],
                        "paths": {
                            "launcher": "launchers/run.sh",
                            "sanitizer": "launchers/sanitize.sh",
                            "runtime_var": "engine/runtime/var",
                        },
                        "offline_environment": {
                            "xdg_data_home": "engine/xdg-data",
                            "xdg_cache_home": "engine/xdg-cache",
                            "runtime_update": False,
                            "runtime": {
                                "family": "steamrt4",
                                "version": "test",
                                "path": "umu/steamrt4",
                                "required_paths": [
                                    {
                                        "path": "VERSIONS.txt",
                                        "type": "file",
                                    },
                                    {
                                        "path": "_v2-entry-point",
                                        "type": "file",
                                    },
                                    {
                                        "path": "pressure-vessel",
                                        "type": "directory",
                                    },
                                    {
                                        "path":
                                            "steamrt4_platform_test",
                                        "type": "directory",
                                    },
                                ],
                            },
                            "cache_entries": [
                                {
                                    "id": "vcrun2017",
                                    "path": "winetricks/vcrun2017",
                                    "required_files": [
                                        {
                                            "path":
                                                "vc_redist.x64.exe",
                                            "digest":
                                                "sha256:"
                                                + self.cache_digest,
                                            "size": len(cache_payload),
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                }
            ],
        }

        self.capsule_path = self.capsule_root / "capsule.json"
        self.capsule_path.write_text(
            json.dumps(capsule),
            encoding="utf-8",
        )
        self.capsule = capsule

    def tearDown(self) -> None:
        os.environ["PATH"] = self.original_path
        os.environ.pop("OGV_TEST_SYSTEMD_RUN_LOG", None)
        self.temporary.cleanup()

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is not installed",
    )
    def test_schema_accepts_offline_contract(self) -> None:
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas/capsule.schema.json"
            ).read_text(encoding="utf-8")
        )
        errors = list(
            Draft202012Validator(schema).iter_errors(self.capsule)
        )
        self.assertEqual(errors, [])

    def test_materialize_run_and_verify_offline_dependencies(self) -> None:
        result = materialize_umu_profile(
            capsule_path=self.capsule_path,
            profile_id="linux-umu-offline",
            vault_root=self.vault,
            destination=self.destination,
        )
        self.assertTrue(result.complete)
        for name in ("JUGAR.sh", "VERIFICAR.sh", "DESINSTALAR.sh"):
            path = self.destination / name
            self.assertTrue(path.is_file())
            self.assertTrue(os.access(path, os.X_OK))

        runtime = (
            self.destination
            / "engine/xdg-data/umu/steamrt4"
        )
        original = (
            runtime
            / "steamrt4_platform_test/files/lib.bin"
        )
        linked = (
            runtime
            / "steamrt4_platform_test/files/lib-copy.bin"
        )
        self.assertEqual(
            original.stat().st_ino,
            linked.stat().st_ino,
        )
        self.assertEqual(
            os.readlink(runtime / "run-host"),
            "/run/host",
        )

        played = run_umu_materialization(
            destination=self.destination
        )
        self.assertTrue(played.complete)
        systemd_arguments = self.systemd_log.read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertIn("--property=PrivateNetwork=yes", systemd_arguments)
        self.assertIn(
            "--property=RestrictAddressFamilies=AF_UNIX AF_NETLINK AF_INET AF_INET6",
            systemd_arguments,
        )
        self.assertIn("--setenv=UMU_RUNTIME_UPDATE=0", systemd_arguments)

        environment = (
            self.destination
            / "payload/environment.txt"
        ).read_text(encoding="utf-8")
        self.assertIn(
            f"XDG_DATA_HOME={self.destination}/engine/xdg-data",
            environment,
        )
        self.assertIn(
            f"XDG_CACHE_HOME={self.destination}/engine/xdg-cache",
            environment,
        )
        self.assertIn(
            "UMU_RUNTIME_UPDATE=0",
            environment,
        )

        verified = verify_umu_materialization(
            destination=self.destination
        )
        self.assertTrue(verified.verified)

        cache_file = (
            self.destination
            / "engine/xdg-cache/winetricks/vcrun2017"
            / "vc_redist.x64.exe"
        )
        cache_file.write_bytes(b"changed")

        with self.assertRaisesRegex(
            UmuAdapterError,
            "Offline cache file",
        ):
            verify_umu_materialization(
                destination=self.destination
            )


    def test_incomplete_preserved_runtime_is_rejected_before_launch(self) -> None:
        incomplete = self.root / "incomplete-materialization"
        runtime = incomplete / "engine/xdg-data/umu/steamrt4"
        (runtime / "pressure-vessel").mkdir(parents=True)
        (runtime / "VERSIONS.txt").write_text(
            "steamrt4\ttest\n",
            encoding="utf-8",
        )
        entrypoint = runtime / "_v2-entry-point"
        entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        entrypoint.chmod(0o755)

        with self.assertRaisesRegex(
            UmuAdapterError,
            "steamrt4_platform_",
        ):
            _discover_offline_environment(incomplete)


if __name__ == "__main__":
    unittest.main()
