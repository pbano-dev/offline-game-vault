'''Composition, user-requested backend compositions.

Contracts and acceptance reports describe known-good combinations. They do not
authorize materialization. This module synthesizes a private operational
profile from preserved Vault objects while retaining integrity and path-safety.
'''

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Sequence
import zipfile

from . import __version__
from .bottles_adapter import (
    BottlesAdapterError,
    deploy_external_bottles_profile,
    require_bottles_managed_path,
    run_external_bottles_deployment,
)
from .composition_profile import (
    DerivedCapsule,
    RunnerOverrideError,
    build_derived_capsule,
)
from .materializer import materialize_profile, remove_materialization
from .neutral_profiles import (
    materialize_neutral_bottle_source,
    validate_neutral_bottles_source,
)
from .playable import materialize_playable_profile, run_playable_profile
from .preserved_runners import RunnerCatalogError, RunnerRecord, scan_runners
from .runner_deployment import RunnerDeploymentError, ensure_bottles_runner
from .umu_adapter import (
    UmuAdapterError,
    _inspect_archive,
    materialize_umu_profile,
    run_umu_materialization,
)
from .verifier import VerifyError, resolve_capsule_object, verify_object
from .manifest_travel import (
    ManifestTravelError,
    copy_manifests_to_materialization,
    validate_manifests_present_for,
    write_generated_files_manifest,
    write_receipt_sidecar,
)


class CompositionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SharedUmuRuntime:
    "Globally resolved components for one reproducible UMU composition."

    component_set_id: str
    component_set_digest: str
    backend_object_id: str
    backend_object: dict[str, Any]
    runtime_object_id: str
    runtime_object: dict[str, Any]
    backend_entrypoint: str
    backend_entrypoint_arguments: tuple[str, ...]
    backend_pythonpath: str | None
    runtime_source: str
    runtime_destination: str
    runtime_var: str
    runtime_family: str
    platform_prefix: str
    platform_directory: str
    backend_archive_policy: dict[str, Any]
    runtime_archive_policy: dict[str, Any]
    allowed_absolute_symlinks: tuple[dict[str, Any], ...]
@dataclass(frozen=True, slots=True)
class CompositionResult:
    schema: int
    capsule_id: str
    backend: str
    runner_id: str
    profile_id: str
    destination: str
    materialized: bool
    played: bool
    play_complete: bool | None
    backend_result: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_BACKEND_PARTITIONS = (
    "engine/python-portable",
    "engine/umu-portable",
    "engine/xdg-data",
)


# UMU 1.4.x resolves the container runtime from Proton's
# toolmanifest.vdf `require_tool_appid`.  The platform directory uses the
# runtime codename for steamrt2/steamrt3 and the family name for steamrt4.
# Source: Open-Wine-Components/umu-launcher, umu_runtime.py RUNTIME_VERSIONS.
_UMU_RUNTIME_BY_APPID: dict[str, tuple[str, str]] = {
    "1391110": ("steamrt2", "soldier"),
    "1628350": ("steamrt3", "sniper"),
    "4183110": ("steamrt4", "steamrt4"),
}
_UMU_PLATFORM_PREFIX_BY_FAMILY: dict[str, str] = {
    family: platform
    for family, platform in _UMU_RUNTIME_BY_APPID.values()
}
_TOOLMANIFEST_LIMIT = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _UmuRuntimeRequirement:
    appid: str
    family: str
    platform_prefix: str


def _read_limited_stream(stream: Any, label: str) -> bytes:
    payload = stream.read(_TOOLMANIFEST_LIMIT + 1)
    if len(payload) > _TOOLMANIFEST_LIMIT:
        raise CompositionError(f"{label} is unexpectedly large.")
    return payload


