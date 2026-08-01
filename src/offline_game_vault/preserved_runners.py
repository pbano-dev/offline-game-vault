from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True, slots=True)
class RunnerRecord:
    runner_id: str
    digest: str
    archive_path: str
    size: int
    format: str
    source_root: str
    wine_path: str
    wineserver_path: str
    compatible_backends: tuple[str, ...]
    metadata_source: str
    proton_path: str | None = None
    kind: str = "wine"
    acceptance_status: str = "not_tested"

    def supports(self, backend_id: str) -> bool:
        return backend_id in self.compatible_backends

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class RunnerCatalogError(RuntimeError):
    pass


_RUNNER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RunnerCatalogError(f"{label} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerCatalogError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerCatalogError(f"{label} does not contain a JSON object")
    return value


def _safe_relative(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise RunnerCatalogError(f"{label} is not a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RunnerCatalogError(f"{label} is not a safe relative path")
    return path


def _strip_archive_suffix(label: str) -> str:
    for suffix in (".tar.gz", ".tgz", ".tar.zst", ".tar", ".zip"):
        if label.endswith(suffix):
            return label[: -len(suffix)]
    return label


def _capsule_hints(collection_root: Path) -> dict[str, list[dict[str, str]]]:
    hints: dict[str, list[dict[str, str]]] = {}
    capsules_root = collection_root / "02_CAPSULES"
    if not capsules_root.is_dir() or capsules_root.is_symlink():
        return hints

    for capsule_path in sorted(capsules_root.glob("*/capsule.json")):
        try:
            capsule = _load_json(capsule_path, str(capsule_path))
        except RunnerCatalogError:
            continue

        objects = capsule.get("objects")
        profiles = capsule.get("profiles")
        if not isinstance(objects, list) or not isinstance(profiles, list):
            continue

        object_index = {
            item.get("id"): item
            for item in objects
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            playable = profile.get("playable")
            if (
                profile.get("platform") != "linux"
                or profile.get("adapter") != "wine"
                or not isinstance(playable, dict)
                or playable.get("backend") != "wine"
            ):
                continue

            paths = playable.get("paths")
            layout = playable.get("layout")
            dependencies = profile.get("dependencies")
            if (
                not isinstance(paths, dict)
                or not isinstance(layout, list)
                or not isinstance(dependencies, list)
            ):
                continue

            runner_objects = []
            for dependency in dependencies:
                declaration = object_index.get(dependency)
                roles = declaration.get("roles") if isinstance(declaration, dict) else None
                if isinstance(roles, list) and "runner" in roles:
                    runner_objects.append(declaration)

            if len(runner_objects) != 1:
                continue

            declaration = runner_objects[0]
            object_id = declaration.get("id")
            digest = declaration.get("digest")
            object_format = declaration.get("format")
            if (
                not isinstance(object_id, str)
                or not isinstance(digest, str)
                or not digest.startswith("sha256:")
                or not isinstance(object_format, str)
                or not object_format
            ):
                continue

            mappings = [
                item
                for item in layout
                if isinstance(item, dict) and item.get("object") == object_id
            ]
            if len(mappings) != 1:
                continue

            mapping = mappings[0]
            source = mapping.get("source")
            runner_destination = paths.get("runner")
            wine = paths.get("wine")
            wineserver = paths.get("wineserver")
            if not all(
                isinstance(value, str) and value
                for value in (source, runner_destination, wine, wineserver)
            ):
                continue

            prefix = runner_destination.rstrip("/") + "/"
            if not wine.startswith(prefix) or not wineserver.startswith(prefix):
                continue

            hints.setdefault(digest, []).append(
                {
                    "runner_id": object_id,
                    "source_root": source,
                    "wine_path": wine[len(prefix):],
                    "wineserver_path": wineserver[len(prefix):],
                    "format": object_format,
                    "metadata_source": (
                        "capsule:"
                        + capsule_path.relative_to(collection_root).as_posix()
                    ),
                }
            )

    return hints


def _receipt_hints(collection_root: Path) -> dict[str, list[dict[str, str]]]:
    hints: dict[str, list[dict[str, str]]] = {}
    operations = collection_root / "04_RECEIPTS/_collection/operations"
    if not operations.is_dir() or operations.is_symlink():
        return hints

    for receipt_path in sorted(operations.glob("*/receipt.json")):
        try:
            receipt = _load_json(receipt_path, str(receipt_path))
        except RunnerCatalogError:
            continue

        if receipt.get("operation") != "add-shared-runner":
            continue

        runner = receipt.get("runner")
        archive = receipt.get("archive")
        if not isinstance(runner, dict) or not isinstance(archive, dict):
            continue

        runner_id = runner.get("id")
        digest = archive.get("digest")
        top_level = archive.get("top_level")
        archive_format = archive.get("format")
        if (
            not isinstance(runner_id, str)
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or not isinstance(top_level, str)
            or not isinstance(archive_format, str)
            or not archive_format
        ):
            continue

        tree_path = receipt_path.parent / "source-tree.json"
        try:
            tree = _load_json(tree_path, str(tree_path))
        except RunnerCatalogError:
            continue

        entries = tree.get("entries")
        if not isinstance(entries, list):
            continue
        paths = {
            item.get("path")
            for item in entries
            if isinstance(item, dict) and item.get("type") in {"file", "symlink"}
        }

        candidates = []
        for wine_path, wineserver_path in (
            ("files/bin/wine", "files/bin/wineserver"),
            ("bin/wine", "bin/wineserver"),
        ):
            if wine_path in paths and wineserver_path in paths:
                candidates.append((wine_path, wineserver_path))

        if len(candidates) != 1:
            continue

        wine_path, wineserver_path = candidates[0]
        hints.setdefault(digest, []).append(
            {
                "runner_id": runner_id,
                "source_root": top_level,
                "wine_path": wine_path,
                "wineserver_path": wineserver_path,
                "format": archive_format,
                "metadata_source": (
                    "receipt:"
                    + receipt_path.relative_to(collection_root).as_posix()
                ),
            }
        )

    return hints


def _archive_format(archive_path: Path, expected_label: str) -> str:
    with archive_path.open("rb", buffering=0) as handle:
        magic = handle.read(4)

    if magic.startswith(b"\x1f\x8b\x08"):
        return "tar.gz"
    if magic == b"\x28\xb5\x2f\xfd":
        return "tar.zst"
    if magic.startswith(b"PK\x03\x04"):
        return "zip"

    lowered = expected_label.casefold()
    if lowered.endswith(".tar"):
        return "tar"
    raise RunnerCatalogError(
        f"Could not determine the format of {archive_path.name}"
    )


def _tar_members(archive_path: Path) -> set[str]:
    members: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            for member in archive:
                raw = member.name.rstrip("/")
                if raw:
                    members.add(
                        _safe_relative(raw, "runner member").as_posix()
                    )
    except (OSError, tarfile.TarError, RunnerCatalogError) as exc:
        raise RunnerCatalogError(
            f"Could not inspect runner archive {archive_path.name}: {exc}"
        ) from exc
    return members


def _archive_members(
    archive_path: Path,
    expected_label: str,
) -> tuple[str, set[str]]:
    format_name = _archive_format(archive_path, expected_label)
    if format_name in {"tar", "tar.gz"}:
        return format_name, _tar_members(archive_path)

    if format_name == "zip":
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = {
                    _safe_relative(
                        item.filename.rstrip("/"),
                        "runner member",
                    ).as_posix()
                    for item in archive.infolist()
                    if item.filename.rstrip("/")
                }
        except (
            OSError,
            UnicodeError,
            zipfile.BadZipFile,
            RunnerCatalogError,
        ) as exc:
            raise RunnerCatalogError(
                f"Could not inspect runner ZIP {archive_path.name}: {exc}"
            ) from exc
        return format_name, members

    zstd = shutil.which("zstd")
    if zstd is None:
        raise RunnerCatalogError(
            "A tar.zst runner requires the preserved host zstd executable "
            "to inspect its structure"
        )
    with tempfile.TemporaryDirectory(prefix="ogv-runner-inspect-") as temporary:
        decompressed = Path(temporary) / "runner.tar"
        with decompressed.open("wb") as output:
            process = subprocess.run(
                [zstd, "--decompress", "--stdout", str(archive_path)],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
        if process.returncode != 0:
            detail = process.stderr.decode(
                "utf-8", errors="replace"
            ).strip()
            raise RunnerCatalogError(
                f"Could not decompress runner tar.zst: {detail}"
            )
        return format_name, _tar_members(decompressed)


def _archive_hint(
    archive_path: Path,
    expected_label: str,
) -> dict[str, str]:
    format_name, members = _archive_members(
        archive_path,
        expected_label,
    )
    roots = {
        _safe_relative(raw, "runner member").parts[0]
        for raw in members
    }

    if len(roots) != 1:
        raise RunnerCatalogError(
            f"Runner archive {archive_path.name} does not have exactly one root"
        )
    source_root = next(iter(roots))
    candidate_id = _strip_archive_suffix(expected_label)
    runner_id = source_root
    if not _RUNNER_ID_RE.fullmatch(runner_id):
        raise RunnerCatalogError(
            f"The runner root is not a portable identifier: {runner_id!r}"
        )

    candidates = []
    for wine_path, wineserver_path in (
        ("files/bin/wine", "files/bin/wineserver"),
        ("bin/wine", "bin/wineserver"),
    ):
        if (
            f"{source_root}/{wine_path}" in members
            and f"{source_root}/{wineserver_path}" in members
        ):
            candidates.append((wine_path, wineserver_path))

    if len(candidates) != 1:
        raise RunnerCatalogError(
            f"{runner_id}: no unambiguous Wine/Wineserver pair exists"
        )

    wine_path, wineserver_path = candidates[0]
    source = "archive-scan"
    if candidate_id and candidate_id != runner_id:
        source += f":label={candidate_id}"

    proton_path = (
        "proton"
        if f"{source_root}/proton" in members
        else None
    )
    return {
        "runner_id": runner_id,
        "source_root": source_root,
        "wine_path": wine_path,
        "wineserver_path": wineserver_path,
        "proton_path": proton_path,
        "format": format_name,
        "metadata_source": source,
    }


def _coalesce_hint(
    digest: str,
    candidates: list[dict[str, str]],
) -> dict[str, str] | None:
    if not candidates:
        return None

    normalized = {
        (
            item["runner_id"],
            item["source_root"],
            item["wine_path"],
            item["wineserver_path"],
            item.get("format"),
        )
        for item in candidates
    }
    if len(normalized) != 1:
        raise RunnerCatalogError(
            f"{digest}: contradictory runner metadata"
        )

    selected = dict(candidates[0])
    selected["metadata_source"] = ",".join(
        sorted({item["metadata_source"] for item in candidates})
    )
    return selected


def scan_runners(
    collection_root: Path,
) -> tuple[tuple[RunnerRecord, ...], tuple[str, ...]]:
    collection_root = Path(collection_root)
    if collection_root.is_symlink() or not collection_root.is_dir():
        raise RunnerCatalogError("The collection is not a regular directory")

    immutable_root = collection_root / "01_IMMUTABLE_VAULT"
    inventory = _load_json(
        immutable_root / "VAULT_INVENTORY.json",
        "VAULT_INVENTORY.json",
    )
    index = _load_json(collection_root / "INDEX.json", "INDEX.json")

    inventory_objects = inventory.get("objects")
    index_objects = index.get("objects")
    if not isinstance(inventory_objects, list) or not isinstance(index_objects, list):
        raise RunnerCatalogError("Inventory or index has no objects[]")

    inventory_by_digest: dict[str, dict[str, Any]] = {}
    for item in inventory_objects:
        if not isinstance(item, dict):
            continue
        digest = item.get("digest")
        if isinstance(digest, str):
            inventory_by_digest[digest] = item

    hints = _capsule_hints(collection_root)
    for digest, values in _receipt_hints(collection_root).items():
        hints.setdefault(digest, []).extend(values)

    runners: list[RunnerRecord] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for item in index_objects:
        if not isinstance(item, dict) or item.get("role") != "shared-runner":
            continue

        label = item.get("label")
        digest_hex = item.get("sha256")
        relative = item.get("path")
        size = item.get("size")
        if (
            not isinstance(label, str)
            or not isinstance(digest_hex, str)
            or not _SHA256_RE.fullmatch(digest_hex)
            or not isinstance(relative, str)
            or not isinstance(size, int)
            or size < 0
        ):
            warnings.append("Skipped runner with invalid INDEX entry")
            continue

        digest = f"sha256:{digest_hex}"
        inventory_item = inventory_by_digest.get(digest)
        if not isinstance(inventory_item, dict):
            warnings.append(f"Skipped {label}: not present in VAULT_INVENTORY")
            continue
        if (
            inventory_item.get("path") != relative
            or inventory_item.get("bytes") != size
        ):
            warnings.append(f"Skipped {label}: INDEX and inventory disagree")
            continue

        try:
            relative_path = _safe_relative(relative, f"{label}.path")
        except RunnerCatalogError as exc:
            warnings.append(str(exc))
            continue

        physical = immutable_root.joinpath(*relative_path.parts)
        try:
            info = physical.lstat()
        except FileNotFoundError:
            warnings.append(f"Skipped {label}: physical object is missing")
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            warnings.append(f"Skipped {label}: object is not regular")
            continue
        if info.st_size != size:
            warnings.append(f"Skipped {label}: physical size differs")
            continue
        if _sha256_file(physical) != digest_hex:
            warnings.append(f"Skipped {label}: physical SHA-256 differs")
            continue

        try:
            hint = _coalesce_hint(digest, hints.get(digest, []))
            if hint is None:
                hint = _archive_hint(physical, label)
            else:
                # Metadata remains authoritative. Archive inspection only
                # augments it when the preserved object is a readable archive.
                try:
                    archive_hint = _archive_hint(physical, label)
                except RunnerCatalogError:
                    archive_hint = None
                if (
                    archive_hint is not None
                    and hint.get("source_root")
                    == archive_hint.get("source_root")
                ):
                    hint["proton_path"] = archive_hint.get("proton_path")

            runner_id = hint["runner_id"]
            if not _RUNNER_ID_RE.fullmatch(runner_id):
                raise RunnerCatalogError(
                    f"{label}: runner_id is not portable"
                )
            if runner_id in seen_ids:
                raise RunnerCatalogError(
                    f"{runner_id}: duplicate runner identifier"
                )

            for key in ("source_root", "wine_path", "wineserver_path"):
                _safe_relative(hint[key], f"{runner_id}.{key}")

            format_name = hint.get("format")
            if format_name not in {"tar", "tar.gz", "tar.zst", "zip"}:
                raise RunnerCatalogError(
                    f"{runner_id}: unsupported or ambiguous runner format"
                )

            runners.append(
                RunnerRecord(
                    runner_id=runner_id,
                    digest=digest,
                    archive_path=relative,
                    size=size,
                    format=format_name,
                    source_root=hint["source_root"],
                    wine_path=hint["wine_path"],
                    wineserver_path=hint["wineserver_path"],
                    compatible_backends=(
                        ("direct-wine", "bottles", "umu")
                        if hint.get("proton_path") == "proton"
                        else ("direct-wine", "bottles")
                    ),
                    metadata_source=hint["metadata_source"],
                    proton_path=(
                        str(hint["proton_path"])
                        if hint.get("proton_path")
                        else None
                    ),
                    kind=(
                        "proton"
                        if hint.get("proton_path")
                        else "wine"
                    ),
                    acceptance_status="not_tested",
                )
            )
            seen_ids.add(runner_id)
        except RunnerCatalogError as exc:
            warnings.append(f"Skipped {label}: {exc}")

    runners.sort(key=lambda item: item.runner_id.casefold())
    if not runners:
        detail = "; ".join(warnings) if warnings else "no shared runner"
        raise RunnerCatalogError(
            f"No usable shared runner exists: {detail}"
        )

    return tuple(runners), tuple(warnings)


def validate_runner_record(
    collection_root: Path,
    runner: RunnerRecord,
) -> None:
    immutable_root = Path(collection_root) / "01_IMMUTABLE_VAULT"
    inventory = _load_json(
        immutable_root / "VAULT_INVENTORY.json",
        "VAULT_INVENTORY.json",
    )
    index = _load_json(Path(collection_root) / "INDEX.json", "INDEX.json")

    inventory_objects = inventory.get("objects")
    index_objects = index.get("objects")
    if not isinstance(inventory_objects, list) or not isinstance(index_objects, list):
        raise RunnerCatalogError("Inventory or index has no objects[]")

    inventory_matches = [
        item
        for item in inventory_objects
        if isinstance(item, dict) and item.get("digest") == runner.digest
    ]
    index_matches = [
        item
        for item in index_objects
        if (
            isinstance(item, dict)
            and item.get("role") == "shared-runner"
            and f"sha256:{item.get('sha256')}" == runner.digest
        )
    ]
    if len(inventory_matches) != 1 or len(index_matches) != 1:
        raise RunnerCatalogError(
            f"{runner.runner_id}: exactly one canonical reference does not exist"
        )

    inventory_item = inventory_matches[0]
    index_item = index_matches[0]
    if (
        inventory_item.get("path") != runner.archive_path
        or inventory_item.get("bytes") != runner.size
        or index_item.get("path") != runner.archive_path
        or index_item.get("size") != runner.size
    ):
        raise RunnerCatalogError(
            f"{runner.runner_id}: descriptor changed after catalog loading"
        )

    physical = immutable_root.joinpath(
        *_safe_relative(runner.archive_path, "runner.archive_path").parts
    )
    info = physical.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RunnerCatalogError(
            f"{runner.runner_id}: physical object is no longer regular"
        )
    if info.st_size != runner.size:
        raise RunnerCatalogError(
            f"{runner.runner_id}: physical size changed"
        )
    expected = runner.digest.removeprefix("sha256:")
    if _sha256_file(physical) != expected:
        raise RunnerCatalogError(
            f"{runner.runner_id}: physical SHA-256 changed"
        )
