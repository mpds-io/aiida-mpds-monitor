"""Recover a rejected CRYSTAL archive and flag broken calculation outputs."""

import hashlib
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from aiida.engine import ProcessState
from aiida.orm import CalcJobNode, ProcessNode, WorkChainNode

from .repair_crystal import repair_archive
from .status import (
    EXTRA_ARCHIVE_PROCESSED, EXTRA_ARCHIVE_RECOVERY, EXTRA_PARENT_PROCESSED, EXTRA_VALIDATION,
)

logger = logging.getLogger(__name__)


def _descendants(node: ProcessNode) -> Iterable[ProcessNode]:
    pending = list(node.called)
    visited = {node.pk}
    while pending:
        child = pending.pop()
        if child.pk in visited:
            continue
        visited.add(child.pk)
        yield child
        pending.extend(child.called)


def mark_broken_processes(
    parent: WorkChainNode, broken: List[Tuple[CalcJobNode, WorkChainNode, str]],
    no_commit: bool = False, before_commit: Optional[Callable[[], None]] = None,
) -> None:
    """Flag confirmed terminal CRYSTAL failures without rewriting process attributes.

    Extras remain editable on sealed nodes. Only calculation nodes are excluded
    from future archives; the base flag records the aggregate validation result.
    """
    if no_commit:
        if before_commit is not None:
            before_commit()
        return
    updates: Dict[int, Tuple[ProcessNode, List[str]]] = {}
    bases = {}
    for calculation, base, reason in broken:
        if (calculation.process_label != "CrystalParallelCalculation"
                or base.process_label != "BaseCrystalWorkChain"):
            raise ValueError("Refusing to change a process outside the CRYSTAL hierarchy")
        bases[base.pk] = base
        for node in (calculation, base):
            state = node.process_state
            if state not in (ProcessState.FINISHED, ProcessState.EXCEPTED):
                raise ValueError(f"Refusing to change non-terminal process {node.pk}")
            updates.setdefault(node.pk, (node, []))[1].append(reason)
    if not updates:
        return
    marked = []
    with parent.backend.transaction():
        # Recheck inside the transaction; never mutate processes from another storage.
        for node, _ in updates.values():
            if node.backend is not parent.backend or node.process_state not in (
                ProcessState.FINISHED, ProcessState.EXCEPTED,
            ):
                raise ValueError(f"Process {node.pk} changed state or belongs to another backend")
        for node, reasons in updates.values():
            reason = "; ".join(dict.fromkeys(reasons))
            previous = node.base.extras.get(EXTRA_VALIDATION, {})
            original = previous.get("original") if isinstance(previous, dict) else None
            if original is None:
                original = {
                    "process_state": node.process_state.value,
                    "exit_status": node.exit_status,
                    "exception": node.exception,
                }
            node.base.extras.set(EXTRA_VALIDATION, {
                "status": "broken", "reason": reason, "original": original,
                "exclude_from_archive": node.process_label == "CrystalParallelCalculation",
            })
            marked.append((node.process_label, node.pk, reason))
        # Let the normal webhook path deliver corrected base statuses next scan.
        for base in bases.values():
            base.base.extras.set(EXTRA_PARENT_PROCESSED, False)
        if parent.base.extras.get(EXTRA_PARENT_PROCESSED, None) is not None:
            parent.base.extras.delete(EXTRA_PARENT_PROCESSED)
        parent.base.extras.set(EXTRA_ARCHIVE_PROCESSED, False)
        if before_commit is not None:
            before_commit()
    # Do not report a successful correction when its transaction rolled back.
    for label, pk, reason in marked:
        logger.warning("Flagged %s %s as broken: %s", label, pk, reason)


