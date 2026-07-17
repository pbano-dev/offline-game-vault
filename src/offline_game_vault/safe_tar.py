"""Safe extraction for preserved POSIX tar archives."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
from typing import Any


class SafeTarError(Exception):
    """Raised when a tar archive cannot be extracted safely."""


@dataclass(frozen=True)
class TarExtractionResult:
    schema: int
    member_count: int
    regular_file_count: int
    directory_count: int
    symlink_count: int
    hardlink_count: int
    regular_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _MemberPlan:
    member: tarfile.TarInfo
    relative: PurePosixPath
    kind: str
    link_target: PurePosixPath | None = None


def _normalize_member_name(name: str) -> PurePosixPath:
    if "\x00" in name:
        raise SafeTarError("Tar member name contains a NUL byte.")
    if "\\" in name:
        raise SafeTarError(
            f"Tar member uses a backslash path separator: {name!r}"
        )

    while name.startswith("./"):
        name = name[2:]

    path = PurePosixPath(name)
    if path.is_absolute():
        raise SafeTarError(f"Absolute tar member path: {name!r}")

    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise SafeTarError(f"Tar path traversal: {name!r}")
        parts.append(part)

    if not parts:
        raise SafeTarError(f"Empty tar member path: {name!r}")

    return PurePosixPath(*parts)


def _collapse_relative_link(
    *,
    member_path: PurePosixPath,
    link_name: str,
) -> PurePosixPath:
    if "\x00" in link_name:
        raise SafeTarError("Tar link target contains a NUL byte.")
    if "\\" in link_name:
        raise SafeTarError(
            f"Tar link target uses a backslash: {link_name!r}"
        )

    link_path = PurePosixPath(link_name)
    if link_path.is_absolute():
        raise SafeTarError(
            f"Absolute symbolic-link target: {link_name!r}"
        )

    parts = list(member_path.parent.parts)
    for part in link_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise SafeTarError(
                    "Symbolic-link target escapes extraction root: "
                    f"{member_path.as_posix()} -> {link_name}"
                )
            parts.pop()
        else:
            parts.append(part)

    if not parts:
        raise SafeTarError(
            "Symbolic-link target resolves to extraction root: "
            f"{member_path.as_posix()} -> {link_name}"
        )

    return PurePosixPath(*parts)


def _normalize_hardlink_target(link_name: str) -> PurePosixPath:
    return _normalize_member_name(link_name)


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "directory"
    if member.isreg():
        return "regular"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"

    raise SafeTarError(
        "Unsupported tar member type "
        f"{member.type!r}: {member.name!r}"
    )


def _plan_members(archive: tarfile.TarFile) -> list[_MemberPlan]:
    plans: list[_MemberPlan] = []
    seen: set[PurePosixPath] = set()

    for member in archive.getmembers():
        relative = _normalize_member_name(member.name)
        if relative in seen:
            raise SafeTarError(
                f"Duplicate tar member path: {relative.as_posix()}"
            )
        seen.add(relative)

        kind = _member_kind(member)
        link_target = None

        if kind == "symlink":
            link_target = _collapse_relative_link(
                member_path=relative,
                link_name=member.linkname,
            )
        elif kind == "hardlink":
            link_target = _normalize_hardlink_target(
                member.linkname
            )

        plans.append(
            _MemberPlan(
                member=member,
                relative=relative,
                kind=kind,
                link_target=link_target,
            )
        )

    return plans


def _target_path(root: Path, relative: PurePosixPath) -> Path:
    return root.joinpath(*relative.parts)


def _safe_mode(member: tarfile.TarInfo) -> int:
    return member.mode & 0o777


def _ensure_parent_directories(root: Path, target: Path) -> None:
    try:
        relative_parent = target.parent.relative_to(root)
    except ValueError as exc:
        raise SafeTarError(
            f"Extraction target escapes root: {target}"
        ) from exc

    current = root
    for component in relative_parent.parts:
        current = current / component

        if current.is_symlink():
            raise SafeTarError(
                f"Extraction parent is a symbolic link: {current}"
            )
        if current.exists():
            if not current.is_dir():
                raise SafeTarError(
                    f"Extraction parent is not a directory: {current}"
                )
            continue

        current.mkdir(mode=0o700)


def _write_regular_file(
    *,
    archive: tarfile.TarFile,
    plan: _MemberPlan,
    root: Path,
    chunk_size: int,
) -> None:
    target = _target_path(root, plan.relative)
    _ensure_parent_directories(root, target)

    if target.exists() or target.is_symlink():
        raise SafeTarError(
            f"Extraction would overwrite an existing path: {target}"
        )

    source = archive.extractfile(plan.member)
    if source is None:
        raise SafeTarError(
            f"Cannot read regular tar member: {plan.member.name!r}"
        )

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    file_descriptor: int | None = None
    written = 0

    try:
        file_descriptor = os.open(
            target,
            flags,
            _safe_mode(plan.member) or 0o600,
        )
        with source, os.fdopen(
            file_descriptor,
            "wb",
            buffering=0,
            closefd=True,
        ) as destination:
            file_descriptor = None

            while True:
                block = source.read(chunk_size)
                if not block:
                    break
                destination.write(block)
                written += len(block)

            destination.flush()
            os.fsync(destination.fileno())
    except OSError as exc:
        raise SafeTarError(
            f"Cannot extract regular member {plan.member.name!r}: {exc}"
        ) from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)

    if written != plan.member.size:
        raise SafeTarError(
            "Extracted byte count does not match tar header for "
            f"{plan.member.name!r}: expected {plan.member.size}, "
            f"wrote {written}"
        )

    try:
        os.chmod(target, _safe_mode(plan.member))
    except OSError as exc:
        raise SafeTarError(
            f"Cannot apply mode to {target}: {exc}"
        ) from exc


def _create_directory(plan: _MemberPlan, root: Path) -> None:
    target = _target_path(root, plan.relative)
    _ensure_parent_directories(root, target)

    if target.is_symlink():
        raise SafeTarError(
            f"Directory target is a symbolic link: {target}"
        )
    if target.exists() and not target.is_dir():
        raise SafeTarError(
            f"Directory target conflicts with a file: {target}"
        )

    target.mkdir(mode=0o700, exist_ok=True)


def _create_symlink(plan: _MemberPlan, root: Path) -> None:
    target = _target_path(root, plan.relative)
    _ensure_parent_directories(root, target)

    if target.exists() or target.is_symlink():
        raise SafeTarError(
            f"Symlink would overwrite an existing path: {target}"
        )

    try:
        os.symlink(plan.member.linkname, target)
    except OSError as exc:
        raise SafeTarError(
            f"Cannot create symbolic link {target}: {exc}"
        ) from exc


def _create_hardlink(plan: _MemberPlan, root: Path) -> None:
    if plan.link_target is None:
        raise SafeTarError("Hardlink plan has no target.")

    target = _target_path(root, plan.relative)
    source = _target_path(root, plan.link_target)
    _ensure_parent_directories(root, target)

    if target.exists() or target.is_symlink():
        raise SafeTarError(
            f"Hardlink would overwrite an existing path: {target}"
        )
    if source.is_symlink() or not source.is_file():
        raise SafeTarError(
            "Hardlink target must be an already extracted regular file: "
            f"{plan.member.linkname!r}"
        )

    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as exc:
        raise SafeTarError(
            f"Cannot create hardlink {target}: {exc}"
        ) from exc


def _apply_directory_modes(
    plans: list[_MemberPlan],
    root: Path,
) -> None:
    directory_plans = [
        plan for plan in plans if plan.kind == "directory"
    ]

    for plan in sorted(
        directory_plans,
        key=lambda item: len(item.relative.parts),
        reverse=True,
    ):
        target = _target_path(root, plan.relative)
        try:
            os.chmod(target, _safe_mode(plan.member))
        except OSError as exc:
            raise SafeTarError(
                f"Cannot apply directory mode to {target}: {exc}"
            ) from exc


def _fsync_directories(root: Path) -> None:
    directories = [root]
    directories.extend(
        path
        for path in root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    )

    for directory in sorted(
        directories,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError as exc:
            raise SafeTarError(
                f"Cannot open directory for fsync {directory}: {exc}"
            ) from exc

        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise SafeTarError(
                f"Cannot fsync directory {directory}: {exc}"
            ) from exc
        finally:
            os.close(descriptor)


def extract_tar_safely(
    *,
    archive_path: Path,
    destination: Path,
    chunk_size: int = 8 * 1024 * 1024,
) -> TarExtractionResult:
    """Extract one tar archive into a newly created directory."""

    if chunk_size <= 0:
        raise SafeTarError("chunk_size must be greater than zero")

    archive_path = archive_path.expanduser().absolute()
    destination = destination.expanduser().absolute()

    if archive_path.is_symlink():
        raise SafeTarError(
            f"Archive path must not be a symbolic link: {archive_path}"
        )
    if not archive_path.is_file():
        raise SafeTarError(
            f"Archive path must be a regular file: {archive_path}"
        )
    if destination.exists() or destination.is_symlink():
        raise SafeTarError(
            f"Extraction destination already exists: {destination}"
        )

    try:
        before = archive_path.stat()
        destination.mkdir(parents=True, mode=0o700)

        with tarfile.open(archive_path, mode="r:*") as archive:
            plans = _plan_members(archive)

            regular_bytes = sum(
                plan.member.size
                for plan in plans
                if plan.kind == "regular"
            )
            free_bytes = shutil.disk_usage(destination.parent).free
            if regular_bytes > free_bytes:
                raise SafeTarError(
                    "Insufficient free space for tar members: "
                    f"required {regular_bytes}, available {free_bytes}"
                )

            for plan in plans:
                if plan.kind == "directory":
                    _create_directory(plan, destination)

            for plan in plans:
                if plan.kind == "regular":
                    _write_regular_file(
                        archive=archive,
                        plan=plan,
                        root=destination,
                        chunk_size=chunk_size,
                    )

            for plan in plans:
                if plan.kind == "hardlink":
                    _create_hardlink(plan, destination)

            for plan in plans:
                if plan.kind == "symlink":
                    _create_symlink(plan, destination)

            _apply_directory_modes(plans, destination)
            _fsync_directories(destination)

        after = archive_path.stat()
    except (tarfile.TarError, OSError) as exc:
        raise SafeTarError(
            f"Cannot extract tar archive {archive_path}: {exc}"
        ) from exc
    except Exception:
        raise

    stable_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
    )
    changed = [
        field
        for field in stable_fields
        if getattr(before, field, None) != getattr(after, field, None)
    ]
    if changed:
        raise SafeTarError(
            "Archive changed during extraction "
            f"({', '.join(changed)}): {archive_path}"
        )

    return TarExtractionResult(
        schema=0,
        member_count=len(plans),
        regular_file_count=sum(
            plan.kind == "regular" for plan in plans
        ),
        directory_count=sum(
            plan.kind == "directory" for plan in plans
        ),
        symlink_count=sum(
            plan.kind == "symlink" for plan in plans
        ),
        hardlink_count=sum(
            plan.kind == "hardlink" for plan in plans
        ),
        regular_bytes=regular_bytes,
    )
