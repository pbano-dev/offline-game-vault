"""Command-line interface for Offline Game Vault."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .inventory import (
    InventoryError,
    VaultInventory,
    build_inventory,
    write_inventory_atomic,
)
from .planner import MaterializationPlan, PlanError, build_plan
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
    ) as exc:
        print(f"ogv: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ogv: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
