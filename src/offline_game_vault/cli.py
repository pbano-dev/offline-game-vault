"""Command-line interface for Offline Game Vault."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .bottles_adapter import (
    BottlesAdapterError,
    BottlesDeploymentResult,
    BottlesDeploymentVerification,
    BottlesLaunchPlan,
    BottlesRemovalResult,
    build_bottles_launch_plan,
    deploy_bottles_profile,
    remove_bottles_deployment,
    run_bottles_deployment,
    verify_bottles_deployment,
)
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


def _command_ingest_object(args: argparse.Namespace) -> int:
    destination_spec = _destination_spec_from_ingest_args(args)
    result = ingest_object(
        source=args.source,
        destination_spec=destination_spec,
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
        _print_text_ingest(result)

    return 0


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
    ingest.set_defaults(handler=_command_ingest_object)


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
        BottlesAdapterError,
        StateError,
    ) as exc:
        print(f"ogv: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ogv: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
