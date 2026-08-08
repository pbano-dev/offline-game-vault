"""Safe Bottles deployment and isolated launch adapter."""

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
import subprocess
import sys
from typing import Any, Iterable
import uuid

from . import __version__
from .composition_state import (
    CompositionStateError,
    prepare_composition_state,
    restore_composition_state,
    verify_composition_state_evidence,
)


DEPLOYMENT_RECEIPT_NAME = ".ogv-bottles-deployment.json"
DEFAULT_FLATPAK_APP = "com.usebottles.bottles"
PORTABLE_RUNTIME_DESTINATION = "metadata/ogv_bottles_runtime.py"
EXTERNAL_PORTABLE_RUNTIME_DESTINATION = (
    "metadata/ogv_external_bottles_runtime.py"
)
EXTERNAL_PREFIX_DESTINATION = "payload/prefix"
EXTERNAL_GAME_DESTINATION = "payload/game"
ROOT_LAUNCHER = "JUGAR.sh"
ROOT_VERIFIER = "VERIFICAR.sh"
ROOT_UNINSTALLER = "DESINSTALAR.sh"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _flatpak_bottles_cli_command(
    *arguments: str,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
    filesystem_root: Path | None = None,
) -> list[str]:
    if not isinstance(flatpak_app, str) or not flatpak_app:
        raise BottlesAdapterError(
            "Flatpak application ID must be a non-empty string."
        )
    command = ["flatpak", "run"]
    if filesystem_root is not None:
        root = filesystem_root.expanduser().absolute()
        if root.is_symlink() or not root.is_dir():
            raise BottlesAdapterError(
                "Flatpak filesystem root must be an existing regular directory."
            )
        command.append(f"--filesystem={root}")
    command.extend(
        [
            "--unshare=network",
            "--command=bottles-cli",
            flatpak_app,
            *arguments,
        ]
    )
    return command


