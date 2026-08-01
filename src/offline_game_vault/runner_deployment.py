from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile

from .preserved_runners import RunnerRecord
from .preserved_runners import RunnerCatalogError, validate_runner_record


class RunnerDeploymentError(RuntimeError):
    pass


def _safe_member(value: str, label: str) -> PurePosixPath:
    if not value or "\x00" in value or "\\" in value:
        raise RunnerDeploymentError(f"{label} is not a portable path")
    path = PurePosixPath(value.rstrip("/"))
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RunnerDeploymentError(f"{label} is not a safe relative path")
    return path


def _link_stays_inside(
    member: PurePosixPath,
    target: str,
    *,
    hardlink: bool,
) -> None:
    if not target or "\x00" in target or "\\" in target:
        raise RunnerDeploymentError("Runner archive contains an invalid link")
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise RunnerDeploymentError(
            "Runner archive contains an absolute link"
        )
    parts = list(target_path.parts)
    base = [] if hardlink else list(member.parent.parts)
    resolved = base
    for part in parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise RunnerDeploymentError(
                    "Runner archive link escapes its root"
                )
            resolved.pop()
        else:
            resolved.append(part)
    if not resolved or resolved[0] != member.parts[0]:
        raise RunnerDeploymentError(
            "Runner archive link escapes its top-level directory"
        )


