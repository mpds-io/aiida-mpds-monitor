"""Atomic archive publication and coordination between local monitor processes."""

import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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
