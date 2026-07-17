"""Command-line interface for Offline Game Vault."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .planner import MaterializationPlan, PlanError, build_plan


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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.handler(args))
    except PlanError as exc:
        print(f"ogv: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ogv: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
