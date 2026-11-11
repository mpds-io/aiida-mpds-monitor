"""Publication policy and phonon layouts on top of CRYSTAL archive recovery."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiida.common.extendeddicts import AttributeDict

from aiida_mpds_monitor import daemon
from aiida_mpds_monitor import generate_archive as archives
from aiida_mpds_monitor.config import DEFAULT_CONFIG
from aiida_mpds_monitor.status import EXTRA_ARCHIVE_PROCESSED, EXTRA_PARENT_PROCESSED
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


def test_phonon_archive_suffixes_cannot_overwrite_existing_labels(tmp_path, monkeypatch):
    labels = ["forces_4", "forces_4_2", "forces", "forces"]
    calculations = [calculation(tmp_path, pk, label=label)
                    for pk, label in enumerate(labels, start=1)]
    workflow = DummyProcessNode(uuid="workflow", called=calculations)
    parent = parent_with(workflow)
    monkeypatch.setattr(archives, "load_node", lambda uuid: parent)
    captured = {}
    capture_archive(monkeypatch, captured)

    assert archives.generate_parent_archive(
        parent.uuid, base_nodes=[workflow], tmp_root=tmp_path / "staging",
    ) is not None

    assert captured["forces_4/OUTPUT"] == b"1"
    assert captured["forces_4_2/OUTPUT"] == b"2"
    assert captured["forces/OUTPUT"] == b"3"
    assert captured["forces_4_3/OUTPUT"] == b"4"
    assert len([name for name in captured if name.endswith("/OUTPUT")]) == 4


def test_daemon_waits_for_parent_owned_calculation_and_retries_repository_errors(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    good = calculation(tmp_path, 1)
    workflow = DummyProcessNode(uuid="workflow", pk=10, label="Si", called=[good])
    workflow.process_label = "Base"
    workflow.base = SimpleNamespace(extras=MagicMock())
    workflow.base.extras.get.return_value = True
    constants = calculation(tmp_path, 2, label="", process_label="PhonopyCalculation")
    constants.is_finished_ok = False
    constants.process_state = SimpleNamespace(value="running")
    constants.exit_status = None
    parent = parent_with(workflow, constants)
    parent.pk = 20
    parent.process_label = "Parent"
    parent.is_terminated = False
    parent.is_finished_ok = False
    parent.process_state = SimpleNamespace(value="running")
    parent.is_failed = parent.is_excepted = parent.is_killed = False
    parent.caller = None
    stored = {}
    parent.base = SimpleNamespace(extras=SimpleNamespace(get=stored.get, set=stored.__setitem__))
    query = MagicMock()
    query.iterall.return_value = [(parent,)]
    config = AttributeDict({**DEFAULT_CONFIG, "archive_keep": True,
                            "workchain_hierarchy": {"Parent": {"Base": ["AbacusCalculation"]}}})
    monkeypatch.setattr(daemon, "WorkChainNode", DummyProcessNode)
    monkeypatch.setattr(daemon, "QueryBuilder", lambda: query)
    monkeypatch.setattr(archives, "load_node", lambda uuid: parent)
    captured = {}
    capture_archive(monkeypatch, captured)
    with patch.object(daemon, "process_base_workchain", return_value=True), patch.object(
        daemon, "base_has_ready_children", return_value=True,
    ), patch.object(daemon, "send_archive", return_value=True) as upload:
        daemon.scan_and_process(config, MagicMock())
        upload.assert_not_called()
        assert EXTRA_PARENT_PROCESSED not in stored
        assert EXTRA_ARCHIVE_PROCESSED not in stored

        parent.is_terminated = True
        parent.is_finished_ok = True
        parent.process_state = SimpleNamespace(value="finished")
        constants.is_finished_ok = True
        constants.process_state = SimpleNamespace(value="finished")
        constants.exit_status = 0
        # A temporary inspection failure must leave both delivery markers unset.
        with patch.object(
            constants.outputs.retrieved, "list_object_names",
            side_effect=OSError("storage temporarily unavailable"),
        ):
            daemon.scan_and_process(config, MagicMock())
        upload.assert_not_called()
        assert EXTRA_PARENT_PROCESSED not in stored
        assert EXTRA_ARCHIVE_PROCESSED not in stored

        daemon.scan_and_process(config, MagicMock())

    upload.assert_called_once()
    assert captured["PhonopyCalculation/OUTPUT"] == b"2"
    assert captured["forces/OUTPUT"] == b"1"
    assert stored[EXTRA_PARENT_PROCESSED] is True
    assert stored[EXTRA_ARCHIVE_PROCESSED] is True


def test_archive_helper_defers_even_empty_publication_while_parent_runs():
    parent = SimpleNamespace(pk=7, called=[], is_terminated=False)
    with patch.object(daemon, "generate_parent_archive") as generate, patch.object(
        daemon, "send_archive",
    ) as upload:
        assert daemon.generate_and_upload_archive(parent, [], {}, MagicMock()) is False
    generate.assert_not_called()
    upload.assert_not_called()


def test_repository_inspection_failure_is_not_an_empty_publication(tmp_path):
    node = calculation(tmp_path, 1)
    parent = parent_with(node)
    parent.pk = 7
    parent.is_terminated = True
    with patch.object(
        node.outputs.retrieved, "list_object_names", side_effect=OSError("storage offline"),
    ), patch.object(daemon, "generate_parent_archive") as generate, patch.object(
        daemon, "send_archive",
    ) as upload:
        assert daemon.generate_and_upload_archive(parent, [], {}, MagicMock()) is False
    generate.assert_not_called()
    upload.assert_not_called()


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
    parent = SimpleNamespace(pk=7, called=[], is_terminated=True)
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
