"""Atomic archive publication and coordination between local monitor processes."""

import hashlib
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

logger = logging.getLogger(__name__)


def preserve_rejected_archive(
    path: Path, directory: Optional[Union[str, Path]] = None,
) -> Path:
    """Save an independent, deduplicated copy before recovery replaces the original."""
    path = Path(path)
    destination = Path(directory).expanduser() if directory else path.parent
    destination.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=destination, delete=False) as stream:
        staged = Path(stream.name)
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
            retained = destination / f"{path.stem}.rejected-{digest.hexdigest()}.7z"
            try:
                os.link(staged, retained)
            except FileExistsError:
                pass
        finally:
            staged.unlink(missing_ok=True)
    logger.info("Kept rejected archive for inspection: %s", retained)
    return retained


def cleanup_uploaded_archive(path: Path, keep_errors: bool = False) -> None:
    """Remove a delivered archive and disposable report, then empty output directories."""
    path = Path(path)
    for artifact in [path] + ([] if keep_errors else [path.with_suffix(".validation.json")]):
        try:
            artifact.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete %s: %s", artifact, exc)
    if path.parent.parent.name == "archives":
        for directory in (path.parent, path.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                break  # Other parents, retained diagnostics, or concurrent work remain.


def write_report(path: Path, contents: str, overwrite: bool = True) -> None:
    """Publish complete JSON only; exclusive creation also handles competing writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(contents.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    try:
        if overwrite:
            staged.replace(path)
        else:
            os.link(staged, path)  # Atomic no-clobber publication on the same filesystem.
    finally:
        staged.unlink(missing_ok=True)


@contextmanager
def parent_archive_lock(identifier: str) -> Iterator[bool]:
    """Skip overlapping work for the same parent in cooperating local daemons."""
    import fcntl

    root = Path.cwd() / "aiida_archives_tmp" / ".locks"
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(identifier).encode()).hexdigest()
    with (root / key).open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
