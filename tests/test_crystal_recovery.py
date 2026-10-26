from unittest.mock import MagicMock, patch

import py7zr
import pytest
import requests
from aiida.common.extendeddicts import AttributeDict
from aiida.engine import ProcessState

from aiida_mpds_monitor import crystal_recovery, daemon
from aiida_mpds_monitor.config import DEFAULT_CONFIG
from aiida_mpds_monitor.status import (
    EXTRA_ARCHIVE_PROCESSED, EXTRA_PARENT_PROCESSED, get_node_status,
)
from aiida_mpds_monitor.webhook import ArchiveUploadErrors, ArchiveUploadResult, send_archive
from tests.test_crystal_repair import OUTPUT, parsed, stage

DETAIL = "Archive validation failed: Found not a finished output"


@pytest.mark.parametrize("no_commit", [False, True])
def test_validation_flags_preserve_process_state_and_sealing(nodes, no_commit):
    parent, base, good, bad = nodes
    original_extras = dict(bad.base.extras.all)
    crystal_recovery.mark_broken_processes(parent, [(bad, base, "PHON: unfinished")], no_commit)
    for node in (base, bad):
        assert not node.is_excepted
        assert node.is_finished_ok
        assert node.exception is None
        assert node.is_sealed
    assert good.is_finished_ok
    assert parent.is_finished_ok
    if no_commit:
        assert parent.base.extras.get(EXTRA_PARENT_PROCESSED)
        assert bad.base.extras.all == original_extras
    else:
        assert get_node_status(base) == "excepted"
        assert "PHON: unfinished" in bad.base.extras.get(crystal_recovery.EXTRA_VALIDATION)["reason"]
        assert bad.base.extras.get(crystal_recovery.EXTRA_VALIDATION)["exclude_from_archive"]
        assert not base.base.extras.get(crystal_recovery.EXTRA_VALIDATION)["exclude_from_archive"]
        assert bad.base.extras.get(crystal_recovery.EXTRA_VALIDATION)["original"]["process_state"] == "finished"
        assert EXTRA_PARENT_PROCESSED not in parent.base.extras.all
        assert not base.base.extras.get(EXTRA_PARENT_PROCESSED)
        assert not parent.base.extras.get(EXTRA_ARCHIVE_PROCESSED)


def test_failed_state_correction_rolls_back_all_processes(nodes):
    parent, base, _, bad = nodes
    original_extras = dict(bad.base.extras.all)
    with patch.object(type(base.base.extras), "set", side_effect=RuntimeError("storage failure")):
        with pytest.raises(RuntimeError, match="storage failure"):
            crystal_recovery.mark_broken_processes(parent, [(bad, base, "bad output")])
    assert bad.is_finished_ok and base.is_finished_ok
    assert bad.is_sealed and base.is_sealed
    assert bad.base.extras.all == original_extras
    assert parent.base.extras.get(EXTRA_PARENT_PROCESSED)


def test_running_process_is_not_modified(nodes):
    parent, base, _, bad = nodes
    bad.backend_entity.set_attribute("sealed", False)
    bad.set_process_state(ProcessState.RUNNING)
    with pytest.raises(ValueError, match="non-terminal"):
        crystal_recovery.mark_broken_processes(parent, [(bad, base, "bad output")])
    assert base.is_finished_ok
    assert bad.process_state == ProcessState.RUNNING


@pytest.mark.parametrize("status,detail", [
    (200, DETAIL), (401, DETAIL), (500, DETAIL),
    (422, "Token expired"), (422, "Archive validation failed: Found invalid input"),
])
def test_only_exact_server_rejection_triggers_recovery(tmp_path, status, detail):
    archive = tmp_path / "result.7z"
    archive.write_bytes(b"archive")
    response = MagicMock(status_code=status)
    response.json.return_value = {"detail": detail}
    with patch.object(daemon, "generate_parent_archive", return_value=archive), patch(
        "aiida_mpds_monitor.webhook.requests.post", return_value=response
    ) as post, patch.object(crystal_recovery, "recover_archive") as recover:
        assert daemon.generate_and_upload_archive(
            MagicMock(), [], {"archive_keep": True}, MagicMock()
        ) is (status == 200)
    recover.assert_not_called()
    post.assert_called_once()


def test_timeout_does_not_trigger_recovery(tmp_path):
    archive = tmp_path / "result.7z"
    archive.write_bytes(b"archive")
    with patch.object(daemon, "generate_parent_archive", return_value=archive), patch(
        "aiida_mpds_monitor.webhook.requests.post", side_effect=requests.Timeout
    ), patch.object(crystal_recovery, "recover_archive") as recover:
        assert not daemon.generate_and_upload_archive(MagicMock(), [], {}, MagicMock())
    recover.assert_not_called()


