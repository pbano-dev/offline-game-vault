#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, re, subprocess, sys
from pathlib import Path

PATTERNS = {
    "Unix home": re.compile(
        r"(?<![A-Za-z0-9_])/(?:home|var/home)/"
        r"(?!<)[A-Za-z0-9._-]+(?:/|$)"
    ),
    "removable media user": re.compile(
        r"(?<![A-Za-z0-9_])/run/media/"
        r"(?!<)[A-Za-z0-9._-]+(?:/|$)"
    ),
    "runtime UID": re.compile(
        r"(?<![A-Za-z0-9_])/run/user/\d+(?:/|$)"
    ),
    "Windows user": re.compile(
        r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/]+Users[\\/]+"
        r"(?!<)[A-Za-z0-9._ -]+(?:[\\/]|$)"
    ),
}
TEXT = {
    ".cfg", ".ini", ".json", ".md", ".py", ".sh", ".toml",
    ".txt", ".vdf", ".xml", ".yaml", ".yml"
}

def tracked(root: Path) -> list[Path]:
    data = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE
    ).stdout
    return [root / p.decode() for p in data.split(b"\0") if p]

def scan(root: Path) -> list[str]:
    errors = []
    for path in tracked(root):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            if os.path.isabs(target):
                errors.append(f"{rel}: absolute symlink target")
            for label, pattern in PATTERNS.items():
                if pattern.search(target):
                    errors.append(f"{rel}: {label} in symlink target")
            continue
        if not path.is_file() or path.suffix.lower() not in TEXT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"{rel}: tracked text is not UTF-8")
            continue
        for label, pattern in PATTERNS.items():
            match = pattern.search(text)
            if match:
                errors.append(f"{rel}: possible {label}: {match.group(0)!r}")
    return errors

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path,
        default=Path(__file__).resolve().parents[1]
    )
    errors = scan(parser.parse_args().root.resolve())
    if errors:
        print(f"PUBLIC PRIVACY CHECK FAILED: {len(errors)}", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("PUBLIC PRIVACY CHECK PASSED")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
