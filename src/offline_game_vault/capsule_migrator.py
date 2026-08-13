"""Migrate legacy Bottles host-contracts to the modern neutral shape.

Historical capsules (Dark Souls Remastered, Dark Souls III, Sekiro)
describe their Bottles profile via a host-contract of shape
``{schema, contract_id, platform, architecture, capabilities, ...}``
that predates the modern neutral contract model. The composition
layer has a bridge (``_neutral_fields_from_playable``) that
synthesises the neutral fields at compose time from the sibling
playable-wine profile. This module persists that synthesis as a
real file so the bridge can be retired.

Migration is destructive on the legacy side (the old
``linux-bottles.json`` is deleted) and additive on the modern side
(a new ``linux-bottles-neutral.json`` is written and the profile's
``host_contract`` field is updated to point at it). The only trace
of the legacy contract that survives is a ``derived_from`` block
inside the new contract carrying ``legacy_contract_id``,
``playable_profile_id``, ``generator``, ``generated_at``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json

from . import __version__
from .composition import _neutral_fields_from_playable, CompositionError


MODERN_CONTRACT_NAME = "ogv-bottles-neutral-v1"
NEW_CONTRACT_RELATIVE = "host-contracts/linux-bottles-neutral.json"


class MigrationError(Exception):
    """Raised when the migration cannot proceed."""


def migrate_bottles_contract(
    *,
    capsule_path: Path,
    flatpak_app: str,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Migrate one capsule's Bottles host-contract from legacy to modern.

    ``capsule_path`` points at ``capsule.json``. ``flatpak_app`` is the
    application id the modern contract will declare (defaults to
    ``bottles_adapter.DEFAULT_FLATPAK_APP`` at the CLI layer).

    Returns a report dict describing the migration. Raises
    ``MigrationError`` if the capsule is not in a shape the migrator
    can handle. Idempotent: a capsule that is already on the modern
    contract returns a report with ``already_migrated: True`` and
    makes no changes.
    """
    capsule_path = capsule_path.expanduser().resolve()
    if not capsule_path.is_file():
        raise MigrationError(f"Capsule file not found: {capsule_path}")
    capsule_root = capsule_path.parent

    try:
        capsule_doc = json.loads(capsule_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationError(f"capsule.json is not valid JSON: {exc}") from exc

    profiles = capsule_doc.get("profiles")
    if not isinstance(profiles, list):
        raise MigrationError("capsule.json declares no profiles array.")

    bottles_profile = _find_bottles_profile(profiles)
    if bottles_profile is None:
        raise MigrationError(
            "Capsule has no Bottles profile to migrate."
        )

    hc_ref = bottles_profile.get("host_contract")
    if not isinstance(hc_ref, str) or not hc_ref:
        raise MigrationError(
            f"Bottles profile {bottles_profile.get('id')!r} has no "
            "host_contract reference."
        )
    legacy_path = capsule_root / hc_ref
    if not legacy_path.is_file():
        raise MigrationError(
            f"Bottles host-contract not found at {hc_ref}."
        )
    try:
        legacy_doc = json.loads(legacy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationError(
            f"Bottles host-contract is not valid JSON: {exc}"
        ) from exc

    if legacy_doc.get("contract") == MODERN_CONTRACT_NAME:
        return {
            "already_migrated": True,
            "capsule_id": capsule_doc.get("capsule_id"),
            "contract_path": hc_ref,
            "dry_run": dry_run,
        }

    if "contract_id" not in legacy_doc:
        raise MigrationError(
            f"{hc_ref} does not look like a legacy Bottles contract: "
            "it has neither 'contract' (modern) nor 'contract_id' "
            "(legacy)."
        )
    legacy_contract_id = legacy_doc.get("contract_id")

    wine_profile = _find_playable_wine_profile(profiles)
    if wine_profile is None:
        raise MigrationError(
            "Capsule has no playable-wine profile to synthesise the "
            "neutral fields from."
        )

    try:
        neutral_fields = _neutral_fields_from_playable(wine_profile)
    except CompositionError as exc:
        raise MigrationError(
            f"Cannot synthesise neutral fields from playable-wine profile: "
            f"{exc}"
        ) from exc

    new_contract = {
        "schema": 0,
        "contract": MODERN_CONTRACT_NAME,
        **neutral_fields,
        "flatpak_app": flatpak_app,
        "network": "isolated",
        "bottle_yml_policy": "template-or-generate-derived",
        "bottle_yml_template": None,
        "derived_from": {
            "legacy_contract_id": legacy_contract_id,
            "playable_profile_id": wine_profile.get("id"),
            "generator": f"offline-game-vault/{__version__}",
            "generated_at": _now(),
        },
    }

    new_contract_path = capsule_root / NEW_CONTRACT_RELATIVE
    if new_contract_path.exists() and not force:
        raise MigrationError(
            f"{NEW_CONTRACT_RELATIVE} already exists; pass --force to "
            "overwrite."
        )

    report = {
        "already_migrated": False,
        "capsule_id": capsule_doc.get("capsule_id"),
        "bottles_profile_id": bottles_profile.get("id"),
        "playable_profile_id": wine_profile.get("id"),
        "legacy_contract_id": legacy_contract_id,
        "legacy_contract_path": hc_ref,
        "new_contract_path": NEW_CONTRACT_RELATIVE,
        "flatpak_app": flatpak_app,
        "dry_run": dry_run,
    }

    if dry_run:
        return report

    new_contract_path.parent.mkdir(parents=True, exist_ok=True)
    new_contract_path.write_text(
        json.dumps(
            new_contract,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    legacy_path.unlink()

    bottles_profile["host_contract"] = NEW_CONTRACT_RELATIVE
    capsule_path.write_text(
        json.dumps(
            capsule_doc,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    return report


# ------------------------------------------------------------ helpers


def _find_bottles_profile(profiles: list[Any]) -> dict[str, Any] | None:
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if profile.get("adapter") == "bottles":
            return profile
    return None


def _find_playable_wine_profile(
    profiles: list[Any],
) -> dict[str, Any] | None:
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if profile.get("adapter") != "wine":
            continue
        if not isinstance(profile.get("playable"), dict):
            continue
        return profile
    return None


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