@pytest.mark.parametrize("retry_status", [200, 422, 503])
@pytest.mark.parametrize("no_commit", [False, True])
@pytest.mark.parametrize("keep_errors", [False, True])
@pytest.mark.parametrize("keep_archive", [False, True])
def test_422_recovery_marks_real_nodes_and_retries_clean_archive_once(
    tmp_path, monkeypatch, nodes, retry_status, no_commit, keep_errors, keep_archive,
):
    monkeypatch.chdir(tmp_path)
    parent, base, good, bad = nodes
    folder = tmp_path / "contents"
    stage(folder, "STRUCT", OUTPUT)
    stage(folder, "PHON", "Segmentation fault")
    archive = tmp_path / "NaCl.7z"
    with py7zr.SevenZipFile(archive, "w") as output:
        output.writeall(folder, arcname="NaCl")
    original = archive.read_bytes()
    errors_dir = tmp_path / "unsent"
    parser = MagicMock()
    parser.acceptable.return_value = True
    parser.return_value.info = parsed()
    monkeypatch.setattr("aiida_mpds_monitor.crystal_validation.CRYSTOUT", parser)

    def generate(*args, **options):
        assert not options.get("validate_crystal")
        options["stage_nodes"].update({"STRUCT": good, "PHON": bad})
        return archive

    calls = 0

    def post(url, **options):
        nonlocal calls
        calls += 1
        with py7zr.SevenZipFile(archive) as contents:
            names = contents.getnames()
        assert any("PHON/OUTPUT" in name for name in names) is (calls == 1)
        assert any("STRUCT/OUTPUT" in name for name in names)
        assert options["headers"] == {"Authorization": "Bearer test-key"}
        assert options["data"] == {"bid": "2", "schema_id": "3"}
        if calls == 2:
            assert bad.is_finished_ok
            assert (crystal_recovery.EXTRA_VALIDATION in bad.base.extras.all) is (not no_commit)
        response = MagicMock(status_code=422 if calls == 1 else retry_status)
        response.json.return_value = {"detail": DETAIL}
        return response

    errors = ArchiveUploadErrors()
    with patch.object(daemon, "generate_parent_archive", side_effect=generate), patch(
        "aiida_mpds_monitor.webhook.requests.post", side_effect=post
    ):
        assert daemon.generate_and_upload_archive(
            parent, [base], {"archive_keep": keep_archive, "archive_keep_errors": keep_errors,
                             "archive_errors_dir": str(errors_dir),
                             "archive_key": "test-key",
                             "archive_bid": 2, "archive_schema_id": 3}, MagicMock(),
            archive_errors=errors, no_commit=no_commit,
        ) is (retry_status == 200)
    assert calls == 2
    assert archive.exists() is (keep_archive or retry_status != 200)
    assert archive.with_suffix(".validation.json").exists() is (
        keep_archive or keep_errors or retry_status != 200
    )
    rejected = list(errors_dir.glob("NaCl.rejected-*.7z"))
    assert len(rejected) == (0 if not keep_errors else 1 if retry_status == 200 else 2)
    if keep_errors:
        assert any(path.read_bytes() == original for path in rejected)
        if retry_status != 200:
            assert any(path.read_bytes() == archive.read_bytes() for path in rejected)
    assert not list(tmp_path.glob("crystal-recovery-*"))
    assert (folder / "PHON" / "OUTPUT").exists()
    assert good.is_finished_ok
    assert bool(errors.consume()) is (retry_status != 200)


def test_response_details_are_per_request_and_reset_after_network_failure(tmp_path):
    archive = tmp_path / "test.7z"
    archive.write_bytes(b"archive")
    result = ArchiveUploadResult()
    response = MagicMock(status_code=422)
    response.json.return_value = {"detail": DETAIL}
    with patch("aiida_mpds_monitor.webhook.requests.post", return_value=response):
        assert not send_archive("https://example.invalid/upload", archive, result=result)
    assert result.unfinished_crystal_output
    with patch("aiida_mpds_monitor.webhook.requests.post", side_effect=requests.Timeout):
        assert not send_archive("https://example.invalid/upload", archive, result=result)
    assert not result.unfinished_crystal_output


def test_normal_next_scan_sends_corrected_status_without_resending_archive(nodes):
    parent, base, _, bad = nodes
    config = AttributeDict(DEFAULT_CONFIG)
    query = MagicMock()
    query.iterall.return_value = [[parent]]

    def upload(*args, **options):
        crystal_recovery.mark_broken_processes(parent, [(bad, base, "PHON: unfinished")])
        return True

    with patch.object(daemon, "QueryBuilder", return_value=query), patch.object(
        daemon, "generate_and_upload_archive", side_effect=upload
    ) as archive, patch.object(daemon, "send_webhook", return_value=True) as webhook:
        daemon.scan_and_process(config, MagicMock(), force=True)
        assert parent.base.extras.get(EXTRA_ARCHIVE_PROCESSED)
        assert EXTRA_PARENT_PROCESSED not in parent.base.extras.all
        assert not base.base.extras.get(EXTRA_PARENT_PROCESSED)
        webhook.assert_not_called()
        daemon.scan_and_process(config, MagicMock())
    archive.assert_called_once()
    webhook.assert_called_once()
    assert webhook.call_args.args[2] == "excepted"
    assert base.base.extras.get(EXTRA_PARENT_PROCESSED)
    assert parent.base.extras.get(EXTRA_PARENT_PROCESSED)