def _verify_retrieved_output(calculation: CalcJobNode, expected_digest: Optional[str]) -> None:
    """Archive damage alone is insufficient evidence to rewrite an AiiDA process."""
    digest = hashlib.sha256()
    with calculation.outputs.retrieved.open("OUTPUT", "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if expected_digest is None or digest.hexdigest() != expected_digest:
        raise ValueError(f"Archived OUTPUT differs from retrieved OUTPUT for node {calculation.pk}")


def _archive_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pending_recovered_archive(parent: WorkChainNode) -> Path:
    """Resume a failed upload without rebuilding or revalidating flagged calculations."""
    record = parent.base.extras.get(EXTRA_ARCHIVE_RECOVERY, None)
    if not isinstance(record, dict) or record.get("status") != "ready":
        raise ValueError("No pending recovered archive")
    path = Path(record["path"]).resolve()
    if path.parent.name != str(parent.uuid) or path.parent.parent.name != "archives":
        raise ValueError("Recovered archive is outside this parent's archive directory")
    if _archive_digest(path) != record["sha256"]:
        raise ValueError("Pending recovered archive changed; refusing to upload it")
    return path


def _snapshot(path: Path) -> Tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


@contextmanager
def _publication(
    cleaned: Path, target: Path, expected: Tuple[int, int, int, int],
) -> Iterator[Callable[[], None]]:
    """Restore the rejected archive if publication or the database commit fails."""
    backup = cleaned.parent / ".rejected-backup"
    os.link(target, backup)
    published = False

    def publish() -> None:
        nonlocal published
        if _snapshot(target) != expected:
            raise ValueError("Rejected archive changed during recovery")
        cleaned.replace(target)
        published = True

    try:
        yield publish
    except BaseException:
        if published:
            backup.replace(target)
        raise
    finally:
        backup.unlink(missing_ok=True)


def recover_archive(
    parent: WorkChainNode, base_nodes: Iterable[WorkChainNode], archive_path: Path,
    stage_nodes: Dict[str, CalcJobNode], no_commit: bool = False,
) -> Optional[Path]:
    """Validate the rejected contents, flag mapped AiiDA nodes, and rebuild once."""
    owners = {}
    for base in base_nodes:
        if base.process_label == "BaseCrystalWorkChain":
            for child in _descendants(base):
                if child.process_label == "CrystalParallelCalculation":
                    owners[child.pk] = base
    if not any(node.pk in owners for node in stage_nodes.values()):
        logger.warning("Rejected archive has no mapped BaseCrystalWorkChain/CRYSTAL calculations")
        return None
    archive_path = Path(archive_path)
    expected = _snapshot(archive_path)
    with tempfile.TemporaryDirectory(prefix="crystal-recovery-", dir=archive_path.parent) as directory:
        cleaned = Path(directory) / archive_path.name
        report = repair_archive(archive_path, cleaned)
        report.write(archive_path.with_suffix(".validation.json"))
        broken = []
        for stage in report.stages:
            if stage.status == "valid":
                continue
            calculation = stage_nodes.get(stage.folder)
            if calculation is None or calculation.pk not in owners:
                logger.error("Cannot map invalid archive folder %s to a CRYSTAL node", stage.folder)
                return None
            if stage.process_failure:
                # A damaged archive must never flag a healthy source calculation.
                _verify_retrieved_output(calculation, stage.output_sha256)
                broken.append((calculation, owners[calculation.pk], f"{stage.folder}: {stage.reason}"))
        if not broken:
            logger.warning("Server rejected unfinished output, but local checks found no broken stages")
            return None
        if _snapshot(archive_path) != expected:
            raise ValueError("Rejected archive changed during recovery")
        if not report.uploadable:
            mark_broken_processes(parent, broken, no_commit=no_commit)
            logger.warning("Cannot recover rejected archive: %s", report.error)
            return None
        cleaned_digest = _archive_digest(cleaned)
        # Publish before committing flags; restore the rejected file on failure.
        with _publication(cleaned, archive_path, expected) as publish:
            def publish_and_record() -> None:
                publish()
                if not no_commit:
                    # Persist the retry independently of current child readiness.
                    parent.base.extras.set(EXTRA_ARCHIVE_RECOVERY, {
                        "status": "ready", "path": str(archive_path.resolve()),
                        "sha256": cleaned_digest,
                    })

            mark_broken_processes(parent, broken, no_commit=no_commit, before_commit=publish_and_record)
    return archive_path
