from __future__ import annotations

from pathlib import Path, PurePosixPath
import json
import os
import re
import stat
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = REPOSITORY_ROOT / "fixtures"

REQUIRED_CORE = {
    "README.md",
    "acceptance.json",
    "capsule.json",
    "docs/00_README.md",
    "docs/FICHA_DEL_JUEGO.md",
    "docs/CREDITOS.md",
    "docs/PRESERVADO_POR.md",
}

PROHIBITED_SUFFIXES = {
    ".exe",
    ".dll",
    ".so",
    ".sl2",
    ".sav",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".zst",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".ogg",
    ".wav",
    ".flac",
}

PRIVATE_PATTERNS = (
    re.compile(r"/home/[^<\s]+"),
    re.compile(r"/var/home/[^<\s]+"),
    re.compile(r"/run/user/\d+"),
)


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")

    return value


def inventory(root: Path) -> tuple[set[str], int, int]:
    regular: set[str] = set()
    symlinks = 0
    special = 0

    for current, dirnames, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        retained: list[str] = []

        for name in sorted(dirnames):
            path = current_path / name
            mode = path.lstat().st_mode

            if stat.S_ISLNK(mode):
                symlinks += 1
            elif stat.S_ISDIR(mode):
                retained.append(name)
            else:
                special += 1

        dirnames[:] = retained

        for name in sorted(filenames):
            path = current_path / name
            mode = path.lstat().st_mode

            if stat.S_ISLNK(mode):
                symlinks += 1
            elif stat.S_ISREG(mode):
                regular.add(
                    path.relative_to(root).as_posix()
                )
            else:
                special += 1

    return regular, symlinks, special


class RepositoryFixtureTests(unittest.TestCase):
    def fixture_roots(self) -> list[Path]:
        roots = sorted(
            path
            for path in FIXTURES_ROOT.iterdir()
            if path.is_dir() and not path.is_symlink()
        )
        self.assertGreaterEqual(len(roots), 2)
        return roots

    def test_every_fixture_has_required_core(self) -> None:
        for fixture in self.fixture_roots():
            with self.subTest(fixture=fixture.name):
                files, symlinks, special = inventory(fixture)
                self.assertEqual(symlinks, 0)
                self.assertEqual(special, 0)
                self.assertTrue(REQUIRED_CORE.issubset(files))

                contracts = sorted(
                    relative
                    for relative in files
                    if relative.startswith("host-contract.")
                    and relative.endswith(".json")
                )
                self.assertGreaterEqual(len(contracts), 1)

    def test_profile_references_resolve(self) -> None:
        for fixture in self.fixture_roots():
            with self.subTest(fixture=fixture.name):
                capsule = read_json(fixture / "capsule.json")
                self.assertIs(capsule.get("sanitized_fixture"), True)
                profiles = capsule.get("profiles")
                self.assertIsInstance(profiles, list)

                for profile in profiles:
                    self.assertIsInstance(profile, dict)
                    host_contract = profile.get("host_contract")
                    self.assertIsInstance(host_contract, str)
                    contract_path = PurePosixPath(host_contract)
                    self.assertFalse(contract_path.is_absolute())
                    self.assertNotIn("..", contract_path.parts)
                    self.assertTrue((fixture / host_contract).is_file())

                    acceptance = profile.get("acceptance_report")

                    if acceptance is not None:
                        self.assertIsInstance(acceptance, str)
                        acceptance_path = PurePosixPath(acceptance)
                        self.assertFalse(acceptance_path.is_absolute())
                        self.assertNotIn("..", acceptance_path.parts)
                        self.assertTrue((fixture / acceptance).is_file())

    def test_documents_resolve(self) -> None:
        for fixture in self.fixture_roots():
            with self.subTest(fixture=fixture.name):
                capsule = read_json(fixture / "capsule.json")
                documents = capsule.get("documents")
                self.assertIsInstance(documents, dict)

                for relative in documents.values():
                    self.assertIsInstance(relative, str)
                    path = PurePosixPath(relative)
                    self.assertFalse(path.is_absolute())
                    self.assertNotIn("..", path.parts)
                    self.assertTrue((fixture / relative).is_file())

    def test_fixtures_have_no_payload_or_private_paths(self) -> None:
        for fixture in self.fixture_roots():
            files, _symlinks, _special = inventory(fixture)

            for relative in sorted(files):
                with self.subTest(
                    fixture=fixture.name,
                    file=relative,
                ):
                    path = fixture / relative
                    self.assertNotIn(
                        path.suffix.casefold(),
                        PROHIBITED_SUFFIXES,
                    )
                    text = path.read_text(
                        encoding="utf-8",
                        errors="strict",
                    )

                    for pattern in PRIVATE_PATTERNS:
                        self.assertIsNone(pattern.search(text))

    def test_optional_profiles_follow_recorded_evidence(self) -> None:
        sekiro = (
            FIXTURES_ROOT
            / "sekiro-shadows-die-twice"
        )
        capsule = read_json(sekiro / "capsule.json")
        profiles = capsule.get("profiles")
        self.assertIsInstance(profiles, list)
        profile_ids = {
            profile.get("id")
            for profile in profiles
            if isinstance(profile, dict)
        }
        self.assertEqual(
            profile_ids,
            {
                "linux-bottles-flatpak",
                "linux-direct-wine",
            },
        )
        self.assertTrue(
            (
                sekiro
                / "host-contract.linux-direct-wine.json"
            ).is_file()
        )
        self.assertTrue(
            (
                sekiro
                / "acceptance.direct-wine.json"
            ).is_file()
        )
        self.assertFalse(
            (
                sekiro
                / "host-contract.windows-native.json"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()
