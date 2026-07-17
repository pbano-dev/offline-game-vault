"""Profile-level ingestion and verification for the object store."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .storage import (
    IngestError,
    capsule_destination_spec,
    ingest_object,
)
from .verifier import VerifyError, resolve_capsule_object, verify_object


class ProfileStoreError(Exception):
    """Raised when profile-level store work cannot be completed safely."""


@dataclass(frozen=True)
class ProfileObjectIngest:
    object_id: str
    digest: str
    archive_path: str
    bytes: int
    status: str
    copied: bool
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProfileIngestResult:
    schema: int
    capsule_id: str
    profile_id: str
    object_count: int
    ingested_count: int
    already_present_count: int
    complete: bool
    objects: tuple[ProfileObjectIngest, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["objects"] = [asdict(item) for item in self.objects]
        return data


@dataclass(frozen=True)
class ProfileObjectVerification:
    object_id: str
    digest: str
    archive_path: str
    expected_size: int | None
    actual_size: int | None
    status: str
    verified: bool
    detail: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProfileVerificationResult:
    schema: int
    capsule_id: str
    profile_id: str
    object_count: int
    verified_count: int
    verified: bool
    objects: tuple[ProfileObjectVerification, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["objects"] = [asdict(item) for item in self.objects]
        return data


@dataclass(frozen=True)
class ProfileDefinition:
    capsule_id: str
    profile_id: str
    dependencies: tuple[str, ...]
    object_index: dict[str, dict[str, Any]]


def _load_capsule(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileStoreError(f"Capsule not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ProfileStoreError(
            f"Capsule is not valid UTF-8: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProfileStoreError(
            f"Invalid JSON in {path}:{exc.lineno}:{exc.colno}: "
            f"{exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise ProfileStoreError(
            f"Capsule top-level value must be an object: {path}"
        )
    return data


def load_profile_definition(
    *,
    capsule_path: Path,
    profile_id: str,
) -> ProfileDefinition:
    """Load one profile and its object declarations."""

    capsule = _load_capsule(capsule_path.absolute())

    capsule_id = capsule.get("capsule_id")
    if not isinstance(capsule_id, str) or not capsule_id:
        raise ProfileStoreError(
            "capsule.capsule_id must be a non-empty string"
        )

    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise ProfileStoreError("capsule.profiles must be an array")

    matching_profiles = [
        item
        for item in profiles
        if isinstance(item, dict) and item.get("id") == profile_id
    ]
    if not matching_profiles:
        raise ProfileStoreError(f"Unknown profile: {profile_id!r}")
    if len(matching_profiles) > 1:
        raise ProfileStoreError(
            f"Duplicate profile ID: {profile_id!r}"
        )

    dependencies = matching_profiles[0].get("dependencies")
    if not isinstance(dependencies, list):
        raise ProfileStoreError(
            "profile.dependencies must be an array"
        )
    if any(
        not isinstance(item, str) or not item
        for item in dependencies
    ):
        raise ProfileStoreError(
            "profile dependency IDs must be non-empty strings"
        )
    if len(set(dependencies)) != len(dependencies):
        raise ProfileStoreError(
            f"Profile {profile_id!r} contains duplicate dependencies"
        )

    objects = capsule.get("objects")
    if not isinstance(objects, list):
        raise ProfileStoreError("capsule.objects must be an array")

    object_index: dict[str, dict[str, Any]] = {}
    for item in objects:
        if not isinstance(item, dict):
            raise ProfileStoreError(
                "Every capsule object declaration must be an object"
            )
        object_id = item.get("id")
        if not isinstance(object_id, str) or not object_id:
            raise ProfileStoreError(
                "Every capsule object requires a non-empty id"
            )
        if object_id in object_index:
            raise ProfileStoreError(
                f"Duplicate object ID: {object_id!r}"
            )
        object_index[object_id] = item

    unknown = [
        dependency
        for dependency in dependencies
        if dependency not in object_index
    ]
    if unknown:
        raise ProfileStoreError(
            "Profile references unknown object ID(s): "
            + ", ".join(unknown)
        )

    return ProfileDefinition(
        capsule_id=capsule_id,
        profile_id=profile_id,
        dependencies=tuple(dependencies),
        object_index=object_index,
    )


def parse_source_assignments(
    values: list[str],
) -> dict[str, Path]:
    """Parse repeated OBJECT_ID=PATH arguments."""

    result: dict[str, Path] = {}

    for value in values:
        if "=" not in value:
            raise ProfileStoreError(
                "Each --source value must use OBJECT_ID=PATH."
            )
        object_id, raw_path = value.split("=", 1)
        object_id = object_id.strip()

        if not object_id:
            raise ProfileStoreError(
                "Source assignment has an empty object ID."
            )
        if not raw_path:
            raise ProfileStoreError(
                f"Source assignment for {object_id!r} has an empty path."
            )
        if object_id in result:
            raise ProfileStoreError(
                f"Duplicate source assignment: {object_id!r}"
            )

        result[object_id] = Path(raw_path).expanduser().absolute()

    return result


def _archive_path_for(
    definition: ProfileDefinition,
    object_id: str,
) -> str:
    value = definition.object_index[object_id].get("archive_path")
    if not isinstance(value, str) or not value:
        raise ProfileStoreError(
            f"Object {object_id!r} has no valid archive_path"
        )
    return value


def ingest_profile(
    *,
    capsule_path: Path,
    profile_id: str,
    vault_root: Path,
    sources: Mapping[str, Path],
) -> ProfileIngestResult:
    """Ingest every dependency of one profile.

    Existing matching objects are verified and reported as already present.
    Missing objects require an explicit source assignment. No source paths are
    retained in the returned report.
    """

    definition = load_profile_definition(
        capsule_path=capsule_path,
        profile_id=profile_id,
    )
    vault_root = vault_root.expanduser().resolve()

    unknown_sources = sorted(
        set(sources) - set(definition.dependencies)
    )
    if unknown_sources:
        raise ProfileStoreError(
            "Source assignment does not belong to profile "
            f"{profile_id!r}: " + ", ".join(unknown_sources)
        )

    destination_specs = {}
    missing_sources: list[str] = []

    for object_id in definition.dependencies:
        try:
            spec = capsule_destination_spec(
                capsule_path=capsule_path,
                object_id=object_id,
                vault_root=vault_root,
            )
        except (IngestError, VerifyError) as exc:
            raise ProfileStoreError(str(exc)) from exc

        destination_specs[object_id] = spec
        if (
            not spec.path.exists()
            and not spec.path.is_symlink()
            and object_id not in sources
        ):
            missing_sources.append(object_id)

    if missing_sources:
        raise ProfileStoreError(
            "Missing --source assignment for absent object(s): "
            + ", ".join(missing_sources)
        )

    results: list[ProfileObjectIngest] = []

    for object_id in definition.dependencies:
        spec = destination_specs[object_id]
        source = sources.get(object_id, spec.path)

        try:
            result = ingest_object(
                source=source,
                destination_spec=spec,
            )
        except (IngestError, VerifyError) as exc:
            raise ProfileStoreError(
                f"Object {object_id!r}: {exc}"
            ) from exc

        results.append(
            ProfileObjectIngest(
                object_id=object_id,
                digest=spec.expected_digest,
                archive_path=_archive_path_for(
                    definition,
                    object_id,
                ),
                bytes=result.bytes,
                status=result.status,
                copied=result.copied,
                verified=result.destination_verified,
            )
        )

    ingested_count = sum(
        item.status == "ingested" for item in results
    )
    already_present_count = sum(
        item.status == "already_present" for item in results
    )

    return ProfileIngestResult(
        schema=0,
        capsule_id=definition.capsule_id,
        profile_id=definition.profile_id,
        object_count=len(results),
        ingested_count=ingested_count,
        already_present_count=already_present_count,
        complete=all(item.verified for item in results),
        objects=tuple(results),
    )


def verify_profile(
    *,
    capsule_path: Path,
    profile_id: str,
    vault_root: Path,
) -> ProfileVerificationResult:
    """Verify every dependency of one profile without modifying the vault."""

    definition = load_profile_definition(
        capsule_path=capsule_path,
        profile_id=profile_id,
    )
    vault_root = vault_root.expanduser().resolve()
    results: list[ProfileObjectVerification] = []

    for object_id in definition.dependencies:
        declaration = definition.object_index[object_id]
        digest = declaration.get("digest")
        expected_size = declaration.get("size")
        archive_path = _archive_path_for(
            definition,
            object_id,
        )

        if not isinstance(digest, str):
            raise ProfileStoreError(
                f"Object {object_id!r} has no valid digest"
            )
        if expected_size is not None and (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise ProfileStoreError(
                f"Object {object_id!r} has no valid size"
            )

        try:
            spec = resolve_capsule_object(
                capsule_path=capsule_path,
                object_id=object_id,
                vault_root=vault_root,
            )
            result = verify_object(spec)
        except VerifyError as exc:
            detail = str(exc)
            status = (
                "missing"
                if "not found" in detail.lower()
                else "error"
            )
            results.append(
                ProfileObjectVerification(
                    object_id=object_id,
                    digest=digest,
                    archive_path=archive_path,
                    expected_size=expected_size,
                    actual_size=None,
                    status=status,
                    verified=False,
                    detail=detail,
                )
            )
            continue

        status = "verified" if result.verified else "mismatch"
        detail = None
        if not result.verified:
            problems = []
            if not result.digest_match:
                problems.append("digest mismatch")
            if result.size_match is False:
                problems.append("size mismatch")
            detail = ", ".join(problems) or "verification mismatch"

        results.append(
            ProfileObjectVerification(
                object_id=object_id,
                digest=digest,
                archive_path=archive_path,
                expected_size=expected_size,
                actual_size=result.actual_size,
                status=status,
                verified=result.verified,
                detail=detail,
            )
        )

    verified_count = sum(item.verified for item in results)

    return ProfileVerificationResult(
        schema=0,
        capsule_id=definition.capsule_id,
        profile_id=definition.profile_id,
        object_count=len(results),
        verified_count=verified_count,
        verified=verified_count == len(results),
        objects=tuple(results),
    )
