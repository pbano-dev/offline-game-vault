# Backend-neutral optional immutable content selection and placement.

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Sequence
import zipfile


_OPTIONAL_RECEIPT_NAME = ".ogv-optional-content.json"
_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789._-"
)


class OptionalContentError(RuntimeError):
    pass


@dataclass(frozen=True)
class OptionalContentRecord:
    content_id: str
    object_id: str
    classification: str
    description: str | None
    source: PurePosixPath
    mode: str
    destination: PurePosixPath
    digest: str
    archive_path: PurePosixPath
    format: str
    size: int | None
    object_declaration: dict[str, Any]
    raw_item: dict[str, Any]


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OptionalContentError(f"Cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise OptionalContentError(f"{label} must be a JSON object.")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _portable_id(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value[0] in "._-"
        or any(char not in _ID_CHARS for char in value)
    ):
        raise OptionalContentError(f"{label} is not a portable identifier.")
    return value


def _safe_relative(
    value: Any,
    label: str,
    *,
    allow_dot: bool = False,
) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise OptionalContentError(f"{label} must be a non-empty relative path.")
    if "\\" in value:
        raise OptionalContentError(f"{label} must use POSIX separators.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise OptionalContentError(f"{label} must stay relative.")
    normalized = PurePosixPath(path.as_posix())
    if normalized == PurePosixPath(".") and not allow_dot:
        raise OptionalContentError(f"{label} must not be '.'.")
    return normalized


def _object_index(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = document.get("objects")
    if not isinstance(raw, list):
        raise OptionalContentError("capsule.objects must be an array.")
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise OptionalContentError("Every capsule object must be an object.")
        object_id = _portable_id(item.get("id"), "object.id")
        if object_id in result:
            raise OptionalContentError(
                f"Duplicate capsule object id: {object_id!r}."
            )
        result[object_id] = item
    return result


def _manifest_path(collection_root: Path, digest: str) -> Path | None:
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    hexdigest = digest.removeprefix("sha256:")
    if (
        len(hexdigest) != 64
        or any(char not in "0123456789abcdef" for char in hexdigest)
    ):
        return None
    return (
        collection_root
        / "01_IMMUTABLE_VAULT"
        / "manifests"
        / "sha256"
        / hexdigest[:2]
        / hexdigest[2:4]
        / hexdigest
    )


def _records(document: dict[str, Any]) -> tuple[OptionalContentRecord, ...]:
    object_index = _object_index(document)
    raw_items = document.get("optional_content", [])
    if not isinstance(raw_items, list):
        raise OptionalContentError("capsule.optional_content must be an array.")

    seen_ids: set[str] = set()
    records: list[OptionalContentRecord] = []
    optional_object_ids: set[str] = set()

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise OptionalContentError(
                f"optional_content[{index}] must be an object."
            )
        content_id = _portable_id(raw.get("id"), f"optional_content[{index}].id")
        if content_id in seen_ids:
            raise OptionalContentError(
                f"Duplicate optional-content id: {content_id!r}."
            )
        seen_ids.add(content_id)

        object_id = _portable_id(
            raw.get("object"), f"optional_content[{index}].object"
        )
        declaration = object_index.get(object_id)
        if declaration is None:
            raise OptionalContentError(
                f"Optional content {content_id!r} references absent "
                f"object {object_id!r}."
            )

        digest = declaration.get("digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise OptionalContentError(
                f"Optional object {object_id!r} has no sha256 digest."
            )
        archive_path = _safe_relative(
            declaration.get("archive_path"),
            f"object {object_id!r}.archive_path",
        )
        archive_format = declaration.get("format")
        if archive_format not in {"tar", "tar.gz", "tar.zst", "zip"}:
            raise OptionalContentError(
                f"Optional object {object_id!r} has unsupported format "
                f"{archive_format!r}."
            )
        size = declaration.get("size")
        if size is not None and (
            not isinstance(size, int) or isinstance(size, bool) or size < 0
        ):
            raise OptionalContentError(
                f"Optional object {object_id!r}.size is invalid."
            )

        classification = raw.get("classification")
        if not isinstance(classification, str) or not classification:
            raise OptionalContentError(
                f"Optional content {content_id!r} has no classification."
            )
        description = raw.get("description")
        if description is not None and not isinstance(description, str):
            raise OptionalContentError(
                f"Optional content {content_id!r}.description must be text."
            )

        source = _safe_relative(
            raw.get("source"), f"optional_content[{index}].source"
        )
        placement = raw.get("placement")
        if not isinstance(placement, dict):
            raise OptionalContentError(
                f"Optional content {content_id!r} has no placement object."
            )
        mode = placement.get("mode")
        if mode not in {"game-overlay", "sidecar"}:
            raise OptionalContentError(
                f"Optional content {content_id!r} has unsupported "
                f"placement mode {mode!r}."
            )
        destination = _safe_relative(
            placement.get("destination"),
            f"optional_content[{index}].placement.destination",
            allow_dot=True,
        )

        optional_object_ids.add(object_id)
        records.append(
            OptionalContentRecord(
                content_id=content_id,
                object_id=object_id,
                classification=classification,
                description=description,
                source=source,
                mode=mode,
                destination=destination,
                digest=digest,
                archive_path=archive_path,
                format=archive_format,
                size=size,
                object_declaration=dict(declaration),
                raw_item=dict(raw),
            )
        )

    raw_profiles = document.get("profiles", [])
    if not isinstance(raw_profiles, list):
        raise OptionalContentError("capsule.profiles must be an array.")
    for profile in raw_profiles:
        if not isinstance(profile, dict):
            continue
        dependencies = profile.get("dependencies", [])
        if not isinstance(dependencies, list):
            continue
        collision = optional_object_ids.intersection(
            value for value in dependencies if isinstance(value, str)
        )
        if collision:
            joined = ", ".join(sorted(collision))
            raise OptionalContentError(
                "Optional-content objects must remain outside profile "
                f"dependencies until explicitly selected: {joined}."
            )

    return tuple(records)


def list_optional_content(
    *,
    capsule_path: Path,
    collection_root: Path,
) -> list[dict[str, Any]]:
    capsule_path = capsule_path.expanduser().resolve(strict=True)
    collection_root = collection_root.expanduser().resolve(strict=True)
    document = _load_json(capsule_path, "capsule.json")
    records = _records(document)
    vault_root = collection_root / "01_IMMUTABLE_VAULT"

    result: list[dict[str, Any]] = []
    for item in records:
        archive = vault_root.joinpath(*item.archive_path.parts)
        manifest = _manifest_path(collection_root, item.digest)
        object_present = archive.is_file() and not archive.is_symlink()
        manifest_present = manifest is not None and manifest.is_file()
        sidecar_present = (
            manifest is not None
            and manifest.with_name(manifest.name + ".sha256").is_file()
        )
        result.append(
            {
                "id": item.content_id,
                "object_id": item.object_id,
                "classification": item.classification,
                "description": item.description,
                "source": item.source.as_posix(),
                "placement": {
                    "mode": item.mode,
                    "destination": item.destination.as_posix(),
                },
                "digest": item.digest,
                "format": item.format,
                "size": item.size,
                "object_present": object_present,
                "manifest_present": manifest_present,
                "manifest_sidecar_present": sidecar_present,
                "available": (
                    object_present and manifest_present and sidecar_present
                ),
            }
        )
    return result


def prepare_operational_optional_content(
    *,
    source_capsule_path: Path,
    operational_capsule_path: Path,
    selected_ids: Sequence[str],
) -> tuple[Path, tuple[OptionalContentRecord, ...]]:
    source_capsule_path = source_capsule_path.expanduser().resolve(strict=True)
    operational_capsule_path = (
        operational_capsule_path.expanduser().resolve(strict=True)
    )
    source_document = _load_json(source_capsule_path, "source capsule")
    records = _records(source_document)

    requested = list(selected_ids)
    if any(not isinstance(value, str) or not value for value in requested):
        raise OptionalContentError("Every selected content id must be text.")
    if len(requested) != len(set(requested)):
        raise OptionalContentError(
            "The same optional-content id cannot be selected more than once."
        )
    if not records and not requested:
        return operational_capsule_path, ()

    by_id = {item.content_id: item for item in records}
    unknown = [value for value in requested if value not in by_id]
    if unknown:
        raise OptionalContentError(
            "Unknown optional-content id(s): " + ", ".join(unknown)
        )
    selected = tuple(by_id[value] for value in requested)

    operational = _load_json(operational_capsule_path, "operational capsule")
    objects = operational.get("objects")
    if not isinstance(objects, list):
        raise OptionalContentError("Operational capsule has no objects array.")

    optional_object_ids = {item.object_id for item in records}
    filtered = [
        value
        for value in objects
        if not (
            isinstance(value, dict)
            and value.get("id") in optional_object_ids
        )
    ]

    selected_object_ids: set[str] = set()
    for item in selected:
        if item.object_id in selected_object_ids:
            continue
        selected_object_ids.add(item.object_id)
        filtered.append(dict(item.object_declaration))

    operational["objects"] = filtered
    if records:
        operational["optional_content"] = [dict(item.raw_item) for item in selected]
    else:
        operational.pop("optional_content", None)

    operational_capsule_path.write_bytes(_canonical_bytes(operational))
    return operational_capsule_path, selected


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_archive(
    vault_root: Path,
    record: OptionalContentRecord,
) -> Path:
    vault_root = vault_root.expanduser().resolve(strict=True)
    archive = vault_root.joinpath(*record.archive_path.parts)
    try:
        resolved = archive.resolve(strict=True)
    except OSError as exc:
        raise OptionalContentError(
            f"Optional object {record.object_id!r} is absent."
        ) from exc
    try:
        resolved.relative_to(vault_root)
    except ValueError as exc:
        raise OptionalContentError(
            f"Optional object {record.object_id!r} escapes the Vault."
        ) from exc
    if archive.is_symlink() or not resolved.is_file():
        raise OptionalContentError(
            f"Optional object {record.object_id!r} is not a regular file."
        )
    if record.size is not None and resolved.stat().st_size != record.size:
        raise OptionalContentError(
            f"Optional object {record.object_id!r} size changed."
        )
    actual = "sha256:" + _sha256_file(resolved)
    if actual != record.digest:
        raise OptionalContentError(
            f"Optional object {record.object_id!r} digest changed."
        )
    return resolved


def _safe_archive_member(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise OptionalContentError("Archive contains an invalid member path.")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise OptionalContentError(
            f"Archive member escapes its object: {name!r}."
        )
    return PurePosixPath(path.as_posix())


def _tail_under(
    member: PurePosixPath,
    source: PurePosixPath,
) -> PurePosixPath | None:
    if member == source:
        return PurePosixPath(".")
    try:
        return member.relative_to(source)
    except ValueError:
        return None


def _stage_path(root: Path, tail: PurePosixPath) -> Path:
    if tail == PurePosixPath("."):
        return root
    return root.joinpath(*tail.parts)


def _remember_casefold(
    seen: dict[str, str],
    tail: PurePosixPath,
) -> None:
    if tail == PurePosixPath("."):
        return
    rendered = tail.as_posix()
    folded = rendered.casefold()
    previous = seen.get(folded)
    if previous is not None and previous != rendered:
        raise OptionalContentError(
            "Optional archive contains case-ambiguous paths: "
            f"{previous!r} and {rendered!r}."
        )
    seen[folded] = rendered


def _write_regular_member(
    *,
    root: Path,
    tail: PurePosixPath,
    stream: Any,
    mode: int,
) -> None:
    if tail == PurePosixPath("."):
        raise OptionalContentError(
            "Optional-content source must name a directory subtree, "
            "not one regular file."
        )
    target = _stage_path(root, tail)
    if target.exists() or target.is_symlink():
        raise OptionalContentError(
            f"Optional archive contains duplicate path {tail.as_posix()!r}."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        shutil.copyfileobj(stream, output, length=1024 * 1024)
    target.chmod(mode & 0o777)


def _extract_tar_stream(
    archive: Any,
    *,
    source: PurePosixPath,
    staging: Path,
) -> int:
    found = 0
    seen: dict[str, str] = {}
    for member in archive:
        path = _safe_archive_member(member.name)
        tail = _tail_under(path, source)
        if tail is None:
            continue
        _remember_casefold(seen, tail)
        if member.issym() or member.islnk():
            raise OptionalContentError(
                "Optional-content objects must not contain links."
            )
        if member.isdir():
            if tail != PurePosixPath("."):
                target = _stage_path(staging, tail)
                if target.exists() and not target.is_dir():
                    raise OptionalContentError(
                        f"Archive directory collides at {tail.as_posix()!r}."
                    )
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(member.mode & 0o777)
            found += 1
            continue
        if not member.isfile():
            raise OptionalContentError(
                "Optional-content objects may contain only regular files "
                "and directories."
            )
        extracted = archive.extractfile(member)
        if extracted is None:
            raise OptionalContentError(
                f"Cannot read archive member {member.name!r}."
            )
        with extracted:
            _write_regular_member(
                root=staging,
                tail=tail,
                stream=extracted,
                mode=member.mode,
            )
        found += 1
    return found


def _extract_tar(
    archive_path: Path,
    *,
    archive_format: str,
    source: PurePosixPath,
    staging: Path,
) -> None:
    if archive_format in {"tar", "tar.gz"}:
        mode = "r:" if archive_format == "tar" else "r:gz"
        with tarfile.open(archive_path, mode) as archive:
            found = _extract_tar_stream(
                archive,
                source=source,
                staging=staging,
            )
    else:
        try:
            process = subprocess.Popen(
                ["zstd", "-dc", "--", str(archive_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise OptionalContentError(
                "zstd is required for tar.zst optional-content objects."
            ) from exc
        assert process.stdout is not None
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                found = _extract_tar_stream(
                    archive,
                    source=source,
                    staging=staging,
                )
        finally:
            process.stdout.close()
        stderr = (
            process.stderr.read().decode("utf-8", errors="replace")
            if process.stderr is not None
            else ""
        )
        returncode = process.wait()
        if returncode != 0:
            raise OptionalContentError(
                f"zstd failed while reading optional content: {stderr.strip()}"
            )

    if found == 0:
        raise OptionalContentError(
            f"Optional-content source {source.as_posix()!r} "
            "does not exist in its archive."
        )


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _extract_zip(
    archive_path: Path,
    *,
    source: PurePosixPath,
    staging: Path,
) -> None:
    found = 0
    seen: dict[str, str] = {}
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename.rstrip("/")
            if not name:
                continue
            path = _safe_archive_member(name)
            tail = _tail_under(path, source)
            if tail is None:
                continue
            _remember_casefold(seen, tail)
            if _zip_is_symlink(info):
                raise OptionalContentError(
                    "Optional-content objects must not contain links."
                )
            if info.is_dir():
                if tail != PurePosixPath("."):
                    target = _stage_path(staging, tail)
                    if target.exists() and not target.is_dir():
                        raise OptionalContentError(
                            f"Archive directory collides at {tail.as_posix()!r}."
                        )
                    target.mkdir(parents=True, exist_ok=True)
                found += 1
                continue
            with archive.open(info, "r") as stream:
                mode = (info.external_attr >> 16) & 0o777
                _write_regular_member(
                    root=staging,
                    tail=tail,
                    stream=stream,
                    mode=mode or 0o644,
                )
            found += 1

    if found == 0:
        raise OptionalContentError(
            f"Optional-content source {source.as_posix()!r} "
            "does not exist in its archive."
        )


def _extract_selected_subtree(
    *,
    archive_path: Path,
    archive_format: str,
    source: PurePosixPath,
    staging: Path,
) -> None:
    staging.mkdir(parents=True, mode=0o700, exist_ok=True)
    if archive_format == "zip":
        _extract_zip(
            archive_path,
            source=source,
            staging=staging,
        )
        return
    _extract_tar(
        archive_path,
        archive_format=archive_format,
        source=source,
        staging=staging,
    )


def _preflight_merge(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise OptionalContentError(
            f"Optional-content destination is a symlink: {destination}."
        )
    if destination.exists() and not destination.is_dir():
        raise OptionalContentError(
            f"Optional-content destination is not a directory: {destination}."
        )

    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        target_root = destination / relative_root

        if target_root.is_symlink():
            raise OptionalContentError(
                f"Optional content would traverse symlink {target_root}."
            )
        if target_root.exists() and not target_root.is_dir():
            raise OptionalContentError(
                f"Optional content collides with {target_root}."
            )

        for name in dirs:
            candidate = target_root / name
            if candidate.is_symlink():
                raise OptionalContentError(
                    f"Optional content would traverse symlink {candidate}."
                )
            if candidate.exists() and not candidate.is_dir():
                raise OptionalContentError(
                    f"Optional content collides with {candidate}."
                )

        for name in files:
            candidate = target_root / name
            if candidate.exists() or candidate.is_symlink():
                raise OptionalContentError(
                    f"Optional content collides with existing path {candidate}."
                )


def _merge_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        target_root = destination / relative_root
        target_root.mkdir(parents=True, exist_ok=True)
        for name in dirs:
            (target_root / name).mkdir(parents=True, exist_ok=True)
        for name in files:
            shutil.copy2(root_path / name, target_root / name)


def _selected_source_contract(
    *,
    source_capsule_path: Path,
    source_profile_id: str,
) -> dict[str, Any]:
    source = _load_json(source_capsule_path, "source capsule")
    profiles = source.get("profiles")
    if not isinstance(profiles, list):
        raise OptionalContentError("Source capsule has no profiles array.")
    matches = [
        value
        for value in profiles
        if isinstance(value, dict) and value.get("id") == source_profile_id
    ]
    if len(matches) != 1:
        raise OptionalContentError("Selected source profile is not unique.")
    profile = matches[0]
    contract_relative = _safe_relative(
        profile.get("host_contract"),
        "source profile host_contract",
    )
    contract_path = source_capsule_path.parent.joinpath(*contract_relative.parts)
    resolved = contract_path.resolve(strict=True)
    try:
        resolved.relative_to(source_capsule_path.parent.resolve(strict=True))
    except ValueError as exc:
        raise OptionalContentError(
            "Source host contract escapes its capsule."
        ) from exc
    contract = _load_json(resolved, "source host contract")
    if contract.get("contract") not in {
        "ogv-game-source-v1",
        "ogv-bottles-neutral-v1",
        "ogv-direct-wine-neutral-v1",
        "ogv-umu-neutral-v1",
    }:
        raise OptionalContentError(
            "Optional game content requires a backend-neutral source contract."
        )
    return contract


def _logical_game_root(
    *,
    source_capsule_path: Path,
    source_profile_id: str,
    operational_capsule_path: Path,
    operational_profile_id: str,
    destination: Path,
) -> Path:
    source_contract = _selected_source_contract(
        source_capsule_path=source_capsule_path,
        source_profile_id=source_profile_id,
    )
    entrypoint_relative = _safe_relative(
        source_contract.get("entrypoint_relative_to_game"),
        "entrypoint_relative_to_game",
    )

    operational = _load_json(operational_capsule_path, "operational capsule")
    profiles = operational.get("profiles")
    if not isinstance(profiles, list):
        raise OptionalContentError("Operational capsule has no profiles array.")
    matches = [
        value
        for value in profiles
        if isinstance(value, dict) and value.get("id") == operational_profile_id
    ]
    if len(matches) != 1:
        raise OptionalContentError("Operational profile is not unique.")
    launch = matches[0].get("launch")
    if not isinstance(launch, dict):
        raise OptionalContentError("Operational profile has no launch object.")
    operational_entrypoint = _safe_relative(
        launch.get("entrypoint"),
        "operational launch.entrypoint",
    )

    entry_parts = entrypoint_relative.parts
    actual_parts = operational_entrypoint.parts
    if (
        len(entry_parts) > len(actual_parts)
        or tuple(actual_parts[-len(entry_parts):]) != tuple(entry_parts)
    ):
        raise OptionalContentError(
            "Operational entrypoint no longer maps to the neutral game root."
        )

    root_parts = actual_parts[:-len(entry_parts)]
    if not root_parts:
        raise OptionalContentError("Cannot derive a materialized game root.")
    root = destination.joinpath(*root_parts)
    resolved_destination = destination.resolve(strict=True)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise OptionalContentError(
            "Derived materialized game root does not exist."
        ) from exc
    try:
        resolved_root.relative_to(resolved_destination)
    except ValueError as exc:
        raise OptionalContentError(
            "Derived materialized game root escapes the destination."
        ) from exc
    if root.is_symlink() or not resolved_root.is_dir():
        raise OptionalContentError(
            "Derived materialized game root is not a regular directory."
        )
    return resolved_root


def materialize_optional_content(
    *,
    source_capsule_path: Path,
    source_profile_id: str,
    operational_capsule_path: Path,
    operational_profile_id: str,
    vault_root: Path,
    destination: Path,
    records: Sequence[OptionalContentRecord],
) -> Path | None:
    if not records:
        return None

    destination = destination.expanduser().resolve(strict=True)
    source_capsule_path = source_capsule_path.expanduser().resolve(strict=True)
    operational_capsule_path = (
        operational_capsule_path.expanduser().resolve(strict=True)
    )

    installed: list[dict[str, Any]] = []
    game_root: Path | None = None

    for record in records:
        archive = _verified_archive(vault_root, record)

        if record.mode == "game-overlay":
            if game_root is None:
                game_root = _logical_game_root(
                    source_capsule_path=source_capsule_path,
                    source_profile_id=source_profile_id,
                    operational_capsule_path=operational_capsule_path,
                    operational_profile_id=operational_profile_id,
                    destination=destination,
                )
            base = game_root
        else:
            base = destination / "extras"

        target = (
            base
            if record.destination == PurePosixPath(".")
            else base.joinpath(*record.destination.parts)
        )
        current = base
        if current.is_symlink():
            raise OptionalContentError(
                f"Optional-content placement base is a symlink: {current}."
            )
        for part in record.destination.parts:
            if part == ".":
                continue
            current = current / part
            if current.is_symlink():
                raise OptionalContentError(
                    "Optional-content placement would traverse symlink "
                    f"{current}."
                )

        with tempfile.TemporaryDirectory(
            prefix=f".ogv-content-{record.content_id}-",
            dir=destination.parent,
        ) as raw_staging:
            staging = Path(raw_staging)
            _extract_selected_subtree(
                archive_path=archive,
                archive_format=record.format,
                source=record.source,
                staging=staging,
            )
            _preflight_merge(staging, target)
            _merge_tree(staging, target)

        try:
            installed_root = target.relative_to(destination).as_posix()
        except ValueError as exc:
            raise OptionalContentError(
                "Optional-content placement escaped the materialization."
            ) from exc

        installed.append(
            {
                "id": record.content_id,
                "object_id": record.object_id,
                "digest": record.digest,
                "classification": record.classification,
                "source": record.source.as_posix(),
                "placement": {
                    "mode": record.mode,
                    "destination": record.destination.as_posix(),
                },
                "installed_root": installed_root,
            }
        )

    receipt = destination / _OPTIONAL_RECEIPT_NAME
    if receipt.exists() or receipt.is_symlink():
        raise OptionalContentError("Optional-content receipt path already exists.")
    receipt.write_bytes(
        _canonical_bytes(
            {
                "schema": 0,
                "contract": "ogv-optional-content-receipt-v1",
                "selected_ids": [record.content_id for record in records],
                "items": installed,
            }
        )
    )
    receipt.chmod(0o600)
    return receipt
