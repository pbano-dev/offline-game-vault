"""Command-line interface for Offline Game Vault."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .bottles_adapter import (
    BottlesAdapterError,
    BottlesDeploymentResult,
    BottlesDeploymentVerification,
    BottlesLaunchPlan,
    BottlesRemovalResult,
    build_bottles_launch_plan,
    deploy_bottles_profile,
    discover_bottles_path,
    remove_bottles_deployment,
    run_bottles_deployment,
    verify_bottles_deployment,
)
from .composition import (
    CompositionError,
    list_shared_umu_runtimes,
    compose_bottles,
    compose_umu,
    compose_wine,
)
from .preserved_runners import RunnerCatalogError, scan_runners
from .inventory import (
    InventoryError,
    VaultInventory,
    build_inventory,
    write_inventory_atomic,
)
from .materializer import (
    MaterializationError,
    MaterializationResult,
    RemovalResult,
    materialize_profile,
    remove_materialization,
)
from .planner import MaterializationPlan, PlanError, build_plan
from .playable import (
    PlayableError,
    PlayableMaterializationResult,
    PlayableRemovalResult,
    PlayableVerificationResult,
    PlayResult,
    materialize_playable_profile,
    remove_playable_profile,
    run_playable_profile,
    verify_playable_profile,
)
from .umu_adapter import (
    UmuAdapterError,
    UmuMaterializationResult,
    UmuRemovalResult,
    UmuRunResult,
    UmuVerificationResult,
    materialize_umu_profile,
    remove_umu_materialization,
    run_umu_materialization,
    verify_umu_materialization,
)
from .profile_store import (
    ProfileIngestResult,
    ProfileStoreError,
    ProfileVerificationResult,
    ingest_profile,
    parse_source_assignments,
    verify_profile,
)
from .storage import (
    IngestError,
    IngestResult,
    capsule_destination_spec,
    direct_destination_spec,
    ingest_object,
)
from .state_manager import (
    CapsuleAuditResult,
    StateBackupResult,
    StateBackupVerification,
    StateError,
    StateRestoreResult,
    audit_capsule,
    preserve_state,
    restore_state,
    verify_state_backup,
)
from .verifier import (
    ObjectSpec,
    VerificationResult,
    VerifyError,
    direct_object_spec,
    resolve_capsule_object,
    verify_object,
)
from .object_manifest import (
    ObjectManifestError,
    compute_sidecar_digest,
    detect_source_root,
    format_manifest,
    generate_object_manifest,
    manifest_path,
    manifest_sidecar_path,
    write_manifest_atomically,
)
from .manifest_catalog import (
    ManifestCatalogError,
    generate_missing_manifests,
    manifest_is_current,
    scan_vault,
)


def _print_text_plan(plan: MaterializationPlan) -> None:
    print(f"Capsule:      {plan.capsule_id}")
    print(f"Profile:      {plan.profile_id}")
    print(f"Adapter:      {plan.adapter}")
    print(f"Platform:     {plan.platform}")
    print(f"Vault:        {plan.vault_root}")
    print(f"Destination:  {plan.destination}")
    print(f"Network:      {plan.network}")
    print(f"Entrypoint:   {plan.entrypoint}")
    print("Objects:")
    for item in plan.objects:
        state = "present" if item.present else "MISSING"
        print(
            f"  - {item.object_id}: {item.strategy}, {state}, "
            f"{item.digest}"
        )
    if plan.missing_required_objects:
        print(
            "Missing required objects: "
            + ", ".join(plan.missing_required_objects)
        )


def _print_text_verification(result: VerificationResult) -> None:
    print(f"Object:       {result.object_id or '(direct path)'}")
    print(f"Path:         {result.path}")
    print(f"Expected:     {result.expected_digest}")
    print(f"Actual:       {result.actual_digest}")
    print(f"Bytes:        {result.actual_size}")
    if result.expected_size is not None:
        print(f"Expected size: {result.expected_size}")
        print(
            "Size match:   "
            + ("yes" if result.size_match else "NO")
        )
    print(
        "Digest match: "
        + ("yes" if result.digest_match else "NO")
    )
    print(
        "Verified:     "
        + ("yes" if result.verified else "NO")
    )


def _command_plan(args: argparse.Namespace) -> int:
    plan = build_plan(
        capsule_path=args.capsule,
        profile_id=args.profile,
        vault_root=args.vault_root,
        destination=args.destination,
        allow_missing=args.allow_missing,
    )

    if args.json:
        print(
            json.dumps(
                plan.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_text_plan(plan)
    return 0


def _object_spec_from_args(args: argparse.Namespace) -> ObjectSpec:
    capsule_mode = any(
        value is not None
        for value in (
            args.capsule,
            args.object_id,
            args.vault_root,
        )
    )
    direct_mode = any(
        value is not None
        for value in (
            args.path,
            args.digest,
            args.expected_size,
        )
    )

    if capsule_mode and direct_mode:
        raise VerifyError(
            "Choose either capsule mode "
            "(--capsule, --object-id, --vault-root) "
            "or direct mode (--path, --digest)."
        )

    if capsule_mode:
        missing = [
            name
            for name, value in (
                ("--capsule", args.capsule),
                ("--object-id", args.object_id),
                ("--vault-root", args.vault_root),
            )
            if value is None
        ]
        if missing:
            raise VerifyError(
                "Capsule mode requires "
                + ", ".join(missing)
                + "."
            )
        return resolve_capsule_object(
            capsule_path=args.capsule,
            object_id=args.object_id,
            vault_root=args.vault_root,
        )

    if direct_mode:
        missing = [
            name
            for name, value in (
                ("--path", args.path),
                ("--digest", args.digest),
            )
            if value is None
        ]
        if missing:
            raise VerifyError(
                "Direct mode requires "
                + ", ".join(missing)
                + "."
            )
        return direct_object_spec(
            path=args.path,
            digest=args.digest,
            expected_size=args.expected_size,
        )

    raise VerifyError(
        "Provide capsule mode "
        "(--capsule, --object-id, --vault-root) "
        "or direct mode (--path, --digest)."
    )


def _command_verify_object(args: argparse.Namespace) -> int:
    spec = _object_spec_from_args(args)
    result = verify_object(spec)

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_text_verification(result)

    return 0 if result.verified else 1



def _print_text_ingest(result: IngestResult) -> None:
    print(f"Object:       {result.object_id or '(direct digest)'}")
    print(f"Source:       {result.source}")
    print(f"Destination:  {result.destination}")
    print(f"Digest:       {result.digest}")
    print(f"Bytes:        {result.bytes}")
    print(f"Status:       {result.status}")
    print(
        "Source verified:      "
        + ("yes" if result.source_verified else "NO")
    )
    print(
        "Destination verified: "
        + ("yes" if result.destination_verified else "NO")
    )


def _destination_spec_from_ingest_args(
    args: argparse.Namespace,
) -> ObjectSpec:
    capsule_mode = any(
        value is not None
        for value in (
            args.capsule,
            args.object_id,
        )
    )
    direct_mode = any(
        value is not None
        for value in (
            args.digest,
            args.expected_size,
        )
    )

    if capsule_mode and direct_mode:
        raise IngestError(
            "Choose either capsule mode "
            "(--capsule, --object-id) "
            "or direct mode (--digest)."
        )

    if capsule_mode:
        missing = [
            name
            for name, value in (
                ("--capsule", args.capsule),
                ("--object-id", args.object_id),
            )
            if value is None
        ]
        if missing:
            raise IngestError(
                "Capsule mode requires "
                + ", ".join(missing)
                + "."
            )
        return capsule_destination_spec(
            capsule_path=args.capsule,
            object_id=args.object_id,
            vault_root=args.vault_root,
        )

    if args.digest is not None:
        return direct_destination_spec(
            vault_root=args.vault_root,
            digest=args.digest,
            expected_size=args.expected_size,
        )

    raise IngestError(
        "Provide capsule mode (--capsule, --object-id) "
        "or direct mode (--digest)."
    )


class _FormatUnavailable(Exception):
    """Signals that the manifest step must be skipped for lack of a format,
    which is different from a manifest failure.
    """


def _validate_ingest_format_intent(args: argparse.Namespace) -> None:
    """Reject an ``--format`` assertion that contradicts the capsule.

    Both the capsule and ``--format`` describe the archive format. When both
    are present and disagree, the user's intent is inconsistent; the ingest
    is aborted so the mismatch is resolved before anything reaches the Vault.
    Non-existent capsules or missing object entries are left to the ingest
    engine itself to surface with its usual errors.
    """
    explicit_format = getattr(args, "format", None)
    if not explicit_format:
        return
    if args.capsule is None or args.object_id is None:
        return
    try:
        capsule_document = json.loads(
            args.capsule.expanduser().read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    for entry in capsule_document.get("objects", []):
        if not isinstance(entry, dict) or entry.get("id") != args.object_id:
            continue
        declared_format = entry.get("format")
        if (
            isinstance(declared_format, str)
            and declared_format
            and declared_format != explicit_format
        ):
            raise ObjectManifestError(
                f"--format {explicit_format!r} contradicts the capsule "
                f"declaration {declared_format!r} for object "
                f"{args.object_id!r}."
            )
        return


def _resolve_ingest_format(args: argparse.Namespace) -> str:
    """Return the archive format for the freshly ingested object.

    Capsule mode reads the format from the capsule; direct mode uses
    ``--format`` when present. If neither yields a format, a clean skip is
    signalled with ``_FormatUnavailable``.
    """
    explicit_format = getattr(args, "format", None)
    capsule_mode = args.capsule is not None or args.object_id is not None

    if capsule_mode:
        try:
            capsule_document = json.loads(
                args.capsule.expanduser().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ObjectManifestError(
                f"Could not read capsule at {args.capsule}: {exc}"
            ) from exc
        for entry in capsule_document.get("objects", []):
            if isinstance(entry, dict) and entry.get("id") == args.object_id:
                declared_format = entry.get("format")
                if isinstance(declared_format, str) and declared_format:
                    return declared_format
                break
        raise ObjectManifestError(
            f"Capsule does not declare a format for object "
            f"{args.object_id!r}."
        )

    if explicit_format:
        return explicit_format

    raise _FormatUnavailable("direct mode without --format")


def _maybe_generate_manifest_for_ingested_object(
    args: argparse.Namespace,
    result: IngestResult,
) -> dict[str, Any]:
    """Try to write the per-object manifest for a freshly ingested object.

    Never raises: manifest failures become fields on the returned dict. By
    the time this runs the ingest itself is already authoritative on the
    object's presence in the Vault, so a manifest mishap must not turn a
    successful ingest into a command-level failure.
    """
    fields: dict[str, Any] = {
        "manifest_generated": False,
        "manifest_already_present": False,
        "manifest_skipped": False,
        "manifest_skipped_reason": None,
        "manifest_warning": None,
        "manifest_path": None,
    }

    def _record_target() -> None:
        try:
            fields["manifest_path"] = str(
                manifest_path(args.vault_root, result.digest)
            )
        except ObjectManifestError:
            pass

    if getattr(args, "no_manifest", False):
        fields["manifest_skipped"] = True
        fields["manifest_skipped_reason"] = "disabled by --no-manifest"
        _record_target()
        return fields

    try:
        archive_format = _resolve_ingest_format(args)
    except _FormatUnavailable as exc:
        fields["manifest_skipped"] = True
        fields["manifest_skipped_reason"] = str(exc)
        _record_target()
        return fields
    except ObjectManifestError as exc:
        fields["manifest_warning"] = str(exc)
        _record_target()
        return fields

    try:
        target = manifest_path(args.vault_root, result.digest)
    except ObjectManifestError as exc:
        fields["manifest_warning"] = str(exc)
        return fields
    fields["manifest_path"] = str(target)

    if manifest_is_current(target, result.digest):
        fields["manifest_already_present"] = True
        return fields

    archive = Path(result.destination)
    try:
        source_root = detect_source_root(archive, archive_format)
        manifest = generate_object_manifest(
            archive=archive,
            archive_format=archive_format,
            source_root=source_root,
            object_digest=result.digest,
            object_size=result.bytes,
        )
        write_manifest_atomically(manifest, target)
    except (ObjectManifestError, OSError) as exc:
        fields["manifest_warning"] = str(exc)
        return fields

    fields["manifest_generated"] = True
    return fields


def _command_ingest_object(args: argparse.Namespace) -> int:
    _validate_ingest_format_intent(args)
    destination_spec = _destination_spec_from_ingest_args(args)
    result = ingest_object(
        source=args.source,
        destination_spec=destination_spec,
    )

    manifest_fields = _maybe_generate_manifest_for_ingested_object(
        args, result
    )
    if manifest_fields["manifest_warning"]:
        print(
            "ogv: warning: manifest not generated: "
            f"{manifest_fields['manifest_warning']}",
            file=sys.stderr,
        )

    if args.json:
        payload = result.to_dict()
        payload.update(manifest_fields)
        print(
            json.dumps(
                payload,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_text_ingest(result)
        if manifest_fields["manifest_generated"]:
            print(
                "Manifest:     generated "
                f"({manifest_fields['manifest_path']})"
            )
        elif manifest_fields["manifest_already_present"]:
            print(
                "Manifest:     already present "
                f"({manifest_fields['manifest_path']})"
            )
        elif manifest_fields["manifest_skipped"]:
            print(
                "Manifest:     skipped "
                f"({manifest_fields['manifest_skipped_reason']})"
            )

    return 0


def _resolve_manifest_target(args: argparse.Namespace) -> tuple[Path, str, int, str]:
    """Return ``(archive, digest, size, format)`` for the target object.

    Supports the same two modes as verify-object, but ``--vault-root`` here
    plays a dual role: it locates the object in capsule mode and it names
    the manifest destination in either mode, so it is accepted alongside
    direct-mode arguments.
    """
    capsule_mode = (
        args.capsule is not None or args.object_id is not None
    )
    direct_mode = args.path is not None or args.digest is not None

    if capsule_mode and direct_mode:
        raise ObjectManifestError(
            "Choose either capsule mode (--capsule, --object-id) "
            "or direct mode (--path, --digest)."
        )

    if capsule_mode:
        missing = [
            name for name, value in (
                ("--capsule", args.capsule),
                ("--object-id", args.object_id),
                ("--vault-root", args.vault_root),
            ) if value is None
        ]
        if missing:
            raise ObjectManifestError(
                "Capsule mode requires " + ", ".join(missing) + "."
            )
        try:
            capsule_document = json.loads(
                args.capsule.expanduser().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ObjectManifestError(
                f"Could not read capsule at {args.capsule}: {exc}"
            ) from exc
        format_name: str | None = None
        for entry in capsule_document.get("objects", []):
            if isinstance(entry, dict) and entry.get("id") == args.object_id:
                format_name = entry.get("format")
                break
        if not isinstance(format_name, str) or not format_name:
            raise ObjectManifestError(
                f"Capsule does not declare a format for object "
                f"{args.object_id!r}."
            )
        try:
            spec = resolve_capsule_object(
                capsule_path=args.capsule,
                object_id=args.object_id,
                vault_root=args.vault_root,
            )
        except VerifyError as exc:
            raise ObjectManifestError(str(exc)) from exc
        return (
            spec.path,
            spec.expected_digest,
            spec.expected_size or 0,
            format_name,
        )

    if direct_mode:
        missing = [
            name for name, value in (
                ("--path", args.path),
                ("--digest", args.digest),
            ) if value is None
        ]
        if missing:
            raise ObjectManifestError(
                "Direct mode requires " + ", ".join(missing) + "."
            )
        if not getattr(args, "format", None):
            raise ObjectManifestError(
                "Direct mode requires --format (e.g. tar.gz, tar.zst, zip)."
            )
        try:
            spec = direct_object_spec(
                path=args.path,
                digest=args.digest,
                expected_size=args.expected_size,
            )
        except VerifyError as exc:
            raise ObjectManifestError(str(exc)) from exc
        return spec.path, spec.expected_digest, spec.expected_size or 0, args.format

    raise ObjectManifestError(
        "Provide capsule mode (--capsule, --object-id, --vault-root) "
        "or direct mode (--path, --digest, --format)."
    )


def _command_generate_object_manifest(args: argparse.Namespace) -> int:
    archive, digest, size, format_name = _resolve_manifest_target(args)

    if size == 0:
        size = archive.stat().st_size

    # Verify the object before writing anything about it. A manifest that
    # describes an object whose bytes do not match its declared digest is
    # not evidence — it is a lie with authority.
    try:
        verification = verify_object(
            ObjectSpec(
                object_id=None,
                path=archive,
                expected_digest=digest,
                expected_size=size if size else None,
                vault_root=None,
            )
        )
    except VerifyError as exc:
        raise ObjectManifestError(str(exc)) from exc
    if not verification.verified:
        raise ObjectManifestError(
            f"Object at {archive} does not match its declared digest; "
            f"refusing to write a manifest for it."
        )

    source_root = args.source_root
    if source_root is None:
        source_root = detect_source_root(archive, format_name)

    manifest = generate_object_manifest(
        archive=archive,
        archive_format=format_name,
        source_root=source_root,
        object_digest=digest,
        object_size=size,
    )

    destination_argument = args.output
    if destination_argument is not None:
        destination_path = destination_argument.expanduser().resolve()
    elif args.vault_root is not None:
        destination_path = manifest_path(args.vault_root, digest)
    else:
        raise ObjectManifestError(
            "Provide either --output or --vault-root to store the manifest."
        )

    if args.dry_run:
        written_manifest = destination_path
        written_sidecar = manifest_sidecar_path(destination_path)
    else:
        written_manifest, written_sidecar = write_manifest_atomically(
            manifest, destination_path
        )

    payload = {
        "schema": 0,
        "object_digest": digest,
        "object_size": size,
        "source_root": source_root,
        "file_count": manifest.file_count,
        "total_bytes": manifest.total_bytes,
        "manifest_path": str(written_manifest),
        "sidecar_path": str(written_sidecar),
        "manifest_digest": compute_sidecar_digest(format_manifest(manifest)),
        "dry_run": bool(args.dry_run),
    }

    if args.json:
        print(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
        )
    else:
        print(f"Object:         {digest}")
        print(f"Format:         {format_name}")
        print(f"Source root:    {source_root}")
        print(f"Files:          {manifest.file_count}")
        print(f"Total bytes:    {manifest.total_bytes}")
        print(f"Manifest:       {written_manifest}")
        print(f"Sidecar:        {written_sidecar}")
        print(f"Manifest digest:{payload['manifest_digest']}")
        if args.dry_run:
            print("(dry-run: nothing was written)")

    return 0


def _command_generate_missing_manifests(args: argparse.Namespace) -> int:
    result = generate_missing_manifests(
        collection_root=args.collection_root,
        vault_root=args.vault_root,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    if args.json:
        payload = result.to_dict()
        payload["dry_run"] = bool(args.dry_run)
        print(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
        )
    else:
        print(f"Generated:       {len(result.generated)}")
        print(f"Already present: {len(result.already_present)}")
        print(f"Skipped:         {len(result.skipped)}")
        print(f"Failed:          {len(result.failed)}")
        if result.skipped:
            print("Skipped objects:")
            for digest, reason in result.skipped:
                print(f"  - {digest}: {reason}")
        if result.failed:
            print("Failed objects:")
            for digest, reason in result.failed:
                print(f"  - {digest}: {reason}")
        if args.dry_run:
            print("(dry-run: nothing was written)")

    return 1 if result.has_failures else 0


def _print_profile_ingest(result: ProfileIngestResult) -> None:
    print(f"Capsule:          {result.capsule_id}")
    print(f"Profile:          {result.profile_id}")
    print(f"Objects:          {result.object_count}")
    print(f"Ingested:         {result.ingested_count}")
    print(f"Already present:  {result.already_present_count}")
    print(f"Complete:         {'yes' if result.complete else 'NO'}")
    for item in result.objects:
        print(
            f"  - {item.object_id}: {item.status}, "
            f"{item.bytes} bytes, verified="
            f"{'yes' if item.verified else 'NO'}"
        )


def _command_ingest_profile(args: argparse.Namespace) -> int:
    sources = parse_source_assignments(args.source)
    result = ingest_profile(
        capsule_path=args.capsule,
        profile_id=args.profile,
        vault_root=args.vault_root,
        sources=sources,
    )

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_profile_ingest(result)

    return 0 if result.complete else 1


def _print_profile_verification(
    result: ProfileVerificationResult,
) -> None:
    print(f"Capsule:   {result.capsule_id}")
    print(f"Profile:   {result.profile_id}")
    print(f"Verified:  {result.verified_count}/{result.object_count}")
    print(
        "Complete:  "
        + ("yes" if result.verified else "NO")
    )
    for item in result.objects:
        line = (
            f"  - {item.object_id}: {item.status}"
        )
        if item.actual_size is not None:
            line += f", {item.actual_size} bytes"
        if item.detail:
            line += f" ({item.detail})"
        print(line)


def _command_verify_profile(args: argparse.Namespace) -> int:
    result = verify_profile(
        capsule_path=args.capsule,
        profile_id=args.profile,
        vault_root=args.vault_root,
    )

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_profile_verification(result)

    return 0 if result.verified else 1


def _print_inventory(inventory: VaultInventory) -> None:
    print(f"Algorithm:     {inventory.algorithm}")
    print(f"Object count:  {inventory.object_count}")
    print(f"Total bytes:   {inventory.total_bytes}")
    for item in inventory.objects:
        print(f"  - {item.digest}  {item.bytes}  {item.path}")


def _command_inventory(args: argparse.Namespace) -> int:
    inventory = build_inventory(vault_root=args.vault_root)

    if args.output is not None:
        write_inventory_atomic(
            inventory=inventory,
            output=args.output,
            vault_root=args.vault_root,
        )

    if args.json or args.output is None:
        print(inventory.to_json(), end="")
    else:
        _print_inventory(inventory)
        print(f"Written:       {args.output.expanduser().absolute()}")

    return 0


def _print_materialization(result: MaterializationResult) -> None:
    print(f"Capsule:      {result.capsule_id}")
    print(f"Profile:      {result.profile_id}")
    print(f"Destination:  {result.destination}")
    print(f"Objects:      {result.object_count}")
    print(f"Complete:     {'yes' if result.complete else 'NO'}")
    print(f"Receipt:      {result.receipt_id}")
    for item in result.objects:
        print(
            f"  - {item.object_id}: {item.strategy}, "
            f"verified={'yes' if item.verified else 'NO'}, "
            f"members={item.member_count}, "
            f"bytes={item.regular_bytes}, "
            f"symlinks={item.symlink_count}, "
            f"hardlinks={item.hardlink_count}"
        )


def _command_materialize(args: argparse.Namespace) -> int:
    result = materialize_profile(
        capsule_path=args.capsule,
        profile_id=args.profile,
        vault_root=args.vault_root,
        destination=args.destination,
    )

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_materialization(result)

    return 0 if result.complete else 1


def _print_removal(result: RemovalResult) -> None:
    print(f"Capsule:      {result.capsule_id}")
    print(f"Profile:      {result.profile_id}")
    print(f"Destination:  {result.destination}")
    print(f"Removed:      {'yes' if result.removed else 'NO'}")
    print(
        "State declared: "
        f"{result.persistent_state_declared}"
    )
    print(
        "State preservation confirmed: "
        + (
            "yes"
            if result.state_preservation_confirmed
            else "no"
        )
    )


def _command_remove_materialization(
    args: argparse.Namespace,
) -> int:
    result = remove_materialization(
        destination=args.destination,
        confirm_state_preserved=args.confirm_state_preserved,
    )

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_removal(result)

    return 0 if result.removed else 1



def _print_playable_materialization(
    result: PlayableMaterializationResult,
) -> None:
    print(f"Capsule:          {result.capsule_id}")
    print(f"Profile:          {result.profile_id}")
    print(f"Backend:          {result.backend}")
    print(f"Destination:      {result.destination}")
    print(f"Objects:          {result.object_count}")
    print(f"Protected files:  {result.protected_file_count}")
    print(f"State items:      {result.state_item_count}")
    print(f"Reused:           {'yes' if result.reused else 'no'}")
    print(f"Complete:         {'yes' if result.complete else 'NO'}")
    print(f"Receipt:          {result.receipt_id}")


def _print_play_result(result: PlayResult) -> None:
    print(f"Capsule:              {result.capsule_id}")
    print(f"Profile:              {result.profile_id}")
    print(f"Backend:              {result.backend}")
    print(f"Destination:          {result.destination}")
    print(f"Preparation ms:       {result.preparation_ms}")
    print(f"Process duration ms:  {result.process_duration_ms}")
    print(f"Wineserver wait ms:   {result.wineserver_wait_ms}")
    print(f"Total ms:             {result.total_ms}")
    print(f"Game process rc:      {result.game_process_rc}")
    print(f"Wineserver wait rc:   {result.wineserver_wait_rc}")
    print(f"Complete:             {'yes' if result.complete else 'NO'}")


def _command_materialize_playable(args: argparse.Namespace) -> int:
    materialization = materialize_playable_profile(
        capsule_path=args.capsule,
        profile_id=args.profile,
        vault_root=args.vault_root,
        destination=args.destination,
        state_backup=args.state_backup,
    )
    play_result = None
    if args.play:
        play_result = run_playable_profile(
            destination=args.destination,
        )

    if args.json:
        document: dict[str, object] = {
            "materialization": materialization.to_dict(),
        }
        if play_result is not None:
            document["play"] = play_result.to_dict()
        print(
            json.dumps(
                document,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_playable_materialization(materialization)
        if play_result is not None:
            print()
            _print_play_result(play_result)

    if not materialization.complete:
        return 1
    if play_result is not None and not play_result.complete:
        return (
            play_result.wineserver_wait_rc
            if play_result.wineserver_wait_rc != 0
            else play_result.game_process_rc
        )
    return 0


def _command_verify_playable(args: argparse.Namespace) -> int:
    result = verify_playable_profile(
        destination=args.destination,
    )
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"Capsule:          {result.capsule_id}")
        print(f"Profile:          {result.profile_id}")
        print(f"Backend:          {result.backend}")
        print(f"Destination:      {result.destination}")
        print(f"Protected files:  {result.protected_file_count}")
        print(f"Verified:         {'yes' if result.verified else 'NO'}")
    return 0 if result.verified else 1


def _command_run_playable(args: argparse.Namespace) -> int:
    arguments = list(args.arguments)
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    result = run_playable_profile(
        destination=args.destination,
        arguments=arguments,
    )
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_play_result(result)
    if result.wineserver_wait_rc != 0:
        return result.wineserver_wait_rc
    return result.game_process_rc


def _command_remove_playable(args: argparse.Namespace) -> int:
    result = remove_playable_profile(
        destination=args.destination,
        export_state=args.export_state,
        discard_state=args.discard_state,
    )
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"Capsule:                 {result.capsule_id}")
        print(f"Profile:                 {result.profile_id}")
        print(f"Backend:                 {result.backend}")
        print(f"Destination:             {result.destination}")
        print(
            "Changed state detected:  "
            + ("yes" if result.changed_state_detected else "no")
        )
        print(
            "State exported:           "
            + ("yes" if result.state_exported else "no")
        )
        print(
            "Discard authorized:       "
            + ("yes" if result.discard_state_authorized else "no")
        )
        print(f"Removed:                  {'yes' if result.removed else 'NO'}")
    return 0 if result.removed else 1



def _print_umu_materialization(
    result: UmuMaterializationResult,
) -> None:
    print(f"Capsule:       {result.capsule_id}")
    print(f"Profile:       {result.profile_id}")
    print(f"Backend:       {result.backend}")
    print(f"Destination:   {result.destination}")
    print(f"Objects:       {result.object_count}")
    print(f"Selected save: {result.selected_save or '(none)'}")
    print(f"Complete:      {'yes' if result.complete else 'NO'}")
    print(f"Receipt:       {result.receipt_id}")


def _print_umu_verification(
    result: UmuVerificationResult,
) -> None:
    print(f"Capsule:          {result.capsule_id}")
    print(f"Profile:          {result.profile_id}")
    print(f"Backend:          {result.backend}")
    print(f"Destination:      {result.destination}")
    print(f"Protected files:  {result.protected_file_count}")
    print(f"Symlinks:         {result.symlink_count}")
    print(f"Verified:         {'yes' if result.verified else 'NO'}")


def _print_umu_run(result: UmuRunResult) -> None:
    print(f"Capsule:              {result.capsule_id}")
    print(f"Profile:              {result.profile_id}")
    print(f"Backend:              {result.backend}")
    print(f"Destination:          {result.destination}")
    print(f"Process rc:           {result.process_rc}")
    print(f"Duration ms:          {result.duration_ms}")
    print(f"Sanitizer rc:         {result.sanitizer_rc}")
    print(
        "Verified after run:   "
        + ("yes" if result.verified_after_run else "NO")
    )
    print(f"Complete:             {'yes' if result.complete else 'NO'}")


def _command_materialize_umu(args: argparse.Namespace) -> int:
    result = materialize_umu_profile(
        capsule_path=args.capsule,
        profile_id=args.profile,
        vault_root=args.vault_root,
        destination=args.destination,
        state_root=args.state_root,
        save_id=args.save,
    )
    run_result = None
    if args.play:
        run_result = run_umu_materialization(
            destination=args.destination,
        )

    if args.json:
        document: dict[str, object] = {
            "materialization": result.to_dict(),
        }
        if run_result is not None:
            document["run"] = run_result.to_dict()
        print(
            json.dumps(
                document,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_umu_materialization(result)
        if run_result is not None:
            print()
            _print_umu_run(run_result)

    if not result.complete:
        return 1
    if run_result is not None and not run_result.complete:
        return (
            run_result.process_rc
            if run_result.process_rc != 0
            else run_result.sanitizer_rc or 1
        )
    return 0


def _command_verify_umu(args: argparse.Namespace) -> int:
    result = verify_umu_materialization(
        destination=args.destination,
    )
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_umu_verification(result)
    return 0 if result.verified else 1


def _command_run_umu(args: argparse.Namespace) -> int:
    arguments = list(args.arguments)
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    result = run_umu_materialization(
        destination=args.destination,
        arguments=arguments,
    )
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_umu_run(result)
    if result.process_rc != 0:
        return result.process_rc
    if result.sanitizer_rc != 0:
        return result.sanitizer_rc
    return 0 if result.complete else 1


def _command_remove_umu(args: argparse.Namespace) -> int:
    result = remove_umu_materialization(
        destination=args.destination,
        confirm_state_preserved=args.confirm_state_preserved,
    )
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"Capsule:          {result.capsule_id}")
        print(f"Profile:          {result.profile_id}")
        print(f"Backend:          {result.backend}")
        print(f"Destination:      {result.destination}")
        print(f"Selected save:    {result.selected_save or '(none)'}")
        print(
            "State confirmed:  "
            + (
                "yes"
                if result.state_preservation_confirmed
                else "no"
            )
        )
        print(f"Removed:          {'yes' if result.removed else 'NO'}")
    return 0 if result.removed else 1


def _print_bottles_deployment(
    result: BottlesDeploymentResult,
) -> None:
    print(f"Capsule:          {result.capsule_id}")
    print(f"Profile:          {result.profile_id}")
    print(f"Bottle:           {result.bottle_name}")
    print(f"Source object:    {result.source_object_id}")
    print(f"Runner:           {result.runner}")
    print(f"Entrypoint:       {result.entrypoint}")
    print(f"Network:          {result.network}")
    print(f"Regular bytes:    {result.regular_bytes}")
    print(f"Files:            {result.file_count}")
    print(f"Directories:      {result.directory_count}")
    print(f"Symlinks:         {result.symlink_count}")
    print(f"Complete:         {'yes' if result.complete else 'NO'}")
    print(f"Deployment ID:    {result.deployment_id}")


def _command_deploy_bottles(args: argparse.Namespace) -> int:
    result = deploy_bottles_profile(
        capsule_path=args.capsule,
        profile_id=args.profile,
        materialization=args.materialization,
        bottles_path=args.bottles_path,
        bottle_name=args.name,
    )

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_bottles_deployment(result)

    return 0 if result.complete else 1


def _print_bottles_verification(
    result: BottlesDeploymentVerification,
) -> None:
    print(f"Capsule:              {result.capsule_id}")
    print(f"Profile:              {result.profile_id}")
    print(f"Bottle:               {result.bottle_name}")
    print(f"Runner:               {result.runner}")
    print(f"Entrypoint:           {result.entrypoint}")
    print(f"Network:              {result.network}")
    print(
        "Receipt valid:        "
        + ("yes" if result.receipt_valid else "NO")
    )
    print(
        "Configuration valid:  "
        + ("yes" if result.configuration_valid else "NO")
    )
    print(
        "Entrypoint present:   "
        + ("yes" if result.entrypoint_present else "NO")
    )
    print(
        "Verified:             "
        + ("yes" if result.verified else "NO")
    )


def _command_verify_bottles(args: argparse.Namespace) -> int:
    result = verify_bottles_deployment(
        bottles_path=args.bottles_path,
        bottle_name=args.name,
    )

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_bottles_verification(result)

    return 0 if result.verified else 1


def _print_bottles_launch_plan(result: BottlesLaunchPlan) -> None:
    print(f"Capsule:      {result.capsule_id}")
    print(f"Profile:      {result.profile_id}")
    print(f"Bottle:       {result.bottle_name}")
    print(f"Entrypoint:   {result.entrypoint}")
    print(f"Network:      {result.network}")
    print(f"Flatpak app:  {result.flatpak_app}")
    print("Command:")
    print("  " + " ".join(result.command))


def _command_plan_bottles_launch(args: argparse.Namespace) -> int:
    result, _ = build_bottles_launch_plan(
        bottles_path=args.bottles_path,
        bottle_name=args.name,
        flatpak_app=args.flatpak_app,
    )

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_bottles_launch_plan(result)

    return 0


def _command_run_bottles(args: argparse.Namespace) -> int:
    result, returncode = run_bottles_deployment(
        bottles_path=args.bottles_path,
        bottle_name=args.name,
        flatpak_app=args.flatpak_app,
    )

    if args.json:
        document = result.to_dict()
        document["returncode"] = returncode
        print(
            json.dumps(
                document,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"Bottle:      {result.bottle_name}")
        print(f"Network:     {result.network}")
        print(f"Return code: {returncode}")

    return returncode


def _print_bottles_removal(result: BottlesRemovalResult) -> None:
    print(f"Capsule:      {result.capsule_id}")
    print(f"Profile:      {result.profile_id}")
    print(f"Bottle:       {result.bottle_name}")
    print(f"Removed:      {'yes' if result.removed else 'NO'}")
    print(
        "State declared: "
        f"{result.persistent_state_declared}"
    )
    print(
        "State preservation confirmed: "
        + (
            "yes"
            if result.state_preservation_confirmed
            else "no"
        )
    )
    print(
        "Stopped confirmed: "
        + ("yes" if result.stopped_confirmed else "no")
    )


def _command_remove_bottles(args: argparse.Namespace) -> int:
    result = remove_bottles_deployment(
        bottles_path=args.bottles_path,
        bottle_name=args.name,
        confirm_state_preserved=args.confirm_state_preserved,
        confirm_stopped=args.confirm_stopped,
    )

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_bottles_removal(result)

    return 0 if result.removed else 1



def _print_capsule_audit(result: CapsuleAuditResult) -> None:
    print(f"Capsule:           {result.capsule_id or '(invalid)'}")
    print(f"Objects:           {result.object_count}")
    print(f"Profiles:          {result.profile_count}")
    print(f"Persistent state:  {result.persistent_state_count}")
    print(f"Backup state:      {result.backup_state_count}")
    print(
        "Definition digest: "
        f"{result.state_definition_digest or '(unavailable)'}"
    )
    print(f"Errors:            {result.error_count}")
    print(f"Warnings:          {result.warning_count}")
    print(f"Valid:             {'yes' if result.valid else 'NO'}")
    print(
        "Operational:       "
        + ("yes" if result.operational else "no")
    )
    for issue in result.issues:
        print(
            f"  - {issue.severity}: {issue.code} "
            f"at {issue.context}: {issue.message}"
        )


def _command_audit_capsule(args: argparse.Namespace) -> int:
    result = audit_capsule(capsule_path=args.capsule)
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_capsule_audit(result)
    return 0 if result.valid else 1


def _print_state_backup(result: StateBackupResult) -> None:
    print(f"Capsule:             {result.capsule_id}")
    print(f"Backup ID:           {result.backup_id}")
    print(f"Kind:                {result.backup_kind}")
    print(f"State items:         {result.item_count}")
    print(f"Present:             {result.present_count}")
    print(f"Missing:             {result.missing_count}")
    print(f"Bytes:               {result.total_bytes}")
    print(
        "Stopped confirmed:   "
        + ("yes" if result.stopped_confirmed else "NO")
    )
    print(f"Complete:            {'yes' if result.complete else 'NO'}")
    for item in result.items:
        print(
            f"  - {item.id}: {item.entry_type}, "
            f"present={'yes' if item.present else 'no'}, "
            f"files={item.file_count}, "
            f"directories={item.directory_count}, "
            f"bytes={item.bytes}"
        )


def _command_preserve_state(args: argparse.Namespace) -> int:
    result = preserve_state(
        capsule_path=args.capsule,
        state_root=args.state_root,
        backup=args.backup,
        confirm_stopped=args.confirm_stopped,
    )
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_state_backup(result)
    return 0 if result.complete else 1


def _print_state_verification(
    result: StateBackupVerification,
) -> None:
    print(f"Capsule:      {result.capsule_id}")
    print(f"Backup ID:    {result.backup_id or '(unavailable)'}")
    print(f"Kind:         {result.backup_kind or '(unavailable)'}")
    print(f"State items:  {result.item_count}")
    print(f"Present:      {result.present_count}")
    print(f"Missing:      {result.missing_count}")
    print(f"Bytes:        {result.total_bytes}")
    print(f"Verified:     {'yes' if result.verified else 'NO'}")
    for problem in result.problems:
        print(f"  - {problem}")


def _command_verify_state_backup(
    args: argparse.Namespace,
) -> int:
    result = verify_state_backup(
        capsule_path=args.capsule,
        backup=args.backup,
    )
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_state_verification(result)
    return 0 if result.verified else 1


def _print_state_restore(result: StateRestoreResult) -> None:
    print(f"Capsule:             {result.capsule_id}")
    print(f"Restore ID:          {result.restore_id}")
    print(f"Backup ID:           {result.backup_id}")
    print(f"Snapshot backup ID:  {result.snapshot_backup_id}")
    print(f"State items:         {result.item_count}")
    print(f"Restored:            {result.restored_count}")
    print(f"Missing restored:    {result.missing_count}")
    print(
        "Stopped confirmed:   "
        + ("yes" if result.stopped_confirmed else "NO")
    )
    print(
        "Rollback performed:  "
        + ("yes" if result.rollback_performed else "no")
    )
    print(
        "Rollback complete:   "
        + ("yes" if result.rollback_complete else "NO")
    )
    print(f"Complete:            {'yes' if result.complete else 'NO'}")


def _command_restore_state(args: argparse.Namespace) -> int:
    result = restore_state(
        capsule_path=args.capsule,
        state_root=args.state_root,
        backup=args.backup,
        snapshot=args.snapshot,
        confirm_stopped=args.confirm_stopped,
    )
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_state_restore(result)
    return 0 if result.complete else 1


def _command_list_preserved_runners(args: argparse.Namespace) -> int:
    runners, warnings = scan_runners(args.collection_root)
    document = {
        "schema": 0,
        "runners": [item.to_dict() for item in runners],
        "warnings": list(warnings),
    }
    if args.json:
        print(
            json.dumps(
                document,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        for runner in runners:
            backends = ", ".join(runner.compatible_backends)
            print(
                f"{runner.runner_id}: {runner.kind}, {runner.size} bytes, "
                f"{backends}"
            )
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
    return 0


def _command_list_shared_umu_runtimes(
    args: argparse.Namespace,
) -> int:
    component_sets = list_shared_umu_runtimes(
        args.collection_root
    )

    document = {
        "schema": 0,
        "component_sets": [
            {
                "component_set_id": (
                    item.component_set_id
                ),
                "component_set_digest": (
                    "sha256:"
                    + item.component_set_digest
                ),
                "backend_component_id": (
                    item.backend_object_id
                ),
                "runtime_component_id": (
                    item.runtime_object_id
                ),
                "backend_entrypoint": (
                    item.backend_entrypoint
                ),
                "runtime_var": item.runtime_var,
                "runtime_family": (
                    item.runtime_family
                ),
                "platform_prefix": (
                    item.platform_prefix
                ),
                "platform_directory": (
                    item.platform_directory
                ),
            }
            for item in component_sets
        ],
    }

    if args.json:
        print(
            json.dumps(
                document,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        for item in component_sets:
            print(
                f"{item.component_set_id}: "
                f"backend={item.backend_object_id}; "
                f"runtime={item.runtime_object_id}; "
                f"family={item.runtime_family}; "
                f"entrypoint="
                f"{item.backend_entrypoint}"
            )

    return 0
def _command_discover_bottles_path(args: argparse.Namespace) -> int:
    path = discover_bottles_path(flatpak_app=args.flatpak_app)
    document = {
        "schema": 0,
        "flatpak_app": args.flatpak_app,
        "bottles_path": str(path),
    }
    if args.json:
        print(
            json.dumps(
                document,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(path)
    return 0


def _command_migrate_bottles_contract(
    args: argparse.Namespace,
) -> int:
    from .bottles_adapter import DEFAULT_FLATPAK_APP
    from .capsule_migrator import (
        MigrationError,
        migrate_bottles_contract,
    )

    flatpak_app = args.flatpak_app or DEFAULT_FLATPAK_APP
    try:
        report = migrate_bottles_contract(
            capsule_path=args.capsule,
            flatpak_app=flatpak_app,
            dry_run=args.dry_run,
            force=args.force,
        )
    except MigrationError as exc:
        print(f"ogv: error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                report,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        if report.get("already_migrated"):
            print(
                f"Capsule {report['capsule_id']}: already migrated "
                f"({report['contract_path']})"
            )
        elif report.get("dry_run"):
            print(
                f"[dry-run] Capsule {report['capsule_id']}: would "
                f"replace {report['legacy_contract_path']} with "
                f"{report['new_contract_path']}"
            )
        else:
            print(
                f"Capsule {report['capsule_id']}: migrated "
                f"{report['legacy_contract_path']} -> "
                f"{report['new_contract_path']} "
                f"(legacy id: {report['legacy_contract_id']})"
            )
    return 0


def _command_compose(args: argparse.Namespace) -> int:
    # Cross-backend validation: --state-root and --save-id only apply
    # to UMU; they name concepts (preserved state_archives, save
    # selection) that have no equivalent under direct-wine or
    # Bottles.
    if args.backend != "umu":
        if args.state_root is not None:
            raise CompositionError(
                "--state-root is only supported for --backend umu, "
                f"not {args.backend}."
            )
        if args.save_id is not None:
            raise CompositionError(
                "--save-id is only supported for --backend umu, "
                f"not {args.backend}."
            )
    # Fresh-start is user intent: no selectable/restored save, while
    # retaining backend-required initial configuration. It is distinct from
    # the stronger --no-state operator control.
    #
    # Use getattr for compatibility with callers/tests that construct an
    # argparse.Namespace directly instead of going through build_parser().
    fresh_start = bool(getattr(args, "fresh_start", False))
    if fresh_start:
        if args.no_state:
            raise CompositionError(
                "--fresh-start and --no-state are mutually exclusive."
            )
        if args.state_backup is not None:
            raise CompositionError(
                "--fresh-start and --state-backup are mutually exclusive."
            )
        if args.save_id is not None:
            raise CompositionError(
                "--fresh-start and --save-id are mutually exclusive."
            )

    # --no-state cross-arg validation: mutually exclusive with
    # --state-backup (both describe how state is provisioned) and
    # with --save-id (there is no save to select when state is
    # skipped). Silencing either would obscure the operator's intent.
    if args.no_state:
        if args.state_backup is not None:
            raise CompositionError(
                "--no-state and --state-backup are mutually exclusive; "
                "--no-state skips all state, --state-backup injects one."
            )
        if args.save_id is not None:
            raise CompositionError(
                "--no-state and --save-id are mutually exclusive; "
                "--no-state skips all state, including selectable saves."
            )
    common = {
        "collection_root": args.collection_root,
        "capsule_path": args.capsule,
        "runner_id": args.runner,
        "source_profile_id": args.source_profile,
        "play": args.play,
    }
    forwarded = tuple(args.arguments)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]

    if args.backend == "direct-wine":
        if args.destination is None:
            raise CompositionError(
                "--destination is required for direct-wine."
            )
        result = compose_wine(
            **common,
            destination=args.destination,
            state_backup=args.state_backup,
            fresh_start=fresh_start,
            no_state=args.no_state,
            arguments=forwarded,
        )
    elif args.backend == "umu":
        if args.destination is None:
            raise CompositionError("--destination is required for UMU.")
        result = compose_umu(
            **common,
            destination=args.destination,
            state_backup=args.state_backup,
            state_root=args.state_root,
            save_id=args.save_id,
            fresh_start=fresh_start,
            no_state=args.no_state,
            arguments=forwarded,
        )
    else:
        if args.destination is None:
            raise CompositionError(
                "--destination is required for Bottles."
            )
        if not args.bottle_name:
            raise CompositionError(
                "--bottle-name is required for Bottles."
            )
        if forwarded:
            raise CompositionError(
                "Additional game arguments are not supported for Bottles."
            )
        result = compose_bottles(
            **common,
            destination=args.destination,
            bottles_path=args.bottles_path,
            bottle_name=args.bottle_name,
            state_backup=args.state_backup,
            fresh_start=fresh_start,
            no_state=args.no_state,
        )

    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"Capsule:      {result.capsule_id}")
        print(f"Backend:      {result.backend}")
        print(f"Runner:       {result.runner_id}")
        print(f"Profile:      {result.profile_id}")
        print(f"Destination:  {result.destination}")
        print(f"Materialized: {'yes' if result.materialized else 'NO'}")
        print(f"Played:       {'yes' if result.played else 'no'}")
        if result.play_complete is not None:
            print(
                "Play complete: "
                + ("yes" if result.play_complete else "NO")
            )
    return 0 if result.play_complete is not False else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ogv",
        description="Offline Game Vault orchestrator.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    audit_capsule_parser = commands.add_parser(
        "audit-capsule",
        help="Audit capsule structure and operational state declarations.",
    )
    audit_capsule_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Path to capsule.json.",
    )
    audit_capsule_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a sanitized machine-readable audit.",
    )
    audit_capsule_parser.set_defaults(
        handler=_command_audit_capsule
    )

    preserve_state_parser = commands.add_parser(
        "preserve-state",
        help="Create an atomic private backup of declared state.",
    )
    preserve_state_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Operational private capsule.",
    )
    preserve_state_parser.add_argument(
        "--state-root",
        type=Path,
        required=True,
        help=(
            "Root below which persistent_state paths are resolved."
        ),
    )
    preserve_state_parser.add_argument(
        "--backup",
        type=Path,
        required=True,
        help="New private backup directory; it must not exist.",
    )
    preserve_state_parser.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="Confirm all writers of the declared state are stopped.",
    )
    preserve_state_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a sanitized machine-readable result.",
    )
    preserve_state_parser.set_defaults(
        handler=_command_preserve_state
    )

    verify_state_parser = commands.add_parser(
        "verify-state-backup",
        help="Verify one private state backup against its capsule.",
    )
    verify_state_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Operational private capsule.",
    )
    verify_state_parser.add_argument(
        "--backup",
        type=Path,
        required=True,
        help="Private backup directory.",
    )
    verify_state_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a sanitized machine-readable result.",
    )
    verify_state_parser.set_defaults(
        handler=_command_verify_state_backup
    )

    restore_state_parser = commands.add_parser(
        "restore-state",
        help=(
            "Restore verified state after an atomic pre-restore snapshot."
        ),
    )
    restore_state_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Operational private capsule.",
    )
    restore_state_parser.add_argument(
        "--state-root",
        type=Path,
        required=True,
        help=(
            "Root below which persistent_state paths are resolved."
        ),
    )
    restore_state_parser.add_argument(
        "--backup",
        type=Path,
        required=True,
        help="Verified private backup to restore.",
    )
    restore_state_parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help=(
            "New private directory for the mandatory pre-restore "
            "snapshot and restore receipt."
        ),
    )
    restore_state_parser.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="Confirm all writers of the declared state are stopped.",
    )
    restore_state_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a sanitized machine-readable result.",
    )
    restore_state_parser.set_defaults(
        handler=_command_restore_state
    )

    plan = commands.add_parser(
        "plan",
        help="Build a read-only materialization plan.",
    )
    plan.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Path to capsule.json.",
    )
    plan.add_argument(
        "--profile",
        required=True,
        help="Execution profile ID.",
    )
    plan.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Root of the immutable vault.",
    )
    plan.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="Host-local materialization destination.",
    )
    plan.add_argument(
        "--allow-missing",
        action="store_true",
        help=(
            "Produce the plan even when required object files are absent. "
            "Missing objects remain explicit in the output."
        ),
    )
    plan.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    plan.set_defaults(handler=_command_plan)

    verify = commands.add_parser(
        "verify-object",
        help="Verify one immutable regular-file object.",
    )
    verify.add_argument(
        "--capsule",
        type=Path,
        help="Capsule containing the object declaration.",
    )
    verify.add_argument(
        "--object-id",
        help="Object ID declared by the capsule.",
    )
    verify.add_argument(
        "--vault-root",
        type=Path,
        help="Root of the immutable vault.",
    )
    verify.add_argument(
        "--path",
        type=Path,
        help="Direct path to a regular-file object.",
    )
    verify.add_argument(
        "--digest",
        help="Expected lowercase sha256: digest in direct mode.",
    )
    verify.add_argument(
        "--expected-size",
        type=int,
        help="Optional expected byte count in direct mode.",
    )
    verify.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    verify.set_defaults(handler=_command_verify_object)


    ingest = commands.add_parser(
        "ingest-object",
        help="Verify and atomically ingest one object into the vault.",
    )
    ingest.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Source regular file outside or inside the host filesystem.",
    )
    ingest.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Root of the content-addressed vault.",
    )
    ingest.add_argument(
        "--capsule",
        type=Path,
        help="Capsule containing the object declaration.",
    )
    ingest.add_argument(
        "--object-id",
        help="Object ID declared by the capsule.",
    )
    ingest.add_argument(
        "--digest",
        help="Expected lowercase sha256: digest in direct mode.",
    )
    ingest.add_argument(
        "--expected-size",
        type=int,
        help="Optional expected byte count in direct mode.",
    )
    ingest.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    ingest.add_argument(
        "--format",
        help=(
            "Archive format used to generate the per-object manifest "
            "(tar, tar.gz, tar.zst, zip). In capsule mode the capsule is "
            "authoritative and this flag must agree with its declaration; "
            "a mismatch aborts the ingest. In direct mode this flag is "
            "required for the manifest to be generated at ingest time."
        ),
    )
    ingest.add_argument(
        "--no-manifest",
        action="store_true",
        dest="no_manifest",
        help=(
            "Skip automatic per-object manifest generation for this ingest. "
            "The manifest can be generated later with "
            "generate-object-manifest or generate-missing-manifests."
        ),
    )
    ingest.set_defaults(handler=_command_ingest_object)


    manifest = commands.add_parser(
        "generate-object-manifest",
        help=(
            "Compute the per-file manifest of one preserved object so a "
            "materialization can be verified without the Vault."
        ),
    )
    manifest.add_argument(
        "--capsule",
        type=Path,
        help="Capsule containing the object declaration (capsule mode).",
    )
    manifest.add_argument(
        "--object-id",
        help="Object ID declared by the capsule (capsule mode).",
    )
    manifest.add_argument(
        "--vault-root",
        type=Path,
        help=(
            "Root of the immutable Vault. Required in capsule mode; also "
            "used as the manifest destination unless --output is given."
        ),
    )
    manifest.add_argument(
        "--path",
        type=Path,
        help="Direct path to a regular-file object (direct mode).",
    )
    manifest.add_argument(
        "--digest",
        help="Expected lowercase sha256: digest in direct mode.",
    )
    manifest.add_argument(
        "--expected-size",
        type=int,
        help="Optional expected byte count in direct mode.",
    )
    manifest.add_argument(
        "--format",
        help=(
            "Archive format in direct mode: tar, tar.gz, tar.zst or zip. "
            "Taken from the capsule in capsule mode."
        ),
    )
    manifest.add_argument(
        "--source-root",
        help=(
            "Top-level directory of the archive. Detected automatically "
            "when omitted."
        ),
    )
    manifest.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the manifest and its sidecar here instead of the "
            "Vault's canonical location."
        ),
    )
    manifest.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute everything but do not write the manifest or sidecar."
        ),
    )
    manifest.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    manifest.set_defaults(handler=_command_generate_object_manifest)


    missing = commands.add_parser(
        "generate-missing-manifests",
        help=(
            "Walk the Vault and generate a manifest for every object that "
            "does not have a valid one yet."
        ),
    )
    missing.add_argument(
        "--collection-root",
        type=Path,
        required=True,
        help="Collection root (the directory that contains 01_IMMUTABLE_VAULT).",
    )
    missing.add_argument(
        "--vault-root",
        type=Path,
        help=(
            "Immutable Vault root. Defaults to "
            "<collection-root>/01_IMMUTABLE_VAULT."
        ),
    )
    missing.add_argument(
        "--limit",
        type=int,
        help=(
            "Process at most this many objects. Objects already covered by "
            "a valid manifest are free and do not count."
        ),
    )
    missing.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be generated without writing anything.",
    )
    missing.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    missing.set_defaults(handler=_command_generate_missing_manifests)


    ingest_profile_parser = commands.add_parser(
        "ingest-profile",
        help=(
            "Ingest and verify every object dependency of one profile."
        ),
    )
    ingest_profile_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Path to capsule.json.",
    )
    ingest_profile_parser.add_argument(
        "--profile",
        required=True,
        help="Execution profile ID.",
    )
    ingest_profile_parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Root of the content-addressed vault.",
    )
    ingest_profile_parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="OBJECT_ID=PATH",
        help=(
            "Explicit source for an absent dependency. "
            "Repeat once per source object."
        ),
    )
    ingest_profile_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a sanitized machine-readable result.",
    )
    ingest_profile_parser.set_defaults(
        handler=_command_ingest_profile
    )

    verify_profile_parser = commands.add_parser(
        "verify-profile",
        help="Verify every stored dependency of one profile.",
    )
    verify_profile_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Path to capsule.json.",
    )
    verify_profile_parser.add_argument(
        "--profile",
        required=True,
        help="Execution profile ID.",
    )
    verify_profile_parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Root of the content-addressed vault.",
    )
    verify_profile_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    verify_profile_parser.set_defaults(
        handler=_command_verify_profile
    )

    inventory_parser = commands.add_parser(
        "inventory",
        help="Verify and inventory all canonical objects in a vault.",
    )
    inventory_parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Root of the content-addressed vault.",
    )
    inventory_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Atomically write the deterministic JSON inventory. "
            "The path must be outside objects/."
        ),
    )
    inventory_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the deterministic JSON inventory.",
    )
    inventory_parser.set_defaults(handler=_command_inventory)


    materialize_parser = commands.add_parser(
        "materialize",
        help=(
            "Verify, safely stage, and atomically publish a profile."
        ),
    )
    materialize_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Path to capsule.json.",
    )
    materialize_parser.add_argument(
        "--profile",
        required=True,
        help="Execution profile ID.",
    )
    materialize_parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Root of the immutable vault.",
    )
    materialize_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help=(
            "New host-local destination outside the vault. "
            "It must not already exist."
        ),
    )
    materialize_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    materialize_parser.set_defaults(handler=_command_materialize)

    remove_parser = commands.add_parser(
        "remove-materialization",
        help="Safely detach and remove a recognized materialization.",
    )
    remove_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="Materialization directory containing its receipt.",
    )
    remove_parser.add_argument(
        "--confirm-state-preserved",
        action="store_true",
        help=(
            "Confirm that every preserve_on_remove state item "
            "has already been backed up."
        ),
    )
    remove_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    remove_parser.set_defaults(
        handler=_command_remove_materialization
    )


    materialize_playable_parser = commands.add_parser(
        "materialize-playable",
        help=(
            "Build or reuse a capsule-driven direct-Wine materialization."
        ),
    )
    materialize_playable_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Path to a private operational capsule.json.",
    )
    materialize_playable_parser.add_argument(
        "--profile",
        required=True,
        help="Direct-Wine profile ID with a playable contract.",
    )
    materialize_playable_parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Root of the immutable vault.",
    )
    materialize_playable_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="Host-local playable destination outside the vault.",
    )
    materialize_playable_parser.add_argument(
        "--state-backup",
        type=Path,
        help=(
            "Verified accepted-state backup. Required when the capsule "
            "declares persistent state."
        ),
    )
    materialize_playable_parser.add_argument(
        "--play",
        action="store_true",
        help="Launch after successful materialization or reuse.",
    )
    materialize_playable_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable result.",
    )
    materialize_playable_parser.set_defaults(
        handler=_command_materialize_playable
    )

    verify_playable_parser = commands.add_parser(
        "verify-playable",
        help="Verify a published playable materialization.",
    )
    verify_playable_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="Published playable materialization root.",
    )
    verify_playable_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable result.",
    )
    verify_playable_parser.set_defaults(
        handler=_command_verify_playable
    )

    run_playable_parser = commands.add_parser(
        "run-playable",
        help="Run a verified direct-Wine playable materialization.",
    )
    run_playable_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="Published playable materialization root.",
    )
    run_playable_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the play receipt after the process exits.",
    )
    run_playable_parser.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
        help="Additional game arguments after '--'.",
    )
    run_playable_parser.set_defaults(
        handler=_command_run_playable
    )

    remove_playable_parser = commands.add_parser(
        "remove-playable",
        help=(
            "Export or explicitly discard changed state, then remove a "
            "recognized playable materialization."
        ),
    )
    remove_playable_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="Published playable materialization root.",
    )
    removal_group = remove_playable_parser.add_mutually_exclusive_group()
    removal_group.add_argument(
        "--export-state",
        type=Path,
        help="Export and verify current state before removal.",
    )
    removal_group.add_argument(
        "--discard-state",
        action="store_true",
        help="Explicitly authorize removal of changed state.",
    )
    remove_playable_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable result.",
    )
    remove_playable_parser.set_defaults(
        handler=_command_remove_playable
    )


    materialize_umu_parser = commands.add_parser(
        "materialize-umu",
        help=(
            "Build or reuse a verified modular UMU materialization."
        ),
    )
    materialize_umu_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Path to capsule.json with an UMU contract.",
    )
    materialize_umu_parser.add_argument(
        "--profile",
        required=True,
        help="UMU execution profile ID.",
    )
    materialize_umu_parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Root of the immutable content-addressed vault.",
    )
    materialize_umu_parser.add_argument(
        "--state-root",
        type=Path,
        help=(
            "Directory containing required and selectable state archives."
        ),
    )
    materialize_umu_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="New writable materialization outside the vault.",
    )
    materialize_umu_parser.add_argument(
        "--save",
        help="Selectable save archive ID. Omit for a clean materialization.",
    )
    materialize_umu_parser.add_argument(
        "--play",
        action="store_true",
        help="Run after successful materialization.",
    )
    materialize_umu_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    materialize_umu_parser.set_defaults(
        handler=_command_materialize_umu
    )

    verify_umu_parser = commands.add_parser(
        "verify-umu",
        help="Verify a published modular UMU materialization.",
    )
    verify_umu_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="UMU materialization directory.",
    )
    verify_umu_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    verify_umu_parser.set_defaults(handler=_command_verify_umu)

    run_umu_parser = commands.add_parser(
        "run-umu",
        help="Run and sanitize a verified UMU materialization.",
    )
    run_umu_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="UMU materialization directory.",
    )
    run_umu_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    run_umu_parser.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
        help="Arguments passed to the capsule launcher after --.",
    )
    run_umu_parser.set_defaults(handler=_command_run_umu)

    remove_umu_parser = commands.add_parser(
        "remove-umu",
        help="Remove a recognized writable UMU derivative.",
    )
    remove_umu_parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="UMU materialization directory.",
    )
    remove_umu_parser.add_argument(
        "--confirm-state-preserved",
        action="store_true",
        help=(
            "Confirm that mutable state was preserved before removal."
        ),
    )
    remove_umu_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    remove_umu_parser.set_defaults(handler=_command_remove_umu)

    deploy_bottles_parser = commands.add_parser(
        "deploy-bottles",
        help=(
            "Copy a materialized bottle into Bottles as a mutable, "
            "non-overwriting derivative."
        ),
    )
    deploy_bottles_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Path to capsule.json.",
    )
    deploy_bottles_parser.add_argument(
        "--profile",
        required=True,
        help="Bottles execution profile ID.",
    )
    deploy_bottles_parser.add_argument(
        "--materialization",
        type=Path,
        required=True,
        help="Verified materialization directory.",
    )
    deploy_bottles_parser.add_argument(
        "--bottles-path",
        type=Path,
        required=True,
        help="Effective managed bottles directory from bottles-cli.",
    )
    deploy_bottles_parser.add_argument(
        "--name",
        required=True,
        help="New non-colliding mutable bottle name.",
    )
    deploy_bottles_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a sanitized machine-readable result.",
    )
    deploy_bottles_parser.set_defaults(
        handler=_command_deploy_bottles
    )

    verify_bottles_parser = commands.add_parser(
        "verify-bottles-deployment",
        help="Verify one managed OGV Bottles derivative.",
    )
    verify_bottles_parser.add_argument(
        "--bottles-path",
        type=Path,
        required=True,
        help="Effective managed bottles directory.",
    )
    verify_bottles_parser.add_argument(
        "--name",
        required=True,
        help="OGV deployment bottle name.",
    )
    verify_bottles_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    verify_bottles_parser.set_defaults(
        handler=_command_verify_bottles
    )

    launch_plan_parser = commands.add_parser(
        "plan-bottles-launch",
        help="Build a sanitized Bottles Flatpak launch plan.",
    )
    launch_plan_parser.add_argument(
        "--bottles-path",
        type=Path,
        required=True,
        help="Effective managed bottles directory.",
    )
    launch_plan_parser.add_argument(
        "--name",
        required=True,
        help="OGV deployment bottle name.",
    )
    launch_plan_parser.add_argument(
        "--flatpak-app",
        default="com.usebottles.bottles",
        help="Bottles Flatpak application ID.",
    )
    launch_plan_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a sanitized machine-readable plan.",
    )
    launch_plan_parser.set_defaults(
        handler=_command_plan_bottles_launch
    )

    run_bottles_parser = commands.add_parser(
        "run-bottles",
        help="Run a verified OGV deployment through Bottles Flatpak.",
    )
    run_bottles_parser.add_argument(
        "--bottles-path",
        type=Path,
        required=True,
        help="Effective managed bottles directory.",
    )
    run_bottles_parser.add_argument(
        "--name",
        required=True,
        help="OGV deployment bottle name.",
    )
    run_bottles_parser.add_argument(
        "--flatpak-app",
        default="com.usebottles.bottles",
        help="Bottles Flatpak application ID.",
    )
    run_bottles_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a sanitized launch result.",
    )
    run_bottles_parser.set_defaults(
        handler=_command_run_bottles
    )

    remove_bottles_parser = commands.add_parser(
        "remove-bottles-deployment",
        help="Remove a recognized mutable Bottles derivative.",
    )
    remove_bottles_parser.add_argument(
        "--bottles-path",
        type=Path,
        required=True,
        help="Effective managed bottles directory.",
    )
    remove_bottles_parser.add_argument(
        "--name",
        required=True,
        help="OGV deployment bottle name.",
    )
    remove_bottles_parser.add_argument(
        "--confirm-state-preserved",
        action="store_true",
        help=(
            "Confirm that all preserve_on_remove state was backed up."
        ),
    )
    remove_bottles_parser.add_argument(
        "--confirm-stopped",
        action="store_true",
        help=(
            "Confirm Bottles and all processes using the deployment "
            "are stopped."
        ),
    )
    remove_bottles_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    remove_bottles_parser.set_defaults(
        handler=_command_remove_bottles
    )

    discover_bottles_parser = commands.add_parser(
        "discover-bottles-path",
        help=(
            "Query the active Bottles Flatpak for its effective managed "
            "bottles directory without network access."
        ),
    )
    discover_bottles_parser.add_argument(
        "--flatpak-app",
        default="com.usebottles.bottles",
        help="Bottles Flatpak application ID.",
    )
    discover_bottles_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    discover_bottles_parser.set_defaults(
        handler=_command_discover_bottles_path
    )

    list_runners_parser = commands.add_parser(
        "list-preserved-runners",
        help="List structurally usable runners preserved in the collection.",
    )
    list_runners_parser.add_argument(
        "--collection-root",
        type=Path,
        required=True,
        help="Offline Game Vault collection root.",
    )
    list_runners_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    list_runners_parser.set_defaults(handler=_command_list_preserved_runners)

    list_umu_runtimes_parser = commands.add_parser(
        "list-shared-umu-runtimes",
        help="List reusable UMU/Python/Steam Runtime objects.",
    )
    list_umu_runtimes_parser.add_argument(
        "--collection-root",
        type=Path,
        required=True,
        help="Offline Game Vault collection root.",
    )
    list_umu_runtimes_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    list_umu_runtimes_parser.set_defaults(
        handler=_command_list_shared_umu_runtimes
    )

    migrate_bottles_parser = commands.add_parser(
        "migrate-bottles-contract",
        help=(
            "Rewrite a legacy Bottles host-contract into the modern "
            "neutral-contract shape (ogv-bottles-neutral-v1)."
        ),
    )
    migrate_bottles_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Path to capsule.json.",
    )
    migrate_bottles_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without touching any file.",
    )
    migrate_bottles_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite the destination host-contract if it already "
            "exists."
        ),
    )
    migrate_bottles_parser.add_argument(
        "--flatpak-app",
        default=None,
        help=(
            "Flatpak application id to declare in the modern contract. "
            "Defaults to bottles_adapter.DEFAULT_FLATPAK_APP."
        ),
    )
    migrate_bottles_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable report.",
    )
    migrate_bottles_parser.set_defaults(
        handler=_command_migrate_bottles_contract
    )

    composition_parser = commands.add_parser(
        "compose",
        help=(
            "Synthesize, materialize and optionally run a user-requested "
            "composition using only preserved Vault objects."
        ),
    )
    composition_parser.add_argument(
        "--collection-root",
        type=Path,
        required=True,
        help="Offline Game Vault collection root.",
    )
    composition_parser.add_argument(
        "--capsule",
        type=Path,
        required=True,
        help="Source capsule.json.",
    )
    composition_parser.add_argument(
        "--backend",
        choices=("bottles", "direct-wine", "umu"),
        required=True,
        help="Requested backend assembled from technically compatible preserved components.",
    )
    composition_parser.add_argument(
        "--runner",
        required=True,
        help="Preserved runner ID from list-preserved-runners.",
    )
    composition_parser.add_argument(
        "--source-profile",
        help=(
            "Optional source profile ID. Required only when more than one "
            "compatible source layout exists."
        ),
    )
    composition_parser.add_argument(
        "--destination",
        type=Path,
        help="New external materialization destination.",
    )
    composition_parser.add_argument(
        "--state-root",
        type=Path,
        help=(
            "Directory holding preserved umu.state_archives tarballs. "
            "Only valid with --backend umu against a capsule that ships "
            "a preserved UMU rich contract. Defaults to "
            "<collection_root>/03_PERSISTENT_STATE/<capsule_id>/ when "
            "that directory exists."
        ),
    )
    composition_parser.add_argument(
        "--save-id",
        help=(
            "ID of the selectable state archive to include (in "
            "addition to any 'always' archives). Only valid with "
            "--backend umu against a preserved UMU rich contract."
        ),
    )
    composition_parser.add_argument(
        "--state-backup",
        type=Path,
        help=(
            "Verified persistent-state backup for the selected backend. "
            "Required when the capsule declares preservable state."
        ),
    )
    composition_parser.add_argument(
        "--fresh-start",
        action="store_true",
        help=(
            "Start without restoring a selectable or generic saved-game "
            "state while preserving backend-required initial configuration. "
            "For preserved UMU-native contracts, policy='always' archives "
            "are still applied and no selectable save is chosen. Mutually "
            "exclusive with --no-state, --state-backup and --save-id."
        ),
    )
    composition_parser.add_argument(
        "--no-state",
        action="store_true",
        help=(
            "Materialize cold: skip the persistent-state requirement. "
            "Under UMU with a preserved umu-native contract this also "
            "skips 'always' policy state_archives. The materialization "
            "may not launch cleanly if the capsule relied on that "
            "state for initial configuration. Mutually exclusive with "
            "--state-backup and --save-id."
        ),
    )
    composition_parser.add_argument(
        "--bottles-path",
        type=Path,
        help=(
            "Optional assertion of the managed Bottles directory. The core "
            "always discovers the effective path via bottles-cli and rejects "
            "a different value."
        ),
    )
    composition_parser.add_argument(
        "--bottle-name",
        help="New Bottles derivative name.",
    )
    composition_parser.add_argument(
        "--play",
        action="store_true",
        help="Launch after materialization.",
    )
    composition_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    composition_parser.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
        help="Additional Direct-Wine or UMU game arguments after --.",
    )
    composition_parser.set_defaults(handler=_command_compose)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.handler(args))
    except (
        PlanError,
        VerifyError,
        IngestError,
        ProfileStoreError,
        InventoryError,
        MaterializationError,
        PlayableError,
        UmuAdapterError,
        BottlesAdapterError,
        StateError,
        CompositionError,
        RunnerCatalogError,
        ObjectManifestError,
        ManifestCatalogError,
    ) as exc:
        print(f"ogv: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ogv: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
