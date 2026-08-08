from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any

from .state_manager import (
    StateError,
    restore_state,
    verify_state_backup,
)


STATE_BACKUP_RECEIPT = "state-backup.json"
STATE_RESTORE_RECEIPT = "state-restore-receipt.json"


class CompositionStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompositionStateSelection:
    backup: Path
    backup_id: str
    item_count: int


@dataclass(frozen=True)
class CompositionStateEvidence:
    schema: int
    backup_id: str
    snapshot_backup_id: str
    item_count: int
    restored_count: int
    missing_count: int
    state_root: str
    baseline_receipt: str
    baseline_receipt_sha256: str
    restore_receipt: str
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_capsule(capsule_path: Path) -> dict[str, Any]:
    try:
        document = json.loads(
            capsule_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompositionStateError(
            f"Cannot load the operational capsule: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise CompositionStateError(
            "The operational capsule root must be an object."
        )

    return document


def _declared_state_count(capsule_path: Path) -> int:
    capsule = _load_capsule(capsule_path)
    declarations = capsule.get("persistent_state", [])

    if not isinstance(declarations, list) or any(
        not isinstance(item, dict)
        for item in declarations
    ):
        raise CompositionStateError(
            "capsule.persistent_state must be an array of objects."
        )

    return sum(
        1
        for item in declarations
        if item.get("backup", True) is True
    )


def prepare_composition_state(
    *,
    capsule_path: Path,
    state_backup: Path | None,
    require_declared_state: bool = False,
) -> CompositionStateSelection | None:
    item_count = _declared_state_count(capsule_path)

    if state_backup is None:
        if item_count and require_declared_state:
            raise CompositionStateError(
                "This capsule declares persistent state; "
                "--state-backup is required."
            )
        return None

    if not item_count:
        raise CompositionStateError(
            "A state backup was supplied but the capsule "
            "declares no preservable state."
        )

    try:
        verification = verify_state_backup(
            capsule_path=capsule_path,
            backup=state_backup,
        )
    except StateError as exc:
        raise CompositionStateError(str(exc)) from exc

    if not verification.verified:
        detail = "; ".join(verification.problems)
        suffix = f": {detail}" if detail else ""
        raise CompositionStateError(
            "State backup failed verification" + suffix
        )

    if (
        not isinstance(verification.backup_id, str)
        or not verification.backup_id
    ):
        raise CompositionStateError(
            "Verified state backup has no backup_id."
        )

    try:
        backup = state_backup.expanduser().resolve(strict=True)
    except OSError as exc:
        raise CompositionStateError(
            f"Cannot resolve state backup: {exc}"
        ) from exc

    if backup.is_symlink() or not backup.is_dir():
        raise CompositionStateError(
            "State backup must be a regular directory."
        )

    if verification.item_count > item_count:
        raise CompositionStateError(
            "Verified state backup contains more items than the "
            "current capsule declares."
        )

    return CompositionStateSelection(
        backup=backup,
        backup_id=verification.backup_id,
        item_count=verification.item_count,
    )


def restore_composition_state(
    *,
    capsule_path: Path,
    state_root: Path,
    state_root_relative: str,
    selection: CompositionStateSelection,
    snapshot: Path,
    evidence_root: Path,
    evidence_relative: str,
) -> CompositionStateEvidence:
    if snapshot.exists() or snapshot.is_symlink():
        raise CompositionStateError(
            "Pre-restore snapshot destination already exists."
        )

    try:
        result = restore_state(
            capsule_path=capsule_path,
            state_root=state_root,
            backup=selection.backup,
            snapshot=snapshot,
            confirm_stopped=True,
        )
    except StateError as exc:
        raise CompositionStateError(str(exc)) from exc

    if not result.complete:
        raise CompositionStateError(
            "Persistent-state restoration did not complete."
        )

    if result.backup_id != selection.backup_id:
        raise CompositionStateError(
            "Restored backup identity differs from the verified selection."
        )

    if result.restored_count != selection.item_count:
        raise CompositionStateError(
            "Not every preservable state item was restored."
        )

    evidence_root.mkdir(parents=True, mode=0o700, exist_ok=False)

    snapshot_destination = evidence_root / "pre-restore-snapshot"
    os.replace(snapshot, snapshot_destination)

    baseline_source = selection.backup / STATE_BACKUP_RECEIPT
    if (
        baseline_source.is_symlink()
        or not baseline_source.is_file()
    ):
        raise CompositionStateError(
            "Verified backup has no regular state-backup receipt."
        )

    baseline_destination = evidence_root / "baseline-state.json"
    shutil.copy2(
        baseline_source,
        baseline_destination,
        follow_symlinks=False,
    )
    baseline_destination.chmod(0o600)

    try:
        baseline = json.loads(
            baseline_destination.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompositionStateError(
            f"Cannot read copied baseline-state receipt: {exc}"
        ) from exc

    if (
        not isinstance(baseline, dict)
        or baseline.get("backup_id") != selection.backup_id
    ):
        raise CompositionStateError(
            "Copied baseline-state receipt has another backup identity."
        )

    restore_receipt = snapshot_destination / STATE_RESTORE_RECEIPT
    if restore_receipt.is_symlink() or not restore_receipt.is_file():
        raise CompositionStateError(
            "State restoration produced no regular restore receipt."
        )

    baseline_digest = (
        "sha256:"
        + hashlib.sha256(
            baseline_destination.read_bytes()
        ).hexdigest()
    )

    return CompositionStateEvidence(
        schema=0,
        backup_id=selection.backup_id,
        snapshot_backup_id=result.snapshot_backup_id,
        item_count=selection.item_count,
        restored_count=result.restored_count,
        missing_count=result.missing_count,
        state_root=state_root_relative,
        baseline_receipt=(
            f"{evidence_relative}/baseline-state.json"
        ),
        baseline_receipt_sha256=baseline_digest,
        restore_receipt=(
            f"{evidence_relative}/pre-restore-snapshot/"
            f"{STATE_RESTORE_RECEIPT}"
        ),
        complete=True,
    )

_STATE_EVIDENCE_KEYS = {
    "schema",
    "backup_id",
    "snapshot_backup_id",
    "item_count",
    "restored_count",
    "missing_count",
    "state_root",
    "baseline_receipt",
    "baseline_receipt_sha256",
    "restore_receipt",
    "complete",
}


def _evidence_relative_path(
    value: object,
    label: str,
    *,
    allow_root: bool = False,
) -> Path:
    if not isinstance(value, str) or not value:
        raise CompositionStateError(
            f"{label} must be a non-empty relative path."
        )

    if "\x00" in value or "\\" in value:
        raise CompositionStateError(
            f"{label} contains an unsafe path separator."
        )

    if allow_root and value == ".":
        return Path(".")

    relative = Path(value)
    if (
        relative.is_absolute()
        or value in {".", ".."}
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise CompositionStateError(
            f"{label} must stay below the materialization root."
        )

    return relative


def _evidence_existing_path(
    *,
    root: Path,
    relative: Path,
    label: str,
    directory: bool,
) -> Path:
    current = root

    if current.is_symlink() or not current.is_dir():
        raise CompositionStateError(
            "Materialization root is absent, linked, or not a directory."
        )

    if relative == Path("."):
        if not directory:
            raise CompositionStateError(
                f"{label} cannot refer to the materialization root."
            )
        return current

    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CompositionStateError(
                f"{label} traverses a symbolic link."
            )

    if directory:
        valid = current.is_dir()
        expected = "directory"
    else:
        valid = current.is_file()
        expected = "regular file"

    if not valid:
        raise CompositionStateError(
            f"{label} is absent or is not a {expected}."
        )

    return current


def _evidence_non_negative_integer(
    value: object,
    label: str,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise CompositionStateError(
            f"{label} must be a non-negative integer."
        )
    return value


def _load_evidence_json(
    path: Path,
    label: str,
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompositionStateError(
            f"Cannot read {label}: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise CompositionStateError(
            f"{label} root must be an object."
        )

    return document


def verify_composition_state_evidence(
    *,
    root: Path,
    evidence: object,
    capsule_id: str,
) -> None:
    "Verify immutable receipts describing a generic state restoration."

    if evidence is None:
        return

    if not isinstance(evidence, dict):
        raise CompositionStateError(
            "state_restore must be an object."
        )

    keys = set(evidence)
    if keys != _STATE_EVIDENCE_KEYS:
        missing = sorted(_STATE_EVIDENCE_KEYS - keys)
        unexpected = sorted(keys - _STATE_EVIDENCE_KEYS)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unexpected:
            detail.append("unexpected=" + ",".join(unexpected))
        raise CompositionStateError(
            "state_restore fields are invalid"
            + (": " + "; ".join(detail) if detail else ".")
        )

    if evidence.get("schema") != 0:
        raise CompositionStateError(
            "state_restore schema is unsupported."
        )

    backup_id = evidence.get("backup_id")
    snapshot_backup_id = evidence.get("snapshot_backup_id")
    if not isinstance(backup_id, str) or not backup_id:
        raise CompositionStateError(
            "state_restore backup_id is invalid."
        )
    if (
        not isinstance(snapshot_backup_id, str)
        or not snapshot_backup_id
    ):
        raise CompositionStateError(
            "state_restore snapshot_backup_id is invalid."
        )

    item_count = _evidence_non_negative_integer(
        evidence.get("item_count"),
        "state_restore.item_count",
    )
    restored_count = _evidence_non_negative_integer(
        evidence.get("restored_count"),
        "state_restore.restored_count",
    )
    missing_count = _evidence_non_negative_integer(
        evidence.get("missing_count"),
        "state_restore.missing_count",
    )

    if restored_count != item_count:
        raise CompositionStateError(
            "state_restore does not cover every declared state item."
        )
    if missing_count > item_count:
        raise CompositionStateError(
            "state_restore missing_count exceeds item_count."
        )
    if evidence.get("complete") is not True:
        raise CompositionStateError(
            "state_restore is not marked complete."
        )

    state_root_relative = _evidence_relative_path(
        evidence.get("state_root"),
        "state_restore.state_root",
        allow_root=True,
    )
    _evidence_existing_path(
        root=root,
        relative=state_root_relative,
        label="state_restore.state_root",
        directory=True,
    )

    baseline_relative = _evidence_relative_path(
        evidence.get("baseline_receipt"),
        "state_restore.baseline_receipt",
    )
    restore_relative = _evidence_relative_path(
        evidence.get("restore_receipt"),
        "state_restore.restore_receipt",
    )

    baseline_path = _evidence_existing_path(
        root=root,
        relative=baseline_relative,
        label="state_restore.baseline_receipt",
        directory=False,
    )
    restore_path = _evidence_existing_path(
        root=root,
        relative=restore_relative,
        label="state_restore.restore_receipt",
        directory=False,
    )

    expected_digest = evidence.get("baseline_receipt_sha256")
    if (
        not isinstance(expected_digest, str)
        or not expected_digest.startswith("sha256:")
        or len(expected_digest) != 71
        or any(
            character not in "0123456789abcdef"
            for character in expected_digest[7:]
        )
    ):
        raise CompositionStateError(
            "state_restore baseline digest is invalid."
        )

    actual_digest = (
        "sha256:"
        + hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    )
    if actual_digest != expected_digest:
        raise CompositionStateError(
            "state_restore baseline receipt digest does not match."
        )

    baseline = _load_evidence_json(
        baseline_path,
        "state_restore baseline receipt",
    )
    restore = _load_evidence_json(
        restore_path,
        "state_restore restore receipt",
    )

    if baseline.get("backup_id") != backup_id:
        raise CompositionStateError(
            "state_restore baseline backup identity does not match."
        )
    if baseline.get("capsule_id") != capsule_id:
        raise CompositionStateError(
            "state_restore baseline capsule identity does not match."
        )

    definition_digest = baseline.get("state_definition_digest")
    if (
        not isinstance(definition_digest, str)
        or not definition_digest.startswith("sha256:")
    ):
        raise CompositionStateError(
            "state_restore baseline definition digest is invalid."
        )

    if restore.get("backup_id") != backup_id:
        raise CompositionStateError(
            "state_restore restore backup identity does not match."
        )
    if restore.get("snapshot_backup_id") != snapshot_backup_id:
        raise CompositionStateError(
            "state_restore snapshot identity does not match."
        )
    if restore.get("capsule_id") != capsule_id:
        raise CompositionStateError(
            "state_restore restore capsule identity does not match."
        )
    if restore.get("state_definition_digest") != definition_digest:
        raise CompositionStateError(
            "state_restore definition identity does not match."
        )
    if restore.get("item_count") != item_count:
        raise CompositionStateError(
            "state_restore receipt item_count does not match."
        )

    item_ids = restore.get("restored_items")
    if (
        not isinstance(item_ids, list)
        or not all(
            isinstance(item_id, str) and item_id
            for item_id in item_ids
        )
        or len(item_ids) != restored_count
        or len(set(item_ids)) != len(item_ids)
    ):
        raise CompositionStateError(
            "state_restore restored item identities are invalid."
        )

    if (
        restore.get("status") != "completed"
        or restore.get("stopped_confirmed") is not True
        or restore.get("rollback_performed") is not False
        or restore.get("rollback_complete") is not True
        or restore.get("complete") is not True
    ):
        raise CompositionStateError(
            "state_restore restore receipt is not a completed restoration."
        )
