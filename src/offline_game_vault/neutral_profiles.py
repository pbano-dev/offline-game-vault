from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid
from typing import Any

from .preserved_runners import RunnerRecord


class NeutralProfileError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NeutralContract:
    profile_id: str
    adapter: str
    document: dict[str, Any]
    path: Path


def _safe_relative(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise NeutralProfileError(f"{label} is not a portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise NeutralProfileError(f"{label} is not a safe relative path")
    return path


def _materialized_object_base(
    root: Path,
    document: dict[str, Any],
) -> tuple[Path, bool]:
    """Resolve the extracted source object inside an OGV materialization.

    OfflineGameVault materializes each dependency below
    ``objects/<object-id>``. Early synthetic fixtures placed ``neutral-object``
    directly at the materialization root, so that layout remains a
    compatibility fallback.
    """
    raw = document.get("source_object")
    if isinstance(raw, str) and raw:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", raw):
            raise NeutralProfileError("source_object is not a portable identifier")
        candidate = root / "objects" / raw
        if candidate.is_symlink():
            raise NeutralProfileError("source_object is a symbolic link")
        if candidate.is_dir():
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise NeutralProfileError(
                    "source_object is outside the materialization"
                ) from exc
            return resolved, True
    return root, False


def resolve_neutral_materialized_paths(
    *,
    materialization: Path,
    document: dict[str, Any],
    require_prefix: bool = True,
) -> tuple[Path, Path | None, Path]:
    """Resolve neutral, prefix and game paths from a materialized dependency."""
    root = Path(materialization).resolve(strict=True)
    base, object_scoped = _materialized_object_base(root, document)

    neutral_root = _safe_relative(
        document.get("neutral_root", "neutral-object"),
        "neutral_root",
    )
    prefix_source = (
        _safe_relative(document.get("prefix_source"), "prefix_source")
        if require_prefix
        else None
    )
    game_source = _safe_relative(document.get("game_source"), "game_source")

    neutral = base.joinpath(*neutral_root.parts)
    prefix = (
        base.joinpath(*prefix_source.parts)
        if prefix_source is not None
        else None
    )
    game = base.joinpath(*game_source.parts)

    checks: list[tuple[Path, str]] = [(neutral, "neutral_root"), (game, "game")]
    if prefix is not None:
        checks.insert(1, (prefix, "prefix"))

    for source, label in checks:
        if source.is_symlink() or not source.is_dir():
            scope = "source_object" if object_scoped else "materialization"
            raise NeutralProfileError(
                f"{label} does not exist inside {scope}"
            )
        try:
            source.resolve(strict=True).relative_to(base)
        except ValueError as exc:
            raise NeutralProfileError(
                f"{label} is outside the materialized object"
            ) from exc
    return neutral, prefix, game


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise NeutralProfileError(f"{label} is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NeutralProfileError(f"{label} does not contain valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise NeutralProfileError(f"{label} does not contain an object")
    return value


def load_neutral_contract(
    capsule_path: Path,
    profile_id: str,
    *,
    expected_contract: str | None = None,
) -> NeutralContract | None:
    capsule_path = Path(capsule_path)
    capsule = _load_json(capsule_path, "capsule.json")
    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise NeutralProfileError("capsule.profiles is not an array")
    matches = [
        item for item in profiles
        if isinstance(item, dict) and item.get("id") == profile_id
    ]
    if len(matches) != 1:
        raise NeutralProfileError("the profile does not exist exactly once")
    profile = matches[0]
    raw = profile.get("host_contract")
    if not isinstance(raw, str) or not raw:
        return None
    relative = _safe_relative(raw, "profile.host_contract")
    capsule_dir = capsule_path.parent.resolve(strict=True)
    current = capsule_dir
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise NeutralProfileError("host_contract traverses a symbolic link")
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(capsule_dir)
    except ValueError as exc:
        raise NeutralProfileError("host_contract is outside the capsule") from exc
    document = _load_json(resolved, "host_contract")
    contract = document.get("contract")
    if expected_contract is not None and contract != expected_contract:
        return None
    return NeutralContract(
        profile_id=profile_id,
        adapter=str(profile.get("adapter", "")),
        document=document,
        path=resolved,
    )


def _copy_tree_contents(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise NeutralProfileError("prefix-template is not a regular directory")
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_symlink():
            target.symlink_to(os.readlink(child))
        elif child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        elif child.is_file():
            shutil.copy2(child, target, follow_symlinks=False)
        else:
            raise NeutralProfileError(f"unsupported type: {child}")


def _copy_game(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise NeutralProfileError("payload/game is not a regular directory")
    if destination.exists() or destination.is_symlink():
        raise NeutralProfileError("the game destination already exists in the prefix")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)


def _place_game(
    *,
    prefix: Path,
    game: Path,
    destination: Path,
    game_destination: PurePosixPath,
) -> bool:
    """Ensure the game payload sits at its declared place inside the bottle.

    Neutral objects keep a clean prefix and the game payload as disjoint
    subtrees, so the game is copied in. Historical full-prefix archives already
    contain the game inside the prefix; the tree copy above therefore already
    placed it, and copying again would both duplicate tens of gigabytes and
    collide with itself. Returns ``True`` when the payload was copied.
    """
    resolved_prefix = prefix.resolve(strict=True)
    resolved_game = game.resolve(strict=True)
    try:
        nested = resolved_game.relative_to(resolved_prefix)
    except ValueError:
        _copy_game(game, destination)
        return True

    if PurePosixPath(nested.as_posix()) != game_destination:
        raise NeutralProfileError(
            "the archived game lives inside the prefix at a different "
            "location than game_destination_in_prefix"
        )
    if destination.is_symlink() or not destination.is_dir():
        raise NeutralProfileError(
            "the archived prefix does not contain the game at "
            "game_destination_in_prefix"
        )
    return False


def _sanitize_bottle_yml(
    payload: str,
    runner_id: str,
    bottle_name: str,
) -> str:
    """Sanitize one source template and canonicalize required identity keys.

    The core Bottles adapter requires exactly one top-level ``Name``, ``Path``,
    ``Custom_Path`` and ``Runner`` entry.  Earlier code only rewrote Runner and
    could silently drop another required key when its source value contained a
    private host path.  This routine keeps unknown non-private settings, removes
    host-path-bearing lines, and emits each required key exactly once.
    """

    private = re.compile(
        r"(?i)(?:/home/|/var/home/|\\\\home\\\\|"
        r"\\\\var\\\\home\\\\|/run/user/)"
    )
    required = {
        "Name": json.dumps(bottle_name, ensure_ascii=False),
        "Path": json.dumps(bottle_name, ensure_ascii=False),
        "Custom_Path": "false",
        "Runner": runner_id,
    }
    order = ("Name", "Path", "Custom_Path", "Runner")
    written: set[str] = set()
    result: list[str] = []

    for raw in payload.splitlines():
        key: str | None = None
        if raw and not raw[0].isspace() and ":" in raw:
            candidate = raw.split(":", 1)[0]
            if candidate in required:
                key = candidate

        if key is not None:
            if key not in written:
                result.append(f"{key}: {required[key]}")
                written.add(key)
            continue

        if private.search(raw):
            # Source and cache paths tied to the host are not part of the
            # portable derivative.
            continue
        result.append(raw)

    for key in order:
        if key not in written:
            result.append(f"{key}: {required[key]}")

    return "\n".join(result).rstrip() + "\n"


def _generated_bottle_yml(
    *,
    bottle_name: str,
    runner_id: str,
    working_directory: str,
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    # Minimal, explicit candidate configuration. A source template is preferred.
    document = [
        "Arch: win64",
        f"Creation_Date: '{now}'",
        "Custom_Path: false",
        "DLL_Overrides: {}",
        "Environment: Custom",
        "External_Programs: {}",
        "Installed_Dependencies: []",
        "Language: sys",
        f"Name: {json.dumps(bottle_name)}",
        f"Path: {json.dumps(bottle_name)}",
        "Parameters: {}",
        f"Runner: {runner_id}",
        "Sandbox: false",
        "State: 0",
        f"Update_Date: '{now}'",
        "Versioning: false",
        "Windows: win10",
        f"WorkingDir: {json.dumps(working_directory)}",
    ]
    return "\n".join(document) + "\n"


def materialize_neutral_bottle_source(
    *,
    materialization: Path,
    capsule_path: Path,
    profile_id: str,
    runner: RunnerRecord,
    bottle_name: str,
) -> dict[str, Any] | None:
    """Convert only the neutral *object* into a Bottles source derivative.

    OfflineGameVault materializations are wrappers whose root contains
    ``materialization-receipt.json`` and ``objects/<object-id>``.  The core
    ``deploy-bottles`` command validates that wrapper before it reads the
    prefix object.  Therefore the conversion must replace the extracted
    source object, not the materialization wrapper itself.

    A direct-root compatibility fallback is retained for old synthetic
    fixtures, but real object-scoped materializations preserve their receipt,
    other dependencies (for example the runner), and ``objects/``.
    """

    contract = load_neutral_contract(
        capsule_path,
        profile_id,
        expected_contract="ogv-bottles-neutral-v1",
    )
    if contract is None:
        return None
    document = contract.document
    game_destination = _safe_relative(
        document.get("game_destination_in_prefix"),
        "game_destination_in_prefix",
    )
    working_directory = str(
        document.get(
            "working_directory_in_prefix",
            game_destination.as_posix(),
        )
    )

    root = Path(materialization)
    if root.is_symlink() or not root.is_dir():
        raise NeutralProfileError(
            "the neutral materialization is not a regular directory"
        )
    root = root.resolve(strict=True)
    base, object_scoped = _materialized_object_base(root, document)

    # Idempotent retry after conversion but before/after a failed deployment.
    existing_receipt_path = base / ".ogv-neutral-source.json"
    existing_bottle_yml = base / "bottle.yml"
    neutral_rel = _safe_relative(
        document.get("neutral_root", "neutral-object"),
        "neutral_root",
    )
    if (
        existing_receipt_path.is_file()
        and not existing_receipt_path.is_symlink()
        and existing_bottle_yml.is_file()
        and not existing_bottle_yml.is_symlink()
        and not base.joinpath(*neutral_rel.parts).exists()
    ):
        existing = _load_json(
            existing_receipt_path,
            "neutral Bottles source receipt",
        )
        if (
            existing.get("contract") != "ogv-neutral-bottles-source-v1"
            or existing.get("profile_id") != profile_id
            or existing.get("source_contract_sha256")
            != hashlib.sha256(contract.path.read_bytes()).hexdigest()
        ):
            raise NeutralProfileError(
                "the existing derived Bottles source does not match "
                "the current contract"
            )
        return {**existing, "reused": True}

    neutral, prefix, game = resolve_neutral_materialized_paths(
        materialization=root,
        document=document,
        require_prefix=True,
    )
    assert prefix is not None

    # Only old direct-root fixtures need their root receipts copied into the
    # replacement.  In a real materialization those receipts stay at wrapper
    # level and must never be moved below the source object.
    receipt_files: dict[str, bytes] = {}
    if not object_scoped:
        for name in (
            "materialization-receipt.json",
            ".ogv-selection.json",
            ".ogv-materialization.json",
        ):
            candidate = root / name
            if candidate.is_file() and not candidate.is_symlink():
                receipt_files[name] = candidate.read_bytes()

    target = base if object_scoped else root
    staging = target.parent / (
        f".{target.name}.bottle-{uuid.uuid4().hex}"
    )
    if staging.exists() or staging.is_symlink():
        raise NeutralProfileError("Bottles staging collision")
    staging.mkdir(mode=0o700)
    try:
        _copy_tree_contents(prefix, staging)
        _place_game(
            prefix=prefix,
            game=game,
            destination=staging.joinpath(*game_destination.parts),
            game_destination=game_destination,
        )

        template_raw = document.get("bottle_yml_template")
        template_payload: str | None = None
        if isinstance(template_raw, str) and template_raw:
            template_rel = _safe_relative(
                template_raw,
                "bottle_yml_template",
            )
            template = capsule_path.parent.joinpath(
                *template_rel.parts
            )
            if template.is_file() and not template.is_symlink():
                template_payload = template.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
        if template_payload is not None:
            bottle_yml = _sanitize_bottle_yml(
                template_payload,
                runner.runner_id,
                bottle_name,
            )
            template_used = True
        else:
            bottle_yml = _generated_bottle_yml(
                bottle_name=bottle_name,
                runner_id=runner.runner_id,
                working_directory=working_directory,
            )
            template_used = False

        (staging / "bottle.yml").write_text(
            bottle_yml,
            encoding="utf-8",
        )
        for name, payload in receipt_files.items():
            (staging / name).write_bytes(payload)

        source_object = document.get("source_object")
        receipt = {
            "schema": 0,
            "contract": "ogv-neutral-bottles-source-v1",
            "profile_id": profile_id,
            "runner_id": runner.runner_id,
            "bottle_name": bottle_name,
            "game_destination_in_prefix":
                game_destination.as_posix(),
            "template_used": template_used,
            "object_scoped": object_scoped,
            "source_object": (
                source_object
                if isinstance(source_object, str)
                else None
            ),
            "source_contract_sha256": hashlib.sha256(
                contract.path.read_bytes()
            ).hexdigest(),
        }
        (staging / ".ogv-neutral-source.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        old = target.parent / (
            f".{target.name}.neutral-old-{uuid.uuid4().hex}"
        )
        os.replace(target, old)
        try:
            os.replace(staging, target)
        except Exception:
            os.replace(old, target)
            raise

        try:
            shutil.rmtree(old)
        except OSError as exc:
            raise NeutralProfileError(
                "the new Bottles source was published, but the previous "
                f"neutral object could not be removed: {old}: {exc}"
            ) from exc
        return receipt
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_neutral_bottles_source(
    *,
    materialization: Path,
    capsule_path: Path,
    profile_id: str,
) -> dict[str, Any]:
    """Validate the derived source against core ``deploy-bottles`` inputs.

    This deliberately mirrors the load-bearing preconditions of the core:
    wrapper receipt at the materialization root, object-scoped source,
    verified extracted object declaration, one regular ``bottle.yml`` and the
    profile entrypoint inside that bottle.
    """

    contract = load_neutral_contract(
        capsule_path,
        profile_id,
        expected_contract="ogv-bottles-neutral-v1",
    )
    if contract is None:
        raise NeutralProfileError(
            "the profile does not declare a neutral Bottles contract"
        )
    document = contract.document
    source_object = document.get("source_object")
    if (
        not isinstance(source_object, str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*",
            source_object,
        )
    ):
        raise NeutralProfileError(
            "the neutral Bottles contract does not declare a portable source_object"
        )

    root = Path(materialization)
    if root.is_symlink() or not root.is_dir():
        raise NeutralProfileError(
            "the Bottles source is not a regular materialization"
        )
    root = root.resolve(strict=True)
    base, object_scoped = _materialized_object_base(root, document)
    if not object_scoped:
        raise NeutralProfileError(
            "the Bottles source lost the objects/<source_object> wrapper"
        )

    capsule = _load_json(capsule_path, "capsule.json")
    capsule_id = capsule.get("capsule_id")
    receipt = _load_json(
        root / "materialization-receipt.json",
        "materialization-receipt.json",
    )
    if (
        receipt.get("destination") != "."
        or receipt.get("capsule_id") != capsule_id
        or receipt.get("profile_id") != profile_id
    ):
        raise NeutralProfileError(
            "the root receipt does not match the capsule and profile"
        )

    receipt_objects = receipt.get("objects")
    if not isinstance(receipt_objects, list):
        raise NeutralProfileError(
            "the root receipt does not declare objects[]"
        )
    matches = [
        item
        for item in receipt_objects
        if isinstance(item, dict)
        and item.get("id") == source_object
    ]
    if len(matches) != 1:
        raise NeutralProfileError(
            "the receipt does not contain exactly the source_object"
        )
    item = matches[0]
    if (
        item.get("destination") != f"objects/{source_object}"
        or item.get("strategy") != "extract"
        or item.get("verified") is not True
    ):
        raise NeutralProfileError(
            "source_object is not listed as a verified extraction"
        )

    bottle_yml = base / "bottle.yml"
    if bottle_yml.is_symlink() or not bottle_yml.is_file():
        raise NeutralProfileError(
            "source_object does not contain a regular bottle.yml"
        )
    try:
        lines = bottle_yml.read_text(
            encoding="utf-8",
            errors="strict",
        ).splitlines()
    except (OSError, UnicodeError) as exc:
        raise NeutralProfileError(
            f"bottle.yml is unreadable: {exc}"
        ) from exc
    required_counts = {
        key: sum(
            1
            for line in lines
            if line
            and not line[0].isspace()
            and line.split(":", 1)[0] == key
            and ":" in line
        )
        for key in ("Name", "Path", "Custom_Path", "Runner")
    }
    invalid = {
        key: count
        for key, count in required_counts.items()
        if count != 1
    }
    if invalid:
        raise NeutralProfileError(
            "bottle.yml does not contain each required key exactly once: "
            f"{invalid}"
        )

    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise NeutralProfileError("capsule.profiles is not an array")
    profile_matches = [
        entry
        for entry in profiles
        if isinstance(entry, dict)
        and entry.get("id") == profile_id
    ]
    if len(profile_matches) != 1:
        raise NeutralProfileError(
            "the profile does not exist exactly once"
        )
    launch = profile_matches[0].get("launch")
    if not isinstance(launch, dict):
        raise NeutralProfileError(
            "the profile does not declare launch"
        )
    entrypoint = _safe_relative(
        launch.get("entrypoint"),
        "profile.launch.entrypoint",
    )
    executable = base.joinpath(*entrypoint.parts)
    if executable.is_symlink() or not executable.is_file():
        raise NeutralProfileError(
            "the entrypoint does not exist inside the derived bottle"
        )
    try:
        executable.resolve(strict=True).relative_to(base)
    except ValueError as exc:
        raise NeutralProfileError(
            "the entrypoint escapes source_object"
        ) from exc

    removal = receipt.get("removal")
    safe_values = (
        removal.get("safe_to_remove")
        if isinstance(removal, dict)
        else None
    )
    if not isinstance(safe_values, list):
        raise NeutralProfileError(
            "the receipt does not declare removal.safe_to_remove"
        )
    allowed: set[str] = set()
    for raw in safe_values:
        relative = _safe_relative(
            raw,
            "removal.safe_to_remove",
        )
        allowed.add(relative.parts[0])
    actual = {entry.name for entry in root.iterdir()}
    unexpected = sorted(actual - allowed)
    if unexpected:
        raise NeutralProfileError(
            "the transient source contains root paths that would prevent "
            "remove-materialization: "
            + ", ".join(unexpected)
        )

    return {
        "schema": 0,
        "source_object": source_object,
        "object_root": f"objects/{source_object}",
        "entrypoint": entrypoint.as_posix(),
        "bottle_yml": f"objects/{source_object}/bottle.yml",
        "wrapper_preserved": True,
    }
