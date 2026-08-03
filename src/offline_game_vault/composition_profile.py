from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import stat
from typing import Any

from . import __version__
from .preserved_runners import RunnerRecord


class RunnerOverrideError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OverlayFile:
    relative_path: str
    payload: bytes
    mode: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DerivedCapsule:
    changed: bool
    document: dict[str, Any]
    serialized: bytes
    original_runner_id: str
    selected_runner_id: str
    companion_files: tuple[OverlayFile, ...] = ()


def _load_capsule(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RunnerOverrideError("capsule.json must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerOverrideError(f"capsule.json is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerOverrideError("capsule.json does not contain an object")
    return value


def _selected_profile(
    document: dict[str, Any],
    profile_id: str,
) -> dict[str, Any]:
    profiles = document.get("profiles")
    if not isinstance(profiles, list):
        raise RunnerOverrideError("capsule.profiles is not an array")
    matches = [
        item
        for item in profiles
        if isinstance(item, dict) and item.get("id") == profile_id
    ]
    if len(matches) != 1:
        raise RunnerOverrideError(
            "The profile must exist exactly once"
        )
    return matches[0]


def _runner_declaration(
    document: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    objects = document.get("objects")
    dependencies = profile.get("dependencies")
    if not isinstance(objects, list) or not isinstance(dependencies, list):
        raise RunnerOverrideError("invalid objects or dependencies")

    object_index = {
        item.get("id"): item
        for item in objects
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    candidates = []
    for dependency in dependencies:
        declaration = object_index.get(dependency)
        roles = declaration.get("roles") if isinstance(declaration, dict) else None
        if isinstance(roles, list) and "runner" in roles:
            candidates.append(declaration)

    if len(candidates) != 1:
        raise RunnerOverrideError(
            "The Direct-Wine profile must depend on exactly one runner object"
        )
    return candidates[0]


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _safe_companion_path(value: Any, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise RunnerOverrideError(f"{field} must be a relative path")
    if "\x00" in value or "\\" in value:
        raise RunnerOverrideError(f"{field} is not a portable path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RunnerOverrideError(f"{field} is not a safe relative path")
    return path


def _remap_neutral_protected_files(
    value: list[Any],
    *,
    prefix_destination: str,
    game_destination_in_prefix: str,
    game_source_destination: str,
) -> list[dict[str, Any]]:
    """Map neutral protected files to real, non-symlinked derived paths.

    Files below ``prefix/<game_destination_in_prefix>`` are mapped to the
    materialized game source. Other files below ``prefix/`` remain mapped to
    the derived prefix root.
    """
    prefix_root = _safe_companion_path(
        prefix_destination,
        "derived Direct-Wine prefix destination",
    )
    game_prefix = _safe_companion_path(
        f"prefix/{game_destination_in_prefix}",
        "neutral Direct-Wine game destination",
    )
    game_root = _safe_companion_path(
        game_source_destination,
        "derived Direct-Wine game source destination",
    )
    neutral_prefix = PurePosixPath("prefix")
    result: list[dict[str, Any]] = []

    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RunnerOverrideError(
                "Every neutral protected-file declaration must be an object"
            )
        path = _safe_companion_path(
            item.get("path"),
            f"protected_files[{index}].path",
        )
        if path == neutral_prefix or not path.is_relative_to(neutral_prefix):
            raise RunnerOverrideError(
                "Neutral protected files must be declared below prefix/"
            )

        remapped = dict(item)
        if path.is_relative_to(game_prefix):
            relative = path.relative_to(game_prefix)
            remapped["path"] = game_root.joinpath(relative).as_posix()
        else:
            remapped["path"] = prefix_root.joinpath(
                *path.parts[1:]
            ).as_posix()
        result.append(remapped)

    return result


def _read_host_contract(
    capsule_path: Path,
    profile: dict[str, Any],
) -> OverlayFile:
    relative = _safe_companion_path(
        profile.get("host_contract"),
        "profile.host_contract",
    )
    capsule_directory = capsule_path.parent.resolve(strict=True)

    current = capsule_directory
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise RunnerOverrideError(
                f"The required host contract does not exist: {relative.as_posix()}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise RunnerOverrideError(
                f"The host contract must not traverse symbolic links: "
                f"{relative.as_posix()}"
            )

    source = current
    info_before = source.stat()
    if not stat.S_ISREG(info_before.st_mode):
        raise RunnerOverrideError(
            f"The host contract is not a regular file: "
            f"{relative.as_posix()}"
        )

    resolved = source.resolve(strict=True)
    try:
        resolved.relative_to(capsule_directory)
    except ValueError as exc:
        raise RunnerOverrideError(
            "The host contract is outside the capsule directory"
        ) from exc

    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise RunnerOverrideError(
            f"Could not read the host contract: {exc}"
        ) from exc

    info_after = source.stat()
    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns"):
        if getattr(info_before, field) != getattr(info_after, field):
            raise RunnerOverrideError(
                "The host contract changed while being read"
            )

    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerOverrideError(
            "The host contract does not contain valid UTF-8 JSON"
        ) from exc
    if not isinstance(document, dict):
        raise RunnerOverrideError(
            "The host contract does not contain a JSON object"
        )

    return OverlayFile(
        relative_path=relative.as_posix(),
        payload=payload,
        mode=0o600,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


_NEUTRAL_CONTRACTS = {
    "ogv-bottles-neutral-v1",
    "ogv-direct-wine-neutral-v1",
    "ogv-umu-neutral-v1",
}


def _protected_from_evidence(capsule_path: Path) -> list[Any]:
    evidence = capsule_path.parent / "evidence/protected-files.json"
    if evidence.is_symlink() or not evidence.is_file():
        return []
    try:
        document = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerOverrideError(
            f"Protected-file evidence is invalid: {exc}"
        ) from exc
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise RunnerOverrideError(
            "Protected-file evidence does not contain items[]"
        )
    return items


def _normalized_direct_wine_contract(
    *,
    capsule_path: Path,
    neutral: dict[str, Any],
    protected: list[Any],
) -> OverlayFile:
    required = (
        "source_object",
        "neutral_root",
        "prefix_source",
        "game_source",
        "game_destination_in_prefix",
        "entrypoint_relative_to_game",
        "working_directory_in_prefix",
    )
    document: dict[str, Any] = {
        key: neutral.get(key)
        for key in required
    }
    document.update(
        {
            "schema": 0,
            "contract": "ogv-direct-wine-neutral-v1",
            "baseline_state": neutral.get(
                "baseline_state", "embedded-or-unknown"
            ),
            "runner_binding": "selected-composition",
            "preferred_runner": None,
            "runtime_directory": str(
                neutral.get("runtime_directory", "runtime")
            ),
            "launcher": str(neutral.get("launcher", "JUGAR.sh")),
            "uninstaller": str(
                neutral.get("uninstaller", "RETIRAR.sh")
            ),
            "protected_files": protected,
            "network": (
                str(neutral.get("network"))
                if neutral.get("network") in {"allowed", "host_default"}
                else "host_default"
            ),
            "source_network": str(
                neutral.get("network", "host_default")
            ),
        }
    )
    payload = _canonical_bytes(document)
    relative = "host-contracts/composition-direct-wine-source.json"
    return OverlayFile(
        relative_path=relative,
        payload=payload,
        mode=0o600,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def build_derived_capsule(
    capsule_path: Path,
    profile_id: str,
    runner: RunnerRecord,
) -> DerivedCapsule:
    original = _load_capsule(Path(capsule_path))
    profile = _selected_profile(original, profile_id)

    if profile.get("platform") != "linux":
        raise RunnerOverrideError(
            "Runner selection applies only to Linux profiles"
        )

    playable = profile.get("playable")
    playable_is_wine = (
        profile.get("adapter") == "wine"
        and isinstance(playable, dict)
        and playable.get("schema") == 0
        and playable.get("backend") == "wine"
    )
    if not playable_is_wine:
        host_contract = _read_host_contract(Path(capsule_path), profile)
        try:
            neutral = json.loads(host_contract.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunnerOverrideError(
                "The neutral host contract does not contain valid JSON"
            ) from exc
        if (
            not isinstance(neutral, dict)
            or neutral.get("contract") not in _NEUTRAL_CONTRACTS
        ):
            raise RunnerOverrideError(
                "The profile has no compatible neutral Linux contract"
            )

        source_object = neutral.get("source_object")
        game_destination = neutral.get("game_destination_in_prefix")
        entrypoint = neutral.get("entrypoint_relative_to_game")
        working_directory = neutral.get("working_directory_in_prefix")
        protected = neutral.get("protected_files", [])
        if not protected:
            protected = _protected_from_evidence(Path(capsule_path))
        if not all(
            isinstance(value, str) and value
            for value in (
                source_object,
                game_destination,
                entrypoint,
                working_directory,
            )
        ):
            raise RunnerOverrideError(
                "The neutral Direct-Wine contract is incomplete"
            )
        if not isinstance(protected, list) or not protected:
            raise RunnerOverrideError(
                "The neutral source has no protected-file evidence"
            )

        normalized_contract = _normalized_direct_wine_contract(
            capsule_path=Path(capsule_path),
            neutral=neutral,
            protected=protected,
        )

        derived = deepcopy(original)
        derived_profile = _selected_profile(derived, profile_id)
        derived_profile["platform"] = "linux"
        derived_profile["adapter"] = "wine"
        derived_profile["host_contract"] = (
            normalized_contract.relative_path
        )
        objects = derived.get("objects")
        dependencies = derived_profile.get("dependencies")
        if not isinstance(objects, list) or not isinstance(dependencies, list):
            raise RunnerOverrideError("invalid objects or dependencies")

        declarations = {
            item.get("id"): item
            for item in objects
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        game_declaration = declarations.get(source_object)
        if not isinstance(game_declaration, dict):
            raise RunnerOverrideError(
                "The neutral contract source_object does not exist"
            )
        old_runner_ids = {
            object_id
            for object_id, declaration in declarations.items()
            if isinstance(declaration.get("roles"), list)
            and "runner" in declaration["roles"]
        }
        existing_selected = declarations.get(runner.runner_id)
        selected_declaration = {
            "archive_path": runner.archive_path,
            "description": (
                f"Runner selected by Offline Game Vault core {__version__} "
                "for the requested composition."
            ),
            "digest": runner.digest,
            "format": runner.format,
            "id": runner.runner_id,
            "required": True,
            "roles": ["runner"],
            "shared": True,
            "size": runner.size,
        }
        if existing_selected is None:
            objects.append(selected_declaration)
        else:
            roles = existing_selected.get("roles")
            if (
                existing_selected.get("digest") != runner.digest
                or existing_selected.get("archive_path") != runner.archive_path
                or not isinstance(roles, list)
                or "runner" not in roles
            ):
                raise RunnerOverrideError(
                    "The selected runner identifier collides with another object"
                )

        derived_profile["dependencies"] = [
            value
            for value in dependencies
            if value not in old_runner_ids
        ]
        if source_object not in derived_profile["dependencies"]:
            derived_profile["dependencies"].append(source_object)
        derived_profile["dependencies"].append(runner.runner_id)
        if len(derived_profile["dependencies"]) != len(
            set(derived_profile["dependencies"])
        ):
            raise RunnerOverrideError(
                "The derived contract would duplicate dependencies"
            )

        runner_destination = f"runner/{runner.runner_id}"
        neutral_destination = "source"
        prefix_destination = f"{neutral_destination}/payload/prefix-template"
        game_destination_path = (
            f"{prefix_destination}/{game_destination}"
        )
        game_source_path = f"{neutral_destination}/payload/game"
        game_link_target = posixpath.relpath(
            game_source_path,
            posixpath.dirname(game_destination_path),
        )
        remapped_protected = _remap_neutral_protected_files(
            protected,
            prefix_destination=prefix_destination,
            game_destination_in_prefix=game_destination,
            game_source_destination=game_source_path,
        )
        derived_profile["playable"] = {
            "schema": 0,
            "backend": "wine",
            "layout": [
                {
                    "object": source_object,
                    "source": "neutral-object",
                    "destination": neutral_destination,
                },
                {
                    "object": runner.runner_id,
                    "source": runner.source_root,
                    "destination": runner_destination,
                },
            ],
            "paths": {
                "prefix": prefix_destination,
                "runner": runner_destination,
                "wine": f"{runner_destination}/{runner.wine_path}",
                "wineserver": (
                    f"{runner_destination}/{runner.wineserver_path}"
                ),
                "runtime": str(neutral.get("runtime_directory", "runtime")),
                "launcher": str(neutral.get("launcher", "PLAY.sh")),
                "uninstaller": str(neutral.get("uninstaller", "REMOVE.sh")),
            },
            "prefix_operations": [
                {
                    "type": "symlink",
                    "path": game_destination_path,
                    "target": game_link_target,
                }
            ],
            "protected_files": remapped_protected,
        }
        launch = derived_profile.setdefault("launch", {})
        working_path = PurePosixPath(working_directory)
        game_destination_relative = PurePosixPath(game_destination)
        if working_path == game_destination_relative:
            derived_working_directory = game_source_path
        elif working_path.is_relative_to(game_destination_relative):
            derived_working_directory = PurePosixPath(game_source_path).joinpath(
                working_path.relative_to(game_destination_relative)
            ).as_posix()
        else:
            derived_working_directory = PurePosixPath(prefix_destination).joinpath(
                working_path
            ).as_posix()

        source_network = neutral.get("network", "host_default")
        direct_wine_network = (
            source_network
            if source_network in {"allowed", "host_default"}
            else "host_default"
        )
        launch.update({
            "entrypoint": f"{game_source_path}/{entrypoint}",
            "working_directory": derived_working_directory,
            "arguments": launch.get("arguments", []),
            "environment": launch.get("environment", {}),
            "network": direct_wine_network,
        })
        derived_profile["notes"] = (
            f"Direct-Wine profile composed by Offline Game Vault core "
            f"{__version__} from a neutral object and runner "
            f"{runner.runner_id!r}."
        )
        original_runner = (
            sorted(old_runner_ids)[0] if old_runner_ids else "<unbound>"
        )
        return DerivedCapsule(
            changed=True,
            document=derived,
            serialized=_canonical_bytes(derived),
            original_runner_id=original_runner,
            selected_runner_id=runner.runner_id,
            companion_files=(normalized_contract,),
        )

    current_runner = _runner_declaration(original, profile)
    current_id = current_runner.get("id")
    current_digest = current_runner.get("digest")
    if not isinstance(current_id, str) or not current_id:
        raise RunnerOverrideError("The original runner has no identifier")
    if not isinstance(current_digest, str) or not current_digest:
        raise RunnerOverrideError("The original runner has no digest")

    paths = playable.get("paths")
    layout = playable.get("layout")
    dependencies = profile.get("dependencies")
    if (
        not isinstance(paths, dict)
        or not isinstance(layout, list)
        or not isinstance(dependencies, list)
    ):
        raise RunnerOverrideError("Incomplete Direct-Wine contract")

    mappings = [
        item
        for item in layout
        if isinstance(item, dict) and item.get("object") == current_id
    ]
    if len(mappings) != 1:
        raise RunnerOverrideError(
            "playable.layout does not map exactly the original runner"
        )

    destination = f"runner/{runner.runner_id}"
    expected_paths = {
        "runner": destination,
        "wine": f"{destination}/{runner.wine_path}",
        "wineserver": f"{destination}/{runner.wineserver_path}",
    }

    unchanged = (
        current_id == runner.runner_id
        and current_digest == runner.digest
        and current_runner.get("archive_path") == runner.archive_path
        and current_runner.get("format") == runner.format
        and current_runner.get("size") in {None, runner.size}
        and mappings[0].get("source") == runner.source_root
        and mappings[0].get("destination") == destination
        and all(paths.get(key) == value for key, value in expected_paths.items())
    )
    if unchanged:
        return DerivedCapsule(
            changed=False,
            document=original,
            serialized=_canonical_bytes(original),
            original_runner_id=current_id,
            selected_runner_id=runner.runner_id,
            companion_files=(),
        )

    derived = deepcopy(original)
    derived_profile = _selected_profile(derived, profile_id)
    derived_playable = derived_profile["playable"]
    derived_paths = derived_playable["paths"]
    derived_layout = derived_playable["layout"]

    objects = derived.get("objects")
    assert isinstance(objects, list)
    existing = [
        item
        for item in objects
        if isinstance(item, dict) and item.get("id") == runner.runner_id
    ]
    selected_declaration = {
        "archive_path": runner.archive_path,
        "description": (
            f"Core-selected shared runner {runner.runner_id} "
            "for the requested composition."
        ),
        "digest": runner.digest,
        "format": runner.format,
        "id": runner.runner_id,
        "required": True,
        "roles": ["runner"],
        "shared": True,
        "size": runner.size,
    }
    if existing:
        if len(existing) != 1:
            raise RunnerOverrideError(
                "The selected runner appears more than once in capsule.objects"
            )
        existing_item = existing[0]
        roles = existing_item.get("roles")
        if (
            existing_item.get("digest") != runner.digest
            or existing_item.get("archive_path") != runner.archive_path
            or existing_item.get("format") != runner.format
            or existing_item.get("size") not in {None, runner.size}
            or existing_item.get("required") is not True
            or existing_item.get("shared") is not True
            or not isinstance(roles, list)
            or "runner" not in roles
        ):
            raise RunnerOverrideError(
                "The selected runner identifier collides with another object"
            )
    else:
        objects.append(selected_declaration)

    derived_dependencies = derived_profile.get("dependencies")
    assert isinstance(derived_dependencies, list)
    replacements = 0
    for index, value in enumerate(derived_dependencies):
        if value == current_id:
            derived_dependencies[index] = runner.runner_id
            replacements += 1
    if replacements != 1:
        raise RunnerOverrideError(
            "Could not replace exactly one runner dependency"
        )
    if len(derived_dependencies) != len(set(derived_dependencies)):
        raise RunnerOverrideError(
            "Replacing the runner would duplicate dependencies"
        )

    derived_mappings = [
        item
        for item in derived_layout
        if isinstance(item, dict) and item.get("object") == current_id
    ]
    if len(derived_mappings) != 1:
        raise RunnerOverrideError(
            "Could not replace exactly one runner layout"
        )
    derived_mappings[0]["object"] = runner.runner_id
    derived_mappings[0]["source"] = runner.source_root
    derived_mappings[0]["destination"] = destination

    derived_paths.update(expected_paths)
    derived_profile["notes"] = (
        f"Profile composed by Offline Game Vault core {__version__} "
        f"from {profile_id!r}: original runner {current_id!r}, "
        f"selected runner {runner.runner_id!r}."
    )

    host_contract = _read_host_contract(
        Path(capsule_path),
        profile,
    )

    return DerivedCapsule(
        changed=True,
        document=derived,
        serialized=_canonical_bytes(derived),
        original_runner_id=current_id,
        selected_runner_id=runner.runner_id,
        companion_files=(host_contract,),
    )