def _decompressed_tar(
    archive: Path,
    temporary: Path,
    *,
    zstd_compressed: bool,
) -> Path:
    if not zstd_compressed:
        return archive
    zstd = shutil.which("zstd")
    if not zstd:
        raise RunnerDeploymentError(
            "A tar.zst runner requires the local zstd executable"
        )
    target = temporary / "runner.tar"
    with target.open("wb") as output:
        process = subprocess.run(
            [zstd, "--decompress", "--stdout", str(archive)],
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise RunnerDeploymentError(
            f"Could not decompress runner tar.zst: {detail}"
        )
    return target


def _extract_tar(
    archive: Path,
    destination: Path,
    expected_root: str,
    temporary: Path,
    *,
    zstd_compressed: bool,
) -> None:
    source = _decompressed_tar(
        archive,
        temporary,
        zstd_compressed=zstd_compressed,
    )
    try:
        with tarfile.open(source, mode="r:*") as handle:
            members = handle.getmembers()
            if not members:
                raise RunnerDeploymentError("Runner archive is empty")
            for member in members:
                path = _safe_member(member.name, "runner archive member")
                if path.parts[0] != expected_root:
                    raise RunnerDeploymentError(
                        "Runner archive has an unexpected top-level directory"
                    )
                if member.ischr() or member.isblk() or member.isfifo():
                    raise RunnerDeploymentError(
                        "Runner archive contains a special file"
                    )
                if member.issym():
                    _link_stays_inside(
                        path,
                        member.linkname,
                        hardlink=False,
                    )
                elif member.islnk():
                    _link_stays_inside(
                        path,
                        member.linkname,
                        hardlink=True,
                    )
            handle.extractall(
                destination,
                members=members,
                filter="data",
                numeric_owner=False,
            )
    except (OSError, tarfile.TarError) as exc:
        raise RunnerDeploymentError(
            f"Could not extract preserved runner archive: {exc}"
        ) from exc


def _zip_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def _extract_zip(
    archive: Path,
    destination: Path,
    expected_root: str,
) -> None:
    try:
        with zipfile.ZipFile(archive) as handle:
            infos = handle.infolist()
            if not infos:
                raise RunnerDeploymentError("Runner archive is empty")
            for info in infos:
                path = _safe_member(info.filename, "runner archive member")
                if path.parts[0] != expected_root:
                    raise RunnerDeploymentError(
                        "Runner archive has an unexpected top-level directory"
                    )
                target = destination.joinpath(*path.parts)
                mode = _zip_mode(info)
                file_type = stat.S_IFMT(mode)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if file_type == stat.S_IFLNK:
                    link_target = handle.read(info).decode(
                        "utf-8",
                        errors="strict",
                    )
                    _link_stays_inside(
                        path,
                        link_target,
                        hardlink=False,
                    )
                    target.symlink_to(link_target)
                    continue
                if file_type not in {0, stat.S_IFREG}:
                    raise RunnerDeploymentError(
                        "Runner ZIP contains a special file"
                    )
                with handle.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                permissions = stat.S_IMODE(mode) or 0o644
                target.chmod(permissions & 0o777)
    except (
        OSError,
        UnicodeError,
        zipfile.BadZipFile,
        RuntimeError,
    ) as exc:
        raise RunnerDeploymentError(
            f"Could not extract preserved runner ZIP: {exc}"
        ) from exc


def _resolved_payload(root: Path, relative: str, label: str) -> Path:
    path = _safe_member(relative, label)
    candidate = root.joinpath(*path.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RunnerDeploymentError(
            f"Installed runner {label} is unavailable or escapes its root"
        ) from exc
    if not resolved.is_file():
        raise RunnerDeploymentError(
            f"Installed runner {label} is not a regular file"
        )
    return resolved



_MARKER_NAME = ".ogv-preserved-runner.json"


def _tree_digest(root: Path) -> str:
    """Hash an installed runner tree without following symbolic links."""

    records: list[bytes] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == _MARKER_NAME:
            continue
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            record = f"d\0{relative}\0{mode:o}\n".encode("utf-8")
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            record = (
                f"l\0{relative}\0{mode:o}\0{target}\n"
            ).encode("utf-8", errors="strict")
        elif stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256()
            with path.open("rb", buffering=0) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            record = (
                f"f\0{relative}\0{mode:o}\0{metadata.st_size}\0"
                f"{digest.hexdigest()}\n"
            ).encode("utf-8")
        else:
            raise RunnerDeploymentError(
                f"Installed runner contains an unsupported file: {relative}"
            )
        records.append(record)
    overall = hashlib.sha256()
    for record in records:
        overall.update(record)
    return overall.hexdigest()


def _marker_payload(runner: RunnerRecord, tree_digest: str) -> dict[str, object]:
    return {
        "schema": 0,
        "runner_id": runner.runner_id,
        "archive_digest": runner.digest,
        "archive_size": runner.size,
        "source_root": runner.source_root,
        "tree_sha256": tree_digest,
    }


def _validate_existing_runner(destination: Path, runner: RunnerRecord) -> None:
    marker = destination / _MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        raise RunnerDeploymentError(
            "The Bottles runner destination already exists but was not "
            "installed from this Vault"
        )
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerDeploymentError(
            f"The preserved runner marker is invalid: {exc}"
        ) from exc
    expected = _marker_payload(runner, _tree_digest(destination))
    if document != expected:
        raise RunnerDeploymentError(
            "The existing Bottles runner differs from the preserved Vault object"
        )
    _resolved_payload(destination, runner.wine_path, "Wine executable")
    _resolved_payload(destination, runner.wineserver_path, "Wineserver executable")
    if runner.proton_path:
        _resolved_payload(destination, runner.proton_path, "Proton launcher")

def ensure_bottles_runner(
    collection_root: Path,
    bottles_path: Path,
    runner: RunnerRecord,
) -> tuple[Path, bool]:
    """Install one preserved runner into Bottles without network access.

    Returns ``(destination, created)``. Existing regular runner directories
    are left untouched and are validated by the normal Bottles inventory.
    """
    try:
        validate_runner_record(collection_root, runner)
    except RunnerCatalogError as exc:
        raise RunnerDeploymentError(str(exc)) from exc

    bottles = Path(bottles_path).resolve(strict=True)
    components = bottles.parent
    runners_root = components / "runners"
    if runners_root.is_symlink():
        raise RunnerDeploymentError("Bottles runners directory is a symlink")
    runners_root.mkdir(mode=0o755, exist_ok=True)
    runners_root = runners_root.resolve(strict=True)
    if runners_root.parent != components:
        raise RunnerDeploymentError(
            "Bottles runners directory is outside the components root"
        )

    destination = runners_root / runner.runner_id
    if destination.is_symlink():
        raise RunnerDeploymentError(
            "The selected Bottles runner destination is a symlink"
        )
    if destination.is_dir():
        destination = destination.resolve(strict=True)
        _validate_existing_runner(destination, runner)
        return destination, False
    if destination.exists():
        raise RunnerDeploymentError(
            "The selected Bottles runner destination already exists"
        )

    immutable_root = Path(collection_root).resolve(strict=True) / (
        "01_IMMUTABLE_VAULT"
    )
    archive_relative = _safe_member(
        runner.archive_path,
        "runner archive path",
    )
    archive = immutable_root.joinpath(*archive_relative.parts)
    try:
        archive = archive.resolve(strict=True)
        archive.relative_to(immutable_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RunnerDeploymentError(
            "The preserved runner archive is unavailable"
        ) from exc

    staging_parent = Path(
        tempfile.mkdtemp(
            prefix=".ogv-runner-",
            dir=runners_root,
        )
    )
    extraction = staging_parent / "extract"
    extraction.mkdir(mode=0o700)
    try:
        if runner.format == "zip":
            _extract_zip(archive, extraction, runner.source_root)
        elif runner.format in {"tar", "tar.gz", "tar.zst"}:
            _extract_tar(
                archive,
                extraction,
                runner.source_root,
                staging_parent,
                zstd_compressed=(runner.format == "tar.zst"),
            )
        else:
            raise RunnerDeploymentError(
                f"Unsupported runner archive format: {runner.format}"
            )

        source_root = extraction / runner.source_root
        if source_root.is_symlink() or not source_root.is_dir():
            raise RunnerDeploymentError(
                "Runner archive did not produce its declared root"
            )
        _resolved_payload(source_root, runner.wine_path, "Wine executable")
        _resolved_payload(
            source_root,
            runner.wineserver_path,
            "Wineserver executable",
        )
        if runner.proton_path:
            _resolved_payload(
                source_root,
                runner.proton_path,
                "Proton launcher",
            )

        marker = source_root / _MARKER_NAME
        marker.write_text(
            json.dumps(
                _marker_payload(runner, _tree_digest(source_root)),
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        marker.chmod(0o600)
        os.replace(source_root, destination)
        return destination.resolve(strict=True), True
    except FileExistsError as exc:
        raise RunnerDeploymentError(
            "Runner destination appeared during installation"
        ) from exc
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
