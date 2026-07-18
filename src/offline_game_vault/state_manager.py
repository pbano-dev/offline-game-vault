"""Generic, private persistent-state backup and restoration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import sys
from typing import Any, Iterable
import uuid

from . import __version__


BACKUP_RECEIPT_NAME = "state-backup.json"
RESTORE_RECEIPT_NAME = "state-restore-receipt.json"

_ID_PATTERN = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
)
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_FILL_MARKER = "[" + "RELLENAR" + "]"
_MISSING_CONTENT_MARKER = "LEEME_FALTA_" + "CONTENIDO"
_UNRESOLVED_MARKER = re.compile(
    r"(?:REDACTED|"
    + re.escape(_FILL_MARKER)
    + "|"
    + re.escape(_MISSING_CONTENT_MARKER)
    + r"|<(?:PRIVATE|ACCOUNT|USER|STEAM|PATH|PREFIX|HOME|UID|UUID)"
    + r"[^>]*>)",
    re.IGNORECASE,
)


class StateError(Exception):
    """Raised when persistent-state handling cannot continue safely."""


@dataclass(frozen=True)
class StateDeclaration:
    id: str
    path: str
    kind: str
    backup: bool
    sensitive: bool
    required: bool


@dataclass(frozen=True)
class StateItemSummary:
    id: str
    present: bool
    entry_type: str
    file_count: int
    directory_count: int
    bytes: int
    tree_digest: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StateBackupResult:
    schema: int
    backup_id: str
    capsule_id: str
    state_definition_digest: str
    backup_kind: str
    item_count: int
    present_count: int
    missing_count: int
    total_bytes: int
    stopped_confirmed: bool
    complete: bool
    items: tuple[StateItemSummary, ...]

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["items"] = [
            item.to_dict() for item in self.items
        ]
        return document


@dataclass(frozen=True)
class StateBackupVerification:
    schema: int
    capsule_id: str
    backup_id: str | None
    backup_kind: str | None
    item_count: int
    present_count: int
    missing_count: int
    total_bytes: int
    verified: bool
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["problems"] = list(self.problems)
        return document


@dataclass(frozen=True)
class StateRestoreResult:
    schema: int
    restore_id: str
    capsule_id: str
    backup_id: str
    snapshot_backup_id: str
    item_count: int
    restored_count: int
    missing_count: int
    stopped_confirmed: bool
    rollback_performed: bool
    rollback_complete: bool
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuditIssue:
    severity: str
    code: str
    context: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CapsuleAuditResult:
    schema: int
    capsule_id: str | None
    object_count: int
    profile_count: int
    persistent_state_count: int
    backup_state_count: int
    state_definition_digest: str | None
    error_count: int
    warning_count: int
    valid: bool
    operational: bool
    issues: tuple[AuditIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["issues"] = [
            issue.to_dict() for issue in self.issues
        ]
        return document


@dataclass(frozen=True)
class _SourceEntry:
    path: str
    type: str
    mode: int
    size: int
    dev: int
    ino: int
    mtime_ns: int
    ctime_ns: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_relative_path(value: str, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise StateError(f"{field} must be a non-empty string.")
    if "\x00" in value or "\\" in value:
        raise StateError(
            f"{field} is not a portable relative path."
        )

    path = PurePosixPath(value)
    if path.is_absolute():
        raise StateError(f"{field} must be relative.")
    if (
        not path.parts
        or re.fullmatch(r"[A-Za-z]:", path.parts[0])
        or path.as_posix() != value
    ):
        raise StateError(
            f"{field} is not a canonical relative path."
        )
    if any(part in {"", ".", ".."} for part in path.parts):
        raise StateError(
            f"{field} contains an unsafe path component."
        )
    return path


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise StateError(f"{label} must not be a symbolic link.")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StateError(f"{label} was not found.") from exc
    except UnicodeDecodeError as exc:
        raise StateError(f"{label} is not valid UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{label} contains invalid JSON at "
            f"line {exc.lineno}, column {exc.colno}."
        ) from exc

    if not isinstance(data, dict):
        raise StateError(
            f"{label} top-level value must be an object."
        )
    return data


def _capsule_path(path: Path) -> Path:
    expanded = path.expanduser().absolute()
    if expanded.is_symlink():
        raise StateError("Capsule path must not be a symbolic link.")
    if not expanded.is_file():
        raise StateError("Capsule path must be a regular file.")
    return expanded


def _normalized_declarations(
    capsule: dict[str, Any],
) -> tuple[str, tuple[StateDeclaration, ...]]:
    capsule_id = capsule.get("capsule_id")
    if (
        not isinstance(capsule_id, str)
        or not _ID_PATTERN.fullmatch(capsule_id)
    ):
        raise StateError("Capsule has no valid capsule_id.")

    raw_state = capsule.get("persistent_state", [])
    if not isinstance(raw_state, list):
        raise StateError("capsule.persistent_state must be an array.")

    declarations: list[StateDeclaration] = []
    ids: set[str] = set()
    paths: list[tuple[str, PurePosixPath]] = []

    for index, raw in enumerate(raw_state):
        context = f"persistent_state[{index}]"
        if not isinstance(raw, dict):
            raise StateError(f"{context} must be an object.")

        item_id = raw.get("id")
        if (
            not isinstance(item_id, str)
            or not _ID_PATTERN.fullmatch(item_id)
        ):
            raise StateError(f"{context}.id is not a safe state ID.")
        if item_id in ids:
            raise StateError(
                f"Duplicate persistent-state ID: {item_id}."
            )
        ids.add(item_id)

        item_path = raw.get("path")
        if not isinstance(item_path, str):
            raise StateError(f"{context}.path must be a string.")
        relative = _safe_relative_path(
            item_path,
            f"{context}.path",
        )

        kind = raw.get("kind")
        if kind not in {
            "save",
            "identity",
            "configuration",
            "cache",
            "log",
            "temporary",
            "other",
        }:
            raise StateError(f"{context}.kind is not supported.")

        backup = raw.get("backup", True)
        sensitive = raw.get("sensitive", False)
        required = raw.get("required", True)

        for name, value in (
            ("backup", backup),
            ("sensitive", sensitive),
            ("required", required),
        ):
            if not isinstance(value, bool):
                raise StateError(
                    f"{context}.{name} must be a boolean."
                )

        declarations.append(
            StateDeclaration(
                id=item_id,
                path=item_path,
                kind=kind,
                backup=backup,
                sensitive=sensitive,
                required=required,
            )
        )
        paths.append((item_id, relative))

    ordered_paths = sorted(
        paths,
        key=lambda item: (len(item[1].parts), item[1].parts),
    )
    for index, (item_id, item_path) in enumerate(ordered_paths):
        for other_id, other_path in ordered_paths[index + 1:]:
            if (
                item_path == other_path
                or other_path.parts[:len(item_path.parts)]
                == item_path.parts
            ):
                raise StateError(
                    "Persistent-state paths overlap: "
                    f"{item_id} and {other_id}."
                )

    return (
        capsule_id,
        tuple(sorted(declarations, key=lambda item: item.id)),
    )


def _definition_digest(
    capsule_id: str,
    declarations: Iterable[StateDeclaration],
) -> str:
    document = {
        "capsule_id": capsule_id,
        "persistent_state": [
            asdict(item)
            for item in sorted(
                declarations,
                key=lambda declaration: declaration.id,
            )
        ],
    }
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def _canonical_object_archive_path(digest: str) -> str | None:
    if not isinstance(digest, str):
        return None
    if not _DIGEST_PATTERN.fullmatch(digest):
        return None
    hexdigest = digest.removeprefix("sha256:")
    return (
        f"objects/sha256/{hexdigest[:2]}/{hexdigest[2:4]}/"
        f"{hexdigest}"
    )


def audit_capsule(*, capsule_path: Path) -> CapsuleAuditResult:
    """Audit one capsule without reading game payloads or state data."""

    issues: list[AuditIssue] = []

    def add(
        severity: str,
        code: str,
        context: str,
        message: str,
    ) -> None:
        issues.append(
            AuditIssue(
                severity=severity,
                code=code,
                context=context,
                message=message,
            )
        )

    try:
        path = _capsule_path(capsule_path)
        capsule = _load_json_object(path, "Capsule")
    except StateError as exc:
        issue = AuditIssue(
            severity="error",
            code="CAPSULE_UNREADABLE",
            context="$",
            message=str(exc),
        )
        return CapsuleAuditResult(
            schema=0,
            capsule_id=None,
            object_count=0,
            profile_count=0,
            persistent_state_count=0,
            backup_state_count=0,
            state_definition_digest=None,
            error_count=1,
            warning_count=0,
            valid=False,
            operational=False,
            issues=(issue,),
        )

    if capsule.get("schema") != 0:
        add(
            "error",
            "UNSUPPORTED_CAPSULE_SCHEMA",
            "$.schema",
            "Only capsule schema generation 0 is supported.",
        )

    sanitized_value = capsule.get("sanitized_fixture", False)
    if not isinstance(sanitized_value, bool):
        add(
            "error",
            "INVALID_SANITIZED_FLAG",
            "$.sanitized_fixture",
            "sanitized_fixture must be a boolean.",
        )

    game = capsule.get("game")
    if not isinstance(game, dict):
        add(
            "error",
            "INVALID_GAME_METADATA",
            "$.game",
            "game must be an object.",
        )
    else:
        for field in (
            "title",
            "source_store",
            "preserved_version",
        ):
            value = game.get(field)
            if not isinstance(value, str) or not value:
                add(
                    "error",
                    "INVALID_GAME_METADATA",
                    f"$.game.{field}",
                    f"{field} must be a non-empty string.",
                )

    capsule_id = capsule.get("capsule_id")
    valid_capsule_id = (
        isinstance(capsule_id, str)
        and _ID_PATTERN.fullmatch(capsule_id) is not None
    )
    if not valid_capsule_id:
        add(
            "error",
            "INVALID_CAPSULE_ID",
            "$.capsule_id",
            "capsule_id must be a safe lowercase identifier.",
        )
        capsule_id_value: str | None = None
    else:
        capsule_id_value = capsule_id

    documents = capsule.get("documents")
    if not isinstance(documents, dict):
        add(
            "error",
            "INVALID_DOCUMENTS",
            "$.documents",
            "documents must be an object.",
        )
    else:
        for key in (
            "readme",
            "game_sheet",
            "credits",
            "preserved_by",
        ):
            value = documents.get(key)
            context = f"$.documents.{key}"
            try:
                relative = _safe_relative_path(value, context)
            except StateError as exc:
                add(
                    "error",
                    "INVALID_DOCUMENT_PATH",
                    context,
                    str(exc),
                )
                continue
            candidate = path.parent.joinpath(*relative.parts)
            if candidate.is_symlink():
                add(
                    "error",
                    "DOCUMENT_SYMLINK",
                    context,
                    "Referenced document must not be a symlink.",
                )
            elif not candidate.is_file():
                add(
                    "error",
                    "DOCUMENT_MISSING",
                    context,
                    "Referenced document does not exist.",
                )

    objects = capsule.get("objects")
    object_count = len(objects) if isinstance(objects, list) else 0
    known_objects: set[str] = set()
    if not isinstance(objects, list) or not objects:
        add(
            "error",
            "INVALID_OBJECTS",
            "$.objects",
            "objects must be a non-empty array.",
        )
    else:
        for index, item in enumerate(objects):
            context = f"$.objects[{index}]"
            if not isinstance(item, dict):
                add(
                    "error",
                    "INVALID_OBJECT",
                    context,
                    "Object declaration must be an object.",
                )
                continue
            object_id = item.get("id")
            if (
                not isinstance(object_id, str)
                or not _ID_PATTERN.fullmatch(object_id)
            ):
                add(
                    "error",
                    "INVALID_OBJECT_ID",
                    f"{context}.id",
                    "Object ID is not a safe identifier.",
                )
            elif object_id in known_objects:
                add(
                    "error",
                    "DUPLICATE_OBJECT_ID",
                    f"{context}.id",
                    "Object ID is duplicated.",
                )
            else:
                known_objects.add(object_id)

            digest = item.get("digest")
            canonical = _canonical_object_archive_path(digest)
            if canonical is None:
                add(
                    "error",
                    "INVALID_OBJECT_DIGEST",
                    f"{context}.digest",
                    "Object digest must be lowercase sha256.",
                )
            elif item.get("archive_path") != canonical:
                add(
                    "error",
                    "NONCANONICAL_ARCHIVE_PATH",
                    f"{context}.archive_path",
                    "archive_path is not canonical for the digest.",
                )

            roles = item.get("roles")
            if (
                not isinstance(roles, list)
                or not roles
                or any(not isinstance(role, str) for role in roles)
                or len(roles) != len(set(roles))
            ):
                add(
                    "error",
                    "INVALID_OBJECT_ROLES",
                    f"{context}.roles",
                    "Object roles must be a non-empty unique array.",
                )
            if item.get("format") not in {
                "file",
                "directory",
                "tar",
                "tar.gz",
                "tar.zst",
                "zip",
                "flatpak_repo",
                "other",
            }:
                add(
                    "error",
                    "INVALID_OBJECT_FORMAT",
                    f"{context}.format",
                    "Object format is not supported.",
                )
            if not isinstance(item.get("required"), bool):
                add(
                    "error",
                    "INVALID_OBJECT_REQUIRED",
                    f"{context}.required",
                    "Object required must be a boolean.",
                )

    profiles = capsule.get("profiles")
    profile_count = len(profiles) if isinstance(profiles, list) else 0
    profile_ids: set[str] = set()
    if not isinstance(profiles, list) or not profiles:
        add(
            "error",
            "INVALID_PROFILES",
            "$.profiles",
            "profiles must be a non-empty array.",
        )
    else:
        for index, profile in enumerate(profiles):
            context = f"$.profiles[{index}]"
            if not isinstance(profile, dict):
                add(
                    "error",
                    "INVALID_PROFILE",
                    context,
                    "Profile declaration must be an object.",
                )
                continue
            profile_id = profile.get("id")
            if (
                not isinstance(profile_id, str)
                or not _ID_PATTERN.fullmatch(profile_id)
            ):
                add(
                    "error",
                    "INVALID_PROFILE_ID",
                    f"{context}.id",
                    "Profile ID is not a safe identifier.",
                )
            elif profile_id in profile_ids:
                add(
                    "error",
                    "DUPLICATE_PROFILE_ID",
                    f"{context}.id",
                    "Profile ID is duplicated.",
                )
            else:
                profile_ids.add(profile_id)

            dependencies = profile.get("dependencies")
            if not isinstance(dependencies, list):
                add(
                    "error",
                    "INVALID_DEPENDENCIES",
                    f"{context}.dependencies",
                    "Profile dependencies must be an array.",
                )
            else:
                if len(dependencies) != len(set(dependencies)):
                    add(
                        "error",
                        "DUPLICATE_DEPENDENCY",
                        f"{context}.dependencies",
                        "Profile dependency IDs must be unique.",
                    )
                for dependency in dependencies:
                    if dependency not in known_objects:
                        add(
                            "error",
                            "UNKNOWN_DEPENDENCY",
                            f"{context}.dependencies",
                            "Profile references an unknown object ID.",
                        )

            if profile.get("platform") not in {"linux", "windows"}:
                add(
                    "error",
                    "INVALID_PROFILE_PLATFORM",
                    f"{context}.platform",
                    "Profile platform is not supported.",
                )
            if profile.get("adapter") not in {
                "bottles",
                "wine",
                "umu",
                "windows",
                "vm",
                "other",
            }:
                add(
                    "error",
                    "INVALID_PROFILE_ADAPTER",
                    f"{context}.adapter",
                    "Profile adapter is not supported.",
                )
            if profile.get("status") not in {
                "verified",
                "candidate",
                "experimental",
                "not_tested",
                "unavailable",
            }:
                add(
                    "error",
                    "INVALID_PROFILE_STATUS",
                    f"{context}.status",
                    "Profile status is not supported.",
                )

            launch = profile.get("launch")
            if not isinstance(launch, dict):
                add(
                    "error",
                    "INVALID_LAUNCH",
                    f"{context}.launch",
                    "Profile launch must be an object.",
                )
            else:
                for launch_field in (
                    "entrypoint",
                    "working_directory",
                ):
                    value = launch.get(launch_field)
                    if (
                        value is None
                        and launch_field == "working_directory"
                    ):
                        continue
                    try:
                        _safe_relative_path(
                            value,
                            f"{context}.launch.{launch_field}",
                        )
                    except StateError as exc:
                        add(
                            "error",
                            "INVALID_LAUNCH_PATH",
                            f"{context}.launch.{launch_field}",
                            str(exc),
                        )

            if (
                profile.get("status") == "verified"
                and profile.get("acceptance_report") is None
            ):
                add(
                    "error",
                    "VERIFIED_PROFILE_WITHOUT_ACCEPTANCE",
                    f"{context}.acceptance_report",
                    "Verified profile requires acceptance evidence.",
                )

            for field in ("host_contract", "acceptance_report"):
                value = profile.get(field)
                if value is None and field == "acceptance_report":
                    continue
                field_context = f"{context}.{field}"
                try:
                    relative = _safe_relative_path(
                        value,
                        field_context,
                    )
                except StateError as exc:
                    add(
                        "error",
                        "INVALID_PROFILE_REFERENCE",
                        field_context,
                        str(exc),
                    )
                    continue
                candidate = path.parent.joinpath(*relative.parts)
                if candidate.is_symlink():
                    add(
                        "error",
                        "PROFILE_REFERENCE_SYMLINK",
                        field_context,
                        "Referenced file must not be a symlink.",
                    )
                elif not candidate.is_file():
                    add(
                        "error",
                        "PROFILE_REFERENCE_MISSING",
                        field_context,
                        "Referenced file does not exist.",
                    )

    state_definition_digest: str | None = None
    persistent_state_count = 0
    backup_state_count = 0
    marker_found = False

    try:
        parsed_capsule_id, declarations = _normalized_declarations(
            capsule
        )
        persistent_state_count = len(declarations)
        backup_state_count = sum(
            1 for item in declarations if item.backup
        )
        state_definition_digest = _definition_digest(
            parsed_capsule_id,
            declarations,
        )

        for index, declaration in enumerate(declarations):
            if _UNRESOLVED_MARKER.search(declaration.path):
                marker_found = True
                add(
                    "warning"
                    if capsule.get("sanitized_fixture") is True
                    else "error",
                    "UNRESOLVED_STATE_PATH",
                    f"$.persistent_state[{index}].path",
                    "State path contains a redaction or placeholder.",
                )
    except StateError as exc:
        raw_state = capsule.get("persistent_state")
        if isinstance(raw_state, list):
            persistent_state_count = len(raw_state)
            backup_state_count = sum(
                1
                for item in raw_state
                if isinstance(item, dict)
                and item.get("backup", True) is True
            )
        add(
            "error",
            "INVALID_PERSISTENT_STATE",
            "$.persistent_state",
            str(exc),
        )

    if persistent_state_count == 0:
        add(
            "warning",
            "NO_PERSISTENT_STATE",
            "$.persistent_state",
            "Capsule declares no persistent state.",
        )

    sanitized = capsule.get("sanitized_fixture") is True
    if sanitized:
        add(
            "warning",
            "SANITIZED_FIXTURE",
            "$.sanitized_fixture",
            "Public sanitized fixtures are not operational capsules.",
        )

    errors = tuple(
        issue for issue in issues if issue.severity == "error"
    )
    warnings = tuple(
        issue for issue in issues if issue.severity == "warning"
    )
    valid = not errors
    operational = valid and not sanitized and not marker_found

    return CapsuleAuditResult(
        schema=0,
        capsule_id=capsule_id_value,
        object_count=object_count,
        profile_count=profile_count,
        persistent_state_count=persistent_state_count,
        backup_state_count=backup_state_count,
        state_definition_digest=state_definition_digest,
        error_count=len(errors),
        warning_count=len(warnings),
        valid=valid,
        operational=operational,
        issues=tuple(issues),
    )


def _load_operational_capsule(
    capsule_path: Path,
) -> tuple[
    dict[str, Any],
    str,
    tuple[StateDeclaration, ...],
    str,
]:
    audit = audit_capsule(capsule_path=capsule_path)
    if not audit.valid:
        codes = ", ".join(
            issue.code
            for issue in audit.issues
            if issue.severity == "error"
        )
        raise StateError(
            "Capsule audit failed"
            + (f": {codes}" if codes else ".")
        )
    if not audit.operational:
        raise StateError(
            "Capsule is not operational; replace public redactions "
            "and placeholders in a private capsule."
        )

    path = _capsule_path(capsule_path)
    capsule = _load_json_object(path, "Capsule")
    capsule_id, declarations = _normalized_declarations(capsule)
    digest = _definition_digest(capsule_id, declarations)
    return capsule, capsule_id, declarations, digest


def _canonical_state_root(path: Path) -> Path:
    expanded = path.expanduser().absolute()
    if expanded.is_symlink():
        raise StateError("State root must not be a symbolic link.")
    if not expanded.is_dir():
        raise StateError("State root must be an existing directory.")
    resolved = expanded.resolve()
    if resolved == Path(resolved.anchor):
        raise StateError("Filesystem root is not a valid state root.")
    if resolved == Path.home().resolve():
        raise StateError("Home directory is not a valid state root.")
    return resolved


def _prospective_path(path: Path) -> Path:
    expanded = path.expanduser().absolute()
    parent = expanded.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    return parent.resolve() / expanded.name


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _canonical_new_directory(path: Path, label: str) -> Path:
    expanded = path.expanduser().absolute()
    if expanded.name in {"", ".", ".."}:
        raise StateError(f"{label} has an unsafe final component.")
    destination = _prospective_path(expanded)

    if destination == Path(destination.anchor):
        raise StateError(f"Refusing filesystem root as {label}.")
    if destination == Path.home().resolve():
        raise StateError(f"Refusing the home directory as {label}.")
    if destination.exists() or destination.is_symlink():
        raise StateError(f"{label} already exists.")
    return destination


def _reject_symlink_components(
    root: Path,
    relative: PurePosixPath,
    *,
    allow_missing: bool,
) -> Path:
    current = root
    parts = relative.parts
    for index, component in enumerate(parts):
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                break
            raise StateError(
                "Declared state path does not exist."
            )
        except OSError as exc:
            raise StateError(
                "Cannot inspect a declared state-path component."
            ) from exc

        if stat.S_ISLNK(metadata.st_mode):
            raise StateError(
                "Declared state path contains a symbolic link."
            )
        if (
            index < len(parts) - 1
            and not stat.S_ISDIR(metadata.st_mode)
        ):
            raise StateError(
                "Declared state path has a non-directory parent."
            )
    return root.joinpath(*parts)


def _ensure_safe_parent_directories(
    *,
    root: Path,
    relative_parent: PurePosixPath,
) -> Path:
    current = root
    for component in relative_parent.parts:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError as exc:
                raise StateError(
                    "Cannot create a state-target parent directory."
                ) from exc
            metadata = current.lstat()
        except OSError as exc:
            raise StateError(
                "Cannot inspect a state-target parent directory."
            ) from exc

        if stat.S_ISLNK(metadata.st_mode):
            raise StateError(
                "State-target parent contains a symbolic link."
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise StateError(
                "State-target parent is not a directory."
            )
    return current


def _source_entry(
    *,
    path: str,
    metadata: os.stat_result,
    entry_type: str,
) -> _SourceEntry:
    return _SourceEntry(
        path=path,
        type=entry_type,
        mode=stat.S_IMODE(metadata.st_mode),
        size=metadata.st_size if entry_type == "file" else 0,
        dev=metadata.st_dev,
        ino=metadata.st_ino,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _scan_source(path: Path) -> tuple[str, tuple[_SourceEntry, ...]]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing", ()

    if stat.S_ISLNK(metadata.st_mode):
        raise StateError("Persistent state contains a symbolic link.")

    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_nlink != 1:
            raise StateError(
                "Persistent-state regular file has multiple hard links."
            )
        return (
            "file",
            (
                _source_entry(
                    path=".",
                    metadata=metadata,
                    entry_type="file",
                ),
            ),
        )

    if not stat.S_ISDIR(metadata.st_mode):
        raise StateError(
            "Persistent state must be a regular file or directory."
        )

    entries: list[_SourceEntry] = [
        _source_entry(
            path=".",
            metadata=metadata,
            entry_type="directory",
        )
    ]

    def walk(directory: Path, relative: PurePosixPath) -> None:
        try:
            with os.scandir(directory) as scan:
                children = sorted(
                    scan,
                    key=lambda entry: entry.name,
                )
        except OSError as exc:
            raise StateError(
                "Cannot enumerate persistent-state directory."
            ) from exc

        for child in children:
            child_relative = (
                PurePosixPath(child.name)
                if str(relative) == "."
                else relative / child.name
            )
            child_path = directory / child.name
            try:
                child_metadata = child.stat(
                    follow_symlinks=False
                )
            except OSError as exc:
                raise StateError(
                    "Cannot inspect persistent-state entry."
                ) from exc

            if stat.S_ISLNK(child_metadata.st_mode):
                raise StateError(
                    "Persistent state contains a symbolic link."
                )
            if stat.S_ISDIR(child_metadata.st_mode):
                entries.append(
                    _source_entry(
                        path=child_relative.as_posix(),
                        metadata=child_metadata,
                        entry_type="directory",
                    )
                )
                walk(child_path, child_relative)
                continue
            if stat.S_ISREG(child_metadata.st_mode):
                if child_metadata.st_nlink != 1:
                    raise StateError(
                        "Persistent-state regular file has "
                        "multiple hard links."
                    )
                entries.append(
                    _source_entry(
                        path=child_relative.as_posix(),
                        metadata=child_metadata,
                        entry_type="file",
                    )
                )
                continue
            raise StateError(
                "Persistent state contains an unsupported "
                "special file."
            )

    walk(path, PurePosixPath("."))
    return "directory", tuple(entries)


def _source_signature(
    entries: Iterable[_SourceEntry],
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            item.path,
            item.type,
            item.mode,
            item.size,
            item.dev,
            item.ino,
            item.mtime_ns,
            item.ctime_ns,
        )
        for item in entries
    )


def _copy_file_private(
    *,
    source: Path,
    destination: Path,
    expected: _SourceEntry,
    chunk_size: int = 1024 * 1024,
) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    hasher = hashlib.sha256()
    total = 0

    try:
        source_descriptor = os.open(source, flags)
        before = os.fstat(source_descriptor)

        destination.parent.mkdir(
            parents=True,
            mode=0o700,
            exist_ok=True,
        )
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
            0o600,
        )

        with os.fdopen(
            source_descriptor,
            "rb",
            buffering=0,
            closefd=True,
        ) as source_handle:
            source_descriptor = None
            with os.fdopen(
                destination_descriptor,
                "wb",
                buffering=0,
                closefd=True,
            ) as destination_handle:
                destination_descriptor = None
                while True:
                    block = source_handle.read(chunk_size)
                    if not block:
                        break
                    destination_handle.write(block)
                    hasher.update(block)
                    total += len(block)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            after = os.fstat(source_handle.fileno())
    except OSError as exc:
        raise StateError(
            "Cannot copy persistent-state regular file."
        ) from exc
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)

    observed = _source_entry(
        path=expected.path,
        metadata=before,
        entry_type="file",
    )
    observed_after = _source_entry(
        path=expected.path,
        metadata=after,
        entry_type="file",
    )
    if observed != expected or observed_after != expected:
        raise StateError(
            "Persistent-state file changed while it was copied."
        )
    if total != expected.size:
        raise StateError(
            "Persistent-state byte count changed while copying."
        )

    os.chmod(destination, 0o600)
    return "sha256:" + hasher.hexdigest(), total


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise StateError("Cannot open directory for fsync.") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise StateError("Cannot fsync directory.") from exc
    finally:
        os.close(descriptor)


def _tree_digest(entries: Iterable[dict[str, Any]]) -> str:
    serialized = json.dumps(
        list(entries),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def _copy_state_item(
    *,
    source: Path,
    data_destination: Path,
) -> dict[str, Any]:
    entry_type, initial = _scan_source(source)
    if entry_type == "missing":
        return {
            "present": False,
            "entry_type": "missing",
            "file_count": 0,
            "directory_count": 0,
            "bytes": 0,
            "tree_digest": None,
            "entries": [],
        }

    if entry_type == "file":
        expected = initial[0]
        digest, total = _copy_file_private(
            source=source,
            destination=data_destination,
            expected=expected,
        )
        records = [
            {
                "path": ".",
                "type": "file",
                "mode": expected.mode,
                "bytes": total,
                "digest": digest,
            }
        ]
        after_type, after = _scan_source(source)
        if (
            after_type != entry_type
            or _source_signature(after)
            != _source_signature(initial)
        ):
            raise StateError(
                "Persistent state changed during backup."
            )
        return {
            "present": True,
            "entry_type": "file",
            "file_count": 1,
            "directory_count": 0,
            "bytes": total,
            "tree_digest": _tree_digest(records),
            "entries": records,
        }

    data_destination.mkdir(mode=0o700)
    directories = sorted(
        (
            item for item in initial
            if item.type == "directory" and item.path != "."
        ),
        key=lambda item: (
            len(PurePosixPath(item.path).parts),
            item.path,
        ),
    )
    for item in directories:
        destination = data_destination.joinpath(
            *PurePosixPath(item.path).parts
        )
        destination.mkdir(mode=0o700)

    records: list[dict[str, Any]] = [
        {
            "path": item.path,
            "type": "directory",
            "mode": item.mode,
        }
        for item in initial
        if item.type == "directory"
    ]
    total_bytes = 0
    file_count = 0

    for item in sorted(
        (entry for entry in initial if entry.type == "file"),
        key=lambda entry: entry.path,
    ):
        relative = PurePosixPath(item.path)
        digest, total = _copy_file_private(
            source=source.joinpath(*relative.parts),
            destination=data_destination.joinpath(*relative.parts),
            expected=item,
        )
        total_bytes += total
        file_count += 1
        records.append(
            {
                "path": item.path,
                "type": "file",
                "mode": item.mode,
                "bytes": total,
                "digest": digest,
            }
        )

    after_type, after = _scan_source(source)
    if (
        after_type != entry_type
        or _source_signature(after) != _source_signature(initial)
    ):
        raise StateError("Persistent state changed during backup.")

    records.sort(key=lambda item: (item["path"], item["type"]))
    for directory in sorted(
        (
            data_destination.joinpath(
                *PurePosixPath(item.path).parts
            )
            for item in directories
        ),
        key=lambda candidate: len(candidate.parts),
        reverse=True,
    ):
        os.chmod(directory, 0o700)
        _fsync_directory(directory)
    os.chmod(data_destination, 0o700)
    _fsync_directory(data_destination)

    return {
        "present": True,
        "entry_type": "directory",
        "file_count": file_count,
        "directory_count": sum(
            1 for item in initial if item.type == "directory"
        ),
        "bytes": total_bytes,
        "tree_digest": _tree_digest(records),
        "entries": records,
    }


def _rename_noreplace(source: Path, destination: Path) -> None:
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise StateError(
                "Atomic no-replace promotion is unavailable."
            )
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise StateError("Destination already exists.")
            raise StateError(
                "Atomic promotion failed: "
                f"{os.strerror(error_number)}"
            )
        return

    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise StateError("Destination already exists.") from exc
        except OSError as exc:
            raise StateError("Atomic promotion failed.") from exc
        return

    raise StateError(
        "Atomic no-replace promotion is supported only on "
        "Linux and Windows."
    )


def _acquire_lock(destination: Path) -> tuple[Path, int]:
    lock = destination.parent / (
        f".ogv-lock-state-{destination.name}"
    )
    try:
        descriptor = os.open(
            lock,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise StateError(
            "Another state operation holds the destination lock."
        ) from exc
    except OSError as exc:
        raise StateError("Cannot create state-operation lock.") from exc
    return lock, descriptor


def _release_lock(lock: Path, descriptor: int) -> None:
    try:
        os.close(descriptor)
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _write_json_exclusive(
    *,
    path: Path,
    document: dict[str, Any],
) -> None:
    serialized = (
        json.dumps(
            document,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
            closefd=True,
        ) as handle:
            descriptor = None
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StateError("Cannot write private state receipt.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    os.chmod(path, 0o600)


def _write_json_atomic_new(
    *,
    directory: Path,
    name: str,
    document: dict[str, Any],
) -> None:
    target = directory / name
    temporary = directory / (
        f".ogv-state-receipt-{os.getpid()}-"
        f"{secrets.token_hex(8)}"
    )
    try:
        _write_json_exclusive(path=temporary, document=document)
        _rename_noreplace(temporary, target)
        _fsync_directory(directory)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _backup_document(
    *,
    backup_id: str,
    capsule_id: str,
    definition_digest: str,
    backup_kind: str,
    stopped_confirmed: bool,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": 0,
        "backup_id": backup_id,
        "capsule_id": capsule_id,
        "state_definition_digest": definition_digest,
        "created_at": _now(),
        "orchestrator_version": __version__,
        "backup_kind": backup_kind,
        "stopped_confirmed": stopped_confirmed,
        "complete": True,
        "items": items,
    }


def _summary_from_item(item: dict[str, Any]) -> StateItemSummary:
    return StateItemSummary(
        id=item["id"],
        present=item["present"],
        entry_type=item["entry_type"],
        file_count=item["file_count"],
        directory_count=item["directory_count"],
        bytes=item["bytes"],
        tree_digest=item["tree_digest"],
    )


def _create_backup(
    *,
    capsule_id: str,
    declarations: tuple[StateDeclaration, ...],
    definition_digest: str,
    state_root: Path,
    destination: Path,
    backup_kind: str,
    stopped_confirmed: bool,
    allow_required_missing: bool,
) -> StateBackupResult:
    selected = tuple(item for item in declarations if item.backup)
    if not selected:
        raise StateError(
            "Capsule declares no persistent state with backup enabled."
        )

    destination = _canonical_new_directory(
        destination,
        "Backup destination",
    )
    lock, lock_descriptor = _acquire_lock(destination)
    staging = destination.parent / (
        f".ogv-stage-state-{destination.name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )
    promoted = False

    try:
        staging.mkdir(mode=0o700)
        payload = staging / "payload"
        payload.mkdir(mode=0o700)
        backup_id = f"state-backup-{uuid.uuid4()}"
        receipt_items: list[dict[str, Any]] = []

        for index, declaration in enumerate(selected):
            relative = _safe_relative_path(
                declaration.path,
                f"persistent_state[{declaration.id}].path",
            )
            source = _reject_symlink_components(
                state_root,
                relative,
                allow_missing=True,
            )
            item_container = payload / (
                f"{index:04d}-{declaration.id}"
            )
            item_container.mkdir(mode=0o700)
            data_path = item_container / "data"

            captured = _copy_state_item(
                source=source,
                data_destination=data_path,
            )
            if (
                not captured["present"]
                and declaration.required
                and not allow_required_missing
            ):
                raise StateError(
                    "Required persistent state is missing: "
                    f"{declaration.id}."
                )

            payload_path = (
                data_path.relative_to(staging).as_posix()
                if captured["present"]
                else None
            )
            item_document = {
                "id": declaration.id,
                "declared_path": declaration.path,
                "kind": declaration.kind,
                "sensitive": declaration.sensitive,
                "required": declaration.required,
                "present": captured["present"],
                "entry_type": captured["entry_type"],
                "payload_path": payload_path,
                "file_count": captured["file_count"],
                "directory_count": captured["directory_count"],
                "bytes": captured["bytes"],
                "tree_digest": captured["tree_digest"],
                "entries": captured["entries"],
            }
            receipt_items.append(item_document)

            if not captured["present"]:
                item_container.rmdir()

        receipt = _backup_document(
            backup_id=backup_id,
            capsule_id=capsule_id,
            definition_digest=definition_digest,
            backup_kind=backup_kind,
            stopped_confirmed=stopped_confirmed,
            items=receipt_items,
        )
        _write_json_exclusive(
            path=staging / BACKUP_RECEIPT_NAME,
            document=receipt,
        )

        os.chmod(payload, 0o700)
        _fsync_directory(payload)
        os.chmod(staging, 0o700)
        _fsync_directory(staging)

        _rename_noreplace(staging, destination)
        promoted = True
        _fsync_directory(destination.parent)

        summaries = tuple(
            _summary_from_item(item) for item in receipt_items
        )
        return StateBackupResult(
            schema=0,
            backup_id=backup_id,
            capsule_id=capsule_id,
            state_definition_digest=definition_digest,
            backup_kind=backup_kind,
            item_count=len(summaries),
            present_count=sum(
                1 for item in summaries if item.present
            ),
            missing_count=sum(
                1 for item in summaries if not item.present
            ),
            total_bytes=sum(item.bytes for item in summaries),
            stopped_confirmed=stopped_confirmed,
            complete=True,
            items=summaries,
        )
    finally:
        if not promoted and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _release_lock(lock, lock_descriptor)


def preserve_state(
    *,
    capsule_path: Path,
    state_root: Path,
    backup: Path,
    confirm_stopped: bool,
) -> StateBackupResult:
    """Create and atomically publish one private state backup."""

    if not confirm_stopped:
        raise StateError(
            "State capture requires --confirm-stopped."
        )

    (
        _,
        capsule_id,
        declarations,
        definition_digest,
    ) = _load_operational_capsule(capsule_path)
    root = _canonical_state_root(state_root)
    backup_candidate = _prospective_path(backup)
    if _paths_overlap(root, backup_candidate):
        raise StateError(
            "Backup destination must not overlap the state root."
        )

    result = _create_backup(
        capsule_id=capsule_id,
        declarations=declarations,
        definition_digest=definition_digest,
        state_root=root,
        destination=backup,
        backup_kind="preserved",
        stopped_confirmed=True,
        allow_required_missing=False,
    )

    verification = verify_state_backup(
        capsule_path=capsule_path,
        backup=backup,
    )
    if not verification.verified:
        shutil.rmtree(
            _canonical_existing_backup(backup),
            ignore_errors=True,
        )
        raise StateError(
            "New state backup failed post-publication verification."
        )
    return result


def _canonical_existing_backup(path: Path) -> Path:
    expanded = path.expanduser().absolute()
    if expanded.is_symlink():
        raise StateError(
            "Backup directory must not be a symbolic link."
        )
    if not expanded.is_dir():
        raise StateError("Backup directory does not exist.")
    return expanded.resolve()


def _require_private_mode(path: Path, label: str) -> None:
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except OSError as exc:
        raise StateError(f"Cannot inspect {label} permissions.") from exc
    if mode & 0o077:
        raise StateError(
            f"{label} permissions expose private state."
        )


def _validate_entry_record(
    record: Any,
    item_id: str,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise StateError(
            f"Backup item {item_id} has an invalid entry record."
        )

    path = record.get("path")
    if path != ".":
        _safe_relative_path(
            path,
            f"backup item {item_id} entry path",
        )

    entry_type = record.get("type")
    if entry_type not in {"file", "directory"}:
        raise StateError(
            f"Backup item {item_id} has an invalid entry type."
        )

    mode = record.get("mode")
    if (
        not isinstance(mode, int)
        or isinstance(mode, bool)
        or mode < 0
        or mode > 0o777
    ):
        raise StateError(
            f"Backup item {item_id} has an invalid mode."
        )

    if entry_type == "file":
        if set(record) != {
            "path",
            "type",
            "mode",
            "bytes",
            "digest",
        }:
            raise StateError(
                f"Backup item {item_id} file record "
                "has unexpected fields."
            )
        size = record.get("bytes")
        digest = record.get("digest")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise StateError(
                f"Backup item {item_id} has an invalid byte count."
            )
        if (
            not isinstance(digest, str)
            or not _DIGEST_PATTERN.fullmatch(digest)
        ):
            raise StateError(
                f"Backup item {item_id} has an invalid digest."
            )
    else:
        if set(record) != {"path", "type", "mode"}:
            raise StateError(
                f"Backup item {item_id} directory record "
                "has unexpected fields."
            )

    return record


def _hash_regular_file(path: Path) -> tuple[str, int]:
    if path.is_symlink():
        raise StateError("Backup payload contains a symbolic link.")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StateError("Cannot inspect backup payload file.") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise StateError(
            "Backup payload expected a regular file."
        )
    if metadata.st_nlink != 1:
        raise StateError(
            "Backup payload regular file has multiple hard links."
        )
    _require_private_mode(path, "Backup payload file")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    hasher = hashlib.sha256()
    total = 0
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        with os.fdopen(
            descriptor,
            "rb",
            buffering=0,
            closefd=True,
        ) as handle:
            descriptor = None
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
                total += len(block)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise StateError("Cannot read backup payload file.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    for field in (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    ):
        if getattr(before, field) != getattr(after, field):
            raise StateError(
                "Backup payload changed during verification."
            )
    if total != before.st_size:
        raise StateError(
            "Backup payload byte count changed during verification."
        )
    return "sha256:" + hasher.hexdigest(), total


def _actual_payload_records(
    *,
    data_path: Path,
    expected_type: str,
) -> list[dict[str, Any]]:
    if data_path.is_symlink():
        raise StateError("Backup payload path is a symbolic link.")

    if expected_type == "file":
        digest, size = _hash_regular_file(data_path)
        return [
            {
                "path": ".",
                "type": "file",
                "bytes": size,
                "digest": digest,
            }
        ]

    if expected_type != "directory":
        raise StateError("Backup item has an invalid entry_type.")
    if not data_path.is_dir():
        raise StateError(
            "Backup payload expected a directory."
        )
    _require_private_mode(data_path, "Backup payload directory")

    records: list[dict[str, Any]] = [
        {"path": ".", "type": "directory"}
    ]
    for current, directories, files in os.walk(
        data_path,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        for name in sorted(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                raise StateError(
                    "Backup payload contains a symbolic link."
                )
            metadata = candidate.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise StateError(
                    "Backup payload directory traversal changed."
                )
            _require_private_mode(
                candidate,
                "Backup payload directory",
            )
            records.append(
                {
                    "path": candidate.relative_to(
                        data_path
                    ).as_posix(),
                    "type": "directory",
                }
            )
        for name in sorted(files):
            candidate = current_path / name
            relative = candidate.relative_to(
                data_path
            ).as_posix()
            digest, size = _hash_regular_file(candidate)
            records.append(
                {
                    "path": relative,
                    "type": "file",
                    "bytes": size,
                    "digest": digest,
                }
            )

    records.sort(key=lambda item: (item["path"], item["type"]))
    return records


def _validate_backup_item(
    *,
    item: Any,
    declaration: StateDeclaration,
    backup_root: Path,
) -> StateItemSummary:
    if not isinstance(item, dict):
        raise StateError(
            f"Backup item {declaration.id} must be an object."
        )

    expected_fields = {
        "id",
        "declared_path",
        "kind",
        "sensitive",
        "required",
        "present",
        "entry_type",
        "payload_path",
        "file_count",
        "directory_count",
        "bytes",
        "tree_digest",
        "entries",
    }
    if set(item) != expected_fields:
        raise StateError(
            f"Backup item {declaration.id} has unexpected fields."
        )

    for field, expected in (
        ("id", declaration.id),
        ("declared_path", declaration.path),
        ("kind", declaration.kind),
        ("sensitive", declaration.sensitive),
        ("required", declaration.required),
    ):
        if item.get(field) != expected:
            raise StateError(
                f"Backup item {declaration.id} does not match "
                f"the capsule {field}."
            )

    present = item.get("present")
    entry_type = item.get("entry_type")
    if not isinstance(present, bool):
        raise StateError(
            f"Backup item {declaration.id} has invalid presence."
        )
    if entry_type not in {"file", "directory", "missing"}:
        raise StateError(
            f"Backup item {declaration.id} has invalid entry_type."
        )
    if present != (entry_type != "missing"):
        raise StateError(
            f"Backup item {declaration.id} presence is inconsistent."
        )

    counts: dict[str, int] = {}
    for field in ("file_count", "directory_count", "bytes"):
        value = item.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise StateError(
                f"Backup item {declaration.id} has invalid {field}."
            )
        counts[field] = value

    entries_raw = item.get("entries")
    if not isinstance(entries_raw, list):
        raise StateError(
            f"Backup item {declaration.id} entries must be an array."
        )
    entries = [
        _validate_entry_record(record, declaration.id)
        for record in entries_raw
    ]
    sorted_entries = sorted(
        entries,
        key=lambda record: (record["path"], record["type"]),
    )
    if entries != sorted_entries:
        raise StateError(
            f"Backup item {declaration.id} entries are not sorted."
        )
    paths = [record["path"] for record in entries]
    if len(paths) != len(set(paths)):
        raise StateError(
            f"Backup item {declaration.id} has duplicate paths."
        )

    if not present:
        if (
            item.get("payload_path") is not None
            or entries
            or any(counts.values())
            or item.get("tree_digest") is not None
        ):
            raise StateError(
                f"Missing backup item {declaration.id} "
                "contains payload metadata."
            )
        return StateItemSummary(
            id=declaration.id,
            present=False,
            entry_type="missing",
            file_count=0,
            directory_count=0,
            bytes=0,
            tree_digest=None,
        )

    payload_path = item.get("payload_path")
    relative = _safe_relative_path(
        payload_path,
        f"backup item {declaration.id} payload_path",
    )
    data_path = _reject_symlink_components(
        backup_root,
        relative,
        allow_missing=False,
    )
    try:
        data_path.resolve().relative_to(backup_root)
    except ValueError as exc:
        raise StateError(
            f"Backup item {declaration.id} payload escapes."
        ) from exc

    actual = _actual_payload_records(
        data_path=data_path,
        expected_type=entry_type,
    )
    expected_comparable = [
        {
            key: record[key]
            for key in (
                ("path", "type", "bytes", "digest")
                if record["type"] == "file"
                else ("path", "type")
            )
        }
        for record in entries
    ]
    if actual != expected_comparable:
        raise StateError(
            f"Backup item {declaration.id} payload does not "
            "match its manifest."
        )

    file_count = sum(
        1 for record in entries if record["type"] == "file"
    )
    directory_count = sum(
        1
        for record in entries
        if record["type"] == "directory"
    )
    total_bytes = sum(
        record["bytes"]
        for record in entries
        if record["type"] == "file"
    )
    if (
        file_count != counts["file_count"]
        or directory_count != counts["directory_count"]
        or total_bytes != counts["bytes"]
    ):
        raise StateError(
            f"Backup item {declaration.id} counts do not match."
        )

    digest = item.get("tree_digest")
    if (
        not isinstance(digest, str)
        or not _DIGEST_PATTERN.fullmatch(digest)
        or digest != _tree_digest(entries)
    ):
        raise StateError(
            f"Backup item {declaration.id} tree digest is invalid."
        )

    return StateItemSummary(
        id=declaration.id,
        present=True,
        entry_type=entry_type,
        file_count=file_count,
        directory_count=directory_count,
        bytes=total_bytes,
        tree_digest=digest,
    )


def _load_and_verify_backup(
    *,
    capsule_id: str,
    declarations: tuple[StateDeclaration, ...],
    definition_digest: str,
    backup: Path,
) -> tuple[
    Path,
    dict[str, Any],
    tuple[StateItemSummary, ...],
]:
    backup_root = _canonical_existing_backup(backup)
    _require_private_mode(backup_root, "Backup directory")

    receipt_path = backup_root / BACKUP_RECEIPT_NAME
    _require_private_mode(receipt_path, "Backup receipt")
    document = _load_json_object(
        receipt_path,
        "State backup receipt",
    )

    expected_top_fields = {
        "schema",
        "backup_id",
        "capsule_id",
        "state_definition_digest",
        "created_at",
        "orchestrator_version",
        "backup_kind",
        "stopped_confirmed",
        "complete",
        "items",
    }
    if set(document) != expected_top_fields:
        raise StateError(
            "State backup receipt has unexpected fields."
        )
    if document.get("schema") != 0:
        raise StateError("Unsupported state backup schema.")
    backup_id = document.get("backup_id")
    if (
        not isinstance(backup_id, str)
        or not backup_id.startswith("state-backup-")
    ):
        raise StateError("State backup has no valid backup_id.")
    if document.get("capsule_id") != capsule_id:
        raise StateError("State backup capsule_id does not match.")
    if document.get(
        "state_definition_digest"
    ) != definition_digest:
        raise StateError(
            "State backup definition does not match the capsule."
        )
    if document.get("backup_kind") not in {
        "preserved",
        "pre_restore_snapshot",
    }:
        raise StateError("State backup has invalid backup_kind.")
    if not isinstance(document.get("stopped_confirmed"), bool):
        raise StateError(
            "State backup has invalid stopped confirmation."
        )
    if document.get("complete") is not True:
        raise StateError("State backup is not marked complete.")

    items = document.get("items")
    if not isinstance(items, list):
        raise StateError("State backup items must be an array.")

    selected = tuple(item for item in declarations if item.backup)
    if len(items) != len(selected):
        raise StateError(
            "State backup item count does not match the capsule."
        )
    item_ids = [
        item.get("id")
        for item in items
        if isinstance(item, dict)
    ]
    if item_ids != [item.id for item in selected]:
        raise StateError(
            "State backup item order or IDs do not match the capsule."
        )

    allowed_top_level = {
        BACKUP_RECEIPT_NAME,
        "payload",
        RESTORE_RECEIPT_NAME,
    }
    unexpected = sorted(
        child.name
        for child in backup_root.iterdir()
        if child.name not in allowed_top_level
    )
    if unexpected:
        raise StateError(
            "State backup contains unexpected top-level entries."
        )

    payload = backup_root / "payload"
    if payload.is_symlink() or not payload.is_dir():
        raise StateError(
            "State backup payload directory is missing or unsafe."
        )
    _require_private_mode(payload, "Backup payload directory")

    for index, (item, declaration) in enumerate(
        zip(items, selected)
    ):
        if not isinstance(item, dict):
            raise StateError("State backup item must be an object.")
        expected_payload = (
            f"payload/{index:04d}-{declaration.id}/data"
        )
        if item.get("present") is True:
            if item.get("payload_path") != expected_payload:
                raise StateError(
                    f"Backup item {declaration.id} has a "
                    "non-canonical payload path."
                )
        elif item.get("payload_path") is not None:
            raise StateError(
                f"Missing backup item {declaration.id} "
                "must not declare a payload path."
            )

    summaries = tuple(
        _validate_backup_item(
            item=item,
            declaration=declaration,
            backup_root=backup_root,
        )
        for item, declaration in zip(items, selected)
    )

    expected_item_containers = {
        PurePosixPath(item["payload_path"]).parts[1]
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("payload_path"), str)
    }
    actual_item_containers = {
        child.name for child in payload.iterdir()
    }
    if actual_item_containers != expected_item_containers:
        raise StateError(
            "State backup payload containers do not match "
            "the receipt."
        )

    return backup_root, document, summaries


def verify_state_backup(
    *,
    capsule_path: Path,
    backup: Path,
) -> StateBackupVerification:
    """Verify one private state backup against its capsule."""

    capsule_id: str = "(unavailable)"
    try:
        (
            _,
            capsule_id,
            declarations,
            definition_digest,
        ) = _load_operational_capsule(capsule_path)
        _, document, summaries = _load_and_verify_backup(
            capsule_id=capsule_id,
            declarations=declarations,
            definition_digest=definition_digest,
            backup=backup,
        )
        return StateBackupVerification(
            schema=0,
            capsule_id=capsule_id,
            backup_id=document["backup_id"],
            backup_kind=document["backup_kind"],
            item_count=len(summaries),
            present_count=sum(
                1 for item in summaries if item.present
            ),
            missing_count=sum(
                1 for item in summaries if not item.present
            ),
            total_bytes=sum(item.bytes for item in summaries),
            verified=True,
            problems=(),
        )
    except StateError as exc:
        return StateBackupVerification(
            schema=0,
            capsule_id=capsule_id,
            backup_id=None,
            backup_kind=None,
            item_count=0,
            present_count=0,
            missing_count=0,
            total_bytes=0,
            verified=False,
            problems=(str(exc),),
        )


def _receipt_item_index(
    document: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    items = document.get("items")
    if not isinstance(items, list):
        raise StateError("State backup has no valid items array.")
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise StateError("State backup item is not an object.")
        item_id = item.get("id")
        if not isinstance(item_id, str):
            raise StateError("State backup item has no valid ID.")
        result[item_id] = item
    return result


def _live_records(
    *,
    target: Path,
    entry_type: str,
) -> list[dict[str, Any]]:
    actual_type, source_entries = _scan_source(target)
    if actual_type != entry_type:
        raise StateError("Live state type does not match snapshot.")
    if actual_type == "missing":
        return []

    records: list[dict[str, Any]] = []
    for entry in source_entries:
        if entry.type == "directory":
            records.append(
                {
                    "path": entry.path,
                    "type": "directory",
                    "mode": entry.mode,
                }
            )
        else:
            relative = (
                PurePosixPath()
                if entry.path == "."
                else PurePosixPath(entry.path)
            )
            source = (
                target
                if entry.path == "."
                else target.joinpath(*relative.parts)
            )
            digest, size = _hash_live_regular_file(
                source,
                entry,
            )
            records.append(
                {
                    "path": entry.path,
                    "type": "file",
                    "mode": entry.mode,
                    "bytes": size,
                    "digest": digest,
                }
            )
    records.sort(key=lambda item: (item["path"], item["type"]))
    return records


def _hash_live_regular_file(
    path: Path,
    expected: _SourceEntry,
) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    hasher = hashlib.sha256()
    total = 0
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        with os.fdopen(
            descriptor,
            "rb",
            buffering=0,
            closefd=True,
        ) as handle:
            descriptor = None
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
                total += len(block)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise StateError("Cannot read live persistent state.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    observed = _source_entry(
        path=expected.path,
        metadata=before,
        entry_type="file",
    )
    observed_after = _source_entry(
        path=expected.path,
        metadata=after,
        entry_type="file",
    )
    if observed != expected or observed_after != expected:
        raise StateError(
            "Live persistent state changed during verification."
        )
    return "sha256:" + hasher.hexdigest(), total


def _live_matches_receipt_item(
    *,
    target: Path,
    item: dict[str, Any],
) -> bool:
    entry_type = item["entry_type"]
    actual_type, _ = _scan_source(target)
    if entry_type == "missing":
        return actual_type == "missing"
    if actual_type != entry_type:
        return False
    records = _live_records(
        target=target,
        entry_type=entry_type,
    )
    return records == item["entries"]


def _copy_verified_payload_file(
    *,
    source: Path,
    destination: Path,
    expected_digest: str,
    expected_size: int,
    restore_mode: int,
) -> None:
    source_flags = os.O_RDONLY
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        destination_flags |= os.O_NOFOLLOW

    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    hasher = hashlib.sha256()
    total = 0

    try:
        source_descriptor = os.open(source, source_flags)
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise StateError(
                "Backup payload source is not a regular file."
            )
        if before.st_nlink != 1:
            raise StateError(
                "Backup payload source has multiple hard links."
            )
        if stat.S_IMODE(before.st_mode) & 0o077:
            raise StateError(
                "Backup payload source permissions expose private state."
            )

        destination.parent.mkdir(
            parents=True,
            mode=0o700,
            exist_ok=True,
        )
        destination_descriptor = os.open(
            destination,
            destination_flags,
            0o600,
        )

        with os.fdopen(
            source_descriptor,
            "rb",
            buffering=0,
            closefd=True,
        ) as source_handle:
            source_descriptor = None
            with os.fdopen(
                destination_descriptor,
                "wb",
                buffering=0,
                closefd=True,
            ) as destination_handle:
                destination_descriptor = None
                while True:
                    block = source_handle.read(1024 * 1024)
                    if not block:
                        break
                    destination_handle.write(block)
                    hasher.update(block)
                    total += len(block)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            after = os.fstat(source_handle.fileno())
    except OSError as exc:
        raise StateError(
            "Cannot stage a verified backup payload file."
        ) from exc
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)

    for field in (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    ):
        if getattr(before, field) != getattr(after, field):
            raise StateError(
                "Backup payload changed while staging restoration."
            )

    actual_digest = "sha256:" + hasher.hexdigest()
    if (
        total != expected_size
        or total != before.st_size
        or actual_digest != expected_digest
    ):
        raise StateError(
            "Backup payload failed verification while staging."
        )

    os.chmod(destination, restore_mode)


def _copy_payload_to_stage(
    *,
    backup_root: Path,
    item: dict[str, Any],
    stage: Path,
) -> None:
    payload_path = _safe_relative_path(
        item["payload_path"],
        "backup payload path",
    )
    data = backup_root.joinpath(*payload_path.parts)
    entries = item["entries"]

    if item["entry_type"] == "file":
        expected = entries[0]
        _copy_verified_payload_file(
            source=data,
            destination=stage,
            expected_digest=expected["digest"],
            expected_size=expected["bytes"],
            restore_mode=expected["mode"],
        )
        return

    stage.mkdir(mode=0o700)
    directory_records = [
        record for record in entries
        if record["type"] == "directory"
        and record["path"] != "."
    ]
    for record in sorted(
        directory_records,
        key=lambda entry: (
            len(PurePosixPath(entry["path"]).parts),
            entry["path"],
        ),
    ):
        relative = PurePosixPath(record["path"])
        stage.joinpath(*relative.parts).mkdir(mode=0o700)

    for record in (
        entry for entry in entries if entry["type"] == "file"
    ):
        relative = PurePosixPath(record["path"])
        source = data.joinpath(*relative.parts)
        destination = stage.joinpath(*relative.parts)
        _copy_verified_payload_file(
            source=source,
            destination=destination,
            expected_digest=record["digest"],
            expected_size=record["bytes"],
            restore_mode=record["mode"],
        )

    for record in sorted(
        (
            entry for entry in entries
            if entry["type"] == "directory"
        ),
        key=lambda entry: len(
            PurePosixPath(entry["path"]).parts
        ),
        reverse=True,
    ):
        target = (
            stage
            if record["path"] == "."
            else stage.joinpath(
                *PurePosixPath(record["path"]).parts
            )
        )
        os.chmod(target, record["mode"])
        _fsync_directory(target)


def _remove_path(path: Path) -> None:
    if path.is_symlink():
        raise StateError(
            "Refusing to remove a symbolic-link state target."
        )
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _apply_backup_item(
    *,
    state_root: Path,
    declaration: StateDeclaration,
    backup_root: Path,
    item: dict[str, Any],
) -> None:
    relative = _safe_relative_path(
        declaration.path,
        f"persistent_state[{declaration.id}].path",
    )
    target = _reject_symlink_components(
        state_root,
        relative,
        allow_missing=True,
    )
    parent_relative = PurePosixPath(*relative.parts[:-1])
    parent = _ensure_safe_parent_directories(
        root=state_root,
        relative_parent=parent_relative,
    )

    token = secrets.token_hex(8)
    stage = parent / (
        f".ogv-state-new-{target.name}-{os.getpid()}-{token}"
    )
    old = parent / (
        f".ogv-state-old-{target.name}-{os.getpid()}-{token}"
    )
    moved_old = False
    installed = False

    try:
        if item["entry_type"] != "missing":
            _copy_payload_to_stage(
                backup_root=backup_root,
                item=item,
                stage=stage,
            )

        if target.exists() or target.is_symlink():
            if target.is_symlink():
                raise StateError(
                    "Refusing to replace a symbolic-link state target."
                )
            os.rename(target, old)
            moved_old = True

        if item["entry_type"] != "missing":
            os.rename(stage, target)
            installed = True

        _fsync_directory(parent)

        if moved_old:
            _remove_path(old)
        if item["entry_type"] == "missing":
            installed = True
    except Exception:
        try:
            if installed and (target.exists() or target.is_symlink()):
                _remove_path(target)
            if moved_old and old.exists():
                os.rename(old, target)
            _fsync_directory(parent)
        except Exception as rollback_exc:
            raise StateError(
                "Per-item restore failed and local rollback failed."
            ) from rollback_exc
        raise
    finally:
        try:
            _remove_path(stage)
        except Exception:
            pass
        try:
            _remove_path(old)
        except Exception:
            pass


def _restore_receipt_document(
    *,
    restore_id: str,
    capsule_id: str,
    definition_digest: str,
    backup_id: str,
    snapshot_backup_id: str,
    stopped_confirmed: bool,
    item_count: int,
    item_ids: list[str],
    status: str,
    failed_item: str | None,
    rollback_performed: bool,
    rollback_complete: bool,
) -> dict[str, Any]:
    return {
        "schema": 0,
        "restore_id": restore_id,
        "capsule_id": capsule_id,
        "state_definition_digest": definition_digest,
        "created_at": _now(),
        "orchestrator_version": __version__,
        "backup_id": backup_id,
        "snapshot_backup_id": snapshot_backup_id,
        "stopped_confirmed": stopped_confirmed,
        "item_count": item_count,
        "restored_items": item_ids,
        "status": status,
        "failed_item": failed_item,
        "rollback_performed": rollback_performed,
        "rollback_complete": rollback_complete,
        "complete": status == "completed",
    }


def restore_state(
    *,
    capsule_path: Path,
    state_root: Path,
    backup: Path,
    snapshot: Path,
    confirm_stopped: bool,
) -> StateRestoreResult:
    """Restore verified state after a private pre-restore snapshot."""

    if not confirm_stopped:
        raise StateError(
            "State restoration requires --confirm-stopped."
        )

    (
        _,
        capsule_id,
        declarations,
        definition_digest,
    ) = _load_operational_capsule(capsule_path)
    root = _canonical_state_root(state_root)
    backup_candidate = _canonical_existing_backup(backup)
    snapshot_candidate = _prospective_path(snapshot)
    if _paths_overlap(root, backup_candidate):
        raise StateError(
            "Restore backup must not overlap the state root."
        )
    if (
        _paths_overlap(root, snapshot_candidate)
        or _paths_overlap(backup_candidate, snapshot_candidate)
    ):
        raise StateError(
            "Pre-restore snapshot must not overlap state or backup."
        )

    backup_root, backup_document, _ = _load_and_verify_backup(
        capsule_id=capsule_id,
        declarations=declarations,
        definition_digest=definition_digest,
        backup=backup,
    )
    if backup_document.get("stopped_confirmed") is not True:
        raise StateError(
            "Restore source was not captured with stopped state."
        )

    snapshot_result = _create_backup(
        capsule_id=capsule_id,
        declarations=declarations,
        definition_digest=definition_digest,
        state_root=root,
        destination=snapshot,
        backup_kind="pre_restore_snapshot",
        stopped_confirmed=True,
        allow_required_missing=True,
    )
    snapshot_root, snapshot_document, _ = _load_and_verify_backup(
        capsule_id=capsule_id,
        declarations=declarations,
        definition_digest=definition_digest,
        backup=snapshot,
    )

    backup_items = _receipt_item_index(backup_document)
    snapshot_items = _receipt_item_index(snapshot_document)
    selected = tuple(item for item in declarations if item.backup)

    restore_id = f"state-restore-{uuid.uuid4()}"
    modified: list[StateDeclaration] = []
    completed: list[str] = []
    failed_item: str | None = None

    try:
        for declaration in selected:
            failed_item = declaration.id
            relative = _safe_relative_path(
                declaration.path,
                f"persistent_state[{declaration.id}].path",
            )
            target = _reject_symlink_components(
                root,
                relative,
                allow_missing=True,
            )
            if not _live_matches_receipt_item(
                target=target,
                item=snapshot_items[declaration.id],
            ):
                raise StateError(
                    "Live state changed after the pre-restore snapshot."
                )

            modified.append(declaration)
            _apply_backup_item(
                state_root=root,
                declaration=declaration,
                backup_root=backup_root,
                item=backup_items[declaration.id],
            )
            if not _live_matches_receipt_item(
                target=target,
                item=backup_items[declaration.id],
            ):
                raise StateError(
                    "Restored state does not match the verified backup."
                )
            completed.append(declaration.id)

        receipt = _restore_receipt_document(
            restore_id=restore_id,
            capsule_id=capsule_id,
            definition_digest=definition_digest,
            backup_id=backup_document["backup_id"],
            snapshot_backup_id=snapshot_result.backup_id,
            stopped_confirmed=True,
            item_count=len(selected),
            item_ids=completed,
            status="completed",
            failed_item=None,
            rollback_performed=False,
            rollback_complete=True,
        )
        _write_json_atomic_new(
            directory=snapshot_root,
            name=RESTORE_RECEIPT_NAME,
            document=receipt,
        )
        return StateRestoreResult(
            schema=0,
            restore_id=restore_id,
            capsule_id=capsule_id,
            backup_id=backup_document["backup_id"],
            snapshot_backup_id=snapshot_result.backup_id,
            item_count=len(selected),
            restored_count=len(completed),
            missing_count=sum(
                1
                for item in backup_items.values()
                if item["entry_type"] == "missing"
            ),
            stopped_confirmed=True,
            rollback_performed=False,
            rollback_complete=True,
            complete=True,
        )
    except Exception as exc:
        rollback_complete = True
        for declaration in reversed(modified):
            try:
                _apply_backup_item(
                    state_root=root,
                    declaration=declaration,
                    backup_root=snapshot_root,
                    item=snapshot_items[declaration.id],
                )
                relative = _safe_relative_path(
                    declaration.path,
                    "rollback state path",
                )
                target = _reject_symlink_components(
                    root,
                    relative,
                    allow_missing=True,
                )
                if not _live_matches_receipt_item(
                    target=target,
                    item=snapshot_items[declaration.id],
                ):
                    rollback_complete = False
            except Exception:
                rollback_complete = False

        status = (
            "rolled_back"
            if rollback_complete
            else "rollback_failed"
        )
        receipt = _restore_receipt_document(
            restore_id=restore_id,
            capsule_id=capsule_id,
            definition_digest=definition_digest,
            backup_id=backup_document["backup_id"],
            snapshot_backup_id=snapshot_result.backup_id,
            stopped_confirmed=True,
            item_count=len(selected),
            item_ids=completed,
            status=status,
            failed_item=failed_item,
            rollback_performed=True,
            rollback_complete=rollback_complete,
        )
        try:
            _write_json_atomic_new(
                directory=snapshot_root,
                name=RESTORE_RECEIPT_NAME,
                document=receipt,
            )
        except Exception:
            rollback_complete = False

        if rollback_complete:
            raise StateError(
                "State restoration failed; rollback from the "
                "pre-restore snapshot completed."
            ) from exc
        raise StateError(
            "State restoration failed and rollback is incomplete; "
            "preserve the pre-restore snapshot and stop."
        ) from exc
