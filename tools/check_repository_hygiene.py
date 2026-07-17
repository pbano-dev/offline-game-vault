#!/usr/bin/env python3
"""Reject generated and temporary files from the repository tree."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable


FORBIDDEN_COMPONENTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    ".ipynb_checkpoints",
}

FORBIDDEN_TOP_LEVEL = {
    "build",
    "dist",
}

FORBIDDEN_BASENAMES = {
    ".coverage",
    ".DS_Store",
    "Thumbs.db",
}

FORBIDDEN_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".swp",
    ".swo",
    ".tmp",
    ".temp",
    ".bak",
    ".orig",
    ".rej",
}

FORBIDDEN_PREFIXES = {
    ".incoming-",
    ".#",
}


def _normalize(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.removeprefix("/")


def reasons_for_path(relative_path: str) -> list[str]:
    """Return all hygiene violations for a repository-relative path."""

    normalized = _normalize(relative_path)
    parts = PurePosixPath(normalized).parts
    reasons: list[str] = []

    if not parts:
        return reasons

    for component in parts:
        if component in FORBIDDEN_COMPONENTS:
            reasons.append(f"generated directory component: {component}")
        if component.endswith(".egg-info"):
            reasons.append(f"generated packaging metadata: {component}")

    if parts[0] in FORBIDDEN_TOP_LEVEL:
        reasons.append(f"generated top-level directory: {parts[0]}")

    basename = parts[-1]

    if basename in FORBIDDEN_BASENAMES:
        reasons.append(f"generated operating-system/tool file: {basename}")

    if basename.startswith(".coverage."):
        reasons.append("generated coverage data")

    for prefix in FORBIDDEN_PREFIXES:
        if basename.startswith(prefix):
            reasons.append(f"temporary filename prefix: {prefix}")

    for suffix in FORBIDDEN_SUFFIXES:
        if basename.endswith(suffix):
            reasons.append(f"temporary/generated suffix: {suffix}")

    if basename.endswith("~"):
        reasons.append("temporary filename suffix: ~")

    if basename.startswith("#") and basename.endswith("#"):
        reasons.append("editor temporary file")

    return sorted(set(reasons))


def tracked_paths(root: Path) -> list[str]:
    """Return Git-tracked paths exactly as recorded by the index."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git executable not found") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git ls-files failed for {root}: {message}"
        ) from exc

    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def filesystem_paths(root: Path) -> list[str]:
    """Return all files, symlinks, and directories except .git internals."""

    result: list[str] = []

    for current, directories, files in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        directories[:] = [
            name for name in directories if name != ".git"
        ]

        for name in directories:
            result.append(
                (current_path / name).relative_to(root).as_posix()
            )

        for name in files:
            result.append(
                (current_path / name).relative_to(root).as_posix()
            )

    return sorted(result)


def scan(paths: Iterable[str]) -> list[tuple[str, list[str]]]:
    """Return one entry per forbidden path."""

    violations: list[tuple[str, list[str]]] = []

    for path in sorted(set(paths)):
        reasons = reasons_for_path(path)
        if reasons:
            violations.append((path, reasons))

    return violations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reject generated and temporary files from an "
            "Offline Game Vault repository."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root.",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--tracked",
        action="store_true",
        help="Scan Git-tracked paths (default).",
    )
    mode.add_argument(
        "--filesystem",
        action="store_true",
        help="Scan the complete work tree, excluding .git.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()

    try:
        paths = (
            filesystem_paths(root)
            if args.filesystem
            else tracked_paths(root)
        )
    except RuntimeError as exc:
        print(f"hygiene: error: {exc}", file=sys.stderr)
        return 2

    violations = scan(paths)

    if violations:
        print(
            f"REPOSITORY HYGIENE FAILED: "
            f"{len(violations)} forbidden path(s)",
            file=sys.stderr,
        )
        for path, reasons in violations:
            print(f"- {path}", file=sys.stderr)
            for reason in reasons:
                print(f"    {reason}", file=sys.stderr)
        return 1

    mode = "filesystem" if args.filesystem else "tracked"
    print(
        f"REPOSITORY HYGIENE PASSED: "
        f"{len(paths)} {mode} path(s) checked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
