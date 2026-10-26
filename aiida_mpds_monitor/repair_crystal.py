"""Check and rebuild a CRYSTAL archive, optionally uploading the repaired version."""

import argparse
import json
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

import py7zr

from .config import DEFAULT_CONFIG_PATH, get_archive_key, read_config, resolve_archive_upload_url
from .crystal_validation import STAGES, ValidationReport, validate_crystal_tree
from .webhook import send_archive

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArchiveLimits:
    max_members: int = 10_000
    max_unpacked_bytes: int = 20 * 1024**3


def _check_tree(root: Path, limits: ArchiveLimits) -> None:
    count, size = 0, 0
    for path in root.rglob("*"):
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("Calculation contents must contain only regular files and directories; symbolic links are forbidden")
        count += 1
        size += info.st_size if stat.S_ISREG(info.st_mode) else 0
        if count > limits.max_members or size > limits.max_unpacked_bytes:
            raise ValueError("Calculation contents exceed archive resource limits")


def _extract(source: Path, destination: Path, limits: ArchiveLimits = ArchiveLimits()) -> None:
    with py7zr.SevenZipFile(source, "r") as archive:
        if len(archive.files) > limits.max_members:
            raise ValueError("Archive has too many members")
        total_size = 0
        names = set()
        for entry in archive.files:
            name = PurePosixPath(entry.filename.replace("\\", "/"))
            if (name.is_absolute() or ".." in name.parts or ":" in str(name)
                    or str(name) in names or entry.is_symlink or entry.is_junction
                    or not (entry.is_file or entry.is_directory)):
                raise ValueError(f"Unsafe archive member: {entry.filename}")
            names.add(str(name))
            total_size += entry.uncompressed or 0
            if total_size > limits.max_unpacked_bytes:
                raise ValueError("Archive exceeds the unpacked size limit")
        archive.extractall(path=destination)


def _stage_root(root: Path) -> Path:
    parents = {path.parent for path in root.rglob("*")
               if path.is_dir() and path.name.upper() in STAGES}
    if len(parents) != 1:
        raise ValueError("Source must contain exactly one structure with CRYSTAL stage folders")
    return parents.pop()


def repair_archive(
    source: Path, output: Path, dry_run: bool = False,
    report_path: Optional[Path] = None,
    limits: ArchiveLimits = ArchiveLimits(),
) -> ValidationReport:
    """Remove invalid folders from a temporary copy; preserve the supplied source.

    The sibling JSON report survives deletion of the staged calculation folders.
    An invalid STRUCT is reported but never produces an uploadable archive.
    """
    source, output = Path(source).resolve(), Path(output).resolve()
    report_path = Path(report_path or output.with_suffix(".validation.json")).resolve()
    if source == output or source == report_path or output == report_path:
        raise ValueError("Source, output archive, and report must have different paths")
    if not dry_run and (output.exists() or report_path.exists()):
        raise FileExistsError("Output archive or report already exists; choose a new --output")
    if source.is_dir() and (source in output.parents or source in report_path.parents):
        raise ValueError("Output archive and report must be outside the source directory")
    if source.is_dir():
        _check_tree(source, limits)
        if dry_run:
            # Validation is read-only; do not copy potentially enormous wavefunctions.
            return validate_crystal_tree(_stage_root(source))
    with tempfile.TemporaryDirectory(prefix="mpds-crystal-") as temporary:
        staging = Path(temporary) / "contents"
        if source.is_dir():
            shutil.copytree(source, staging, symlinks=True)
        elif source.is_file() and source.suffix.lower() == ".7z":
            staging.mkdir()
            _extract(source, staging, limits)
        else:
            raise ValueError("Source must be a calculation directory or a .7z archive")
        # Never dereference links while validating or building the repaired archive.
        _check_tree(staging, limits)
        root = _stage_root(staging)
        report = validate_crystal_tree(root, remove_invalid=not dry_run)
        if dry_run:
            return report
        report.write(report_path, overwrite=False)
        if not report.uploadable:
            return report
        output.parent.mkdir(parents=True, exist_ok=True)
        # Build beside the destination and expose the archive only after close succeeds.
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".7z", delete=False) as handle:
            staged_archive = Path(handle.name)
        try:
            with py7zr.SevenZipFile(staged_archive, "w") as archive:
                for result in report.stages:
                    if result.status == "valid":
                        for path in sorted((root / result.folder).rglob("*")):
                            if path.is_file():
                                archive.write(path, arcname=str(path.relative_to(root)))
            os.link(staged_archive, output)
        finally:
            staged_archive.unlink(missing_ok=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="One CRYSTAL structure directory or .7z archive")
    parser.add_argument("--output", type=Path, help="New .7z file (default: SOURCE.clean.7z)")
    parser.add_argument("--dry-run", action="store_true", help="Check only; write and upload nothing")
    parser.add_argument("--upload", action="store_true", help="Upload the repaired archive")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    output = args.output or args.source.with_name(args.source.stem + ".clean.7z")
    try:
        report = repair_archive(args.source, output, dry_run=args.dry_run)
        print(json.dumps(asdict(report), indent=2, allow_nan=False))
        if not report.uploadable:
            return 2
        if args.upload and not args.dry_run:
            config = read_config(args.config)
            if not send_archive(
                resolve_archive_upload_url(config, logger=logger), output,
                bid=config.get("archive_bid"), schema_id=config.get("archive_schema_id"),
                key=get_archive_key(config),
            ):
                logger.error("Upload failed; repaired archive and report retained at %s", output)
                return 3
            logger.info("Uploaded repaired archive %s", output)
        return 0
    except Exception as exc:
        logger.error("CRYSTAL archive repair failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
