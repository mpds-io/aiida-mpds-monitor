"""Publication policy and phonon layouts on top of CRYSTAL archive recovery."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aiida_mpds_monitor import daemon
from aiida_mpds_monitor import generate_archive as archives
from tests.test_generate_archive import (
    DummyCalcJobNode, DummyProcessNode, _create_retrieved_folder,
)


@pytest.fixture(autouse=True)
def calcjob_type(monkeypatch):
    monkeypatch.setattr(archives, "CalcJobNode", DummyCalcJobNode)


def calculation(tmp_path, pk, *, label="forces", process_label="AbacusCalculation",
                successful=True, files=None):
    node = DummyCalcJobNode(
        uuid=f"calc-{pk}", pk=pk, label=label, is_finished_ok=successful,
        retrieved=_create_retrieved_folder(
            tmp_path / str(pk), files if files is not None else {
                "INPUT": b"input", "OUTPUT": str(pk).encode(),
            },
        ),
    )
    node.process_label = process_label
    return node


def parent_with(*nodes):
    return DummyProcessNode(uuid="parent", label="YAlO3 phonons", called=list(nodes))


def test_input_generation_alone_is_not_publishable(tmp_path):
    node = calculation(tmp_path, 1, process_label="FleurinputgenCalculation")
    parent = parent_with(node)
    assert not archives.has_publishable_results(parent, parent.called)


@pytest.mark.parametrize("successful,files", [(False, None), (True, {})])
def test_failed_or_empty_results_are_not_publishable(tmp_path, successful, files):
    node = calculation(tmp_path, 1, successful=successful, files=files)
    parent = parent_with(node)
    assert not archives.has_publishable_results(parent, parent.called)


def test_parents_own_phonopy_result_is_publishable(tmp_path):
    node = calculation(tmp_path, 1, label="", process_label="PhonopyCalculation")
    assert archives.has_publishable_results(parent_with(node), [])


def test_flagged_result_is_not_publishable(tmp_path, monkeypatch):
    node = calculation(tmp_path, 1)
    monkeypatch.setattr(archives, "excluded_from_archive", lambda candidate: candidate is node)
    assert not archives.has_publishable_results(parent_with(node), [])


def capture_archive(monkeypatch, captured):
    def compress(source, **options):
        captured.update({str(p.relative_to(source)): p.read_bytes()
                         for p in source.rglob("*") if p.is_file()})
        destination = source.parent / f"{source.name}.7z"
        destination.write_bytes(b"archive")
        return destination
    monkeypatch.setattr(archives, "archive_and_save", compress)


def test_phonon_archive_keeps_duplicate_labels_and_direct_result(tmp_path, monkeypatch):
    first = calculation(tmp_path, 1)
    second = calculation(tmp_path, 2)
    workflow = DummyProcessNode(uuid="workflow", called=[first, second])
    constants = calculation(tmp_path, 3, label="", process_label="PhonopyCalculation")
    parent = parent_with(workflow, constants)
    monkeypatch.setattr(archives, "load_node", lambda uuid: parent)
    captured = {}
    capture_archive(monkeypatch, captured)
    result = archives.generate_parent_archive(
        parent.uuid, base_nodes=[workflow], tmp_root=tmp_path / "staging",
    )
    assert result is not None
    assert captured["forces/OUTPUT"] == b"1"
    assert captured["forces_2/OUTPUT"] == b"2"
    assert captured["PhonopyCalculation/OUTPUT"] == b"3"


def test_crystal_retry_still_replaces_previous_stage(tmp_path, monkeypatch):
    first = calculation(tmp_path, 1, process_label="CrystalParallelCalculation")
    second = calculation(tmp_path, 2, process_label="CrystalParallelCalculation")
    workflow = DummyProcessNode(uuid="workflow", called=[first, second])
    parent = parent_with(workflow)
    monkeypatch.setattr(archives, "load_node", lambda uuid: parent)
    captured = {}
    capture_archive(monkeypatch, captured)
    assert archives.generate_parent_archive(
        parent.uuid, base_nodes=[workflow], tmp_root=tmp_path / "staging",
    ) is not None
    assert captured == {"forces/INPUT": b"input", "forces/OUTPUT": b"2"}


def test_failed_calculations_are_logged_but_never_archived(tmp_path, monkeypatch, caplog):
    good = calculation(tmp_path, 1)
    bad = calculation(tmp_path, 2, successful=False)
    workflow = DummyProcessNode(uuid="workflow", called=[good, bad])
    parent = parent_with(workflow)
    monkeypatch.setattr(archives, "load_node", lambda uuid: parent)
    captured = {}
    capture_archive(monkeypatch, captured)
    assert archives.generate_parent_archive(
        parent.uuid, base_nodes=[workflow], tmp_root=tmp_path / "staging",
        require_all_subnodes=False,
    ) is not None
    assert captured == {"forces/INPUT": b"input", "forces/OUTPUT": b"1"}
    assert "left out 1 unsuccessful calculation(s): 2" in caplog.text


def test_empty_publication_counts_as_complete_without_generation_or_upload():
    parent = SimpleNamespace(pk=7, called=[])
    with patch.object(daemon, "generate_parent_archive") as generate, patch.object(
        daemon, "send_archive",
    ) as upload:
        assert daemon.generate_and_upload_archive(parent, [], {}, MagicMock()) is True
    generate.assert_not_called()
    upload.assert_not_called()


def test_ready_recovered_archive_bypasses_source_eligibility():
    parent = MagicMock()
    parent.base.extras.get.return_value = {"status": "ready"}
    with patch.object(daemon, "has_publishable_results", return_value=False) as eligible, patch.object(
        daemon, "_generate_and_upload_archive", return_value=True,
    ) as upload, patch.object(daemon, "parent_archive_lock") as lock:
        lock.return_value.__enter__.return_value = True
        assert daemon.generate_and_upload_archive(parent, [], {}, MagicMock()) is True
    eligible.assert_not_called()
    upload.assert_called_once()


@pytest.mark.parametrize("caller_type,expected", [
    (None, False), ("MPDSAbacusWorkChain", True), ("UnmonitoredWorkChain", False),
])
def test_nested_phonon_root_is_only_reported_once(caller_type, expected):
    caller = SimpleNamespace(process_label=caller_type) if caller_type else None
    phonons = SimpleNamespace(process_label="PhonopyAbacusWorkChain", caller=caller)
    hierarchy = {"MPDSAbacusWorkChain": {"PhonopyAbacusWorkChain": []}}
    assert daemon.is_reported_by_caller(phonons, hierarchy) is expected
