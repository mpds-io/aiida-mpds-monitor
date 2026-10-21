from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aiida_mpds_monitor import archive_files, daemon, submit
from tests.test_generate_archive import (
    DummyCalcJobNode, DummyProcessNode, _create_retrieved_folder,
)


def test_rejected_copies_survive_rebuilds_and_deduplicate(tmp_path):
    archive = tmp_path / "material.7z"
    archive.write_bytes(b"first rejected archive")
    first = archive_files.preserve_rejected_archive(archive)
    assert archive_files.preserve_rejected_archive(archive) == first
    # A later polling attempt may regenerate the working archive at the same path.
    archive.write_bytes(b"second rejected archive")
    second = archive_files.preserve_rejected_archive(archive)
    archive.unlink()
    assert first.read_bytes() == b"first rejected archive"
    assert second.read_bytes() == b"second rejected archive"
    assert set(tmp_path.iterdir()) == {first, second}


def test_rejected_copy_failure_leaves_no_staged_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        archive_files.preserve_rejected_archive(tmp_path / "missing.7z")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("directory", ["", "saved/rejections", "~/saved/rejections"])
def test_rejected_copy_configured_directory(tmp_path, monkeypatch, directory):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "os.path.expanduser",
        lambda path: str(tmp_path) + path[1:] if path.startswith("~") else path,
    )
    archive = tmp_path / "working" / "material.7z"
    archive.parent.mkdir()
    archive.write_bytes(b"rejected")
    retained = archive_files.preserve_rejected_archive(archive, directory=directory)
    destination = tmp_path / "saved" / "rejections" if directory else archive.parent
    assert retained.resolve().parent == destination
    assert retained.read_bytes() == archive.read_bytes()
    assert archive_files.preserve_rejected_archive(archive, directory=directory) == retained
    expected = {archive, retained.resolve()} if not directory else {retained.resolve()}
    assert set(destination.iterdir()) == expected


@pytest.mark.parametrize("keep_errors", [False, True])
def test_uploaded_cleanup_respects_retained_diagnostics_and_other_parents(tmp_path, keep_errors):
    root = tmp_path / "archives"
    archive = root / "parent" / "material.7z"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"delivered")
    report = archive.with_suffix(".validation.json")
    report.write_text("{}")
    rejected = None
    if keep_errors:
        rejected = archive_files.preserve_rejected_archive(archive)
    sibling = root / "other-parent" / "material.7z"
    sibling.parent.mkdir()
    sibling.write_bytes(b"pending upload")
    archive_files.cleanup_uploaded_archive(archive, keep_errors=keep_errors)
    assert not archive.exists()
    assert archive.parent.exists() is keep_errors
    assert report.exists() is keep_errors
    if rejected:
        assert rejected.read_bytes() == b"delivered"
    assert sibling.read_bytes() == b"pending upload"
    archive_files.cleanup_uploaded_archive(sibling)
    assert root.exists() is keep_errors


@pytest.mark.parametrize("failure", ["copy", "compression", "move", None])
@pytest.mark.parametrize("parent_archive", [False, True])
def test_generation_cleans_staging_on_success_and_failure(tmp_path, failure, parent_archive):
    from aiida_mpds_monitor import generate_archive as generation

    retrieved = _create_retrieved_folder(
        tmp_path / "repository", {"INPUT": b"input", "OUTPUT": b"output"}
    )
    calculation = DummyCalcJobNode(uuid="calc", label="STRUCT", retrieved=retrieved)
    child = DummyProcessNode(uuid="child", called=[calculation])
    parent = DummyProcessNode(uuid="parent", label="material", called=[child])
    staging = tmp_path / "staging"

    def compress(source, **kwargs):
        source.with_name(source.name + ".7z").write_bytes(b"archive")
        if parent_archive:
            (source.parent / "summary.json").write_text("{}")
        if failure == "compression":
            raise OSError("compression failed")

    with patch.object(generation, "load_node", return_value=parent if parent_archive else calculation), patch.object(
        generation, "CalcJobNode", DummyCalcJobNode
    ), patch.object(generation, "archive_and_save", side_effect=compress), patch.object(
        generation.shutil, "move", wraps=generation.shutil.move,
        side_effect=OSError("move failed") if failure == "move" else None,
    ), patch.object(
        retrieved, "open", wraps=retrieved.open,
        side_effect=OSError("copy failed") if failure == "copy" else None,
    ):
        generate = generation.generate_parent_archive if parent_archive else generation.generate_archive
        identifier = "parent" if parent_archive else "calc"
        if failure in ("compression", "move"):
            with pytest.raises(OSError):
                generate(identifier, tmp_root=staging)
        else:
            result = generate(identifier, tmp_root=staging)
            assert (result is not None) is (failure is None)
            if result:
                assert result.read_bytes() == b"archive"
    assert not staging.exists()


def test_preservation_failure_prevents_destructive_recovery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / "material.7z"
    archive.write_bytes(b"rejected")

    def reject(*args, result, **kwargs):
        result.status_code = 422
        result.description = "Archive validation failed: Found not a finished output"
        return False

    with patch.object(daemon, "generate_parent_archive", return_value=archive), patch.object(
        daemon, "send_archive", side_effect=reject
    ), patch.object(daemon, "preserve_rejected_archive", side_effect=OSError("disk full")), patch(
        "aiida_mpds_monitor.crystal_recovery.recover_archive"
    ) as recover:
        assert not daemon.generate_and_upload_archive(
            SimpleNamespace(pk=1, uuid="parent"), [], {"archive_keep_errors": True}, MagicMock()
        )
    recover.assert_not_called()
    assert archive.read_bytes() == b"rejected"


def test_manual_submit_preserves_server_rejected_archive(tmp_path):
    archive = tmp_path / "material.7z"
    archive.write_bytes(b"rejected")
    child = SimpleNamespace(label="material", pk=2, process_label="BaseCrystalWorkChain")
    parent = SimpleNamespace(
        pk=1, uuid="parent", process_label="MPDSStructureWorkChain", called=[child],
        is_failed=False, is_excepted=False, is_killed=False,
    )
    config = {
        "archive_keep_errors": True,
        "archive_errors_dir": str(tmp_path / "unsent"),
        "workchain_hierarchy": {"MPDSStructureWorkChain": {"BaseCrystalWorkChain": []}},
    }

    def reject(*args, result, **kwargs):
        result.status_code = 422
        return False

    with patch.object(submit, "load_node", return_value=parent), patch.object(
        submit, "WorkChainNode", object
    ), patch.object(submit, "get_node_status", return_value="finished"), patch.object(
        submit, "send_webhook", return_value=True
    ), patch.object(submit, "base_has_ready_children", return_value=True), patch.object(
        submit, "generate_parent_archive", return_value=archive
    ), patch.object(submit, "send_archive", side_effect=reject):
        submit.submit_parent(1, "http://localhost/webhook", config=config)
    assert archive.exists()
    retained, = (tmp_path / "unsent").glob("*.rejected-*.7z")
    assert retained.read_bytes() == b"rejected"
