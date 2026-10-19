import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import py7zr
import pytest

from aiida_mpds_monitor import archive_files, crystal_recovery, crystal_validation, repair_crystal
from aiida_mpds_monitor import daemon
from aiida.common.extendeddicts import AttributeDict
from aiida_mpds_monitor.config import DEFAULT_CONFIG
from aiida_mpds_monitor.status import EXTRA_ARCHIVE_PROCESSED, EXTRA_ARCHIVE_RECOVERY, EXTRA_PARENT_PROCESSED
from aiida_mpds_monitor.webhook import send_archive
from tests.test_crystal_repair import OUTPUT, parsed, stage


@pytest.fixture
def parser(monkeypatch):
    parser = MagicMock()
    parser.acceptable.return_value = True
    parser.return_value.info = parsed()
    monkeypatch.setattr(crystal_validation, "CRYSTOUT", parser)
    return parser


def rejected_archive(tmp_path, parser):
    material = tmp_path / "material"
    stage(material, "STRUCT")
    stage(material, "PHON", "Segmentation fault")
    archive = tmp_path / "rejected.7z"
    with py7zr.SevenZipFile(archive, "w") as output:
        output.writeall(material, arcname=material.name)
    return archive


def test_unexpected_parser_error_does_not_label_a_process_broken(tmp_path, parser):
    folder = stage(tmp_path, "STRUCT")
    parser.side_effect = RuntimeError("parser implementation bug")
    with pytest.raises(RuntimeError, match="implementation bug"):
        crystal_validation.validate_stage(folder)


def test_unsupported_parser_feature_is_not_process_failure(tmp_path, parser):
    folder = stage(tmp_path, "STRUCT")
    parser.side_effect = crystal_validation.CRYSTOUT_Error("unsupported output feature")
    result = crystal_validation.validate_stage(folder)
    assert result.status == "corrupted"
    assert not result.process_failure


def test_local_io_failure_is_not_calculation_corruption(tmp_path, parser):
    folder = stage(tmp_path, "STRUCT")
    with patch.object(Path, "read_bytes", side_effect=PermissionError("unreadable staging file")):
        with pytest.raises(PermissionError):
            crystal_validation.validate_stage(folder)


def test_policy_failure_carries_no_process_failure_evidence(tmp_path, parser):
    folder = stage(tmp_path, "STRUCT")
    (folder / "INPUT").write_text("nonstandard functional")
    result = crystal_validation.validate_stage(folder)
    assert result.status == "corrupted"
    assert not result.process_failure


def test_archive_damage_cannot_mark_healthy_source_process_broken(tmp_path, parser, nodes):
    parent, base, good, bad = nodes
    archive = rejected_archive(tmp_path, parser)
    original = archive.read_bytes()
    # The archive's broken PHON folder was incorrectly mapped to a healthy OUTPUT.
    with pytest.raises(ValueError, match="differs from retrieved"):
        crystal_recovery.recover_archive(parent, [base], archive, {"STRUCT": bad, "PHON": good})
    assert good.is_finished_ok and bad.is_finished_ok and base.is_finished_ok
    assert archive.read_bytes() == original


def test_policy_only_rejection_never_rewrites_aiida_states(tmp_path, parser, nodes):
    parent, base, good, bad = nodes
    material = tmp_path / "material"
    stage(material, "STRUCT")
    stage(material, "PHON")
    (material / "PHON" / "INPUT").write_text("nonstandard input")
    archive = tmp_path / "rejected.7z"
    with py7zr.SevenZipFile(archive, "w") as output:
        output.writeall(material, arcname=material.name)
    original = archive.read_bytes()
    assert crystal_recovery.recover_archive(parent, [base], archive, {"STRUCT": good, "PHON": bad}) is None
    assert good.is_finished_ok and bad.is_finished_ok and base.is_finished_ok
    assert archive.read_bytes() == original


def test_failed_publication_rolls_back_aiida_state_changes(tmp_path, parser, nodes):
    parent, base, good, bad = nodes
    archive = rejected_archive(tmp_path, parser)
    original = archive.read_bytes()
    replace = Path.replace

    def fail_publication(path, target):
        if path.suffix == ".7z" and Path(target) == archive:
            raise OSError("publication failure")
        return replace(path, target)

    with patch.object(Path, "replace", fail_publication), pytest.raises(OSError, match="publication failure"):
        crystal_recovery.recover_archive(parent, [base], archive, {"STRUCT": good, "PHON": bad})
    assert archive.read_bytes() == original
    assert good.is_finished_ok and bad.is_finished_ok and base.is_finished_ok


