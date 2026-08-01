'''Experimental, user-requested backend variants.

Contracts and acceptance reports describe known-good combinations. They do not
authorize materialization. This module synthesizes a private operational
profile from preserved Vault objects while retaining integrity and path-safety.
'''

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import tempfile
from typing import Any, Sequence

from . import __version__
from .bottles_adapter import deploy_bottles_profile, run_bottles_deployment
from .experimental_profile import (
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
from .umu_adapter import materialize_umu_profile, run_umu_materialization


class ExperimentalVariantError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UmuBackendTemplate:
    capsule_id: str
    capsule_path: Path
    profile_id: str
    composite_object_id: str
    composite_object: dict[str, Any]
    runtime_var: str

    @property
    def template_id(self) -> str:
        return f"{self.capsule_id}/{self.profile_id}"


@dataclass(frozen=True, slots=True)
class ExperimentalVariantResult:
    schema: int
    capsule_id: str
    backend: str
    runner_id: str
    profile_id: str
    destination: str
    materialized: bool
    played: bool
    play_complete: bool | None
    acceptance_inherited: bool
    backend_result: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_BACKEND_PARTITIONS = (
    "engine/python-portable",
    "engine/umu-portable",
    "engine/xdg-data",
)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ExperimentalVariantError(f"{label} must be a regular file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentalVariantError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ExperimentalVariantError(f"{label} must contain a JSON object.")
    return value


def _safe_relative(value: Any, label: str, *, dot: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ExperimentalVariantError(f"{label} is not a safe relative path.")
    if dot and value == ".":
        return PurePosixPath(".")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ExperimentalVariantError(f"{label} is not a safe relative path.")
    return path


def _portable_id(value: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("._-")
    if not value:
        raise ExperimentalVariantError("Cannot derive a portable profile ID.")
    if not value[0].isalnum():
        value = "variant-" + value
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
        raise ExperimentalVariantError(str(exc)) from exc
    matches = [runner for runner in runners if runner.runner_id == runner_id]
    if len(matches) != 1:
        raise ExperimentalVariantError(
            f"Preserved runner {runner_id!r} does not exist exactly once."
        )
    runner = matches[0]
    if not runner.supports(backend):
        raise ExperimentalVariantError(
            f"Runner {runner_id!r} is not structurally compatible with {backend}."
        )
    return runner


_NEUTRAL_CONTRACTS = {
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
    },
    "bottles": {
        "ogv-bottles-neutral-v1": 0,
        "ogv-direct-wine-neutral-v1": 1,
        "ogv-umu-neutral-v1": 2,
    },
    "umu": {
        "ogv-umu-neutral-v1": 0,
        "ogv-direct-wine-neutral-v1": 1,
        "ogv-bottles-neutral-v1": 2,
        "playable-wine": 3,
    },
}


def _linux_profiles(capsule: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = capsule.get("profiles")
    if not isinstance(profiles, list):
        raise ExperimentalVariantError("capsule.profiles must be an array.")
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
            raise ExperimentalVariantError("host_contract traverses a symlink.")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ExperimentalVariantError("host_contract escapes the capsule.") from exc
    return _load_json(resolved, "host contract")


def _source_kind(
    capsule_path: Path,
    profile: dict[str, Any],
) -> str | None:
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
    except ExperimentalVariantError:
        return None
    name = contract.get("contract")
    return name if name in _NEUTRAL_CONTRACTS else None


def _select_source_profile(
    capsule_path: Path,
    *,
    backend: str,
    profile_id: str | None,
) -> str:
    priorities = _SOURCE_PRIORITIES.get(backend)
    if priorities is None:
        raise ExperimentalVariantError(f"Unknown experimental backend: {backend}.")

    capsule = _load_json(capsule_path, "capsule.json")
    candidates: list[tuple[int, str, str]] = []
    known_ids: set[str] = set()
    for profile in _linux_profiles(capsule):
        identifier = profile.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        known_ids.add(identifier)
        if profile_id is not None and identifier != profile_id:
            continue
        kind = _source_kind(capsule_path, profile)
        if kind in priorities:
            candidates.append((priorities[kind], identifier, kind))

    if profile_id is not None and profile_id not in known_ids:
        raise ExperimentalVariantError(
            f"Source profile {profile_id!r} does not exist."
        )
    if not candidates:
        selected = f" {profile_id!r}" if profile_id is not None else ""
        raise ExperimentalVariantError(
            f"Linux source profile{selected} cannot provide the material "
            f"required by {backend}."
        )

    best_priority = min(item[0] for item in candidates)
    best = [item for item in candidates if item[0] == best_priority]
    if len(best) != 1:
        labels = ", ".join(f"{identifier} ({kind})" for _, identifier, kind in best)
        raise ExperimentalVariantError(
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
        raise ExperimentalVariantError("Derived capsule has no profiles array.")
    matches = [
        item for item in profiles
        if isinstance(item, dict) and item.get("id") == source_profile_id
    ]
    if len(matches) != 1:
        raise ExperimentalVariantError("Derived source profile is absent.")
    profile = matches[0]
    profile["id"] = profile_id
    profile["status"] = "experimental"
    profile.pop("acceptance_report", None)
    profile["variant"] = {
        "kind": "experimental",
        "source_profile_id": source_profile_id,
        "backend": backend,
        "runner_id": runner.runner_id,
        "acceptance_inherited": False,
    }
    profile["notes"] = (
        f"User-requested experimental {backend} variant generated by "
        f"Offline Game Vault {__version__}. It uses only preserved Vault "
        "objects and inherits no functional acceptance."
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
            raise ExperimentalVariantError("Companion file failed verification.")
    return capsule_path


def _variant_profile_id(backend: str, runner: RunnerRecord) -> str:
    label = {"direct-wine": "wine", "bottles": "bottles", "umu": "umu"}[backend]
    return _portable_id(f"experimental-{label}-{runner.runner_id}")


def materialize_experimental_wine(
    *,
    collection_root: Path,
    capsule_path: Path,
    runner_id: str,
    destination: Path,
    source_profile_id: str | None = None,
    state_backup: Path | None = None,
    play: bool = False,
    arguments: Sequence[str] = (),
) -> ExperimentalVariantResult:
    collection_root = collection_root.expanduser().resolve(strict=True)
    vault_root = collection_root / "01_IMMUTABLE_VAULT"
    runner = _select_runner(collection_root, runner_id, "direct-wine")
    source_id = _select_source_profile(
        capsule_path,
        backend="direct-wine",
        profile_id=source_profile_id,
    )
    try:
        derived = build_derived_capsule(capsule_path, source_id, runner)
    except RunnerOverrideError as exc:
        raise ExperimentalVariantError(str(exc)) from exc
    profile_id = _variant_profile_id("direct-wine", runner)

    with tempfile.TemporaryDirectory(prefix="ogv-experimental-wine-") as temporary:
        operational_capsule = _write_overlay(
            Path(temporary) / "capsule",
            derived,
            profile_id=profile_id,
            backend="direct-wine",
            runner=runner,
            source_profile_id=source_id,
        )
        result = materialize_playable_profile(
            capsule_path=operational_capsule,
            profile_id=profile_id,
            vault_root=vault_root,
            destination=destination,
            state_backup=state_backup,
        )
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
    return ExperimentalVariantResult(
        schema=0,
        capsule_id=str(capsule["capsule_id"]),
        backend="direct-wine",
        runner_id=runner.runner_id,
        profile_id=profile_id,
        destination=str(destination),
        materialized=True,
        played=played,
        play_complete=play_complete,
        acceptance_inherited=False,
        backend_result={"materialization": asdict(result), "play": play_result},
    )


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
        raise ExperimentalVariantError("Capsule is incomplete.")
    matches = [
        item for item in profiles
        if isinstance(item, dict) and item.get("id") == source_profile_id
    ]
    if len(matches) != 1:
        raise ExperimentalVariantError("Bottles source profile is absent.")
    source = matches[0]
    contract = _read_contract(capsule_path, source)
    contract_name = contract.get("contract")
    if contract_name not in _NEUTRAL_CONTRACTS:
        raise ExperimentalVariantError(
            "Bottles requires a compatible neutral Linux source contract."
        )

    required_fields = (
        "source_object",
        "neutral_root",
        "prefix_source",
        "game_source",
        "game_destination_in_prefix",
        "entrypoint_relative_to_game",
        "working_directory_in_prefix",
    )
    missing = [
        name for name in required_fields
        if not isinstance(contract.get(name), str) or not contract.get(name)
    ]
    if missing:
        raise ExperimentalVariantError(
            "Neutral source contract is incomplete for Bottles: "
            + ", ".join(missing)
        )

    source_object = str(contract["source_object"])
    declarations = [
        item for item in objects
        if isinstance(item, dict) and item.get("id") == source_object
    ]
    if len(declarations) != 1:
        raise ExperimentalVariantError("Bottles source object is not unique.")
    roles = declarations[0].get("roles")
    if not isinstance(roles, list):
        raise ExperimentalVariantError("Bottles source object has no roles.")
    if "prefix_baseline" not in roles:
        roles.append("prefix_baseline")

    profile_id = _variant_profile_id("bottles", runner)
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
        "runner_binding": "selected-experimental",
        "preferred_runner": runner.runner_id,
        "flatpak_app": "com.usebottles.bottles",
        "bottle_yml_policy": "generate-derived",
        "network": "isolated",
    }
    generated_contract_path = "host-contracts/experimental-bottles.json"

    source.clear()
    source.update(
        {
            "id": profile_id,
            "platform": "linux",
            "adapter": "bottles",
            "status": "experimental",
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
            "variant": {
                "kind": "experimental",
                "source_profile_id": source_profile_id,
                "backend": "bottles",
                "runner_id": runner.runner_id,
                "acceptance_inherited": False,
            },
            "notes": (
                "User-requested experimental Bottles variant generated by "
                f"Offline Game Vault {__version__}; no acceptance is inherited."
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

def materialize_experimental_bottles(
    *,
    collection_root: Path,
    capsule_path: Path,
    runner_id: str,
    bottles_path: Path,
    bottle_name: str,
    source_profile_id: str | None = None,
    play: bool = False,
) -> ExperimentalVariantResult:
    collection_root = collection_root.expanduser().resolve(strict=True)
    vault_root = collection_root / "01_IMMUTABLE_VAULT"
    runner = _select_runner(collection_root, runner_id, "bottles")
    source_id = _select_source_profile(
        capsule_path,
        backend="bottles",
        profile_id=source_profile_id,
    )
    capsule = _load_json(capsule_path, "capsule.json")

    deployment = None
    runner_created = False
    play_result = None
    played = False
    play_complete: bool | None = None
    with tempfile.TemporaryDirectory(prefix="ogv-experimental-bottles-") as temporary:
        root = Path(temporary)
        operational_capsule, profile_id = _bottles_overlay(
            capsule_path=capsule_path,
            source_profile_id=source_id,
            runner=runner,
            destination=root / "capsule",
        )
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
            )
            validate_neutral_bottles_source(
                materialization=raw,
                capsule_path=operational_capsule,
                profile_id=profile_id,
            )
            try:
                _runner_path, runner_created = ensure_bottles_runner(
                    collection_root,
                    bottles_path,
                    runner,
                )
            except (RunnerCatalogError, RunnerDeploymentError) as exc:
                raise ExperimentalVariantError(str(exc)) from exc
            deployment = deploy_bottles_profile(
                capsule_path=operational_capsule,
                profile_id=profile_id,
                materialization=raw,
                bottles_path=bottles_path,
                bottle_name=bottle_name,
            )
            if play:
                launch_plan, returncode = run_bottles_deployment(
                    bottles_path=bottles_path,
                    bottle_name=bottle_name,
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
    return ExperimentalVariantResult(
        schema=0,
        capsule_id=str(capsule["capsule_id"]),
        backend="bottles",
        runner_id=runner.runner_id,
        profile_id=profile_id,
        destination=str(Path(bottles_path) / bottle_name),
        materialized=True,
        played=played,
        play_complete=play_complete,
        acceptance_inherited=False,
        backend_result={
            "deployment": asdict(deployment),
            "runner_installed": runner_created,
            "play": play_result,
        },
    )


def _scan_umu_templates(collection_root: Path) -> tuple[UmuBackendTemplate, ...]:
    result: list[UmuBackendTemplate] = []
    capsules_root = collection_root / "02_CAPSULES"
    for capsule_path in sorted(capsules_root.glob("*/capsule.json")):
        try:
            capsule = _load_json(capsule_path, str(capsule_path))
        except ExperimentalVariantError:
            continue
        capsule_id = capsule.get("capsule_id")
        objects = capsule.get("objects")
        profiles = capsule.get("profiles")
        if (
            not isinstance(capsule_id, str)
            or not isinstance(objects, list)
            or not isinstance(profiles, list)
        ):
            continue
        object_index = {
            item.get("id"): item for item in objects
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for profile in profiles:
            if (
                not isinstance(profile, dict)
                or profile.get("adapter") != "umu"
                or profile.get("platform") != "linux"
            ):
                continue
            contract = profile.get("umu")
            if not isinstance(contract, dict):
                continue
            layout = contract.get("layout")
            paths = contract.get("paths")
            dependencies = profile.get("dependencies")
            profile_id = profile.get("id")
            if (
                not isinstance(layout, list)
                or not isinstance(paths, dict)
                or not isinstance(dependencies, list)
                or not isinstance(profile_id, str)
            ):
                continue
            mappings = [
                item for item in layout
                if isinstance(item, dict)
                and item.get("source") == "engine"
                and item.get("destination") == "engine"
                and isinstance(item.get("object"), str)
            ]
            if len(mappings) != 1:
                continue
            object_id = mappings[0]["object"]
            declaration = object_index.get(object_id)
            runtime_var = paths.get("runtime_var")
            if (
                object_id not in dependencies
                or not isinstance(declaration, dict)
                or not isinstance(runtime_var, str)
            ):
                continue
            result.append(
                UmuBackendTemplate(
                    capsule_id=capsule_id,
                    capsule_path=capsule_path.resolve(strict=True),
                    profile_id=profile_id,
                    composite_object_id=object_id,
                    composite_object=deepcopy(declaration),
                    runtime_var=_safe_relative(
                        runtime_var, "umu.paths.runtime_var"
                    ).as_posix(),
                )
            )
    return tuple(result)


def list_umu_templates(collection_root: Path) -> tuple[UmuBackendTemplate, ...]:
    return _scan_umu_templates(collection_root.expanduser().resolve(strict=True))


def _select_umu_template(
    collection_root: Path,
    template_id: str | None,
) -> UmuBackendTemplate:
    templates = _scan_umu_templates(collection_root)
    if template_id is not None:
        templates = tuple(
            item for item in templates if item.template_id == template_id
        )
    if len(templates) != 1:
        labels = ", ".join(item.template_id for item in templates) or "none"
        raise ExperimentalVariantError(
            "Exactly one preserved UMU backend template must be selected; "
            f"available matches: {labels}."
        )
    return templates[0]


def _shell_array(values: list[str]) -> str:
    return " ".join(shlex.quote(value) for value in values)


def _generic_umu_launcher(
    *,
    capsule: dict[str, Any],
    profile: dict[str, Any],
    runner: RunnerRecord,
) -> bytes:
    playable = profile.get("playable")
    launch = profile.get("launch")
    if not isinstance(playable, dict) or not isinstance(launch, dict):
        raise ExperimentalVariantError("Direct-Wine source profile is incomplete.")
    paths = playable.get("paths")
    if not isinstance(paths, dict):
        raise ExperimentalVariantError("Direct-Wine playable paths are absent.")
    prefix = _safe_relative(paths.get("prefix"), "playable.paths.prefix").as_posix()
    entrypoint = _safe_relative(
        launch.get("entrypoint"), "launch.entrypoint"
    ).as_posix()
    working = _safe_relative(
        launch.get("working_directory", "."),
        "launch.working_directory",
        dot=True,
    ).as_posix()
    arguments = launch.get("arguments", [])
    if not isinstance(arguments, list) or any(not isinstance(v, str) for v in arguments):
        raise ExperimentalVariantError("launch.arguments must be strings.")

    game = capsule.get("game")
    if not isinstance(game, dict):
        game = {}
    appid = game.get("appid")
    game_id = str(appid) if isinstance(appid, int) and appid >= 0 else "0"
    source_store = game.get("source_store")
    store = (
        "steam"
        if isinstance(source_store, str) and source_store.casefold() == "steam"
        else "none"
    )
    proton_relative = f"engine/proton/{runner.source_root}"
    fixed = _shell_array(arguments)
    suffix = f" {fixed}" if fixed else ""

    script = f'''#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")/.." && pwd -P)"
PREFIX="$ROOT/{prefix}"
PROTON="$ROOT/{proton_relative}"
XDG_DATA="$ROOT/engine/xdg-data"
EXE="$ROOT/{entrypoint}"
WORKDIR="$ROOT/{working}"

for required in "$PREFIX" "$PROTON" "$XDG_DATA" "$WORKDIR"; do
    [[ -d "$required" ]] || {{
        printf 'UMU experimental variant: missing directory: %s\\n' "$required" >&2
        exit 1
    }}
done
[[ -f "$EXE" ]] || {{
    printf 'UMU experimental variant: missing entrypoint: %s\\n' "$EXE" >&2
    exit 1
}}

UMU_COMMAND=()
mapfile -t UMU_BINARIES < <(
    find "$ROOT/engine/umu-portable" -type f -name 'umu-run' -perm -u+x -print 2>/dev/null
)
if (( ${{#UMU_BINARIES[@]}} == 1 )); then
    UMU_COMMAND=("${{UMU_BINARIES[0]}}")
else
    mapfile -t UMU_PACKAGES < <(
        find "$ROOT/engine/umu-portable" -type f -path '*/umu_run/__main__.py' -print 2>/dev/null
    )
    mapfile -t PYTHON_BINARIES < <(
        find "$ROOT/engine/python-portable" -type f -name 'python3' -perm -u+x -print 2>/dev/null
    )
    if (( ${{#UMU_PACKAGES[@]}} == 1 && ${{#PYTHON_BINARIES[@]}} == 1 )); then
        UMU_PACKAGE_ROOT="$(dirname -- "$(dirname -- "${{UMU_PACKAGES[0]}}")")"
        export PYTHONPATH="$UMU_PACKAGE_ROOT"
        UMU_COMMAND=("${{PYTHON_BINARIES[0]}}" -m umu_run)
    else
        printf '%s\\n' 'Preserved UMU/Python entrypoint is absent or ambiguous.' >&2
        exit 1
    fi
fi

export WINEPREFIX="$PREFIX"
export PROTONPATH="$PROTON"
export XDG_DATA_HOME="$XDG_DATA"
export GAMEID={shlex.quote(game_id)}
export STORE={shlex.quote(store)}
cd -- "$WORKDIR"
exec "${{UMU_COMMAND[@]}}" "$EXE"{suffix} "$@"
'''
    return script.encode("utf-8")


def _generic_umu_sanitizer(runtime_var: str, runner: RunnerRecord) -> bytes:
    runtime = _safe_relative(runtime_var, "runtime_var").as_posix()
    proton = f"engine/proton/{runner.source_root}"
    return f'''#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")/.." && pwd -P)"
RUNTIME_VAR="$ROOT/{runtime}"
PROTON_ROOT="$ROOT/{proton}"
if [[ -d "$RUNTIME_VAR" && ! -L "$RUNTIME_VAR" ]]; then
    find "$RUNTIME_VAR" -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +
fi
rm -f -- "$PROTON_ROOT/files/steampipe_fixups_mtime"
'''.encode("utf-8")


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
    template: UmuBackendTemplate,
    output: Path,
) -> tuple[Path, str]:
    try:
        wine_derived = build_derived_capsule(
            source_capsule_path, source_profile_id, runner
        )
    except RunnerOverrideError as exc:
        raise ExperimentalVariantError(str(exc)) from exc
    capsule = deepcopy(wine_derived.document)
    objects = capsule.get("objects")
    profiles = capsule.get("profiles")
    if not isinstance(objects, list) or not isinstance(profiles, list):
        raise ExperimentalVariantError("Derived capsule is incomplete.")
    matches = [
        item for item in profiles
        if isinstance(item, dict) and item.get("id") == source_profile_id
    ]
    if len(matches) != 1:
        raise ExperimentalVariantError("Derived Wine profile is absent.")
    source = matches[0]
    playable = source.get("playable")
    dependencies = source.get("dependencies")
    if not isinstance(playable, dict) or not isinstance(dependencies, list):
        raise ExperimentalVariantError("Derived Wine profile has no playable layout.")
    layout = playable.get("layout")
    if not isinstance(layout, list) or not layout:
        raise ExperimentalVariantError("Derived Wine layout is absent.")

    object_index = {
        item.get("id"): item for item in objects
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    runner_ids = {
        dependency for dependency in dependencies
        if isinstance(dependency, str)
        and isinstance(object_index.get(dependency), dict)
        and "runner" in object_index[dependency].get("roles", [])
    }
    if len(runner_ids) != 1:
        raise ExperimentalVariantError("Derived Wine profile has no unique runner.")
    selected_runner_object = next(iter(runner_ids))
    remaining_dependencies = [
        value for value in dependencies if value not in runner_ids
    ]
    source_layout = [
        deepcopy(item) for item in layout
        if isinstance(item, dict) and item.get("object") not in runner_ids
    ]
    if {
        item.get("object") for item in source_layout if isinstance(item, dict)
    } != set(remaining_dependencies):
        raise ExperimentalVariantError(
            "Derived Wine layout does not map all non-runner dependencies."
        )

    existing_ids = set(object_index)
    backend_object_id = _unique_id(existing_ids, "shared-umu-backend")
    backend_object = deepcopy(template.composite_object)
    backend_object["id"] = backend_object_id
    backend_object["description"] = (
        "Preserved UMU, portable Python and Steam Linux Runtime backend "
        "selected for an experimental variant. Embedded Proton is not mapped."
    )
    objects.append(backend_object)

    output.mkdir(parents=True, mode=0o700)
    launcher_item = _write_asset(
        output,
        "launchers/JUGAR_UMU_EXPERIMENTAL.sh",
        _generic_umu_launcher(capsule=capsule, profile=source, runner=runner),
        0o755,
    )
    sanitizer_item = _write_asset(
        output,
        "launchers/sanear_umu_experimental.sh",
        _generic_umu_sanitizer(template.runtime_var, runner),
        0o755,
    )

    profile_id = _variant_profile_id("umu", runner)
    source["id"] = profile_id
    source["adapter"] = "umu"
    source["platform"] = "linux"
    source["status"] = "experimental"
    source["dependencies"] = [
        *remaining_dependencies,
        backend_object_id,
        selected_runner_object,
    ]
    source.pop("playable", None)
    source.pop("acceptance_report", None)
    host_contract_relative = "host-contracts/experimental-umu.json"
    host_contract_path = output / host_contract_relative
    host_contract_path.parent.mkdir(parents=True, exist_ok=True)
    host_contract_path.write_bytes(
        _canonical_bytes(
            {
                "schema": 0,
                "contract": "ogv-experimental-umu-v1",
                "source_profile_id": source_profile_id,
                "backend_template": template.template_id,
                "runner_id": runner.runner_id,
                "acceptance_inherited": False,
            }
        )
    )
    source["host_contract"] = host_contract_relative
    launch = source.get("launch")
    if not isinstance(launch, dict):
        launch = {}
    launch["network"] = "host_default"
    source["launch"] = launch
    source["variant"] = {
        "kind": "experimental",
        "source_profile_id": source_profile_id,
        "backend": "umu",
        "runner_id": runner.runner_id,
        "acceptance_inherited": False,
        "backend_template": template.template_id,
    }
    source["umu"] = {
        "schema": 0,
        "layout": [
            *source_layout,
            *[
                {
                    "object": backend_object_id,
                    "source": partition,
                    "destination": partition,
                }
                for partition in _BACKEND_PARTITIONS
            ],
            {
                "object": selected_runner_object,
                "source": runner.source_root,
                "destination": f"engine/proton/{runner.source_root}",
            },
        ],
        "allowed_absolute_symlinks": [],
        "nested_archives": [],
        "state_archives": [],
        "launchers": [launcher_item, sanitizer_item],
        "protected_manifests": [],
        "symlink_manifests": [],
        "mutable_paths": [
            f"engine/proton/{runner.source_root}/files/steampipe_fixups_mtime"
        ],
        "paths": {
            "launcher": launcher_item["destination"],
            "sanitizer": sanitizer_item["destination"],
            "runtime_var": template.runtime_var,
        },
    }
    source["notes"] = (
        f"User-requested experimental UMU variant generated by Offline Game "
        f"Vault {__version__}. It combines preserved backend "
        f"{template.template_id!r} and runner {runner.runner_id!r}; no "
        "functional acceptance is inherited."
    )
    capsule_path = output / "capsule.json"
    capsule_path.write_bytes(_canonical_bytes(capsule))
    return capsule_path, profile_id


def materialize_experimental_umu(
    *,
    collection_root: Path,
    capsule_path: Path,
    runner_id: str,
    destination: Path,
    source_profile_id: str | None = None,
    backend_template_id: str | None = None,
    play: bool = False,
    arguments: Sequence[str] = (),
) -> ExperimentalVariantResult:
    collection_root = collection_root.expanduser().resolve(strict=True)
    vault_root = collection_root / "01_IMMUTABLE_VAULT"
    runner = _select_runner(collection_root, runner_id, "umu")
    source_id = _select_source_profile(
        capsule_path,
        backend="umu",
        profile_id=source_profile_id,
    )
    template = _select_umu_template(collection_root, backend_template_id)

    with tempfile.TemporaryDirectory(prefix="ogv-experimental-umu-") as temporary:
        operational_capsule, profile_id = _umu_overlay(
            source_capsule_path=capsule_path,
            source_profile_id=source_id,
            runner=runner,
            template=template,
            output=Path(temporary) / "capsule",
        )
        result = materialize_umu_profile(
            capsule_path=operational_capsule,
            profile_id=profile_id,
            vault_root=vault_root,
            destination=destination,
        )
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
    return ExperimentalVariantResult(
        schema=0,
        capsule_id=str(capsule["capsule_id"]),
        backend="umu",
        runner_id=runner.runner_id,
        profile_id=profile_id,
        destination=str(destination),
        materialized=True,
        played=played,
        play_complete=play_complete,
        acceptance_inherited=False,
        backend_result={
            "materialization": asdict(result),
            "backend_template": template.template_id,
            "play": play_result,
        },
    )
