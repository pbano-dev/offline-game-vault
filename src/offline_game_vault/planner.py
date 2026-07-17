"""Pure planning logic for Offline Game Vault."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import json


class PlanError(Exception):
    """Raised when a safe materialization plan cannot be produced."""


@dataclass(frozen=True)
class PlannedObject:
    object_id: str
    digest: str
    source: str
    present: bool
    format: str
    strategy: str
    required: bool
    shared: bool


@dataclass(frozen=True)
class MaterializationPlan:
    schema: int
    capsule_id: str
    profile_id: str
    adapter: str
    platform: str
    vault_root: str
    destination: str
    host_contract: str
    entrypoint: str
    working_directory: str | None
    network: str
    objects: tuple[PlannedObject, ...]
    missing_required_objects: tuple[str, ...]
    mutates_vault: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["objects"] = [asdict(item) for item in self.objects]
        data["missing_required_objects"] = list(
            self.missing_required_objects
        )
        return data


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanError(f"File not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise PlanError(f"File is not valid UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(
            f"Invalid JSON in {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise PlanError(f"Top-level JSON value must be an object: {path}")
    return data


def _safe_relative_path(value: str, field: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise PlanError(f"{field} must be a safe relative path: {value!r}")
    return path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _strategy_for_format(format_name: str) -> str:
    if format_name in {"tar", "tar.gz", "tar.zst", "zip"}:
        return "extract"
    if format_name == "directory":
        return "copy_tree"
    if format_name in {"file", "flatpak_repo", "other"}:
        return "copy"
    raise PlanError(f"Unsupported object format: {format_name!r}")


def build_plan(
    *,
    capsule_path: Path,
    profile_id: str,
    vault_root: Path,
    destination: Path,
    allow_missing: bool = False,
) -> MaterializationPlan:
    """Build a read-only materialization plan.

    This function does not create, copy, extract, or modify any file.
    """

    capsule_path = capsule_path.resolve()
    fixture_dir = capsule_path.parent
    vault_root = vault_root.expanduser().resolve()
    destination = destination.expanduser().resolve()

    if destination == vault_root or _is_within(destination, vault_root):
        raise PlanError(
            "Destination must be outside the immutable vault root."
        )

    capsule = load_json_object(capsule_path)

    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise PlanError("capsule.profiles must be an array")

    matching = [
        profile
        for profile in profiles
        if isinstance(profile, dict)
        and profile.get("id") == profile_id
    ]
    if not matching:
        raise PlanError(f"Unknown profile: {profile_id!r}")
    if len(matching) > 1:
        raise PlanError(f"Duplicate profile ID: {profile_id!r}")
    profile = matching[0]

    objects = capsule.get("objects")
    if not isinstance(objects, list):
        raise PlanError("capsule.objects must be an array")

    object_index: dict[str, dict[str, Any]] = {}
    for item in objects:
        if not isinstance(item, dict):
            raise PlanError("Every capsule object must be an object")
        object_id = item.get("id")
        if not isinstance(object_id, str) or not object_id:
            raise PlanError("Every capsule object requires a non-empty id")
        if object_id in object_index:
            raise PlanError(f"Duplicate object ID: {object_id!r}")
        object_index[object_id] = item

    dependencies = profile.get("dependencies")
    if not isinstance(dependencies, list):
        raise PlanError("profile.dependencies must be an array")

    planned_objects: list[PlannedObject] = []
    missing_required: list[str] = []

    for dependency in dependencies:
        if not isinstance(dependency, str):
            raise PlanError("Dependency IDs must be strings")
        item = object_index.get(dependency)
        if item is None:
            raise PlanError(
                f"Profile references unknown object: {dependency!r}"
            )

        archive_path = item.get("archive_path")
        if not isinstance(archive_path, str):
            raise PlanError(
                f"Object {dependency!r} has no archive_path"
            )

        relative_source = _safe_relative_path(
            archive_path,
            f"objects[{dependency!r}].archive_path",
        )
        source = (vault_root / relative_source).resolve()
        if not _is_within(source, vault_root):
            raise PlanError(
                f"Object {dependency!r} escapes the vault root"
            )

        format_name = item.get("format")
        if not isinstance(format_name, str):
            raise PlanError(
                f"Object {dependency!r} has no valid format"
            )

        required = item.get("required") is True
        present = source.exists()
        if required and not present:
            missing_required.append(dependency)

        digest = item.get("digest")
        if not isinstance(digest, str):
            raise PlanError(
                f"Object {dependency!r} has no valid digest"
            )

        planned_objects.append(
            PlannedObject(
                object_id=dependency,
                digest=digest,
                source=str(source),
                present=present,
                format=format_name,
                strategy=_strategy_for_format(format_name),
                required=required,
                shared=item.get("shared") is True,
            )
        )

    if missing_required and not allow_missing:
        joined = ", ".join(missing_required)
        raise PlanError(f"Missing required object(s): {joined}")

    host_contract_value = profile.get("host_contract")
    if not isinstance(host_contract_value, str):
        raise PlanError("profile.host_contract must be a relative path")
    host_contract_rel = _safe_relative_path(
        host_contract_value,
        "profile.host_contract",
    )
    host_contract_path = (fixture_dir / host_contract_rel).resolve()
    if not _is_within(host_contract_path, fixture_dir):
        raise PlanError("Host contract escapes fixture directory")
    if not host_contract_path.is_file():
        raise PlanError(
            f"Host contract not found: {host_contract_path}"
        )

    launch = profile.get("launch")
    if not isinstance(launch, dict):
        raise PlanError("profile.launch must be an object")

    entrypoint = launch.get("entrypoint")
    if not isinstance(entrypoint, str):
        raise PlanError("profile.launch.entrypoint must be a string")
    _safe_relative_path(entrypoint, "profile.launch.entrypoint")

    working_directory = launch.get("working_directory")
    if working_directory is not None:
        if not isinstance(working_directory, str):
            raise PlanError(
                "profile.launch.working_directory must be a string"
            )
        _safe_relative_path(
            working_directory,
            "profile.launch.working_directory",
        )

    adapter = profile.get("adapter")
    platform = profile.get("platform")
    network = launch.get("network", "host_default")
    capsule_id = capsule.get("capsule_id")

    for field_name, value in {
        "capsule_id": capsule_id,
        "profile.adapter": adapter,
        "profile.platform": platform,
        "profile.launch.network": network,
    }.items():
        if not isinstance(value, str) or not value:
            raise PlanError(f"{field_name} must be a non-empty string")

    return MaterializationPlan(
        schema=0,
        capsule_id=capsule_id,
        profile_id=profile_id,
        adapter=adapter,
        platform=platform,
        vault_root=str(vault_root),
        destination=str(destination),
        host_contract=str(host_contract_path),
        entrypoint=entrypoint,
        working_directory=working_directory,
        network=network,
        objects=tuple(planned_objects),
        missing_required_objects=tuple(missing_required),
    )