def _read_runner_archive_member(
    collection_root: Path,
    runner: RunnerRecord,
    relative: str,
) -> bytes:
    archive = (
        collection_root
        / "01_IMMUTABLE_VAULT"
        / _safe_relative(runner.archive_path, "runner.archive_path")
    )
    target = (
        PurePosixPath(runner.source_root)
        / _safe_relative(relative, "runner member")
    ).as_posix()

    if runner.format == "zip":
        try:
            with zipfile.ZipFile(archive) as handle:
                with handle.open(target) as stream:
                    return _read_limited_stream(stream, target)
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            raise CompositionError(
                f"Preserved runner lacks readable {target}."
            ) from exc

    if runner.format in {"tar", "tar.gz"}:
        try:
            with tarfile.open(archive, mode="r:*") as handle:
                member = handle.getmember(target)
                if not member.isfile():
                    raise CompositionError(
                        f"Preserved runner {target} is not a regular file."
                    )
                stream = handle.extractfile(member)
                if stream is None:
                    raise CompositionError(
                        f"Cannot read preserved runner {target}."
                    )
                with stream:
                    return _read_limited_stream(stream, target)
        except (KeyError, OSError, tarfile.TarError) as exc:
            raise CompositionError(
                f"Preserved runner lacks readable {target}."
            ) from exc

    if runner.format != "tar.zst":
        raise CompositionError(
            f"Unsupported preserved runner format: {runner.format}."
        )
    zstd = shutil.which("zstd")
    if zstd is None:
        raise CompositionError(
            "zstd is required to inspect the preserved Proton runner."
        )

    process = subprocess.Popen(
        [zstd, "-dc", "--no-progress", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    found: bytes | None = None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as handle:
            for member in handle:
                if member.name.rstrip("/") != target:
                    continue
                if not member.isfile():
                    raise CompositionError(
                        f"Preserved runner {target} is not a regular file."
                    )
                stream = handle.extractfile(member)
                if stream is None:
                    raise CompositionError(
                        f"Cannot read preserved runner {target}."
                    )
                with stream:
                    found = _read_limited_stream(stream, target)
                break
    except (OSError, tarfile.TarError) as exc:
        raise CompositionError(
            f"Cannot inspect preserved runner {target}: {exc}"
        ) from exc
    finally:
        if found is not None:
            process.kill()
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        returncode = process.wait()
    if found is None:
        if returncode != 0:
            raise CompositionError(
                f"Cannot inspect preserved runner: {stderr.strip()}"
            )
        raise CompositionError(
            f"Preserved runner lacks readable {target}."
        )
    return found


def _required_umu_runtime(
    collection_root: Path,
    runner: RunnerRecord,
) -> _UmuRuntimeRequirement:
    if runner.proton_path is None:
        raise CompositionError(
            f"Runner {runner.runner_id!r} is not a Proton runner."
        )
    raw = _read_runner_archive_member(
        collection_root,
        runner,
        "toolmanifest.vdf",
    )
    document = raw.decode("utf-8", errors="replace")
    match = re.search(
        r"(?im)[\"']?require_tool_appid[\"']?\s+[\"']?([0-9]+)[\"']?",
        document,
    )
    if match is None:
        raise CompositionError(
            f"Runner {runner.runner_id!r} does not declare "
            "require_tool_appid in toolmanifest.vdf."
        )
    appid = match.group(1)
    resolved = _UMU_RUNTIME_BY_APPID.get(appid)
    if resolved is None:
        raise CompositionError(
            f"Runner {runner.runner_id!r} requires unknown Steam runtime "
            f"AppID {appid}; the core cannot select an offline runtime safely."
        )
    family, platform_prefix = resolved
    return _UmuRuntimeRequirement(
        appid=appid,
        family=family,
        platform_prefix=platform_prefix,
    )


def _archive_directory_exists(names: set[str], relative: str) -> bool:
    prefix = relative.rstrip("/") + "/"
    return relative in names or any(name.startswith(prefix) for name in names)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CompositionError(f"{label} must be a regular file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompositionError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CompositionError(f"{label} must contain a JSON object.")
    return value


def _safe_relative(value: Any, label: str, *, dot: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise CompositionError(f"{label} is not a safe relative path.")
    if dot and value == ".":
        return PurePosixPath(".")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CompositionError(f"{label} is not a safe relative path.")
    return path


def _portable_id(value: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("._-")
    if not value:
        raise CompositionError("Cannot derive a portable profile ID.")
    if not value[0].isalnum():
        value = "composition-" + value
    return value


def _unique_id(existing: set[str], preferred: str) -> str:
    candidate = _portable_id(preferred)
    if candidate not in existing:
        return candidate
    number = 2
    while f"{candidate}-{number}" in existing:
        number += 1
    return f"{candidate}-{number}"


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _select_runner(
    collection_root: Path,
    runner_id: str,
    backend: str,
) -> RunnerRecord:
    try:
        runners, _warnings = scan_runners(collection_root)
    except RunnerCatalogError as exc:
        raise CompositionError(str(exc)) from exc
    matches = [runner for runner in runners if runner.runner_id == runner_id]
    if len(matches) != 1:
        raise CompositionError(
            f"Preserved runner {runner_id!r} does not exist exactly once."
        )
    runner = matches[0]
    if not runner.supports(backend):
        raise CompositionError(
            f"Runner {runner_id!r} is not structurally compatible with {backend}."
        )
    return runner


_NEUTRAL_CONTRACTS = {
    "ogv-game-source-v1",
    "ogv-bottles-neutral-v1",
    "ogv-direct-wine-neutral-v1",
    "ogv-umu-neutral-v1",
}

_SOURCE_PRIORITIES: dict[str, dict[str, int]] = {
    "direct-wine": {
        "playable-wine": 0,
        "ogv-direct-wine-neutral-v1": 1,
        "ogv-bottles-neutral-v1": 2,
        "ogv-umu-neutral-v1": 3,
        "ogv-game-source-v1": 4,
    },
    "bottles": {
        "ogv-bottles-neutral-v1": 0,
        "ogv-direct-wine-neutral-v1": 1,
        "ogv-umu-neutral-v1": 2,
        "ogv-game-source-v1": 3,
    },
    "umu": {
        "umu-native": 0,
        "ogv-umu-neutral-v1": 1,
        "ogv-direct-wine-neutral-v1": 2,
        "ogv-bottles-neutral-v1": 3,
        "playable-wine": 4,
        "ogv-game-source-v1": 5,
    },
}


def _linux_profiles(capsule: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise CompositionError("capsule.profiles must be an array.")
    return [
        item
        for item in profiles
        if isinstance(item, dict) and item.get("platform") == "linux"
    ]


def _read_contract(capsule_path: Path, profile: dict[str, Any]) -> dict[str, Any]:
    relative = _safe_relative(profile.get("host_contract"), "profile.host_contract")
    root = capsule_path.parent.resolve(strict=True)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CompositionError("host_contract traverses a symlink.")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CompositionError("host_contract escapes the capsule.") from exc
    return _load_json(resolved, "host contract")


def _source_kind(
    capsule_path: Path,
    profile: dict[str, Any],
) -> str | None:
    # Preserved UMU rich contract: the umu block is the source of truth.
    # No overlay with global backend + runtime + runner is needed; the
    # capsule already declares its own launchers, protected manifests,
    # state archives, symlinks and nested archives.
    umu = profile.get("umu")
    if (
        profile.get("adapter") == "umu"
        and isinstance(umu, dict)
        and umu.get("schema") == 0
    ):
        return "umu-native"

    playable = profile.get("playable")
    if (
        profile.get("adapter") == "wine"
        and isinstance(playable, dict)
        and playable.get("schema") == 0
        and playable.get("backend") == "wine"
    ):
        return "playable-wine"

    raw_contract = profile.get("host_contract")
    if not isinstance(raw_contract, str) or not raw_contract:
        return None
    try:
        contract = _read_contract(capsule_path, profile)
    except CompositionError:
        return None
    name = contract.get("contract")
    return name if name in _NEUTRAL_CONTRACTS else None


def _select_source_profile(
    capsule_path: Path,
    *,
    backend: str,
    profile_id: str | None,
    runner: RunnerRecord | None = None,
) -> str:
    priorities = _SOURCE_PRIORITIES.get(backend)
    if priorities is None:
        raise CompositionError(f"Unknown composition backend: {backend}.")

    capsule = _load_json(capsule_path, "capsule.json")
    candidates: list[tuple[int, str, str]] = []
    known_ids: set[str] = set()
    native_runner_mismatches: list[
        tuple[str, tuple[str, ...]]
    ] = []
    objects = capsule.get("objects")
    object_records = (
        objects if isinstance(objects, list) else []
    )
    declarations = {
        item.get("id"): item
        for item in object_records
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
    }
    for profile in _linux_profiles(capsule):
        identifier = profile.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        known_ids.add(identifier)
        if profile_id is not None and identifier != profile_id:
            continue
        kind = _source_kind(capsule_path, profile)
        if kind in priorities:
            if (
                backend == "umu"
                and kind == "umu-native"
                and runner is not None
            ):
                umu = profile.get("umu")
                layout = (
                    umu.get("layout")
                    if isinstance(umu, dict)
                    else None
                )
                bound_objects: list[str] = []
                if isinstance(layout, list):
                    for layout_item in layout:
                        if not isinstance(layout_item, dict):
                            continue
                        destination = layout_item.get(
                            "destination"
                        )
                        object_id = layout_item.get("object")
                        if (
                            not isinstance(destination, str)
                            or not isinstance(object_id, str)
                        ):
                            continue
                        parts = PurePosixPath(
                            destination
                        ).parts
                        if (
                            len(parts) >= 3
                            and parts[:2]
                            == ("engine", "proton")
                        ):
                            bound_objects.append(object_id)
                native_runner_ids = tuple(
                    sorted(set(bound_objects))
                )
                native_runner_digests = tuple(
                    sorted(
                        {
                            str(declaration["digest"])
                            for object_id in native_runner_ids
                            for declaration in (
                                declarations.get(object_id),
                            )
                            if isinstance(declaration, dict)
                            and isinstance(
                                declaration.get("digest"),
                                str,
                            )
                        }
                    )
                )

                direct_match = (
                    len(native_runner_ids) == 1
                    and native_runner_digests
                    == (runner.digest,)
                )

                embedded_match = False
                embedded_bindings: list[str] = []
                if (
                    not direct_match
                    and not native_runner_ids
                    and isinstance(layout, list)
                ):
                    collection_root = capsule_path.parents[2]

                    def manifest_entries(
                        digest: str,
                    ) -> tuple[tuple[str, int, str], ...]:
                        if not digest.startswith("sha256:"):
                            return ()
                        hexdigest = digest.removeprefix("sha256:")
                        manifest = (
                            collection_root
                            / "01_IMMUTABLE_VAULT"
                            / "manifests"
                            / "sha256"
                            / hexdigest[:2]
                            / hexdigest[2:4]
                            / hexdigest
                        )
                        if not manifest.is_file():
                            return ()
                        entries: list[tuple[str, int, str]] = []
                        for raw in manifest.read_text(
                            encoding="utf-8"
                        ).splitlines():
                            parts = raw.strip().split(None, 2)
                            if len(parts) != 3:
                                continue
                            file_digest, size_text, relative = parts
                            file_digest = file_digest.removeprefix(
                                "sha256:"
                            )
                            if (
                                len(file_digest) != 64
                                or any(
                                    char not in "0123456789abcdef"
                                    for char in file_digest
                                )
                            ):
                                continue
                            try:
                                size = int(size_text)
                            except ValueError:
                                continue
                            entries.append(
                                (
                                    file_digest,
                                    size,
                                    PurePosixPath(relative).as_posix(),
                                )
                            )
                        return tuple(sorted(entries))

                    def strip_manifest_prefix(
                        entries: tuple[
                            tuple[str, int, str], ...
                        ],
                        prefix: str,
                    ) -> tuple[tuple[str, int, str], ...]:
                        normalized = (
                            PurePosixPath(prefix).as_posix().rstrip("/")
                            + "/"
                        )
                        stripped = [
                            (
                                digest,
                                size,
                                relative[len(normalized):],
                            )
                            for digest, size, relative in entries
                            if relative.startswith(normalized)
                            and relative != normalized
                        ]
                        return tuple(sorted(stripped))

                    runner_entries = manifest_entries(runner.digest)
                    normalized_runner_entries = (
                        strip_manifest_prefix(
                            runner_entries,
                            runner.source_root,
                        )
                        or runner_entries
                    )

                    if normalized_runner_entries:
                        for layout_item in layout:
                            if not isinstance(layout_item, dict):
                                continue
                            destination = layout_item.get(
                                "destination"
                            )
                            source = layout_item.get("source")
                            object_id = layout_item.get("object")
                            if (
                                destination != "engine"
                                or not isinstance(source, str)
                                or not isinstance(object_id, str)
                            ):
                                continue
                            declaration = declarations.get(object_id)
                            if not isinstance(declaration, dict):
                                continue
                            object_digest = declaration.get("digest")
                            if not isinstance(object_digest, str):
                                continue
                            object_entries = manifest_entries(
                                object_digest
                            )
                            embedded_prefix = (
                                PurePosixPath(source)
                                / "proton"
                                / runner.source_root
                            ).as_posix()
                            embedded_entries = strip_manifest_prefix(
                                object_entries,
                                embedded_prefix,
                            )
                            if (
                                embedded_entries
                                and embedded_entries
                                == normalized_runner_entries
                            ):
                                embedded_match = True
                                embedded_bindings.append(
                                    f"{object_id}:{embedded_prefix}"
                                )
                                break

                if not direct_match and not embedded_match:
                    binding_details = (
                        native_runner_ids
                        + tuple(embedded_bindings)
                    )
                    native_runner_mismatches.append(
                        (identifier, binding_details)
                    )
                    continue
            candidates.append((priorities[kind], identifier, kind))

    if profile_id is not None and profile_id not in known_ids:
        raise CompositionError(
            f"Source profile {profile_id!r} does not exist."
        )
    if not candidates and native_runner_mismatches:
        bindings = "; ".join(
            (
                f"{identifier}: "
                + (
                    ", ".join(runner_ids)
                    if runner_ids
                    else "<no unique runner binding>"
                )
            )
            for identifier, runner_ids in native_runner_mismatches
        )
        requested = (
            runner.runner_id
            if runner is not None
            else "<unspecified>"
        )
        if profile_id is not None:
            raise CompositionError(
                f"UMU-native source profile {profile_id!r} is not bound "
                f"exclusively to requested runner {requested!r}; "
                f"declared native Proton binding: {bindings}."
            )
        raise CompositionError(
            "No Linux source profile can satisfy the requested UMU "
            f"runner {requested!r}. UMU-native Proton bindings rejected by "
            f"the exact-runner contract: {bindings}."
        )

    if not candidates:
        selected = f" {profile_id!r}" if profile_id is not None else ""
        raise CompositionError(
            f"Linux source profile{selected} cannot provide the material "
            f"required by {backend}."
        )

    if profile_id is None and runner is not None:
        auto_candidates = candidates
        candidates = []
        derivation_failures: list[tuple[str, str]] = []
        for priority in sorted({item[0] for item in auto_candidates}):
            viable: list[tuple[int, str, str]] = []
            for candidate in auto_candidates:
                if candidate[0] != priority:
                    continue
                _priority, identifier, kind = candidate
                if (
                    kind not in _NEUTRAL_CONTRACTS
                    and kind != "playable-wine"
                ):
                    viable.append(candidate)
                    continue
                try:
                    build_derived_capsule(
                        capsule_path,
                        identifier,
                        runner,
                    )
                except RunnerOverrideError as exc:
                    derivation_failures.append(
                        (identifier, str(exc))
                    )
                else:
                    viable.append(candidate)
            if viable:
                candidates = viable
                break

        if not candidates:
            detail = "; ".join(
                f"{identifier}: {message}"
                for identifier, message in derivation_failures
            )
            raise CompositionError(
                "No automatically selected Linux source profile is "
                f"derivable for {backend} with runner "
                f"{runner.runner_id!r}"
                + (f": {detail}" if detail else ".")
            )

    best_priority = min(item[0] for item in candidates)
    best = [item for item in candidates if item[0] == best_priority]
    if len(best) != 1:
        labels = ", ".join(f"{identifier} ({kind})" for _, identifier, kind in best)
        raise CompositionError(
            "Several equally suitable source profiles exist; select one "
            f"explicitly: {labels}."
        )
    return best[0][1]


def _write_overlay(
    root: Path,
    derived: DerivedCapsule,
    *,
    profile_id: str,
    backend: str,
    runner: RunnerRecord,
    source_profile_id: str,
) -> Path:
    document = deepcopy(derived.document)
    profiles = document.get("profiles")
    if not isinstance(profiles, list):
        raise CompositionError("Derived capsule has no profiles array.")
    matches = [
        item for item in profiles
        if isinstance(item, dict) and item.get("id") == source_profile_id
    ]
    if len(matches) != 1:
        raise CompositionError("Derived source profile is absent.")
    profile = matches[0]
    profile["id"] = profile_id
    profile["notes"] = (
        f"User-requested {backend} composition generated by "
        f"Offline Game Vault {__version__}. It uses only preserved Vault "
        "objects."
    )

    root.mkdir(parents=True, mode=0o700)
    capsule_path = root / "capsule.json"
    capsule_path.write_bytes(_canonical_bytes(document))
    for companion in derived.companion_files:
        relative = _safe_relative(companion.relative_path, "companion file")
        destination = root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(companion.payload)
        destination.chmod(companion.mode)
        actual = hashlib.sha256(destination.read_bytes()).hexdigest()
        if actual != companion.sha256:
            raise CompositionError("Companion file failed verification.")
    return capsule_path


def _composition_profile_id(backend: str, runner: RunnerRecord) -> str:
    label = {"direct-wine": "wine", "bottles": "bottles", "umu": "umu"}[backend]
    return _portable_id(f"composition-{label}-{runner.runner_id}")



# --------------------------------------------------------- fase 4 helpers

# Primary receipt filename per backend, at the destination root. Used by the
# travel step to sidecar the receipt against accidental corruption.
_PLAYABLE_RECEIPT_NAME = "playable-materialization.json"
_UMU_RECEIPT_NAME = "umu-materialization.json"
_BOTTLES_RECEIPT_NAME = ".ogv-bottles-deployment.json"


def _capsule_object_digests(operational_capsule: Path) -> list[str]:
    """Return the sha256 digests declared by an operational capsule.

    Used at the start of ``compose_*`` to fail fast if any object lacks a
    manifest in the Vault. Duplicates are preserved by insertion order so
    the caller sees exactly what the capsule declares.
    """
    document = json.loads(
        operational_capsule.read_text(encoding="utf-8")
    )
    digests: list[str] = []
    for entry in document.get("objects", []):
        if not isinstance(entry, dict):
            continue
        digest = entry.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:"):
            digests.append(digest)
    return digests


def _rollback_destination(destination: Path) -> None:
    """Remove ``destination`` on manifest-travel failure, best-effort.

    Called only when a compose has already published to ``destination`` but
    the travel step could not complete. The compose's invariant is that a
    successful return implies a self-verifiable tree; if travel failed, the
    materialization must not remain in place under a false promise.
    """
    try:
        shutil.rmtree(destination)
    except OSError as rollback_exc:
        print(
            "ogv: warning: could not roll back destination "
            f"{destination}: {rollback_exc}",
            file=sys.stderr,
        )


def _validate_fresh_start_intent(
    *,
    fresh_start: bool,
    no_state: bool,
    state_backup: Path | None,
    save_id: str | None = None,
) -> None:
    # User-level fresh-start must not be conflated with the stronger
    # low-level no-state operator control.
    if not fresh_start:
        return
    if no_state:
        raise CompositionError(
            "fresh_start and no_state are mutually exclusive: fresh_start "
            "preserves backend-required initial configuration, while "
            "no_state skips all state."
        )
    if state_backup is not None:
        raise CompositionError(
            "fresh_start and state_backup are mutually exclusive."
        )
    if save_id is not None:
        raise CompositionError(
            "fresh_start and save_id are mutually exclusive."
        )


def _effective_materializer_no_state(
    *,
    source_kind: str,
    fresh_start: bool,
    no_state: bool,
) -> bool:
    # no_state always skips state. Fresh-start skips restorable generic state,
    # but preserved UMU-native contracts must keep policy=always archives.
    return no_state or (fresh_start and source_kind != "umu-native")


def compose_wine(
    *,
    collection_root: Path,
    capsule_path: Path,
    runner_id: str,
    destination: Path,
    source_profile_id: str | None = None,
    state_backup: Path | None = None,
    fresh_start: bool = False,
    no_state: bool = False,
    play: bool = False,
    arguments: Sequence[str] = (),
) -> CompositionResult:
    _validate_fresh_start_intent(
        fresh_start=fresh_start,
        no_state=no_state,
        state_backup=state_backup,
    )
    effective_no_state = _effective_materializer_no_state(
        source_kind="generic",
        fresh_start=fresh_start,
        no_state=no_state,
    )
    collection_root = collection_root.expanduser().resolve(strict=True)
    vault_root = collection_root / "01_IMMUTABLE_VAULT"
    runner = _select_runner(collection_root, runner_id, "direct-wine")
    source_id = _select_source_profile(
        capsule_path,
        backend="direct-wine",
        profile_id=source_profile_id,
        runner=runner,
    )
    try:
        derived = build_derived_capsule(capsule_path, source_id, runner)
    except RunnerOverrideError as exc:
        raise CompositionError(str(exc)) from exc
    profile_id = _composition_profile_id("direct-wine", runner)

    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".ogv-control-direct-wine-",
        dir=destination.parent,
    ) as temporary:
        operational_capsule = _write_overlay(
            Path(temporary) / "capsule",
            derived,
            profile_id=profile_id,
            backend="direct-wine",
            runner=runner,
            source_profile_id=source_id,
        )
        digests = _capsule_object_digests(operational_capsule)
        try:
            validate_manifests_present_for(
                vault_root=vault_root, digests=digests
            )
        except ManifestTravelError as exc:
            raise CompositionError(str(exc)) from exc
        # For generic Direct-Wine materialization, fresh-start and
        # no-state both omit restorable state. The semantic distinction is
        # retained at the composition boundary for backends such as UMU-native.
        result = materialize_playable_profile(
            capsule_path=operational_capsule,
            profile_id=profile_id,
            vault_root=vault_root,
            destination=destination,
            state_backup=None if effective_no_state else state_backup,
            state_capsule_path=capsule_path,
            no_state=effective_no_state,
        )
        try:
            copied_manifests = copy_manifests_to_materialization(
                vault_root=vault_root,
                destination=destination,
                digests=digests,
            )
            receipt_path = destination / _PLAYABLE_RECEIPT_NAME
            write_generated_files_manifest(
                destination=destination,
                object_manifest_paths=copied_manifests,
                excluded_paths={
                    receipt_path.with_name(receipt_path.name + ".sha256")
                },
            )
            write_receipt_sidecar(receipt_path)
        except (ManifestTravelError, OSError) as exc:
            _rollback_destination(destination)
            raise CompositionError(
                "Materialization was published but manifest travel "
                f"failed: {exc}. The destination was removed to "
                "preserve the invariant that a successful compose "
                "produces a self-verifiable tree."
            ) from exc
        played = False
        play_complete: bool | None = None
        play_result: dict[str, Any] | None = None
        if play:
            run_result = run_playable_profile(
                destination=destination,
                arguments=arguments,
            )
            play_result = asdict(run_result)
            played = True
            play_complete = run_result.complete

    capsule = _load_json(capsule_path, "capsule.json")
    return CompositionResult(
        schema=0,
        capsule_id=str(capsule["capsule_id"]),
        backend="direct-wine",
        runner_id=runner.runner_id,
        profile_id=profile_id,
        destination=str(destination),
        materialized=True,
        played=played,
        play_complete=play_complete,
        backend_result={
            "materialization": asdict(result),
            "state_provisioned": not effective_no_state,
            "fresh_start": fresh_start,
            "play": play_result,
        },
    )


def _strip_prefix_root(value: Any, prefix: str, label: str) -> PurePosixPath:
    """Rebase a playable path such as ``prefix/drive_c/...`` on the prefix."""
    relative = _safe_relative(value, label)
    root = _safe_relative(prefix, "playable.paths.prefix")
    try:
        return PurePosixPath(relative.relative_to(root))
    except ValueError as exc:
        raise CompositionError(
            f"{label} is not located inside the playable prefix."
        ) from exc





def _bottles_overlay(
    *,
    capsule_path: Path,
    source_profile_id: str,
    runner: RunnerRecord,
    destination: Path,
) -> tuple[Path, str]:
    capsule = deepcopy(_load_json(capsule_path, "capsule.json"))
    profiles = capsule.get("profiles")
    objects = capsule.get("objects")
    if not isinstance(profiles, list) or not isinstance(objects, list):
        raise CompositionError("Capsule is incomplete.")
    matches = [
        item for item in profiles
        if isinstance(item, dict) and item.get("id") == source_profile_id
    ]
    if len(matches) != 1:
        raise CompositionError("Bottles source profile is absent.")
    source = matches[0]
    required_fields = (
        "source_object",
        "neutral_root",
        "prefix_source",
        "game_source",
        "game_destination_in_prefix",
        "entrypoint_relative_to_game",
        "working_directory_in_prefix",
    )

    # The historical synthesis path (playable-wine -> neutral fields) has
    # been retired: legacy capsules must be migrated to a real
    # ogv-bottles-neutral-v1 contract with `ogv migrate-bottles-contract`.
    # _select_source_profile already refuses playable-wine profiles for
    # bottles (they are absent from _SOURCE_PRIORITIES["bottles"]); this
    # branch reports the remaining case: a source profile whose contract
    # exists but is not a compatible neutral shape.
    source_kind = _source_kind(capsule_path, source)
    contract = _read_contract(capsule_path, source)
    contract_name = contract.get("contract")
    if contract_name not in _NEUTRAL_CONTRACTS:
        actual = contract_name or source_kind or "unknown"
        raise CompositionError(
            "Bottles requires a compatible neutral Linux source contract; "
            f"the selected source profile provides {actual!r} only."
        )
    prefix_contains_game = False

    missing = [
        name for name in required_fields
        if not isinstance(contract.get(name), str) or not contract.get(name)
    ]
    if missing:
        raise CompositionError(
            "Neutral source contract is incomplete for Bottles: "
            + ", ".join(missing)
        )

    source_object = str(contract["source_object"])
    declarations = [
        item for item in objects
        if isinstance(item, dict) and item.get("id") == source_object
    ]
    if len(declarations) != 1:
        raise CompositionError("Bottles source object is not unique.")
    roles = declarations[0].get("roles")
    if not isinstance(roles, list):
        raise CompositionError("Bottles source object has no roles.")
    if "prefix_baseline" not in roles:
        roles.append("prefix_baseline")

    profile_id = _composition_profile_id("bottles", runner)
    launch = source.get("launch")
    arguments: list[str] = []
    environment: dict[str, str] = {}
    if isinstance(launch, dict):
        raw_arguments = launch.get("arguments", [])
        raw_environment = launch.get("environment", {})
        if isinstance(raw_arguments, list) and all(
            isinstance(value, str) for value in raw_arguments
        ):
            arguments = list(raw_arguments)
        if isinstance(raw_environment, dict) and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_environment.items()
        ):
            environment = dict(raw_environment)

    generated_contract = {
        "schema": 0,
        "contract": "ogv-bottles-neutral-v1",
        **{
            field: contract[field]
            for field in required_fields
        },
        "baseline_state": contract.get(
            "baseline_state", "embedded-or-unknown"
        ),
        "prefix_contains_game": prefix_contains_game,
        "runner_binding": "selected",
        "preferred_runner": runner.runner_id,
        "flatpak_app": "com.usebottles.bottles",
        "bottle_yml_policy": "generate-derived",
        "network": "isolated",
    }
    generated_contract_path = "host-contracts/composition-bottles.json"

    source.clear()
    source.update(
        {
            "id": profile_id,
            "platform": "linux",
            "adapter": "bottles",
            "dependencies": [source_object],
            "host_contract": generated_contract_path,
            "launch": {
                "entrypoint": (
                    f"{contract['game_destination_in_prefix']}/"
                    f"{contract['entrypoint_relative_to_game']}"
                ),
                "working_directory": contract[
                    "working_directory_in_prefix"
                ],
                "arguments": arguments,
                "environment": environment,
                "network": "isolated",
            },
            "notes": (
                "User-requested Bottles composition generated by "
                f"Offline Game Vault {__version__}."
            ),
        }
    )

    destination.mkdir(parents=True, mode=0o700)
    output = destination / "capsule.json"
    output.write_bytes(_canonical_bytes(capsule))
    target_contract = destination / generated_contract_path
    target_contract.parent.mkdir(parents=True, exist_ok=True)
    target_contract.write_bytes(_canonical_bytes(generated_contract))
    target_contract.chmod(0o600)
    return output, profile_id

def compose_bottles(
    *,
    collection_root: Path,
    capsule_path: Path,
    runner_id: str,
    destination: Path,
    bottles_path: Path | None = None,
    bottle_name: str,
    source_profile_id: str | None = None,
    state_backup: Path | None = None,
    fresh_start: bool = False,
    no_state: bool = False,
    play: bool = False,
) -> CompositionResult:
    _validate_fresh_start_intent(
        fresh_start=fresh_start,
        no_state=no_state,
        state_backup=state_backup,
    )
    effective_no_state = _effective_materializer_no_state(
        source_kind="generic",
        fresh_start=fresh_start,
        no_state=no_state,
    )
    collection_root = collection_root.expanduser().resolve(strict=True)
    vault_root = collection_root / "01_IMMUTABLE_VAULT"
    runner = _select_runner(collection_root, runner_id, "bottles")
    source_id = _select_source_profile(
        capsule_path,
        backend="bottles",
        profile_id=source_profile_id,
    )
    capsule = _load_json(capsule_path, "capsule.json")

    try:
        bottles_path = require_bottles_managed_path(bottles_path)
    except BottlesAdapterError as exc:
        raise CompositionError(str(exc)) from exc
    if not os.access(bottles_path, os.W_OK):
        raise CompositionError(
            "The Bottles managed path is not writable."
        )

    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise CompositionError(
            f"The destination already exists: {destination}"
        )
    destination_parent = destination.parent
    if (
        destination_parent.is_symlink()
        or not destination_parent.is_dir()
    ):
        raise CompositionError(
            "The destination parent must be an existing regular directory."
        )
    destination_parent = destination_parent.resolve(strict=True)
    destination = destination_parent / destination.name
    try:
        destination.relative_to(collection_root)
    except ValueError:
        pass
    else:
        raise CompositionError(
            "The destination must remain outside the Vault collection."
        )

    # Resolving the runner is cheap and can fail for material reasons, so it
    # happens before tens of gigabytes are extracted rather than after. It
    # also fixes the directory name Bottles will see, which the generated
    # bottle.yml must carry.
    try:
        installation = ensure_bottles_runner(
            collection_root,
            bottles_path,
            runner,
        )
    except (RunnerCatalogError, RunnerDeploymentError) as exc:
        raise CompositionError(str(exc)) from exc

    deployment = None
    runner_created = installation.created
    play_result = None
    played = False
    play_complete: bool | None = None
    # Heavy staging belongs beside the requested external materialization.
    # This keeps all large copies on the selected filesystem and allows the
    # adapter to publish atomically without duplicating the bottle in Bottles.
    with tempfile.TemporaryDirectory(
        prefix=f".ogv-work-{_portable_id(bottle_name)}-",
        dir=destination_parent,
    ) as temporary:
        root = Path(temporary)
        operational_capsule, profile_id = _bottles_overlay(
            capsule_path=capsule_path,
            source_profile_id=source_id,
            runner=runner,
            destination=root / "capsule",
        )
        digests = _capsule_object_digests(operational_capsule)
        try:
            validate_manifests_present_for(
                vault_root=vault_root, digests=digests
            )
        except ManifestTravelError as exc:
            raise CompositionError(str(exc)) from exc
        raw = root / "neutral-source"
        materialize_profile(
            capsule_path=operational_capsule,
            profile_id=profile_id,
            vault_root=vault_root,
            destination=raw,
        )
        try:
            materialize_neutral_bottle_source(
                materialization=raw,
                capsule_path=operational_capsule,
                profile_id=profile_id,
                runner=runner,
                bottle_name=bottle_name,
                runner_name=installation.name,
            )
            validate_neutral_bottles_source(
                materialization=raw,
                capsule_path=operational_capsule,
                profile_id=profile_id,
            )
            deployment = deploy_external_bottles_profile(
                capsule_path=operational_capsule,
                profile_id=profile_id,
                materialization=raw,
                destination=destination,
                bottles_path=bottles_path,
                bottle_name=bottle_name,
                # Generic Bottles fresh-start omits restorable state,
                # while retaining the distinct stronger no-state control.
                state_backup=(
                    None if effective_no_state else state_backup
                ),
                require_state_backup=not effective_no_state,
                state_capsule_path=capsule_path,
            )
            try:
                copied_manifests = copy_manifests_to_materialization(
                    vault_root=vault_root,
                    destination=destination,
                    digests=digests,
                )
                receipt_path = destination / _BOTTLES_RECEIPT_NAME
                write_generated_files_manifest(
                    destination=destination,
                    object_manifest_paths=copied_manifests,
                    excluded_paths={
                        receipt_path.with_name(
                            receipt_path.name + ".sha256"
                        )
                    },
                )
                write_receipt_sidecar(receipt_path)
            except (ManifestTravelError, OSError) as exc:
                # Un-register the external bottle before dropping the
                # destination; leaving a dangling symlink in the managed
                # Bottles directory would be worse than a lost
                # materialization.
                try:
                    (bottles_path / bottle_name).unlink()
                except OSError:
                    pass
                _rollback_destination(destination)
                raise CompositionError(
                    "Materialization was published but manifest "
                    f"travel failed: {exc}. The destination and its "
                    "external registration were removed to preserve "
                    "the invariant that a successful compose produces "
                    "a self-verifiable tree."
                ) from exc
            if play:
                launch_plan, returncode = (
                    run_external_bottles_deployment(
                        destination=destination,
                        bottles_path=bottles_path,
                    )
                )
                play_result = {
                    "plan": launch_plan.to_dict(),
                    "returncode": returncode,
                    "complete": returncode == 0,
                }
                played = True
                play_complete = returncode == 0
        finally:
            if raw.exists():
                remove_materialization(
                    destination=raw,
                    confirm_state_preserved=True,
                )

    assert deployment is not None
    return CompositionResult(
        schema=0,
        capsule_id=str(capsule["capsule_id"]),
        backend="bottles",
        runner_id=runner.runner_id,
        profile_id=profile_id,
        destination=str(destination),
        materialized=True,
        played=played,
        play_complete=play_complete,
        backend_result={
            "deployment": asdict(deployment),
            "runner_installed": runner_created,
            "state_provisioned": not effective_no_state,
            "fresh_start": fresh_start,
            "play": play_result,
        },
    )


def _component_set_id(
    digest: str,
    runtime_var: str,
) -> str:
    runtime_tag = hashlib.sha256(
        runtime_var.encode("utf-8")
    ).hexdigest()[:8]

    return (
        f"umu-component-set-"
        f"{digest[:12]}-"
        f"{runtime_tag}"
    )
def _runtime_generation(runtime_var: str) -> int:
    match = re.search(r"steamrt[-_]?([0-9]+)", runtime_var.casefold())
    return int(match.group(1)) if match else -1


def _sha256_global_component(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _registered_global_components(
    collection_root: Path,
    *,
    role: str,
    roles: tuple[str, ...],
    kind: str,
) -> tuple[tuple[dict[str, Any], dict[str, Any], Path], ...]:
    immutable_root = collection_root / "01_IMMUTABLE_VAULT"
    inventory = _load_json(
        immutable_root / "VAULT_INVENTORY.json",
        "VAULT_INVENTORY.json",
    )
    index = _load_json(collection_root / "INDEX.json", "INDEX.json")

    inventory_objects = inventory.get("objects")
    index_objects = index.get("objects")
    if not isinstance(inventory_objects, list):
        raise CompositionError(
            "VAULT_INVENTORY.json has no objects array."
        )
    if not isinstance(index_objects, list):
        raise CompositionError("INDEX.json has no objects array.")

    inventory_by_digest = {
        item.get("digest"): item
        for item in inventory_objects
        if isinstance(item, dict)
        and isinstance(item.get("digest"), str)
    }
    records: list[tuple[dict[str, Any], dict[str, Any], Path]] = []

    for item in index_objects:
        if not isinstance(item, dict) or item.get("role") != role:
            continue

        component_id = item.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            component_id = item.get("capsule_object_id")

        label = item.get("label")
        digest_hex = item.get("sha256")
        relative = item.get("path")
        size = item.get("size")

        if (
            not isinstance(component_id, str)
            or not re.fullmatch(
                r"[a-z0-9]+(?:[._-][a-z0-9]+)*",
                component_id,
            )
            or not isinstance(label, str)
            or not isinstance(digest_hex, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest_hex)
            or not isinstance(relative, str)
            or not isinstance(size, int)
            or size < 0
        ):
            continue

        inventory_item = inventory_by_digest.get(f"sha256:{digest_hex}")
        if not isinstance(inventory_item, dict):
            continue
        if (
            inventory_item.get("path") != relative
            or inventory_item.get("bytes") != size
        ):
            continue

        try:
            relative_path = _safe_relative(relative, f"{component_id}.path")
        except CompositionError:
            continue

        physical = immutable_root.joinpath(*relative_path.parts)
        try:
            information = physical.lstat()
        except FileNotFoundError:
            continue

        if physical.is_symlink() or not physical.is_file():
            continue
        if information.st_size != size:
            continue
        if _sha256_global_component(physical) != digest_hex:
            continue

        lowered = label.casefold()
        if lowered.endswith(".tar.zst"):
            format_name = "tar.zst"
        elif lowered.endswith(".tar.gz"):
            format_name = "tar.gz"
        elif lowered.endswith(".tar"):
            format_name = "tar"
        elif lowered.endswith(".zip"):
            format_name = "zip"
        else:
            continue

        declaration = {
            "archive_path": relative,
            "description": f"Global Vault component {component_id}.",
            "digest": f"sha256:{digest_hex}",
            "format": format_name,
            "id": component_id,
            "kind": kind,
            "required": True,
            "roles": list(roles),
            "scope": "shared",
            "shared": True,
            "size": size,
        }
        records.append((dict(item), declaration, physical))

    return tuple(sorted(records, key=lambda record: record[1]["id"]))


def _validate_global_umu_backend(
    declaration: dict[str, Any],
    physical: Path,
) -> tuple[str, tuple[str, ...], str | None]:
    try:
        members = _inspect_archive(
            physical,
            allowed_absolute=set(),
            allow_absolute_symlinks=True,
            allow_hardlinks=True,
            declared_format=str(
                declaration["format"]
            ),
        )
    except UmuAdapterError as exc:
        raise CompositionError(
            "Global UMU backend archive is not usable: "
            f"{exc}"
        ) from exc

    kinds = {
        item.name: item.kind
        for item in members
    }
    names = set(kinds)

    for directory in (
        "engine/umu-portable",
        "engine/python-portable",
    ):
        if not _archive_directory_exists(
            names,
            directory,
        ):
            raise CompositionError(
                f"Global UMU backend lacks {directory}."
            )

    preferred = (
        "engine/python-portable/umu-run-fully-local",
        "engine/umu-portable/umu-run-portable",
        "engine/umu-portable/bin/umu-run-portable",
        "engine/umu-portable/umu-run",
        "engine/umu-portable/bin/umu-run",
        "engine/python-portable/umu-run",
    )

    for candidate in preferred:
        if kinds.get(candidate) == "regular":
            return candidate, (), None

    public = sorted(
        name
        for name, kind in kinds.items()
        if kind == "regular"
        and (
            name.startswith(
                "engine/umu-portable/"
            )
            or name.startswith(
                "engine/python-portable/"
            )
        )
        and PurePosixPath(name).name == "umu-run"
    )

    if len(public) == 1:
        return public[0], (), None

    modules = sorted(
        name
        for name, kind in kinds.items()
        if kind == "regular"
        and (
            name.startswith(
                "engine/umu-portable/"
            )
            or name.startswith(
                "engine/python-portable/"
            )
        )
        and name.endswith(
            "/umu_run/__main__.py"
        )
    )

    pythons = sorted(
        name
        for name, kind in kinds.items()
        if kind == "regular"
        and name.startswith(
            "engine/python-portable/"
        )
        and PurePosixPath(name).name
        in {
            "python3",
            "python",
        }
    )

    if len(modules) == 1 and len(pythons) == 1:
        package_root = (
            PurePosixPath(modules[0])
            .parent
            .parent
            .as_posix()
        )

        return (
            pythons[0],
            (
                "-m",
                "umu_run",
            ),
            package_root,
        )

    raise CompositionError(
        "Global UMU backend lacks one deterministic "
        "preserved UMU entrypoint."
    )
def _validate_global_steam_runtime(
    index_item: dict[str, Any],
    declaration: dict[str, Any],
    physical: Path,
) -> tuple[str, str, str, str]:
    family = index_item.get("runtime_family")
    archive_root = index_item.get("archive_root")
    if not isinstance(family, str) or not isinstance(archive_root, str):
        raise CompositionError(
            "Global Steam Runtime metadata is incomplete."
        )

    archive_root = _safe_relative(
        archive_root,
        "shared-umu-runtime.archive_root",
    ).as_posix()
    platform_prefix = _UMU_PLATFORM_PREFIX_BY_FAMILY.get(family)
    if platform_prefix is None:
        raise CompositionError(
            f"Unsupported Steam Runtime family: {family}."
        )

    try:
        members = _inspect_archive(
            physical,
            allowed_absolute=set(),
            allow_absolute_symlinks=True,
            allow_hardlinks=True,
            declared_format=str(declaration["format"]),
        )
    except UmuAdapterError as exc:
        raise CompositionError(
            f"Global {family} archive is not usable: {exc}"
        ) from exc

    kinds = {item.name: item.kind for item in members}
    names = set(kinds)
    required_files = (
        f"{archive_root}/VERSIONS.txt",
        f"{archive_root}/_v2-entry-point",
        f"{archive_root}/mtree.txt.gz",
        f"{archive_root}/pressure-vessel/bin/pv-verify",
    )
    missing = [
        path for path in required_files if kinds.get(path) != "regular"
    ]
    if missing:
        raise CompositionError(
            f"Global {family} is incomplete: " + ", ".join(missing)
        )

    for directory in (
        archive_root,
        f"{archive_root}/var",
        f"{archive_root}/pressure-vessel",
        f"{archive_root}/pressure-vessel/bin",
    ):
        if not _archive_directory_exists(names, directory):
            raise CompositionError(
                f"Global {family} lacks {directory}."
            )

    prefix = archive_root + "/"
    top_level = {
        name[len(prefix):].split("/", 1)[0]
        for name in names
        if name.startswith(prefix) and len(name) > len(prefix)
    }
    platform_directories = sorted(
        name
        for name in top_level
        if re.fullmatch(
            re.escape(platform_prefix) + r"_platform_.+",
            name,
        )
        and _archive_directory_exists(
            names,
            f"{archive_root}/{name}/files",
        )
    )
    if len(platform_directories) != 1:
        raise CompositionError(
            f"Global {family} has no unique platform directory."
        )

    destination = f"engine/xdg-data/umu/{family}"
    return (
        family,
        platform_prefix,
        platform_directories[0],
        f"{destination}/var",
    )


def _scan_shared_umu_runtimes(
    collection_root: Path,
) -> tuple[SharedUmuRuntime, ...]:
    backend_records = []
    for record in _registered_global_components(
        collection_root,
        role="shared-umu-stack",
        roles=("backend", "tool"),
        kind="backend",
    ):
        _index_item, declaration, physical = record
        try:
            backend_command = _validate_global_umu_backend(
                declaration,
                physical,
            )
        except CompositionError:
            continue

        backend_records.append(
            (
                *record,
                backend_command,
            )
        )

    runtime_records = []
    seen_runtime_keys: set[tuple[str, str, str]] = set()
    for record in _registered_global_components(
        collection_root,
        role="shared-umu-runtime",
        roles=("runtime",),
        kind="runtime",
    ):
        index_item, declaration, physical = record
        try:
            runtime_data = _validate_global_steam_runtime(
                index_item,
                declaration,
                physical,
            )
        except CompositionError:
            continue
        family = runtime_data[0]
        source = str(index_item["archive_root"])
        key = (str(declaration["digest"]), source, family)
        seen_runtime_keys.add(key)
        runtime_records.append((*record, runtime_data))

    def embedded_runtime_families(
        declaration: dict[str, Any],
    ) -> tuple[str, ...]:
        raw_digest = declaration.get("digest")
        if (
            not isinstance(raw_digest, str)
            or not raw_digest.startswith("sha256:")
        ):
            return ()
        hexdigest = raw_digest.removeprefix("sha256:")
        if (
            len(hexdigest) != 64
            or any(
                char not in "0123456789abcdef"
                for char in hexdigest
            )
        ):
            return ()
        manifest = (
            collection_root
            / "01_IMMUTABLE_VAULT"
            / "manifests"
            / "sha256"
            / hexdigest[:2]
            / hexdigest[2:4]
            / hexdigest
        )
        if not manifest.is_file():
            return ()

        prefixes = {
            family: f"engine/xdg-data/umu/{family}/"
            for family in _UMU_PLATFORM_PREFIX_BY_FAMILY
        }
        found: set[str] = set()
        try:
            lines = manifest.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return ()

        for raw in lines:
            parts = raw.strip().split(None, 2)
            if len(parts) != 3:
                continue
            relative = PurePosixPath(parts[2]).as_posix()
            for family, prefix in prefixes.items():
                if relative.startswith(prefix):
                    found.add(family)
        return tuple(sorted(found))

    # A registered shared UMU stack is itself an immutable reusable
    # component. If it contains a complete Steam Runtime subtree, expose
    # that subtree to generic composition instead of requiring a second,
    # byte-duplicated CAS object. Candidate discovery uses the immutable
    # object manifest; the existing archive validator remains authoritative
    # for completeness and topology.
    for (
        _backend_index,
        backend_declaration,
        backend_physical,
        _backend_command,
    ) in backend_records:
        for family in embedded_runtime_families(backend_declaration):
            source = f"engine/xdg-data/umu/{family}"
            key = (
                str(backend_declaration["digest"]),
                source,
                family,
            )
            if key in seen_runtime_keys:
                continue

            embedded_index: dict[str, Any] = {
                "archive_root": source,
                "runtime_family": family,
                "_embedded_runtime": True,
            }
            embedded_declaration = deepcopy(backend_declaration)
            embedded_declaration["id"] = (
                f"{backend_declaration['id']}-embedded-{family}"
            )
            raw_roles = embedded_declaration.get("roles")
            roles = (
                [
                    role
                    for role in raw_roles
                    if isinstance(role, str)
                ]
                if isinstance(raw_roles, list)
                else []
            )
            if "runtime" not in roles:
                roles.append("runtime")
            embedded_declaration["roles"] = sorted(set(roles))
            embedded_declaration["description"] = (
                f"Validated {family} subtree reused from preserved shared "
                f"UMU stack {backend_declaration['id']!r}."
            )

            try:
                runtime_data = _validate_global_steam_runtime(
                    embedded_index,
                    embedded_declaration,
                    backend_physical,
                )
            except CompositionError:
                continue

            seen_runtime_keys.add(key)
            runtime_records.append(
                (
                    embedded_index,
                    embedded_declaration,
                    backend_physical,
                    runtime_data,
                )
            )

    results: list[SharedUmuRuntime] = []
    archive_policy = {
        "allow_absolute_symlinks": True,
        "allow_hardlinks": True,
    }

    for (
        _backend_index,
        backend_declaration,
        _backend_physical,
        backend_command,
    ) in backend_records:
        (
            backend_entrypoint,
            backend_entrypoint_arguments,
            backend_pythonpath,
        ) = backend_command

        backend_digest = str(
            backend_declaration["digest"]
        ).removeprefix("sha256:")

        for (
            runtime_index,
            runtime_declaration,
            _runtime_physical,
            runtime_data,
        ) in runtime_records:
            (
                family,
                platform_prefix,
                platform_directory,
                runtime_var,
            ) = runtime_data
            runtime_digest = str(
                runtime_declaration["digest"]
            ).removeprefix("sha256:")
            runtime_source = str(runtime_index["archive_root"])
            composition_identity = f"{backend_digest}:{runtime_digest}"
            if runtime_index.get("_embedded_runtime") is True:
                # The same immutable stack may contain more than one
                # independently reusable subtree. Include the source path
                # so component-set identity describes the selected bytes,
                # while preserving legacy ids for first-class runtimes.
                composition_identity += f":{runtime_source}"
            composition_digest = hashlib.sha256(
                composition_identity.encode("utf-8")
            ).hexdigest()

            results.append(
                SharedUmuRuntime(
                    component_set_id=_component_set_id(
                        composition_digest,
                        runtime_var,
                    ),
                    component_set_digest=composition_digest,
                    backend_object_id=str(
                        backend_declaration["id"]
                    ),
                    backend_object=deepcopy(
                        backend_declaration
                    ),
                    runtime_object_id=str(
                        runtime_declaration["id"]
                    ),
                    runtime_object=deepcopy(
                        runtime_declaration
                    ),
                    backend_entrypoint=backend_entrypoint,
                    backend_entrypoint_arguments=(
                        backend_entrypoint_arguments
                    ),
                    backend_pythonpath=backend_pythonpath,
                    runtime_source=runtime_source,
                    runtime_destination=(
                        f"engine/xdg-data/umu/{family}"
                    ),
                    runtime_var=runtime_var,
                    runtime_family=family,
                    platform_prefix=platform_prefix,
                    platform_directory=platform_directory,
                    backend_archive_policy=deepcopy(archive_policy),
                    runtime_archive_policy=deepcopy(archive_policy),
                    allowed_absolute_symlinks=(),
                )
            )

    return tuple(
        sorted(
            results,
            key=lambda item: (
                -_runtime_generation(item.runtime_var),
                item.component_set_id,
            ),
        )
    )
def list_shared_umu_runtimes(
    collection_root: Path,
) -> tuple[SharedUmuRuntime, ...]:
    return _scan_shared_umu_runtimes(
        collection_root.expanduser().resolve(strict=True)
    )


def _select_shared_umu_runtime(
    collection_root: Path,
    runner: RunnerRecord,
) -> SharedUmuRuntime:
    requirement = _required_umu_runtime(collection_root, runner)
    all_runtimes = _scan_shared_umu_runtimes(collection_root)
    matching = tuple(
        item
        for item in all_runtimes
        if item.runtime_family == requirement.family
        and item.platform_prefix == requirement.platform_prefix
    )
    if not matching:
        available = sorted(
            {item.runtime_family for item in all_runtimes}
        )
        suffix = (
            "; complete families available: " + ", ".join(available)
            if available
            else "; no complete global UMU composition is available"
        )
        raise CompositionError(
            f"Runner {runner.runner_id!r} requires {requirement.family} "
            f"(Steam AppID {requirement.appid}), but the Vault has no "
            f"matching global UMU component composition{suffix}."
        )
    return matching[0]
def _shell_array(values: list[str]) -> str:
    return " ".join(shlex.quote(value) for value in values)


def _generic_umu_prefix_setup(
    playable: dict[str, Any],
    prefix: str,
) -> str:
    """Render safe, idempotent Direct-Wine prefix operations for UMU.

    Generic UMU composition is derived from a Direct-Wine-capable source.
    Its layout alone is not sufficient: ``playable.prefix_operations`` may
    create required Wine-prefix topology (for example ``dosdevices/c:``) or
    link a neutral game payload into the prefix. Those operations are part
    of the source material contract and must survive backend adaptation.
    """

    raw_operations = playable.get("prefix_operations", [])
    if not isinstance(raw_operations, list):
        raise CompositionError(
            "playable.prefix_operations must be an array."
        )

    prefix_path = PurePosixPath(prefix)
    lines: list[str] = []

    def normalized_target(
        link_path: PurePosixPath,
        raw_target: Any,
        index: int,
    ) -> str:
        if (
            not isinstance(raw_target, str)
            or not raw_target
            or "\x00" in raw_target
            or "\\" in raw_target
        ):
            raise CompositionError(
                f"playable.prefix_operations[{index}].target is invalid."
            )
        target = PurePosixPath(raw_target)
        if target.is_absolute():
            raise CompositionError(
                f"playable.prefix_operations[{index}].target "
                "must be relative."
            )

        resolved_parts: list[str] = []
        for part in (*link_path.parent.parts, *target.parts):
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved_parts:
                    raise CompositionError(
                        f"playable.prefix_operations[{index}].target "
                        "escapes the materialization."
                    )
                resolved_parts.pop()
                continue
            resolved_parts.append(part)

        if not resolved_parts:
            raise CompositionError(
                f"playable.prefix_operations[{index}].target "
                "resolves to the materialization root."
            )
        return raw_target

    for index, raw_operation in enumerate(raw_operations):
        if not isinstance(raw_operation, dict):
            raise CompositionError(
                f"playable.prefix_operations[{index}] must be an object."
            )

        operation_type = raw_operation.get("type")
        operation_path = _safe_relative(
            raw_operation.get("path"),
            f"playable.prefix_operations[{index}].path",
        )
        if (
            operation_path == prefix_path
            or not operation_path.is_relative_to(prefix_path)
        ):
            raise CompositionError(
                f"playable.prefix_operations[{index}].path "
                "must remain below the declared prefix."
            )

        relative = operation_path.relative_to(prefix_path).as_posix()
        path_expression = f'"$PREFIX"/{shlex.quote(relative)}'

        if operation_type == "mkdir":
            lines.extend(
                (
                    f"OGV_PREFIX_OP_PATH={path_expression}",
                    (
                        'if [[ -L "$OGV_PREFIX_OP_PATH" '
                        '|| ( -e "$OGV_PREFIX_OP_PATH" '
                        '&& ! -d "$OGV_PREFIX_OP_PATH" ) ]]; then'
                    ),
                    (
                        "    printf 'UMU composition: prefix mkdir "
                        "collides with non-directory: %s\\n' "
                        '"$OGV_PREFIX_OP_PATH" >&2'
                    ),
                    "    exit 1",
                    "fi",
                    'mkdir -p -- "$OGV_PREFIX_OP_PATH"',
                    "",
                )
            )
            continue

        if operation_type == "symlink":
            target = normalized_target(
                operation_path,
                raw_operation.get("target"),
                index,
            )
            target_literal = shlex.quote(target)
            lines.extend(
                (
                    f"OGV_PREFIX_OP_PATH={path_expression}",
                    f"OGV_PREFIX_OP_TARGET={target_literal}",
                    'if [[ -L "$OGV_PREFIX_OP_PATH" ]]; then',
                    (
                        '    if [[ "$(readlink -- "$OGV_PREFIX_OP_PATH")" '
                        '!= "$OGV_PREFIX_OP_TARGET" ]]; then'
                    ),
                    (
                        "        printf 'UMU composition: prefix symlink "
                        "has unexpected target: %s\\n' "
                        '"$OGV_PREFIX_OP_PATH" >&2'
                    ),
                    "        exit 1",
                    "    fi",
                    'elif [[ -e "$OGV_PREFIX_OP_PATH" ]]; then',
                    (
                        "    printf 'UMU composition: prefix symlink "
                        "collides with existing path: %s\\n' "
                        '"$OGV_PREFIX_OP_PATH" >&2'
                    ),
                    "    exit 1",
                    "else",
                    '    mkdir -p -- "$(dirname -- "$OGV_PREFIX_OP_PATH")"',
                    (
                        '    ln -s -- "$OGV_PREFIX_OP_TARGET" '
                        '"$OGV_PREFIX_OP_PATH"'
                    ),
                    "fi",
                    "",
                )
            )
            continue

        raise CompositionError(
            f"playable.prefix_operations[{index}].type "
            f"{operation_type!r} is unsupported by generic UMU composition."
        )

    return "\n".join(lines)


def _generic_umu_launcher(
    *,
    capsule: dict[str, Any],
    profile: dict[str, Any],
    runner: RunnerRecord,
    runtime: SharedUmuRuntime,
) -> bytes:
    playable = profile.get("playable")
    launch = profile.get("launch")

    if (
        not isinstance(playable, dict)
        or not isinstance(launch, dict)
    ):
        raise CompositionError(
            "Direct-Wine source profile is incomplete."
        )

    paths = playable.get("paths")

    if not isinstance(paths, dict):
        raise CompositionError(
            "Direct-Wine playable paths are absent."
        )

    prefix = _safe_relative(
        paths.get("prefix"),
        "playable.paths.prefix",
    ).as_posix()
    prefix_setup = _generic_umu_prefix_setup(
        playable,
        prefix,
    )

    entrypoint = _safe_relative(
        launch.get("entrypoint"),
        "launch.entrypoint",
    ).as_posix()

    working = _safe_relative(
        launch.get(
            "working_directory",
            ".",
        ),
        "launch.working_directory",
        dot=True,
    ).as_posix()

    arguments = launch.get(
        "arguments",
        [],
    )

    if (
        not isinstance(arguments, list)
        or any(
            not isinstance(value, str)
            for value in arguments
        )
    ):
        raise CompositionError(
            "launch.arguments must contain strings."
        )

    game = capsule.get("game")

    if not isinstance(game, dict):
        game = {}

    appid = game.get("appid")

    game_id = (
        str(appid)
        if isinstance(appid, int)
        and appid >= 0
        else "0"
    )

    source_store = game.get("source_store")

    store = (
        "steam"
        if isinstance(source_store, str)
        and source_store.casefold() == "steam"
        else "none"
    )

    proton_relative = (
        f"engine/proton/{runner.source_root}"
    )

    backend_entrypoint = _safe_relative(
        runtime.backend_entrypoint,
        "UMU backend entrypoint",
    ).as_posix()

    backend_arguments = _shell_array(
        list(
            runtime.backend_entrypoint_arguments
        )
    )

    backend_suffix = (
        f" {backend_arguments}"
        if backend_arguments
        else ""
    )

    pythonpath_line = ""

    if runtime.backend_pythonpath is not None:
        backend_pythonpath = _safe_relative(
            runtime.backend_pythonpath,
            "UMU backend PYTHONPATH",
        ).as_posix()

        pythonpath_line = (
            'export PYTHONPATH='
            f'"$ROOT/{backend_pythonpath}"\n'
        )

    fixed = _shell_array(arguments)

    game_suffix = (
        f" {fixed}"
        if fixed
        else ""
    )

    script = f"""#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")/.." && pwd -P)"
PREFIX="$ROOT/{prefix}"
PROTON="$ROOT/{proton_relative}"
XDG_DATA="$ROOT/engine/xdg-data"
EXE="$ROOT/{entrypoint}"
WORKDIR="$ROOT/{working}"
UMU_ENTRYPOINT="$ROOT/{backend_entrypoint}"

for required in "$PREFIX" "$PROTON" "$XDG_DATA" "$WORKDIR"; do
    [[ -d "$required" ]] || {{
        printf 'UMU composition: missing directory: %s\n' "$required" >&2
        exit 1
    }}
done

{prefix_setup}
[[ -f "$EXE" ]] || {{
    printf 'UMU composition: missing game entrypoint: %s\n' "$EXE" >&2
    exit 1
}}

[[ -f "$UMU_ENTRYPOINT" && -x "$UMU_ENTRYPOINT" ]] || {{
    printf 'UMU composition: missing executable backend entrypoint: %s\n' "$UMU_ENTRYPOINT" >&2
    exit 1
}}

{pythonpath_line}export WINEPREFIX="$PREFIX"
export PROTONPATH="$PROTON"
export XDG_DATA_HOME="$XDG_DATA"
export XDG_CACHE_HOME="$ROOT/engine/xdg-cache"
export UMU_RUNTIME_UPDATE=0
export GAMEID={shlex.quote(game_id)}
export STORE={shlex.quote(store)}

cd -- "$WORKDIR"

exec "$UMU_ENTRYPOINT"{backend_suffix} "$EXE"{game_suffix} "$@"
"""

    return script.encode("utf-8")
def _generic_umu_sanitizer(
    runtime_var: str,
    runner: RunnerRecord,
) -> bytes:
    runtime = _safe_relative(
        runtime_var,
        "runtime_var",
    ).as_posix()

    proton = (
        f"engine/proton/{runner.source_root}"
    )

    return f"""#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")/.." && pwd -P)"
RUNTIME_VAR="$ROOT/{runtime}"
PROTON_ROOT="$ROOT/{proton}"

[[ -d "$RUNTIME_VAR" && ! -L "$RUNTIME_VAR" ]] || {{
    printf 'UMU composition: runtime var is absent or linked: %s\n' "$RUNTIME_VAR" >&2
    exit 1
}}

# This is a known Proton-generated marker. Unknown archived runtime data is
# retained because its regenerability has not been demonstrated.
rm -f -- "$PROTON_ROOT/files/steampipe_fixups_mtime"
""".encode("utf-8")
def _write_asset(root: Path, relative: str, payload: bytes, mode: int) -> dict[str, Any]:
    path = root.joinpath(*_safe_relative(relative, "generated asset").parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)
    return {
        "source": relative,
        "destination": relative,
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "mode": mode,
    }


def _umu_overlay(
    *,
    source_capsule_path: Path,
    source_profile_id: str,
    runner: RunnerRecord,
    runtime: SharedUmuRuntime,
    output: Path,
) -> tuple[Path, str]:
    try:
        wine_derived = build_derived_capsule(
            source_capsule_path,
            source_profile_id,
            runner,
        )
    except RunnerOverrideError as exc:
        raise CompositionError(str(exc)) from exc

    capsule = deepcopy(wine_derived.document)
    objects = capsule.get("objects")
    profiles = capsule.get("profiles")
    if not isinstance(objects, list) or not isinstance(profiles, list):
        raise CompositionError("Derived capsule is incomplete.")

    matches = [
        item
        for item in profiles
        if isinstance(item, dict)
        and item.get("id") == source_profile_id
    ]
    if len(matches) != 1:
        raise CompositionError("Derived Wine profile is absent.")

    source = matches[0]
    playable = source.get("playable")
    dependencies = source.get("dependencies")
    if not isinstance(playable, dict) or not isinstance(dependencies, list):
        raise CompositionError(
            "Derived Wine profile has no playable layout."
        )

    layout = playable.get("layout")
    if not isinstance(layout, list) or not layout:
        raise CompositionError("Derived Wine layout is absent.")

    playable_paths = playable.get("paths")
    if not isinstance(playable_paths, dict):
        raise CompositionError(
            "Derived Wine playable paths are absent."
        )
    prefix_path = playable_paths.get("prefix")
    if not isinstance(prefix_path, str) or not prefix_path:
        raise CompositionError(
            "Derived Wine prefix path is absent."
        )

    object_index = {
        item.get("id"): item
        for item in objects
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    runner_ids = {
        dependency
        for dependency in dependencies
        if isinstance(dependency, str)
        and isinstance(object_index.get(dependency), dict)
        and "runner" in object_index[dependency].get("roles", [])
    }
    if len(runner_ids) != 1:
        raise CompositionError(
            "Derived Wine profile has no unique runner."
        )

    selected_runner_object = next(iter(runner_ids))
    remaining_dependencies = [
        value for value in dependencies if value not in runner_ids
    ]
    source_layout = [
        deepcopy(item)
        for item in layout
        if isinstance(item, dict)
        and item.get("object") not in runner_ids
    ]
    if {
        item.get("object")
        for item in source_layout
        if isinstance(item, dict)
    } != set(remaining_dependencies):
        raise CompositionError(
            "Derived Wine layout does not map all non-runner dependencies."
        )

    existing_ids = set(object_index)
    backend_object_id = _unique_id(
        existing_ids,
        runtime.backend_object_id,
    )
    existing_ids.add(backend_object_id)
    runtime_object_id = _unique_id(
        existing_ids,
        runtime.runtime_object_id,
    )

    backend_object = deepcopy(runtime.backend_object)
    backend_object["id"] = backend_object_id
    backend_object["description"] = (
        "Preserved global UMU and Python component."
    )
    runtime_object = deepcopy(runtime.runtime_object)
    runtime_object["id"] = runtime_object_id
    runtime_object["description"] = (
        f"Preserved global {runtime.runtime_family} component."
    )
    objects.extend((backend_object, runtime_object))

    output.mkdir(parents=True, mode=0o700)
    launcher_item = _write_asset(
        output,
        "launchers/JUGAR_UMU.sh",
        _generic_umu_launcher(
            capsule=capsule,
            profile=source,
            runner=runner,
            runtime=runtime,
        ),
        0o755,
    )
    sanitizer_item = _write_asset(
        output,
        "launchers/sanear_umu.sh",
        _generic_umu_sanitizer(runtime.runtime_var, runner),
        0o755,
    )

    profile_id = _composition_profile_id("umu", runner)
    source["id"] = profile_id
    source["adapter"] = "umu"
    source["platform"] = "linux"
    source["dependencies"] = [
        *remaining_dependencies,
        backend_object_id,
        runtime_object_id,
        selected_runner_object,
    ]
    source.pop("playable", None)

    host_contract_relative = "host-contracts/umu-composition.json"
    host_contract_path = output / host_contract_relative
    host_contract_path.parent.mkdir(parents=True, exist_ok=True)
    host_contract_path.write_bytes(
        _canonical_bytes(
            {
                "schema": 0,
                "contract": "ogv-umu-composition-v1",
                "source_profile_id": source_profile_id,
                "backend_component_id": runtime.backend_object_id,
                "runtime_component_id": runtime.runtime_object_id,
                "component_set_id": runtime.component_set_id,
                "runner_id": runner.runner_id,
            }
        )
    )
    source["host_contract"] = host_contract_relative

    launch = source.get("launch")
    if not isinstance(launch, dict):
        launch = {}
    launch["network"] = "isolated"
    source["launch"] = launch

    source["umu"] = {
        "schema": 0,
        "layout": [
            *source_layout,
            {
                "object": backend_object_id,
                "source": "engine/umu-portable",
                "destination": "engine/umu-portable",
                "archive_policy": deepcopy(
                    runtime.backend_archive_policy
                ),
            },
            {
                "object": backend_object_id,
                "source": "engine/python-portable",
                "destination": "engine/python-portable",
                "archive_policy": deepcopy(
                    runtime.backend_archive_policy
                ),
            },
            {
                "object": runtime_object_id,
                "source": runtime.runtime_source,
                "destination": runtime.runtime_destination,
                "archive_policy": deepcopy(
                    runtime.runtime_archive_policy
                ),
            },
            {
                "object": selected_runner_object,
                "source": runner.source_root,
                "destination": f"engine/proton/{runner.source_root}",
            },
        ],
        "allowed_absolute_symlinks": [
            deepcopy(item)
            for item in runtime.allowed_absolute_symlinks
        ],
        "nested_archives": [],
        "state_archives": [],
        "launchers": [launcher_item, sanitizer_item],
        "protected_manifests": [],
        "symlink_manifests": [],
        "mutable_paths": [
            f"engine/proton/{runner.source_root}/"
            "files/steampipe_fixups_mtime"
        ],
        "paths": {
            "launcher": launcher_item["destination"],
            "sanitizer": sanitizer_item["destination"],
            "runtime_var": runtime.runtime_var,
            "prefix": prefix_path,
        },
    }
    source["notes"] = (
        "User-requested UMU composition generated from global backend "
        f"{runtime.backend_object_id!r}, global runtime "
        f"{runtime.runtime_object_id!r} and runner {runner.runner_id!r}."
    )

    capsule_path = output / "capsule.json"
    capsule_path.write_bytes(_canonical_bytes(capsule))
    return capsule_path, profile_id
def _derive_state_root_for_umu_native(
    collection_root: Path,
    capsule_doc: dict[str, Any],
    profile_id: str,
) -> Path | None:
    """Return the canonical state_root for a umu-native capsule.

    Convention: preserved ``umu.state_archives`` tarballs live at
    ``<collection_root>/03_PERSISTENT_STATE/<capsule_id>/
    <profile_id>/active/``. The nested ``<profile_id>/`` segment
    accommodates capsules that ship more than one UMU profile
    (each with its own preserved tarballs), and the ``active/``
    leaf separates the operative tarballs from the paralell
    ``related-artifacts/`` subtree that carries preserved evidence
    which is intentionally NOT consumed during materialisation.

    When the directory exists it is returned; when it does not,
    ``None`` is returned so ``materialize_umu_profile`` surfaces
    its own error naming the concrete argument the user must
    supply via ``--state-root``.
    """
    capsule_id = capsule_doc.get("capsule_id")
    if not isinstance(capsule_id, str) or not capsule_id:
        return None
    if not isinstance(profile_id, str) or not profile_id:
        return None
    candidate = (
        collection_root
        / "03_PERSISTENT_STATE"
        / capsule_id
        / profile_id
        / "active"
    )
    return candidate if candidate.is_dir() else None


def _always_state_archive_ids(profile: dict[str, Any]) -> list[str]:
    """Return the ids of umu.state_archives entries with policy 'always'.

    Used by compose_umu to report exactly which archives --no-state is
    skipping when materializing an umu-native capsule cold. Malformed
    entries are ignored here on purpose: materialize_umu_profile owns
    the strict validation and will surface the real error message if
    the caller ever runs the same compose without --no-state. This
    helper is purely informative.
    """
    umu = profile.get("umu")
    if not isinstance(umu, dict):
        return []
    entries = umu.get("state_archives")
    if not isinstance(entries, list):
        return []
    ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("policy") != "always":
            continue
        item_id = entry.get("id")
        if isinstance(item_id, str) and item_id:
            ids.append(item_id)
    return ids


def compose_umu(
    *,
    collection_root: Path,
    capsule_path: Path,
    runner_id: str,
    destination: Path,
    source_profile_id: str | None = None,
    state_backup: Path | None = None,
    state_root: Path | None = None,
    save_id: str | None = None,
    fresh_start: bool = False,
    no_state: bool = False,
    play: bool = False,
    arguments: Sequence[str] = (),
) -> CompositionResult:
    _validate_fresh_start_intent(
        fresh_start=fresh_start,
        no_state=no_state,
        state_backup=state_backup,
        save_id=save_id,
    )
    collection_root = collection_root.expanduser().resolve(strict=True)
    vault_root = collection_root / "01_IMMUTABLE_VAULT"
    # Runner selection is a validation (must exist and declare umu
    # compatibility), regardless of source kind.
    runner = _select_runner(collection_root, runner_id, "umu")
    source_id = _select_source_profile(
        capsule_path,
        backend="umu",
        profile_id=source_profile_id,
        runner=runner,
    )

    # Determine source kind before touching shared runtime resolution.
    # A preserved umu-native profile carries its own runtime + backend
    # references inside the capsule and does NOT need a matching global
    # UMU component composition; forcing that check up front would
    # block valid umu-native materializations (e.g. DMC5 with a
    # preserved Proton whose steamrt family is not otherwise present).
    source_capsule_doc = _load_json(capsule_path, "capsule.json")
    try:
        source_profile_doc = next(
            profile
            for profile in _linux_profiles(source_capsule_doc)
            if profile.get("id") == source_id
        )
    except StopIteration as exc:
        raise CompositionError(
            f"Selected source profile {source_id!r} vanished between "
            "selection and lookup."
        ) from exc
    source_kind = _source_kind(capsule_path, source_profile_doc)
    effective_no_state = _effective_materializer_no_state(
        source_kind=source_kind or "",
        fresh_start=fresh_start,
        no_state=no_state,
    )

    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".ogv-control-umu-",
        dir=destination.parent,
    ) as temporary:
        if source_kind == "umu-native":
            # Preserved rich contract; no overlay. The capsule itself
            # is the operational capsule and provisions its own state
            # from umu.state_archives, whose tarballs live in the
            # convention path 03_PERSISTENT_STATE/<capsule_id>/
            # unless the caller overrides via --state-root.
            operational_capsule = capsule_path
            profile_id = source_id
            require_state_backup = False
            runtime = None
            # --no-state on umu-native: enumerate the always-policy
            # state archives that would otherwise be applied so the
            # user sees exactly what is being skipped, and clear
            # state_root so materialization does not try to read them.
            # Consistent with the project's honest-failure philosophy:
            # the skip is loud, not silent.
            skipped_state_archives: list[str] = []
            if no_state:
                skipped_state_archives = _always_state_archive_ids(
                    source_profile_doc
                )
                if skipped_state_archives:
                    sys.stderr.write(
                        "warning: capsule declares always-policy state "
                        "archives (" + ", ".join(skipped_state_archives)
                        + "); --no-state will skip them. The "
                        "materialization may not launch cleanly.\n"
                    )
                state_root = None
            elif state_root is None:
                state_root = _derive_state_root_for_umu_native(
                    collection_root,
                    source_capsule_doc,
                    source_id,
                )
        else:
            if state_root is not None or save_id is not None:
                raise CompositionError(
                    "--state-root and --save-id apply only to "
                    "capsules with a preserved UMU rich contract; "
                    "the selected source profile synthesizes its "
                    "contract from global components and has no "
                    "state_archives to place."
                )
            runtime = _select_shared_umu_runtime(
                collection_root,
                runner,
            )
            operational_capsule, profile_id = _umu_overlay(
                source_capsule_path=capsule_path,
                source_profile_id=source_id,
                runner=runner,
                runtime=runtime,
                output=Path(temporary) / "capsule",
            )
            require_state_backup = not effective_no_state
            # `skipped_state_archives` is meaningful only for umu-native
            # (synthesized UMU has no preserved state_archives). Keep it
            # defined for the shared backend_result assembly below.
            skipped_state_archives = []
        digests = _capsule_object_digests(operational_capsule)
        try:
            validate_manifests_present_for(
                vault_root=vault_root, digests=digests
            )
        except ManifestTravelError as exc:
            raise CompositionError(str(exc)) from exc
        result = materialize_umu_profile(
            capsule_path=operational_capsule,
            profile_id=profile_id,
            vault_root=vault_root,
            destination=destination,
            state_backup=(
                None if effective_no_state or fresh_start else state_backup
            ),
            state_root=state_root,
            save_id=save_id,
            require_state_backup=require_state_backup,
            state_capsule_path=capsule_path,
            no_state=effective_no_state,
        )
        try:
            copied_manifests = copy_manifests_to_materialization(
                vault_root=vault_root,
                destination=destination,
                digests=digests,
            )
            receipt_path = destination / _UMU_RECEIPT_NAME
            write_generated_files_manifest(
                destination=destination,
                object_manifest_paths=copied_manifests,
                excluded_paths={
                    receipt_path.with_name(receipt_path.name + ".sha256")
                },
            )
            write_receipt_sidecar(receipt_path)
        except (ManifestTravelError, OSError) as exc:
            _rollback_destination(destination)
            raise CompositionError(
                "Materialization was published but manifest travel "
                f"failed: {exc}. The destination was removed to "
                "preserve the invariant that a successful compose "
                "produces a self-verifiable tree."
            ) from exc
        played = False
        play_complete: bool | None = None
        play_result: dict[str, Any] | None = None
        if play:
            run_result = run_umu_materialization(
                destination=destination,
                arguments=arguments,
            )
            play_result = asdict(run_result)
            played = True
            play_complete = run_result.complete

    capsule = _load_json(capsule_path, "capsule.json")
    return CompositionResult(
        schema=0,
        capsule_id=str(capsule["capsule_id"]),
        backend="umu",
        runner_id=runner.runner_id,
        profile_id=profile_id,
        destination=str(destination),
        materialized=True,
        played=played,
        play_complete=play_complete,
        backend_result={
            "materialization": asdict(result),
            "component_set_id": (
                runtime.component_set_id if runtime else None
            ),
            "backend_component_id": (
                runtime.backend_object_id if runtime else None
            ),
            "runtime_component_id": (
                runtime.runtime_object_id if runtime else None
            ),
            "backend_entrypoint": (
                runtime.backend_entrypoint if runtime else None
            ),
            "source_kind": source_kind,
            "state_provisioned": not effective_no_state,
            "fresh_start": fresh_start,
            "skipped_state_archives": skipped_state_archives,
            "play": play_result,
        },
    )