def test_failed_database_commit_restores_rejected_archive(tmp_path, parser, nodes):
    parent, base, good, bad = nodes
    archive = rejected_archive(tmp_path, parser)
    original = archive.read_bytes()
    transaction = parent.backend.transaction
    original_mark = crystal_recovery.mark_broken_processes
    depth = 0

    @contextmanager
    def failing_transaction():
        nonlocal depth
        depth += 1
        try:
            with transaction() as session:
                yield session
                if depth == 1:
                    raise RuntimeError("commit failed")
        finally:
            depth -= 1

    def failing_mark(*args, **options):
        # Inject failure only into the mutation transaction, never ORM reads.
        with patch.object(parent.backend, "transaction", failing_transaction):
            return original_mark(*args, **options)

    with patch.object(crystal_recovery, "mark_broken_processes", side_effect=failing_mark):
        with pytest.raises(RuntimeError, match="commit failed"):
            crystal_recovery.recover_archive(parent, [base], archive, {"STRUCT": good, "PHON": bad})
    assert archive.read_bytes() == original
    assert bad.is_finished_ok and base.is_finished_ok


def test_partial_compression_is_never_published(tmp_path, parser):
    material = tmp_path / "material"
    stage(material, "STRUCT")
    output = tmp_path / "clean.7z"
    with patch.object(py7zr.SevenZipFile, "write", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            repair_crystal.repair_archive(material, output)
    assert not output.exists()
    assert output.with_suffix(".validation.json").exists()


@pytest.mark.parametrize("kind", ["size", "members"])
def test_extraction_limits_checked_before_extraction(tmp_path, kind):
    archive = tmp_path / "large.7z"
    source = tmp_path / "large"
    source.write_bytes(b"x" * 100)
    with py7zr.SevenZipFile(archive, "w") as contents:
        contents.write(source, arcname="STRUCT/OUTPUT")
    limits = repair_crystal.ArchiveLimits(
        max_members=0 if kind == "members" else 10,
        max_unpacked_bytes=50 if kind == "size" else 1000,
    )
    with patch.object(py7zr.SevenZipFile, "extractall") as extract:
        with pytest.raises(ValueError):
            repair_crystal._extract(archive, tmp_path / "destination", limits)
    extract.assert_not_called()


@pytest.mark.parametrize("problem", ["traversal", "drive", "symlink", "junction", "special", "duplicate"])
def test_unsafe_archive_members_are_rejected_before_writing(tmp_path, problem):
    entry = SimpleNamespace(filename="STRUCT/OUTPUT", is_file=True, is_directory=False,
                            is_symlink=False, is_junction=False, uncompressed=1)
    if problem == "traversal":
        entry.filename = "../escape"
    elif problem == "drive":
        entry.filename = "C:/escape"
    elif problem == "symlink":
        entry.is_symlink = True
    elif problem == "junction":
        entry.is_junction = True
    elif problem == "special":
        entry.is_file = False
    contents = MagicMock()
    contents.files = [entry, entry] if problem == "duplicate" else [entry]
    with patch.object(py7zr, "SevenZipFile") as archive:
        archive.return_value.__enter__.return_value = contents
        with pytest.raises(ValueError, match="Unsafe archive member"):
            repair_crystal._extract(tmp_path / "source.7z", tmp_path / "destination")
    contents.extractall.assert_not_called()
    assert not (tmp_path / "destination").exists()


def test_duplicate_stage_aliases_are_not_silently_combined(tmp_path, parser):
    for name in ("STRUCT", "PHON", "PHONON"):
        stage(tmp_path, name)
    report = crystal_validation.validate_crystal_tree(tmp_path)
    assert not report.uploadable
    assert "Duplicate" in report.error


def test_directory_dry_run_does_not_copy_wavefunctions(tmp_path, parser):
    material = tmp_path / "material"
    stage(material, "STRUCT")
    with patch.object(repair_crystal.shutil, "copytree") as copy:
        assert repair_crystal.repair_archive(material, tmp_path / "out.7z", dry_run=True).uploadable
    copy.assert_not_called()


def test_old_completion_record_cannot_hide_a_truncated_restart(tmp_path, parser):
    folder = stage(tmp_path, "STRUCT", OUTPUT + "== SCF ENDED - CONVERGENCE ON ENERGY\ntruncated")
    result = crystal_validation.validate_stage(folder)
    assert result.status == "broken"
    assert result.process_failure


def test_cleaned_upload_retry_survives_strict_readiness_and_avoids_revalidation(tmp_path, parser, nodes):
    parent, base, good, bad = nodes
    raw = rejected_archive(tmp_path, parser)
    archive = tmp_path / "archives" / str(parent.uuid) / "NaCl.7z"
    archive.parent.mkdir(parents=True)
    raw.replace(archive)
    config = AttributeDict({**DEFAULT_CONFIG, "archive_keep": True,
                            "send_archives_all_stages_ready": True})

    def generate(*args, **options):
        options["stage_nodes"].update({"STRUCT": good, "PHON": bad})
        return archive

    responses = []
    for status in (422, 503, 200):
        response = MagicMock(status_code=status)
        response.json.return_value = {"detail": "Archive validation failed: Found not a finished output"}
        responses.append(response)
    query = MagicMock()
    query.iterall.return_value = [[parent]]
    with patch.object(daemon, "generate_parent_archive", side_effect=generate) as generation, patch(
        "aiida_mpds_monitor.webhook.requests.post", side_effect=responses
    ) as post, patch.object(crystal_recovery, "repair_archive", wraps=repair_crystal.repair_archive) as repair:
        assert not daemon.generate_and_upload_archive(parent, [base], config, MagicMock())
        assert parent.base.extras.get(EXTRA_ARCHIVE_RECOVERY)["status"] == "ready"
        with patch.object(daemon, "QueryBuilder", return_value=query), patch.object(
            daemon, "send_webhook", return_value=True
        ):
            daemon.scan_and_process(config, MagicMock())
    generation.assert_called_once()
    repair.assert_called_once()
    assert post.call_count == 3
    assert parent.base.extras.get(EXTRA_PARENT_PROCESSED)
    assert parent.base.extras.get(EXTRA_ARCHIVE_PROCESSED)
    assert EXTRA_ARCHIVE_RECOVERY not in parent.base.extras.all


def test_modified_pending_archive_is_not_uploaded(tmp_path, parser, nodes):
    parent, base, good, bad = nodes
    raw = rejected_archive(tmp_path, parser)
    archive = tmp_path / "archives" / str(parent.uuid) / "NaCl.7z"
    archive.parent.mkdir(parents=True)
    raw.replace(archive)
    assert crystal_recovery.recover_archive(parent, [base], archive, {"STRUCT": good, "PHON": bad})
    archive.write_bytes(b"tampered")
    with patch.object(daemon, "send_archive") as upload:
        assert not daemon.generate_and_upload_archive(parent, [base], {}, MagicMock())
    upload.assert_not_called()


def test_no_commit_preserves_pending_recovery_record_and_file(tmp_path, parser, nodes):
    parent, base, good, bad = nodes
    raw = rejected_archive(tmp_path, parser)
    archive = tmp_path / "archives" / str(parent.uuid) / "NaCl.7z"
    archive.parent.mkdir(parents=True)
    raw.replace(archive)
    crystal_recovery.recover_archive(parent, [base], archive, {"STRUCT": good, "PHON": bad})
    extras = dict(parent.base.extras.all)
    with patch.object(daemon, "send_archive", return_value=True), patch.object(
        daemon, "generate_parent_archive"
    ) as generation:
        assert daemon.generate_and_upload_archive(parent, [base], {}, MagicMock(), no_commit=True)
    generation.assert_not_called()
    assert parent.base.extras.all == extras
    assert archive.exists()


def test_report_exclusive_publication_does_not_overwrite_competing_writer(tmp_path):
    path = tmp_path / "report.json"
    archive_files.write_report(path, '{"writer": 1}')
    with pytest.raises(FileExistsError):
        archive_files.write_report(path, '{"writer": 2}', overwrite=False)
    assert json.loads(path.read_text()) == {"writer": 1}


def test_parent_lock_excludes_overlapping_work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with archive_files.parent_archive_lock("parent") as first:
        with archive_files.parent_archive_lock("parent") as second:
            assert first and not second
        with archive_files.parent_archive_lock("other-parent") as other:
            assert other
    with archive_files.parent_archive_lock("parent") as available:
        assert available


@pytest.mark.parametrize("exception", [False, True])
def test_archive_error_logs_redact_credentials(tmp_path, caplog, exception):
    archive = tmp_path / "data.7z"
    archive.write_bytes(b"archive")
    key = "private:token"
    response = MagicMock(status_code=422, text="echo private:token / private%3Atoken")
    response.json.return_value = {"detail": response.text}
    with patch("aiida_mpds_monitor.webhook.requests.post", return_value=response,
               side_effect=RuntimeError(response.text) if exception else None):
        assert not send_archive("https://example.invalid/upload", archive, key=key)
    assert key not in caplog.text
    assert "private%3Atoken" not in caplog.text
    assert "[REDACTED]" in caplog.text