def _run_bottles_cli(
    *arguments: str,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
    filesystem_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = _flatpak_bottles_cli_command(
        *arguments,
        flatpak_app=flatpak_app,
        filesystem_root=filesystem_root,
    )
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise BottlesAdapterError(
            "flatpak executable was not found."
        ) from exc
    except OSError as exc:
        raise BottlesAdapterError(
            f"Cannot invoke Bottles CLI: {exc}"
        ) from exc


def _json_documents_from_output(output: str) -> list[Any]:
    documents: list[Any] = []
    cleaned = _ANSI_ESCAPE.sub("", output)
    candidates = [cleaned.strip()]
    candidates.extend(
        line.strip()
        for line in cleaned.splitlines()
        if line.strip().startswith(("{", "[", '"'))
    )
    seen: set[str] = set()
    for candidate in reversed(candidates):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            documents.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return documents


def _walk_text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_text_values(item)


def _absolute_path_candidates(output: str) -> tuple[Path, ...]:
    raw_values: list[str] = []
    for document in _json_documents_from_output(output):
        raw_values.extend(_walk_text_values(document))

    cleaned = _ANSI_ESCAPE.sub("", output)
    raw_values.extend(line.strip() for line in cleaned.splitlines())

    candidates: list[Path] = []
    seen: set[str] = set()
    for raw in raw_values:
        value = raw.strip().strip("'\"")
        if value.startswith("file://"):
            value = value[7:]
        if not value.startswith("/"):
            continue
        path = Path(value)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(path)
    return tuple(candidates)


def discover_bottles_path(
    *,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> Path:
    """Return the effective directory managed by the active Bottles Flatpak.

    The query is always run without network access.  JSON output is preferred,
    with a plain-text retry for Bottles versions that do not serialize this
    particular ``info`` command.
    """

    attempts = (
        ("--json", "info", "bottles-path"),
        ("info", "bottles-path"),
    )
    diagnostics: list[str] = []
    for arguments in attempts:
        process = _run_bottles_cli(
            *arguments,
            flatpak_app=flatpak_app,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip()
            diagnostics.append(
                f"{' '.join(arguments)} exited with {process.returncode}"
                + (f": {detail[-1000:]}" if detail else "")
            )
            continue

        valid: list[Path] = []
        for candidate in _absolute_path_candidates(process.stdout):
            try:
                if candidate.is_symlink():
                    continue
                resolved = candidate.expanduser().resolve(strict=True)
            except OSError:
                continue
            if resolved.is_dir() and resolved not in valid:
                valid.append(resolved)

        if len(valid) == 1:
            return valid[0]
        if len(valid) > 1:
            raise BottlesAdapterError(
                "Bottles CLI returned more than one existing absolute "
                "directory for bottles-path."
            )
        diagnostics.append(
            f"{' '.join(arguments)} returned no existing absolute directory"
        )

    raise BottlesAdapterError(
        "Cannot discover the effective Bottles managed directory via "
        "bottles-cli info bottles-path. "
        + " | ".join(diagnostics)
    )


def require_bottles_managed_path(
    requested: Path | None = None,
    *,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> Path:
    """Resolve Bottles' own managed path and reject arbitrary alternatives."""

    discovered = discover_bottles_path(flatpak_app=flatpak_app)
    if requested is None:
        return discovered

    expanded = requested.expanduser().absolute()
    if expanded.is_symlink():
        raise BottlesAdapterError(
            "Requested Bottles path must not be a symlink."
        )
    try:
        resolved = expanded.resolve(strict=True)
    except FileNotFoundError as exc:
        raise BottlesAdapterError(
            "Requested Bottles path does not exist."
        ) from exc
    if resolved != discovered:
        raise BottlesAdapterError(
            "Requested Bottles path does not match the directory reported "
            "by bottles-cli info bottles-path."
        )
    return discovered


def _bottle_names_from_output(output: str) -> tuple[str, ...]:
    names: set[str] = set()
    for document in _json_documents_from_output(output):
        if isinstance(document, dict):
            raw = document.get("bottles")
            if isinstance(raw, list):
                names.update(
                    item.strip()
                    for item in raw
                    if isinstance(item, str) and item.strip()
                )
            names.update(
                key.strip()
                for key, value in document.items()
                if (
                    isinstance(key, str)
                    and key.strip()
                    and isinstance(value, dict)
                )
            )
        elif isinstance(document, list):
            names.update(
                item.strip()
                for item in document
                if isinstance(item, str) and item.strip()
            )

    cleaned = _ANSI_ESCAPE.sub("", output)
    for line in cleaned.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            candidate = stripped[2:].strip()
            if candidate:
                names.add(candidate)
    return tuple(sorted(names))


def assert_bottle_registered(
    bottle_name: str,
    *,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
    filesystem_root: Path | None = None,
) -> None:
    """Require Bottles CLI to enumerate a newly published bottle."""

    bottle_name = _validate_bottle_name(bottle_name)
    attempts = (
        ("--json", "list", "bottles"),
        ("list", "bottles"),
    )
    diagnostics: list[str] = []
    for arguments in attempts:
        process = _run_bottles_cli(
            *arguments,
            flatpak_app=flatpak_app,
            filesystem_root=filesystem_root,
        )
        if process.returncode != 0:
            diagnostics.append(
                f"{' '.join(arguments)} exited with {process.returncode}"
            )
            continue
        if bottle_name in _bottle_names_from_output(process.stdout):
            return
        diagnostics.append(
            f"{' '.join(arguments)} did not enumerate {bottle_name!r}"
        )

    raise BottlesAdapterError(
        "Bottles did not recognize the newly published bottle. "
        + " | ".join(diagnostics)
    )


class BottlesAdapterError(Exception):
    """Raised when a Bottles adapter operation cannot proceed safely."""


@dataclass(frozen=True)
class BottlesDeploymentResult:
    schema: int
    deployment_id: str
    capsule_id: str
    profile_id: str
    bottle_name: str
    source_object_id: str
    runner: str
    entrypoint: str
    network: str
    source_tree_sha256: str
    deployed_tree_sha256: str
    regular_bytes: int
    file_count: int
    directory_count: int
    symlink_count: int
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BottlesDeploymentVerification:
    schema: int
    deployment_id: str
    capsule_id: str
    profile_id: str
    bottle_name: str
    runner: str
    entrypoint: str
    network: str
    receipt_valid: bool
    configuration_valid: bool
    entrypoint_present: bool
    operational_scripts_valid: bool
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BottlesLaunchPlan:
    schema: int
    deployment_id: str
    capsule_id: str
    profile_id: str
    bottle_name: str
    entrypoint: str
    network: str
    flatpak_app: str
    command: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["command"] = list(self.command)
        return data


@dataclass(frozen=True)
class BottlesRemovalResult:
    schema: int
    deployment_id: str
    capsule_id: str
    profile_id: str
    bottle_name: str
    removed: bool
    persistent_state_declared: int
    state_preservation_confirmed: bool
    stopped_confirmed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _TreeSummary:
    digest: str
    regular_bytes: int
    file_count: int
    directory_count: int
    symlink_count: int


def _safe_relative_path(value: str, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise BottlesAdapterError(f"{field} must be a non-empty string.")
    if "\x00" in value or "\\" in value:
        raise BottlesAdapterError(
            f"{field} is not a safe portable relative path: {value!r}"
        )

    path = PurePosixPath(value)
    if path.is_absolute():
        raise BottlesAdapterError(f"{field} must be relative: {value!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise BottlesAdapterError(
            f"{field} contains an unsafe component: {value!r}"
        )
    return path


def _validate_bottle_name(name: str) -> str:
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        raise BottlesAdapterError(
            "Bottle name must match "
            "[A-Za-z0-9][A-Za-z0-9._-]{0,127}."
        )
    if name.startswith("."):
        raise BottlesAdapterError("Bottle name must not be hidden.")
    return name


def _load_json_regular(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise BottlesAdapterError(f"{label} must not be a symlink: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BottlesAdapterError(f"{label} not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise BottlesAdapterError(
            f"{label} is not valid UTF-8: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise BottlesAdapterError(
            f"Invalid JSON in {label}: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise BottlesAdapterError(
            f"{label} top-level value must be an object."
        )
    return document


def _canonical_existing_directory(path: Path, label: str) -> Path:
    expanded = path.expanduser().absolute()
    if expanded.is_symlink():
        raise BottlesAdapterError(f"{label} must not be a symlink: {expanded}")
    try:
        resolved = expanded.resolve(strict=True)
    except FileNotFoundError as exc:
        raise BottlesAdapterError(f"{label} does not exist: {expanded}") from exc
    if not resolved.is_dir():
        raise BottlesAdapterError(f"{label} is not a directory: {resolved}")
    return resolved


def _load_capsule_profile(
    *,
    capsule_path: Path,
    profile_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    capsule = _load_json_regular(capsule_path, "Capsule")
    capsule_id = capsule.get("capsule_id")
    if not isinstance(capsule_id, str) or not capsule_id:
        raise BottlesAdapterError("capsule_id must be a non-empty string.")

    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise BottlesAdapterError("capsule.profiles must be an array.")

    matches = [
        item
        for item in profiles
        if isinstance(item, dict) and item.get("id") == profile_id
    ]
    if len(matches) != 1:
        raise BottlesAdapterError(
            f"Expected exactly one profile {profile_id!r}; "
            f"found {len(matches)}."
        )

    profile = matches[0]
    if profile.get("adapter") != "bottles":
        raise BottlesAdapterError(
            f"Profile {profile_id!r} is not a Bottles profile."
        )
    if profile.get("platform") != "linux":
        raise BottlesAdapterError(
            f"Profile {profile_id!r} is not a Linux profile."
        )

    launch = profile.get("launch")
    if not isinstance(launch, dict):
        raise BottlesAdapterError("profile.launch must be an object.")

    entrypoint = launch.get("entrypoint")
    _safe_relative_path(entrypoint, "profile.launch.entrypoint")

    arguments = launch.get("arguments", [])
    if not isinstance(arguments, list) or not all(
        isinstance(item, str) for item in arguments
    ):
        raise BottlesAdapterError(
            "profile.launch.arguments must be an array of strings."
        )

    network = launch.get("network", "host_default")
    if network not in {"isolated", "host_default"}:
        raise BottlesAdapterError(
            "profile.launch.network must be isolated or host_default."
        )

    dependencies = profile.get("dependencies")
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise BottlesAdapterError(
            "profile.dependencies must be an array of strings."
        )

    objects = capsule.get("objects")
    if not isinstance(objects, list):
        raise BottlesAdapterError("capsule.objects must be an array.")

    candidates = [
        item
        for item in objects
        if (
            isinstance(item, dict)
            and item.get("id") in dependencies
            and isinstance(item.get("roles"), list)
            and "prefix_baseline" in item["roles"]
        )
    ]
    if len(candidates) != 1:
        raise BottlesAdapterError(
            "A Bottles profile must depend on exactly one object "
            "with role prefix_baseline."
        )

    persistent_state = capsule.get("persistent_state", [])
    if not isinstance(persistent_state, list) or not all(
        isinstance(item, dict) for item in persistent_state
    ):
        raise BottlesAdapterError(
            "capsule.persistent_state must be an array of objects."
        )

    return capsule, profile, candidates[0], persistent_state


def _load_materialization_receipt(
    *,
    materialization: Path,
    capsule_id: str,
    profile_id: str,
    object_id: str,
) -> dict[str, Any]:
    receipt = _load_json_regular(
        materialization / "materialization-receipt.json",
        "Materialization receipt",
    )

    if receipt.get("destination") != ".":
        raise BottlesAdapterError(
            "Materialization receipt is not anchored to its directory."
        )
    if receipt.get("capsule_id") != capsule_id:
        raise BottlesAdapterError(
            "Materialization receipt capsule_id does not match capsule."
        )
    if receipt.get("profile_id") != profile_id:
        raise BottlesAdapterError(
            "Materialization receipt profile_id does not match profile."
        )

    objects = receipt.get("objects")
    if not isinstance(objects, list):
        raise BottlesAdapterError(
            "Materialization receipt objects must be an array."
        )

    matches = [
        item
        for item in objects
        if isinstance(item, dict) and item.get("id") == object_id
    ]
    if len(matches) != 1:
        raise BottlesAdapterError(
            f"Materialization receipt does not contain exactly one "
            f"{object_id!r} object."
        )

    item = matches[0]
    if item.get("destination") != f"objects/{object_id}":
        raise BottlesAdapterError(
            "Materialized object destination does not match object ID."
        )
    if item.get("strategy") != "extract" or item.get("verified") is not True:
        raise BottlesAdapterError(
            "Bottle object is not a verified extracted materialization."
        )

    return receipt


def _walk_no_follow(root: Path) -> Iterable[tuple[Path, os.stat_result]]:
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: entry.name,
                reverse=True,
            )
        except OSError as exc:
            raise BottlesAdapterError(
                f"Cannot enumerate materialized bottle tree: {exc}"
            ) from exc

        for entry in entries:
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise BottlesAdapterError(
                    f"Cannot stat materialized bottle entry: {exc}"
                ) from exc
            yield path, info
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)


def _find_bottle_root(object_root: Path) -> Path:
    candidates: list[Path] = []
    for path, info in _walk_no_follow(object_root):
        if path.name != "bottle.yml":
            continue
        if not stat.S_ISREG(info.st_mode):
            raise BottlesAdapterError(
                "bottle.yml must be a regular file."
            )
        candidates.append(path.parent)

    if len(candidates) != 1:
        raise BottlesAdapterError(
            "Expected exactly one bottle.yml below the materialized "
            f"prefix object; found {len(candidates)}."
        )
    return candidates[0]


def _parse_yaml_scalar(value: str, field: str) -> str | bool:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BottlesAdapterError(
                f"Cannot parse {field} in bottle.yml."
            ) from exc
        if not isinstance(parsed, str):
            raise BottlesAdapterError(
                f"{field} in bottle.yml must be a string."
            )
        return parsed
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _read_top_level_bottle_fields(
    bottle_yml: Path,
) -> dict[str, str | bool]:
    try:
        lines = bottle_yml.read_text(
            encoding="utf-8",
            errors="strict",
        ).splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise BottlesAdapterError(
            f"Cannot read bottle.yml: {exc}"
        ) from exc

    wanted = {"Name", "Path", "Custom_Path", "Runner"}
    found: dict[str, list[str | bool]] = {key: [] for key in wanted}

    for line in lines:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key in wanted:
            found[key].append(_parse_yaml_scalar(raw, key))

    result: dict[str, str | bool] = {}
    for key, values in found.items():
        if len(values) != 1:
            raise BottlesAdapterError(
                f"Expected one top-level {key} in bottle.yml; "
                f"found {len(values)}."
            )
        result[key] = values[0]

    if not isinstance(result["Runner"], str) or not result["Runner"]:
        raise BottlesAdapterError(
            "Top-level Runner in bottle.yml must be a non-empty string."
        )
    return result


def _rewrite_bottle_identity(
    *,
    bottle_yml: Path,
    bottle_name: str,
) -> None:
    try:
        original = bottle_yml.read_text(
            encoding="utf-8",
            errors="strict",
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise BottlesAdapterError(
            f"Cannot read staged bottle.yml: {exc}"
        ) from exc

    replacements = {
        "Name": json.dumps(bottle_name, ensure_ascii=False),
        "Path": json.dumps(bottle_name, ensure_ascii=False),
        "Custom_Path": "false",
    }
    counts = {key: 0 for key in replacements}
    output: list[str] = []

    for line in original.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        newline = line[len(raw):]
        if raw and not raw[0].isspace() and ":" in raw:
            key = raw.split(":", 1)[0]
            if key in replacements:
                output.append(f"{key}: {replacements[key]}{newline}")
                counts[key] += 1
                continue
        output.append(line)

    invalid = {
        key: count
        for key, count in counts.items()
        if count != 1
    }
    if invalid:
        details = ", ".join(
            f"{key}={count}" for key, count in sorted(invalid.items())
        )
        raise BottlesAdapterError(
            "Cannot rewrite bottle identity; unexpected key counts: "
            + details
        )

    serialized = "".join(output)
    temporary = bottle_yml.with_name(
        f".{bottle_yml.name}.ogv-{secrets.token_hex(8)}"
    )

    try:
        with temporary.open(
            "x",
            encoding="utf-8",
            newline="",
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(bottle_yml.stat().st_mode))
        os.replace(temporary, bottle_yml)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise BottlesAdapterError(
            f"Cannot rewrite staged bottle.yml: {exc}"
        ) from exc


def _symlink_target_within_root(
    *,
    root: Path,
    link: Path,
    raw_target: str,
) -> bool:
    if "\x00" in raw_target or os.path.isabs(raw_target):
        return False
    try:
        candidate = (link.parent / raw_target).resolve(strict=False)
        candidate.relative_to(root)
    except (ValueError, RuntimeError, OSError):
        return False
    return True


def _tree_manifest(
    root: Path,
    *,
    exclude_receipt: bool = False,
) -> tuple[dict[str, tuple[Any, ...]], _TreeSummary]:
    manifest: dict[str, tuple[Any, ...]] = {}
    inode_paths: dict[tuple[int, int], list[str]] = {}
    regular_bytes = 0
    file_count = 0
    directory_count = 0
    symlink_count = 0

    for path, info in _walk_no_follow(root):
        relative = path.relative_to(root).as_posix()
        if exclude_receipt and relative == DEPLOYMENT_RECEIPT_NAME:
            continue

        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            raw_target = os.readlink(path)
            if not _symlink_target_within_root(
                root=root,
                link=path,
                raw_target=raw_target,
            ):
                raise BottlesAdapterError(
                    f"Unsafe symbolic link in bottle tree: {relative}"
                )
            if not path.exists():
                raise BottlesAdapterError(
                    f"Broken symbolic link in bottle tree: {relative}"
                )
            manifest[relative] = ("symlink", mode, raw_target)
            symlink_count += 1
        elif stat.S_ISDIR(info.st_mode):
            manifest[relative] = ("directory", mode)
            directory_count += 1
        elif stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    while True:
                        block = handle.read(8 * 1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
            except OSError as exc:
                raise BottlesAdapterError(
                    f"Cannot hash bottle file {relative}: {exc}"
                ) from exc

            manifest[relative] = (
                "file",
                mode,
                info.st_size,
                digest.hexdigest(),
            )
            inode_paths.setdefault(
                (info.st_dev, info.st_ino),
                [],
            ).append(relative)
            regular_bytes += info.st_size
            file_count += 1
        else:
            raise BottlesAdapterError(
                f"Unsupported special entry in bottle tree: {relative}"
            )

    hardlinks = [
        sorted(paths)
        for paths in inode_paths.values()
        if len(paths) > 1
    ]
    if hardlinks:
        raise BottlesAdapterError(
            "Bottle deployment does not yet support source hardlinks."
        )

    tree_hash = hashlib.sha256()
    for relative in sorted(manifest):
        record = [relative, *manifest[relative]]
        tree_hash.update(
            json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        tree_hash.update(b"\n")

    summary = _TreeSummary(
        digest=tree_hash.hexdigest(),
        regular_bytes=regular_bytes,
        file_count=file_count,
        directory_count=directory_count,
        symlink_count=symlink_count,
    )
    return manifest, summary


def _fsync_tree(root: Path) -> None:
    regular_files: list[Path] = []
    directories = [root]

    for path, info in _walk_no_follow(root):
        if stat.S_ISREG(info.st_mode):
            regular_files.append(path)
        elif stat.S_ISDIR(info.st_mode):
            directories.append(path)

    for path in regular_files:
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise BottlesAdapterError(
                f"Cannot fsync deployed file: {exc}"
            ) from exc

    for directory in sorted(
        directories,
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise BottlesAdapterError(
                f"Cannot fsync deployed directory: {exc}"
            ) from exc


def _rename_noreplace(source: Path, destination: Path) -> None:
    if not sys.platform.startswith("linux"):
        raise BottlesAdapterError(
            "Bottles deployment currently requires Linux."
        )

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BottlesAdapterError(
            "Linux libc does not expose renameat2; "
            "atomic no-replace publication is unavailable."
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
            raise BottlesAdapterError(
                f"Destination already exists: {destination}"
            )
        raise BottlesAdapterError(
            "Atomic publication failed: "
            f"{os.strerror(error_number)}"
        )


def _acquire_lock(parent: Path, bottle_name: str) -> tuple[Path, int]:
    lock = parent / f".ogv-lock-bottles-{bottle_name}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(lock, flags, 0o600)
    except FileExistsError as exc:
        raise BottlesAdapterError(
            "Another Bottles adapter operation holds the deployment lock."
        ) from exc
    except OSError as exc:
        raise BottlesAdapterError(
            f"Cannot create Bottles deployment lock: {exc}"
        ) from exc
    return lock, descriptor


def _release_lock(lock: Path, descriptor: int) -> None:
    try:
        os.close(descriptor)
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _portable_bottles_runtime_source() -> str:
    from . import portable_bottles_runtime

    path = Path(portable_bottles_runtime.__file__).resolve()
    if path.is_symlink() or not path.is_file():
        raise BottlesAdapterError(
            "Cannot locate the portable Bottles runtime source."
        )
    return path.read_text(encoding="utf-8")


def _operational_script(command: str) -> str:
    if command not in {"play", "verify", "uninstall"}:
        raise BottlesAdapterError("Unsupported operational script command.")
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd -P)"
exec "${{PYTHON:-python3}}" \
  "$root/{PORTABLE_RUNTIME_DESTINATION}" \
  {command} --root "$root" "$@"
"""


def _write_operational_file(path: Path, payload: str, mode: int) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise BottlesAdapterError(
            f"Operational path already exists in staged bottle: {path.name}"
        )
    try:
        path.write_text(payload, encoding="utf-8", newline="\n")
        path.chmod(mode)
    except OSError as exc:
        raise BottlesAdapterError(
            f"Cannot write operational file {path.name}: {exc}"
        ) from exc


def _install_operational_scripts(staging: Path) -> dict[str, str]:
    runtime = staging / PORTABLE_RUNTIME_DESTINATION
    launcher = staging / ROOT_LAUNCHER
    verifier = staging / ROOT_VERIFIER
    uninstaller = staging / ROOT_UNINSTALLER
    _write_operational_file(
        runtime,
        _portable_bottles_runtime_source(),
        0o600,
    )
    compile(runtime.read_text(encoding="utf-8"), str(runtime), "exec")
    _write_operational_file(
        launcher, _operational_script("play"), 0o700
    )
    _write_operational_file(
        verifier, _operational_script("verify"), 0o700
    )
    _write_operational_file(
        uninstaller, _operational_script("uninstall"), 0o700
    )
    if shutil.which("bash") is not None:
        for script in (launcher, verifier, uninstaller):
            process = subprocess.run(
                ["bash", "-n", str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if (
                isinstance(process.returncode, int)
                and process.returncode != 0
            ):
                raise BottlesAdapterError(
                    f"Generated shell script is invalid: {script.name}"
                )
    return {
        "launcher": ROOT_LAUNCHER,
        "verifier": ROOT_VERIFIER,
        "uninstaller": ROOT_UNINSTALLER,
        "portable_runtime": PORTABLE_RUNTIME_DESTINATION,
    }


def _portable_external_bottles_runtime_source() -> str:
    from . import portable_external_bottles_runtime

    path = Path(portable_external_bottles_runtime.__file__).resolve()
    if path.is_symlink() or not path.is_file():
        raise BottlesAdapterError(
            "Cannot locate the external Bottles runtime source."
        )
    return path.read_text(encoding="utf-8")


def _external_operational_script(command: str) -> str:
    if command not in {"play", "verify", "uninstall"}:
        raise BottlesAdapterError(
            "Unsupported external operational script command."
        )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd -P)"
exec "${{PYTHON:-python3}}" \
  "$root/{EXTERNAL_PORTABLE_RUNTIME_DESTINATION}" \
  {command} --root "$root" "$@"
"""


def _install_external_operational_scripts(
    staging: Path,
) -> dict[str, str]:
    runtime = staging / EXTERNAL_PORTABLE_RUNTIME_DESTINATION
    launcher = staging / ROOT_LAUNCHER
    verifier = staging / ROOT_VERIFIER
    uninstaller = staging / ROOT_UNINSTALLER
    _write_operational_file(
        runtime,
        _portable_external_bottles_runtime_source(),
        0o600,
    )
    compile(runtime.read_text(encoding="utf-8"), str(runtime), "exec")
    _write_operational_file(
        launcher,
        _external_operational_script("play"),
        0o700,
    )
    _write_operational_file(
        verifier,
        _external_operational_script("verify"),
        0o700,
    )
    _write_operational_file(
        uninstaller,
        _external_operational_script("uninstall"),
        0o700,
    )
    if shutil.which("bash") is not None:
        for script in (launcher, verifier, uninstaller):
            process = subprocess.run(
                ["bash", "-n", str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if process.returncode != 0:
                raise BottlesAdapterError(
                    f"Generated shell script is invalid: {script.name}"
                )
    return {
        "launcher": ROOT_LAUNCHER,
        "verifier": ROOT_VERIFIER,
        "uninstaller": ROOT_UNINSTALLER,
        "portable_runtime": EXTERNAL_PORTABLE_RUNTIME_DESTINATION,
    }


def _write_deployment_receipt(
    *,
    staging: Path,
    document: dict[str, Any],
) -> None:
    path = staging / DEPLOYMENT_RECEIPT_NAME
    serialized = (
        json.dumps(
            document,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )
    try:
        with path.open(
            "x",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BottlesAdapterError(
            f"Cannot write Bottles deployment receipt: {exc}"
        ) from exc


def deploy_bottles_profile(
    *,
    capsule_path: Path,
    profile_id: str,
    materialization: Path,
    bottles_path: Path,
    bottle_name: str,
    state_backup: Path | None = None,
    require_state_backup: bool = False,
    state_capsule_path: Path | None = None,
) -> BottlesDeploymentResult:
    """Copy one materialized bottle into Bottles as a mutable derivative."""

    capsule_path = capsule_path.expanduser().absolute()
    state_capsule_path = (
        capsule_path
        if state_capsule_path is None
        else state_capsule_path.expanduser().absolute()
    )
    materialization = _canonical_existing_directory(
        materialization,
        "Materialization",
    )
    bottle_name = _validate_bottle_name(bottle_name)
    bottles_path = require_bottles_managed_path(bottles_path)

    if not os.access(bottles_path, os.W_OK):
        raise BottlesAdapterError(
            "Bottles managed path is not writable."
        )

    capsule, profile, bottle_object, persistent_state = (
        _load_capsule_profile(
            capsule_path=capsule_path,
            profile_id=profile_id,
        )
    )
    state_capsule = _load_json_regular(
        state_capsule_path,
        "State capsule",
    )
    if (
        state_capsule.get("capsule_id")
        != capsule.get("capsule_id")
    ):
        raise BottlesAdapterError(
            "State capsule belongs to another capsule."
        )
    try:
        state_selection = prepare_composition_state(
            capsule_path=state_capsule_path,
            state_backup=state_backup,
            require_declared_state=require_state_backup,
        )
    except CompositionStateError as exc:
        raise BottlesAdapterError(str(exc)) from exc

    capsule_id = capsule["capsule_id"]
    object_id = bottle_object.get("id")
    if not isinstance(object_id, str) or not object_id:
        raise BottlesAdapterError(
            "Bottle object ID must be a non-empty string."
        )

    materialization_receipt = _load_materialization_receipt(
        materialization=materialization,
        capsule_id=capsule_id,
        profile_id=profile_id,
        object_id=object_id,
    )
    materialization_receipt_id = materialization_receipt.get(
        "receipt_id"
    )
    if (
        not isinstance(materialization_receipt_id, str)
        or not materialization_receipt_id
    ):
        raise BottlesAdapterError(
            "Materialization receipt_id must be a non-empty string."
        )

    object_root = materialization / "objects" / object_id
    if object_root.is_symlink() or not object_root.is_dir():
        raise BottlesAdapterError(
            "Materialized bottle object root is not a regular directory."
        )

    source = _find_bottle_root(object_root)
    source_fields = _read_top_level_bottle_fields(
        source / "bottle.yml"
    )
    runner = source_fields["Runner"]
    assert isinstance(runner, str)

    launch = profile["launch"]
    entrypoint = launch["entrypoint"]
    entrypoint_path = _safe_relative_path(
        entrypoint,
        "profile.launch.entrypoint",
    )
    source_entrypoint = source.joinpath(*entrypoint_path.parts)
    if source_entrypoint.is_symlink() or not source_entrypoint.is_file():
        raise BottlesAdapterError(
            "Profile entrypoint is not a regular file in the "
            "materialized bottle."
        )

    network = launch.get("network", "host_default")
    arguments = tuple(launch.get("arguments", []))

    target = bottles_path / bottle_name
    if target.exists() or target.is_symlink():
        raise BottlesAdapterError(
            f"Bottles deployment destination already exists: {bottle_name}"
        )

    source_manifest, source_summary = _tree_manifest(source)
    free_bytes = shutil.disk_usage(bottles_path).free
    if source_summary.regular_bytes > free_bytes:
        raise BottlesAdapterError(
            "Insufficient free space for Bottles deployment: "
            f"required {source_summary.regular_bytes}, "
            f"available {free_bytes}."
        )

    lock, descriptor = _acquire_lock(bottles_path, bottle_name)
    staging = bottles_path / (
        f".ogv-stage-bottles-{bottle_name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )
    promoted = False
    published = False
    state_snapshot = bottles_path / (
        f".ogv-state-snapshot-{bottle_name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )
    state_evidence = None

    try:
        if target.exists() or target.is_symlink():
            raise BottlesAdapterError(
                f"Bottles deployment destination appeared: {bottle_name}"
            )

        try:
            shutil.copytree(
                source,
                staging,
                symlinks=True,
                copy_function=shutil.copy2,
            )
        except OSError as exc:
            raise BottlesAdapterError(
                f"Cannot copy materialized bottle: {exc}"
            ) from exc

        staged_manifest, staged_source_summary = _tree_manifest(staging)
        if source_manifest != staged_manifest:
            raise BottlesAdapterError(
                "Staged bottle does not match materialized source."
            )

        _rewrite_bottle_identity(
            bottle_yml=staging / "bottle.yml",
            bottle_name=bottle_name,
        )

        staged_fields = _read_top_level_bottle_fields(
            staging / "bottle.yml"
        )
        if (
            staged_fields["Name"] != bottle_name
            or staged_fields["Path"] != bottle_name
            or staged_fields["Custom_Path"] is not False
            or staged_fields["Runner"] != runner
        ):
            raise BottlesAdapterError(
                "Staged bottle identity verification failed."
            )

        if state_selection is not None:
            try:
                state_evidence = restore_composition_state(
                    capsule_path=state_capsule_path,
                    state_root=staging,
                    state_root_relative=".",
                    selection=state_selection,
                    snapshot=state_snapshot,
                    evidence_root=staging / "receipts/state",
                    evidence_relative="receipts/state",
                )
            except CompositionStateError as exc:
                raise BottlesAdapterError(str(exc)) from exc

        _, deployed_summary = _tree_manifest(staging)

        receipt_state: list[dict[str, Any]] = []
        for index, item in enumerate(persistent_state):
            item_id = item.get("id")
            item_path = item.get("path")
            if not isinstance(item_id, str) or not item_id:
                raise BottlesAdapterError(
                    f"persistent_state[{index}].id is invalid."
                )
            _safe_relative_path(
                item_path,
                f"persistent_state[{index}].path",
            )
            receipt_state.append(
                {
                    "id": item_id,
                    "path": item_path,
                    "preserve_on_remove": bool(item.get("backup")),
                    "sensitive": bool(item.get("sensitive")),
                }
            )

        operational_paths = _install_operational_scripts(staging)

        deployment_id = str(uuid.uuid4())
        receipt = {
            "schema": 0,
            "deployment_id": deployment_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "orchestrator_version": __version__,
            "adapter": "bottles-flatpak",
            "capsule_id": capsule_id,
            "profile_id": profile_id,
            "materialization_receipt_id": materialization_receipt_id,
            "source_object_id": object_id,
            "bottle_name": bottle_name,
            "destination": ".",
            "runner": runner,
            "launch": {
                "entrypoint": entrypoint,
                "arguments": list(arguments),
                "network": network,
            },
            "operational_paths": operational_paths,
            "source_tree_sha256": source_summary.digest,
            "deployed_tree_sha256": deployed_summary.digest,
            "persistent_state": receipt_state,
            "removal": {
                "requires_stopped_confirmation": True,
                "must_preserve": [
                    item["path"]
                    for item in receipt_state
                    if item["preserve_on_remove"]
                ],
            },
        }
        if state_evidence is not None:
            receipt["state_restore"] = state_evidence.to_dict()

        _write_deployment_receipt(
            staging=staging,
            document=receipt,
        )
        _fsync_tree(staging)
        _rename_noreplace(staging, target)
        published = True
        assert_bottle_registered(bottle_name)
        promoted = True

        return BottlesDeploymentResult(
            schema=0,
            deployment_id=deployment_id,
            capsule_id=capsule_id,
            profile_id=profile_id,
            bottle_name=bottle_name,
            source_object_id=object_id,
            runner=runner,
            entrypoint=entrypoint,
            network=network,
            source_tree_sha256=source_summary.digest,
            deployed_tree_sha256=deployed_summary.digest,
            regular_bytes=staged_source_summary.regular_bytes,
            file_count=staged_source_summary.file_count,
            directory_count=staged_source_summary.directory_count,
            symlink_count=staged_source_summary.symlink_count,
            complete=True,
        )
    finally:
        if not promoted:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if published and target.exists() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
        if state_snapshot.exists():
            shutil.rmtree(state_snapshot, ignore_errors=True)
        _release_lock(lock, descriptor)


def _load_external_bottles_contract(
    *,
    capsule_path: Path,
    profile: dict[str, Any],
) -> tuple[PurePosixPath, PurePosixPath]:
    raw = profile.get("host_contract")
    relative = _safe_relative_path(
        raw,
        "profile.host_contract",
    )
    capsule_root = capsule_path.parent.resolve(strict=True)
    current = capsule_root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise BottlesAdapterError(
                "Bottles host contract is absent."
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise BottlesAdapterError(
                "Bottles host contract traverses a symbolic link."
            )
    contract_path = current.resolve(strict=True)
    try:
        contract_path.relative_to(capsule_root)
    except ValueError as exc:
        raise BottlesAdapterError(
            "Bottles host contract escapes the capsule."
        ) from exc
    contract = _load_json_regular(
        contract_path,
        "Bottles host contract",
    )
    if contract.get("contract") != "ogv-bottles-neutral-v1":
        raise BottlesAdapterError(
            "External Bottles deployment requires "
            "ogv-bottles-neutral-v1."
        )
    game_destination = _safe_relative_path(
        contract.get("game_destination_in_prefix"),
        "host_contract.game_destination_in_prefix",
    )
    entrypoint_relative = _safe_relative_path(
        contract.get("entrypoint_relative_to_game"),
        "host_contract.entrypoint_relative_to_game",
    )
    expected_entrypoint = game_destination.joinpath(
        entrypoint_relative
    ).as_posix()
    launch = profile.get("launch")
    if (
        not isinstance(launch, dict)
        or launch.get("entrypoint") != expected_entrypoint
    ):
        raise BottlesAdapterError(
            "Profile entrypoint does not match the neutral contract."
        )
    return game_destination, entrypoint_relative


def _new_external_destination(
    destination: Path,
    *,
    bottles_path: Path,
) -> Path:
    expanded = destination.expanduser().absolute()
    if expanded.exists() or expanded.is_symlink():
        raise BottlesAdapterError(
            f"External Bottles destination already exists: {expanded}"
        )
    parent = expanded.parent
    if parent.is_symlink() or not parent.is_dir():
        raise BottlesAdapterError(
            "External Bottles destination parent must be "
            "an existing regular directory."
        )
    parent = parent.resolve(strict=True)
    target = parent / expanded.name
    if target.name in {"", ".", ".."}:
        raise BottlesAdapterError(
            "External Bottles destination name is invalid."
        )
    if target == Path(target.anchor) or target == Path.home().resolve():
        raise BottlesAdapterError(
            "External Bottles destination is unsafe."
        )
    try:
        target.relative_to(bottles_path)
    except ValueError:
        pass
    else:
        raise BottlesAdapterError(
            "External Bottles destination must remain outside "
            "the managed Bottles directory."
        )
    return target


def _registration_matches(
    registration: Path,
    expected: Path,
) -> bool:
    if not registration.is_symlink():
        return False
    try:
        return (
            registration.resolve(strict=True)
            == expected.resolve(strict=True)
        )
    except OSError:
        return False


def _create_external_registration(
    *,
    registration: Path,
    target: Path,
) -> None:
    if registration.exists() or registration.is_symlink():
        raise BottlesAdapterError(
            "Bottles registration name already exists."
        )
    try:
        registration.symlink_to(
            target,
            target_is_directory=True,
        )
    except FileExistsError as exc:
        raise BottlesAdapterError(
            "Bottles registration appeared concurrently."
        ) from exc
    except OSError as exc:
        raise BottlesAdapterError(
            f"Cannot create Bottles registration: {exc}"
        ) from exc


def deploy_external_bottles_profile(
    *,
    capsule_path: Path,
    profile_id: str,
    materialization: Path,
    destination: Path,
    bottles_path: Path,
    bottle_name: str,
    state_backup: Path | None = None,
    require_state_backup: bool = False,
    state_capsule_path: Path | None = None,
) -> BottlesDeploymentResult:
    """Publish one Bottles derivative at a user-selected external root."""

    capsule_path = capsule_path.expanduser().absolute()
    state_capsule_path = (
        capsule_path
        if state_capsule_path is None
        else state_capsule_path.expanduser().absolute()
    )
    materialization = _canonical_existing_directory(
        materialization,
        "Materialization",
    )
    bottle_name = _validate_bottle_name(bottle_name)
    bottles_path = require_bottles_managed_path(bottles_path)
    if not os.access(bottles_path, os.W_OK):
        raise BottlesAdapterError(
            "Bottles managed path is not writable."
        )
    target = _new_external_destination(
        destination,
        bottles_path=bottles_path,
    )
    if not os.access(target.parent, os.W_OK):
        raise BottlesAdapterError(
            "External Bottles destination parent is not writable."
        )

    capsule, profile, bottle_object, persistent_state = (
        _load_capsule_profile(
            capsule_path=capsule_path,
            profile_id=profile_id,
        )
    )
    game_destination, entrypoint_relative = (
        _load_external_bottles_contract(
            capsule_path=capsule_path,
            profile=profile,
        )
    )
    state_capsule = _load_json_regular(
        state_capsule_path,
        "State capsule",
    )
    if state_capsule.get("capsule_id") != capsule.get("capsule_id"):
        raise BottlesAdapterError(
            "State capsule belongs to another capsule."
        )
    try:
        state_selection = prepare_composition_state(
            capsule_path=state_capsule_path,
            state_backup=state_backup,
            require_declared_state=require_state_backup,
        )
    except CompositionStateError as exc:
        raise BottlesAdapterError(str(exc)) from exc

    capsule_id = capsule["capsule_id"]
    object_id = bottle_object.get("id")
    if not isinstance(object_id, str) or not object_id:
        raise BottlesAdapterError(
            "Bottle object ID must be a non-empty string."
        )
    materialization_receipt = _load_materialization_receipt(
        materialization=materialization,
        capsule_id=capsule_id,
        profile_id=profile_id,
        object_id=object_id,
    )
    materialization_receipt_id = materialization_receipt.get(
        "receipt_id"
    )
    if (
        not isinstance(materialization_receipt_id, str)
        or not materialization_receipt_id
    ):
        raise BottlesAdapterError(
            "Materialization receipt_id must be a non-empty string."
        )

    object_root = materialization / "objects" / object_id
    if object_root.is_symlink() or not object_root.is_dir():
        raise BottlesAdapterError(
            "Materialized bottle object root is not a regular directory."
        )
    source = _find_bottle_root(object_root)
    source_fields = _read_top_level_bottle_fields(
        source / "bottle.yml"
    )
    runner = source_fields["Runner"]
    assert isinstance(runner, str)

    source_game = source.joinpath(*game_destination.parts)
    source_entrypoint = source_game.joinpath(
        *entrypoint_relative.parts
    )
    if source_game.is_symlink() or not source_game.is_dir():
        raise BottlesAdapterError(
            "Neutral Bottles game source is not a regular directory."
        )
    if (
        source_entrypoint.is_symlink()
        or not source_entrypoint.is_file()
    ):
        raise BottlesAdapterError(
            "Profile entrypoint is not a regular file in the "
            "materialized bottle."
        )

    launch = profile["launch"]
    network = launch.get("network", "host_default")
    arguments = tuple(launch.get("arguments", []))
    registration = bottles_path / bottle_name
    if registration.exists() or registration.is_symlink():
        raise BottlesAdapterError(
            f"Bottles registration already exists: {bottle_name}"
        )

    source_manifest, source_summary = _tree_manifest(source)
    free_bytes = shutil.disk_usage(target.parent).free
    if source_summary.regular_bytes > free_bytes:
        raise BottlesAdapterError(
            "Insufficient free space for external Bottles deployment: "
            f"required {source_summary.regular_bytes}, "
            f"available {free_bytes}."
        )

    lock, descriptor = _acquire_lock(bottles_path, bottle_name)
    staging = target.parent / (
        f".ogv-stage-external-bottles-{target.name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )
    state_snapshot = target.parent / (
        f".ogv-state-snapshot-{target.name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )
    published = False
    registered = False
    promoted = False
    state_evidence = None

    try:
        if target.exists() or target.is_symlink():
            raise BottlesAdapterError(
                "External Bottles destination appeared concurrently."
            )
        if registration.exists() or registration.is_symlink():
            raise BottlesAdapterError(
                "Bottles registration appeared concurrently."
            )

        prefix = staging / EXTERNAL_PREFIX_DESTINATION
        game = staging / EXTERNAL_GAME_DESTINATION
        prefix.parent.mkdir(parents=True, mode=0o700)
        try:
            shutil.copytree(
                source,
                prefix,
                symlinks=True,
                copy_function=shutil.copy2,
            )
        except OSError as exc:
            raise BottlesAdapterError(
                f"Cannot copy materialized bottle: {exc}"
            ) from exc

        staged_manifest, staged_source_summary = _tree_manifest(prefix)
        if source_manifest != staged_manifest:
            raise BottlesAdapterError(
                "Staged prefix does not match materialized source."
            )

        _rewrite_bottle_identity(
            bottle_yml=prefix / "bottle.yml",
            bottle_name=bottle_name,
        )
        staged_fields = _read_top_level_bottle_fields(
            prefix / "bottle.yml"
        )
        if (
            staged_fields["Name"] != bottle_name
            or staged_fields["Path"] != bottle_name
            or staged_fields["Custom_Path"] is not False
            or staged_fields["Runner"] != runner
        ):
            raise BottlesAdapterError(
                "Staged bottle identity verification failed."
            )

        if state_selection is not None:
            try:
                state_evidence = restore_composition_state(
                    capsule_path=state_capsule_path,
                    state_root=prefix,
                    state_root_relative=EXTERNAL_PREFIX_DESTINATION,
                    selection=state_selection,
                    snapshot=state_snapshot,
                    evidence_root=staging / "receipts/state",
                    evidence_relative="receipts/state",
                )
            except CompositionStateError as exc:
                raise BottlesAdapterError(str(exc)) from exc

        prefix_game = prefix.joinpath(*game_destination.parts)
        if prefix_game.is_symlink() or not prefix_game.is_dir():
            raise BottlesAdapterError(
                "Game payload disappeared from the staged prefix."
            )
        game.parent.mkdir(parents=True, exist_ok=True)
        os.replace(prefix_game, game)
        relative_target = os.path.relpath(
            game,
            start=prefix_game.parent,
        )
        prefix_game.symlink_to(
            relative_target,
            target_is_directory=True,
        )
        deployed_entrypoint = game.joinpath(
            *entrypoint_relative.parts
        )
        if (
            deployed_entrypoint.is_symlink()
            or not deployed_entrypoint.is_file()
        ):
            raise BottlesAdapterError(
                "External game entrypoint is absent after publication."
            )

        receipt_state: list[dict[str, Any]] = []
        for index, item in enumerate(persistent_state):
            item_id = item.get("id")
            item_path = item.get("path")
            if not isinstance(item_id, str) or not item_id:
                raise BottlesAdapterError(
                    f"persistent_state[{index}].id is invalid."
                )
            _safe_relative_path(
                item_path,
                f"persistent_state[{index}].path",
            )
            receipt_state.append(
                {
                    "id": item_id,
                    "path": item_path,
                    "preserve_on_remove": bool(item.get("backup")),
                    "sensitive": bool(item.get("sensitive")),
                }
            )

        operational_paths = _install_external_operational_scripts(
            staging
        )
        _, deployed_summary = _tree_manifest(staging)
        deployment_id = str(uuid.uuid4())
        entrypoint = (
            PurePosixPath(EXTERNAL_GAME_DESTINATION)
            .joinpath(entrypoint_relative)
            .as_posix()
        )
        receipt = {
            "schema": 0,
            "deployment_id": deployment_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "orchestrator_version": __version__,
            "adapter": "bottles-flatpak",
            "capsule_id": capsule_id,
            "profile_id": profile_id,
            "materialization_receipt_id": materialization_receipt_id,
            "source_object_id": object_id,
            "bottle_name": bottle_name,
            "destination": ".",
            "runner": runner,
            "layout": {
                "kind": "external-wrapper-v1",
                "prefix": EXTERNAL_PREFIX_DESTINATION,
                "game": EXTERNAL_GAME_DESTINATION,
                "registration_target": EXTERNAL_PREFIX_DESTINATION,
                "game_destination_in_prefix":
                    game_destination.as_posix(),
            },
            "launch": {
                "entrypoint": entrypoint,
                "arguments": list(arguments),
                "network": network,
            },
            "operational_paths": operational_paths,
            "source_tree_sha256": source_summary.digest,
            "deployed_tree_sha256": deployed_summary.digest,
            "persistent_state": receipt_state,
            "removal": {
                "requires_stopped_confirmation": True,
                "must_preserve": [
                    item["path"]
                    for item in receipt_state
                    if item["preserve_on_remove"]
                ],
            },
        }
        if state_evidence is not None:
            receipt["state_restore"] = state_evidence.to_dict()

        _write_deployment_receipt(
            staging=staging,
            document=receipt,
        )
        _fsync_tree(staging)
        _rename_noreplace(staging, target)
        published = True

        registration_target = (
            target / EXTERNAL_PREFIX_DESTINATION
        )
        _create_external_registration(
            registration=registration,
            target=registration_target,
        )
        registered = True
        assert_bottle_registered(
            bottle_name,
            filesystem_root=target,
        )
        promoted = True

        return BottlesDeploymentResult(
            schema=0,
            deployment_id=deployment_id,
            capsule_id=capsule_id,
            profile_id=profile_id,
            bottle_name=bottle_name,
            source_object_id=object_id,
            runner=runner,
            entrypoint=entrypoint,
            network=network,
            source_tree_sha256=source_summary.digest,
            deployed_tree_sha256=deployed_summary.digest,
            regular_bytes=staged_source_summary.regular_bytes,
            file_count=staged_source_summary.file_count,
            directory_count=staged_source_summary.directory_count,
            symlink_count=staged_source_summary.symlink_count,
            complete=True,
        )
    finally:
        if not promoted:
            if registered and _registration_matches(
                registration,
                target / EXTERNAL_PREFIX_DESTINATION,
            ):
                registration.unlink(missing_ok=True)
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)
            if published and target.exists() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
        if state_snapshot.exists() and not state_snapshot.is_symlink():
            shutil.rmtree(state_snapshot, ignore_errors=True)
        _release_lock(lock, descriptor)


def _external_deployment(
    *,
    destination: Path,
    bottles_path: Path | None,
    flatpak_app: str,
    require_registration: bool,
) -> tuple[
    Path,
    Path,
    Path,
    dict[str, Any],
    str,
]:
    root = _canonical_existing_directory(
        destination,
        "External Bottles materialization",
    )
    receipt = _load_deployment_receipt(
        target=root,
        bottle_name=str(
            _load_json_regular(
                root / DEPLOYMENT_RECEIPT_NAME,
                "Bottles deployment receipt",
            ).get("bottle_name", "")
        ),
    )
    layout = receipt.get("layout")
    if (
        not isinstance(layout, dict)
        or layout.get("kind") != "external-wrapper-v1"
    ):
        raise BottlesAdapterError(
            "Deployment is not an external Bottles materialization."
        )
    prefix_relative = _safe_relative_path(
        layout.get("prefix"),
        "deployment.layout.prefix",
    )
    game_relative = _safe_relative_path(
        layout.get("game"),
        "deployment.layout.game",
    )
    registration_relative = _safe_relative_path(
        layout.get("registration_target"),
        "deployment.layout.registration_target",
    )
    prefix = root.joinpath(*prefix_relative.parts)
    game = root.joinpath(*game_relative.parts)
    registration_target = root.joinpath(
        *registration_relative.parts
    )
    if registration_target != prefix:
        raise BottlesAdapterError(
            "Registration target does not match external prefix."
        )
    if prefix.is_symlink() or not prefix.is_dir():
        raise BottlesAdapterError(
            "External Bottles prefix is absent."
        )
    if game.is_symlink() or not game.is_dir():
        raise BottlesAdapterError(
            "External Bottles game payload is absent."
        )

    managed = require_bottles_managed_path(
        bottles_path,
        flatpak_app=flatpak_app,
    )
    bottle_name = _validate_bottle_name(receipt["bottle_name"])
    registration = managed / bottle_name
    if require_registration and not _registration_matches(
        registration,
        registration_target,
    ):
        raise BottlesAdapterError(
            "External Bottles registration is absent or targets "
            "another materialization."
        )
    return root, prefix, registration, receipt, bottle_name


def verify_external_bottles_deployment(
    *,
    destination: Path,
    bottles_path: Path | None = None,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> BottlesDeploymentVerification:
    root, prefix, _registration, receipt, bottle_name = (
        _external_deployment(
            destination=destination,
            bottles_path=bottles_path,
            flatpak_app=flatpak_app,
            require_registration=True,
        )
    )
    try:
        verify_composition_state_evidence(
            root=root,
            evidence=receipt.get("state_restore"),
            capsule_id=receipt["capsule_id"],
        )
    except CompositionStateError as exc:
        raise BottlesAdapterError(
            f"Invalid state restoration evidence: {exc}"
        ) from exc

    fields = _read_top_level_bottle_fields(
        prefix / "bottle.yml"
    )
    configuration_valid = (
        fields["Name"] == bottle_name
        and fields["Path"] == bottle_name
        and fields["Custom_Path"] is False
        and fields["Runner"] == receipt["runner"]
    )
    launch = receipt["launch"]
    entrypoint_path = _safe_relative_path(
        launch["entrypoint"],
        "deployment.launch.entrypoint",
    )
    entrypoint = root.joinpath(*entrypoint_path.parts)
    entrypoint_present = (
        not entrypoint.is_symlink()
        and entrypoint.is_file()
    )

    operational = receipt.get("operational_paths")
    operational_scripts_valid = isinstance(operational, dict)
    if operational_scripts_valid:
        for key in (
            "launcher",
            "verifier",
            "uninstaller",
            "portable_runtime",
        ):
            value = operational.get(key)
            try:
                relative = _safe_relative_path(
                    value,
                    f"deployment.operational_paths.{key}",
                )
            except BottlesAdapterError:
                operational_scripts_valid = False
                break
            path = root.joinpath(*relative.parts)
            if path.is_symlink() or not path.is_file():
                operational_scripts_valid = False
                break
            if key != "portable_runtime" and not os.access(
                path,
                os.X_OK,
            ):
                operational_scripts_valid = False
                break

    verified = (
        configuration_valid
        and entrypoint_present
        and operational_scripts_valid
    )
    return BottlesDeploymentVerification(
        schema=0,
        deployment_id=receipt["deployment_id"],
        capsule_id=receipt["capsule_id"],
        profile_id=receipt["profile_id"],
        bottle_name=bottle_name,
        runner=receipt["runner"],
        entrypoint=launch["entrypoint"],
        network=launch["network"],
        receipt_valid=True,
        configuration_valid=configuration_valid,
        entrypoint_present=entrypoint_present,
        operational_scripts_valid=operational_scripts_valid,
        verified=verified,
    )


def build_external_bottles_launch_plan(
    *,
    destination: Path,
    bottles_path: Path | None = None,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> tuple[BottlesLaunchPlan, tuple[str, ...]]:
    root, _prefix, registration, receipt, bottle_name = (
        _external_deployment(
            destination=destination,
            bottles_path=bottles_path,
            flatpak_app=flatpak_app,
            require_registration=False,
        )
    )
    layout = receipt["layout"]
    registration_target_relative = _safe_relative_path(
        layout["registration_target"],
        "deployment.layout.registration_target",
    )
    registration_target = root.joinpath(
        *registration_target_relative.parts
    )
    if registration.is_symlink():
        if not _registration_matches(
            registration,
            registration_target,
        ):
            raise BottlesAdapterError(
                "Bottle name is registered to another materialization."
            )
    elif registration.exists():
        raise BottlesAdapterError(
            "Bottle name collides with a managed directory."
        )
    else:
        _create_external_registration(
            registration=registration,
            target=registration_target,
        )

    assert_bottle_registered(
        bottle_name,
        flatpak_app=flatpak_app,
        filesystem_root=root,
    )
    verification = verify_external_bottles_deployment(
        destination=root,
        bottles_path=registration.parent,
        flatpak_app=flatpak_app,
    )
    if not verification.verified:
        raise BottlesAdapterError(
            "External Bottles deployment is not verified."
        )

    launch = receipt["launch"]
    entrypoint_relative = _safe_relative_path(
        launch["entrypoint"],
        "deployment.launch.entrypoint",
    )
    executable = root.joinpath(*entrypoint_relative.parts)
    command: list[str] = [
        "flatpak",
        "run",
        f"--filesystem={root}",
    ]
    if launch["network"] == "isolated":
        command.append("--unshare=network")
    command.extend(
        [
            "--command=bottles-cli",
            flatpak_app,
            "run",
            "-b",
            bottle_name,
            "-e",
            str(executable),
        ]
    )
    arguments = tuple(launch.get("arguments", []))
    if arguments:
        command.append("--")
        command.extend(arguments)

    display_command = [
        "flatpak",
        "run",
        "--filesystem=<MATERIALIZATION_ROOT>",
    ]
    if launch["network"] == "isolated":
        display_command.append("--unshare=network")
    display_command.extend(
        [
            "--command=bottles-cli",
            flatpak_app,
            "run",
            "-b",
            bottle_name,
            "-e",
            f"<MATERIALIZATION_ROOT>/{launch['entrypoint']}",
        ]
    )
    if arguments:
        display_command.append("--")
        display_command.extend(arguments)

    plan = BottlesLaunchPlan(
        schema=0,
        deployment_id=receipt["deployment_id"],
        capsule_id=receipt["capsule_id"],
        profile_id=receipt["profile_id"],
        bottle_name=bottle_name,
        entrypoint=launch["entrypoint"],
        network=launch["network"],
        flatpak_app=flatpak_app,
        command=tuple(display_command),
    )
    return plan, tuple(command)


def run_external_bottles_deployment(
    *,
    destination: Path,
    bottles_path: Path | None = None,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> tuple[BottlesLaunchPlan, int]:
    plan, command = build_external_bottles_launch_plan(
        destination=destination,
        bottles_path=bottles_path,
        flatpak_app=flatpak_app,
    )
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        raise BottlesAdapterError(
            "flatpak executable was not found."
        ) from exc
    except OSError as exc:
        raise BottlesAdapterError(
            f"Cannot launch Bottles Flatpak: {exc}"
        ) from exc
    return plan, int(completed.returncode)


def _deployment_target(
    *,
    bottles_path: Path,
    bottle_name: str,
) -> tuple[Path, str]:
    bottle_name = _validate_bottle_name(bottle_name)
    bottles_path = require_bottles_managed_path(bottles_path)
    target = bottles_path / bottle_name

    if target.is_symlink():
        raise BottlesAdapterError(
            "Bottles deployment target must not be a symlink."
        )
    if not target.is_dir():
        raise BottlesAdapterError(
            f"Bottles deployment does not exist: {bottle_name}"
        )
    return target, bottle_name


def _load_deployment_receipt(
    *,
    target: Path,
    bottle_name: str,
) -> dict[str, Any]:
    receipt = _load_json_regular(
        target / DEPLOYMENT_RECEIPT_NAME,
        "Bottles deployment receipt",
    )
    required_strings = (
        "deployment_id",
        "capsule_id",
        "profile_id",
        "source_object_id",
        "runner",
    )
    for field in required_strings:
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise BottlesAdapterError(
                f"Bottles deployment receipt {field} is invalid."
            )

    if receipt.get("schema") != 0:
        raise BottlesAdapterError(
            "Unsupported Bottles deployment receipt schema."
        )
    if receipt.get("adapter") != "bottles-flatpak":
        raise BottlesAdapterError(
            "Deployment receipt is not a Bottles Flatpak deployment."
        )
    if receipt.get("destination") != ".":
        raise BottlesAdapterError(
            "Deployment receipt is not anchored to its own directory."
        )
    if receipt.get("bottle_name") != bottle_name:
        raise BottlesAdapterError(
            "Deployment receipt bottle_name does not match directory."
        )

    launch = receipt.get("launch")
    if not isinstance(launch, dict):
        raise BottlesAdapterError(
            "Deployment receipt launch must be an object."
        )
    _safe_relative_path(
        launch.get("entrypoint"),
        "deployment.launch.entrypoint",
    )
    arguments = launch.get("arguments")
    if not isinstance(arguments, list) or not all(
        isinstance(item, str) for item in arguments
    ):
        raise BottlesAdapterError(
            "Deployment launch arguments must be strings."
        )
    if launch.get("network") not in {"isolated", "host_default"}:
        raise BottlesAdapterError(
            "Deployment launch network value is invalid."
        )

    state = receipt.get("persistent_state", [])
    if not isinstance(state, list) or not all(
        isinstance(item, dict) for item in state
    ):
        raise BottlesAdapterError(
            "Deployment persistent_state must be an array."
        )
    return receipt


def verify_bottles_deployment(
    *,
    bottles_path: Path,
    bottle_name: str,
) -> BottlesDeploymentVerification:
    """Verify the structural identity of one managed Bottles derivative."""

    target, bottle_name = _deployment_target(
        bottles_path=bottles_path,
        bottle_name=bottle_name,
    )
    receipt = _load_deployment_receipt(
        target=target,
        bottle_name=bottle_name,
    )
    try:
        verify_composition_state_evidence(
            root=target,
            evidence=receipt.get("state_restore"),
            capsule_id=receipt["capsule_id"],
        )
    except CompositionStateError as exc:
        raise BottlesAdapterError(
            f"Invalid state restoration evidence: {exc}"
        ) from exc

    fields = _read_top_level_bottle_fields(target / "bottle.yml")
    configuration_valid = (
        fields["Name"] == bottle_name
        and fields["Path"] == bottle_name
        and fields["Custom_Path"] is False
        and fields["Runner"] == receipt["runner"]
    )

    launch = receipt["launch"]
    entrypoint_path = _safe_relative_path(
        launch["entrypoint"],
        "deployment.launch.entrypoint",
    )
    entrypoint = target.joinpath(*entrypoint_path.parts)
    entrypoint_present = (
        not entrypoint.is_symlink() and entrypoint.is_file()
    )

    operational = receipt.get("operational_paths")
    operational_scripts_valid = isinstance(operational, dict)
    if operational_scripts_valid:
        for key in ("launcher", "verifier", "uninstaller", "portable_runtime"):
            value = operational.get(key)
            try:
                relative = _safe_relative_path(
                    value, f"deployment.operational_paths.{key}"
                )
            except BottlesAdapterError:
                operational_scripts_valid = False
                break
            path = target.joinpath(*relative.parts)
            if path.is_symlink() or not path.is_file():
                operational_scripts_valid = False
                break
            if key != "portable_runtime" and not os.access(path, os.X_OK):
                operational_scripts_valid = False
                break

    verified = (
        configuration_valid
        and entrypoint_present
        and operational_scripts_valid
    )
    return BottlesDeploymentVerification(
        schema=0,
        deployment_id=receipt["deployment_id"],
        capsule_id=receipt["capsule_id"],
        profile_id=receipt["profile_id"],
        bottle_name=bottle_name,
        runner=receipt["runner"],
        entrypoint=launch["entrypoint"],
        network=launch["network"],
        receipt_valid=True,
        configuration_valid=configuration_valid,
        entrypoint_present=entrypoint_present,
        operational_scripts_valid=operational_scripts_valid,
        verified=verified,
    )


def build_bottles_launch_plan(
    *,
    bottles_path: Path,
    bottle_name: str,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> tuple[BottlesLaunchPlan, tuple[str, ...]]:
    """Build a sanitized plan and the exact host command."""

    if not isinstance(flatpak_app, str) or not flatpak_app:
        raise BottlesAdapterError(
            "Flatpak application ID must be a non-empty string."
        )

    target, bottle_name = _deployment_target(
        bottles_path=bottles_path,
        bottle_name=bottle_name,
    )
    receipt = _load_deployment_receipt(
        target=target,
        bottle_name=bottle_name,
    )
    verification = verify_bottles_deployment(
        bottles_path=target.parent,
        bottle_name=bottle_name,
    )
    if not verification.verified:
        raise BottlesAdapterError(
            "Bottles deployment is not structurally verified."
        )

    launch = receipt["launch"]
    entrypoint_path = _safe_relative_path(
        launch["entrypoint"],
        "deployment.launch.entrypoint",
    )
    executable = target.joinpath(*entrypoint_path.parts)

    command: list[str] = ["flatpak", "run"]
    if launch["network"] == "isolated":
        command.append("--unshare=network")
    command.extend(
        [
            "--command=bottles-cli",
            flatpak_app,
            "run",
            "-b",
            bottle_name,
            "-e",
            str(executable),
        ]
    )
    arguments = tuple(launch.get("arguments", []))
    if arguments:
        command.append("--")
        command.extend(arguments)

    display_command: list[str] = ["flatpak", "run"]
    if launch["network"] == "isolated":
        display_command.append("--unshare=network")
    display_command.extend(
        [
            "--command=bottles-cli",
            flatpak_app,
            "run",
            "-b",
            bottle_name,
            "-e",
            f"<BOTTLES_PATH>/{bottle_name}/{launch['entrypoint']}",
        ]
    )
    if arguments:
        display_command.append("--")
        display_command.extend(arguments)

    plan = BottlesLaunchPlan(
        schema=0,
        deployment_id=receipt["deployment_id"],
        capsule_id=receipt["capsule_id"],
        profile_id=receipt["profile_id"],
        bottle_name=bottle_name,
        entrypoint=launch["entrypoint"],
        network=launch["network"],
        flatpak_app=flatpak_app,
        command=tuple(display_command),
    )
    return plan, tuple(command)


def run_bottles_deployment(
    *,
    bottles_path: Path,
    bottle_name: str,
    flatpak_app: str = DEFAULT_FLATPAK_APP,
) -> tuple[BottlesLaunchPlan, int]:
    """Run one verified deployment through Bottles Flatpak."""

    plan, command = build_bottles_launch_plan(
        bottles_path=bottles_path,
        bottle_name=bottle_name,
        flatpak_app=flatpak_app,
    )
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        raise BottlesAdapterError(
            "flatpak executable was not found."
        ) from exc
    except OSError as exc:
        raise BottlesAdapterError(
            f"Cannot launch Bottles Flatpak: {exc}"
        ) from exc
    return plan, int(completed.returncode)


def remove_bottles_deployment(
    *,
    bottles_path: Path,
    bottle_name: str,
    confirm_state_preserved: bool,
    confirm_stopped: bool,
) -> BottlesRemovalResult:
    """Atomically detach and remove one recognized mutable deployment."""

    target, bottle_name = _deployment_target(
        bottles_path=bottles_path,
        bottle_name=bottle_name,
    )
    receipt = _load_deployment_receipt(
        target=target,
        bottle_name=bottle_name,
    )

    if not confirm_stopped:
        raise BottlesAdapterError(
            "Removal requires confirmation that Bottles and all "
            "processes using this deployment are stopped."
        )

    persistent_state = receipt.get("persistent_state", [])
    must_preserve = [
        item
        for item in persistent_state
        if item.get("preserve_on_remove") is True
    ]
    if must_preserve and not confirm_state_preserved:
        raise BottlesAdapterError(
            "Persistent state must be preserved before deployment removal."
        )

    lock, descriptor = _acquire_lock(bottles_path, bottle_name)
    detached = bottles_path / (
        f".ogv-remove-bottles-{bottle_name}-"
        f"{os.getpid()}-{secrets.token_hex(8)}"
    )
    renamed = False

    try:
        _rename_noreplace(target, detached)
        renamed = True
        try:
            shutil.rmtree(detached)
        except OSError as exc:
            raise BottlesAdapterError(
                f"Deployment detached but removal failed: {exc}"
            ) from exc

        return BottlesRemovalResult(
            schema=0,
            deployment_id=receipt["deployment_id"],
            capsule_id=receipt["capsule_id"],
            profile_id=receipt["profile_id"],
            bottle_name=bottle_name,
            removed=True,
            persistent_state_declared=len(persistent_state),
            state_preservation_confirmed=confirm_state_preserved,
            stopped_confirmed=confirm_stopped,
        )
    finally:
        if renamed and detached.exists():
            try:
                _rename_noreplace(detached, target)
            except BottlesAdapterError:
                pass
        _release_lock(lock, descriptor)