def test_unrepairable_struct_is_marked_broken_but_not_reuploaded(tmp_path, monkeypatch, nodes):
    parent, base, good, bad = nodes
    folder = tmp_path / "contents"
    stage(folder, "STRUCT", "Segmentation fault")
    stage(folder, "PHON", OUTPUT)
    archive = tmp_path / "NaCl.7z"
    with py7zr.SevenZipFile(archive, "w") as output:
        output.writeall(folder, arcname="NaCl")
    original = archive.read_bytes()
    parser = MagicMock()
    parser.acceptable.return_value = True
    parser.return_value.info = parsed()
    monkeypatch.setattr("aiida_mpds_monitor.crystal_validation.CRYSTOUT", parser)
    assert crystal_recovery.recover_archive(
        parent, [base], archive, {"STRUCT": bad, "PHON": good},
    ) is None
    assert bad.is_finished_ok and base.is_finished_ok
    assert bad.base.extras.get(crystal_recovery.EXTRA_VALIDATION)["status"] == "broken"
    assert good.is_finished_ok
    assert archive.read_bytes() == original
    assert archive.with_suffix(".validation.json").exists()


def test_unmapped_invalid_stage_cannot_change_states(tmp_path, monkeypatch, nodes):
    parent, base, good, bad = nodes
    folder = tmp_path / "contents"
    stage(folder, "STRUCT", OUTPUT)
    stage(folder, "PHON", "Segmentation fault")
    archive = tmp_path / "NaCl.7z"
    with py7zr.SevenZipFile(archive, "w") as output:
        output.writeall(folder, arcname="NaCl")
    parser = MagicMock()
    parser.acceptable.return_value = True
    parser.return_value.info = parsed()
    monkeypatch.setattr("aiida_mpds_monitor.crystal_validation.CRYSTOUT", parser)
    assert crystal_recovery.recover_archive(parent, [base], archive, {"STRUCT": good}) is None
    assert bad.is_finished_ok and base.is_finished_ok


@pytest.mark.parametrize("strict", [False, True])
def test_future_rebuild_excludes_flagged_calculation(nodes, tmp_path, strict):
    from aiida_mpds_monitor import generate_archive as archives
    from aiida_mpds_monitor.status import base_has_ready_children

    parent, base, good, bad = nodes
    good.label = "Geometry optimization"
    bad.label = "Phonon frequencies"
    crystal_recovery.mark_broken_processes(parent, [(bad, base, "PHON: unfinished")])
    assert base_has_ready_children(base, all_stages_ready=strict)
    mapped = {}

    def compress(root, **options):
        assert (root / "STRUCT" / "OUTPUT").is_file()
        assert not (root / "PHONON").exists()
        result = root.with_suffix(".7z")
        result.write_bytes(b"clean archive")
        return result

    with patch.object(archives, "load_node", return_value=parent), patch.object(
        archives, "archive_and_save", side_effect=compress
    ), patch.object(archives, "_copy_calculation_input", return_value=True):
        assert archives.generate_parent_archive(
            parent.uuid, [base], tmp_root=tmp_path / "staging",
            require_all_subnodes=strict, stage_nodes=mapped,
        )
    assert mapped == {"STRUCT": good}
    assert good.is_finished_ok and bad.is_finished_ok and base.is_finished_ok
    assert bad.outputs.retrieved.get_object_content("OUTPUT") == "Segmentation fault"


def test_only_flagged_calculations_produce_no_archive(nodes, tmp_path):
    from aiida_mpds_monitor import generate_archive as archives
    from aiida_mpds_monitor.status import base_has_ready_children

    parent, base, good, bad = nodes
    crystal_recovery.mark_broken_processes(
        parent, [(good, base, "STRUCT: unfinished"), (bad, base, "PHON: unfinished")],
    )
    assert not base_has_ready_children(base, all_stages_ready=True)
    with patch.object(archives, "load_node", return_value=parent), patch.object(
        archives, "archive_and_save"
    ) as compress:
        assert archives.generate_parent_archive(
            parent.uuid, [base], tmp_root=tmp_path / "staging",
        ) is None
        assert archives.generate_archive(good.uuid, tmp_root=tmp_path / "staging") is None
    compress.assert_not_called()
