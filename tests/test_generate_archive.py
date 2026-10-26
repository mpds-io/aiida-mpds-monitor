import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aiida_mpds_monitor.generate_archive import generate_archive, generate_parent_archive


class DummyRetrievedFolder:
    def __init__(self, folder_path: Path):
        self._folder_path = folder_path

    def list_object_names(self):
        return [path.name for path in self._folder_path.iterdir() if path.is_file()]

    def open(self, name, mode):
        return open(self._folder_path / name, mode)


class DummyProcessNode:
    def __init__(
        self,
        *,
        uuid,
        label="",
        pk=None,
        called=None,
        retrieved=None,
        is_finished_ok=True,
    ):
        self.uuid = uuid
        self.label = label
        self.pk = pk
        self.called = list(called or [])
        self.is_finished_ok = is_finished_ok
        self.outputs = SimpleNamespace(retrieved=retrieved)
        self.inputs = SimpleNamespace()
        self.process_label = type(self).__name__
        self.process_state = SimpleNamespace(value="finished")
        self.exit_status = 0 if is_finished_ok else 1


class DummyCalcJobNode(DummyProcessNode):
    pass


def _create_retrieved_folder(tmp_path: Path, files):
    repo_dir = tmp_path / "retrieved"
    repo_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        with (repo_dir / name).open("wb") as fh:
            fh.write(content)
    return DummyRetrievedFolder(repo_dir)


def _fake_archive_and_save(source_dir, *args, **kwargs):
    archive_path = Path(str(source_dir) + ".7z")
    archive_path.write_bytes(b"fake archive")
    return archive_path


@patch("aiida_mpds_monitor.generate_archive.archive_and_save", side_effect=_fake_archive_and_save)
@patch("aiida_mpds_monitor.generate_archive.load_node")
def test_generate_parent_archive_creates_named_root(mock_load_node, mock_archive_and_save):
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir) / "tmp"
        parent_uuid = "00000000-0000-0000-0000-000000000000"
        parent_label = "NaCl_225_cF8"

        parent = DummyProcessNode(uuid=parent_uuid, label=parent_label)
        mock_load_node.return_value = parent

        # Child workchain with two grandchildren.
        grandchild_opt = DummyCalcJobNode(
            uuid="opt-uuid",
            label="OPTIMISE",
            pk=1,
            retrieved=_create_retrieved_folder(
                Path(tmp_dir) / "opt_repo", {"OUTPUT": b"opt", "INPUT": b"opt input"}
            ),
        )
        grandchild_phon = DummyCalcJobNode(
            uuid="phon-uuid",
            label="PHON",
            pk=2,
            retrieved=_create_retrieved_folder(
                Path(tmp_dir) / "phon_repo", {"OUTPUT": b"phon", "INPUT": b"phon input"}
            ),
        )
        child = DummyProcessNode(
            uuid="child-uuid", called=[grandchild_opt, grandchild_phon]
        )

        with patch(
            "aiida_mpds_monitor.generate_archive.CalcJobNode", DummyCalcJobNode
        ):
            archive_path = generate_parent_archive(
                parent_uuid, base_nodes=[child], tmp_root=tmp_root
            )

        assert archive_path is not None
        assert archive_path.name == "NaCl_225_cF8.7z"
        assert archive_path.exists()
        assert archive_path.read_bytes() == b"fake archive"

        expected_archive_root = tmp_root / parent_uuid / parent_label
        mock_archive_and_save.assert_called_once_with(
            expected_archive_root, aiida=True, skip_errors=True
        )
        assert not (tmp_root / parent_uuid).exists()
        # Two parents with the same material label must retain separate archives.
        with patch("aiida_mpds_monitor.generate_archive.CalcJobNode", DummyCalcJobNode):
            second_archive = generate_parent_archive(
                "another-parent", base_nodes=[child], tmp_root=tmp_root
            )
        assert second_archive is not None and second_archive != archive_path
        assert archive_path.exists() and second_archive.exists()


@pytest.mark.parametrize("input_filename", ["INPUT", "main.d12"])
@pytest.mark.parametrize(
    "label, directory",
    [
        ("Geometry optimization", "STRUCT"),
        ("Phonon frequencies", "PHONON"),
        ("Elastic constants", "ELASTIC"),
        ("STRUCT", "STRUCT"),
        ("PHON", "PHON"),
        ("ELAST", "ELAST"),
    ],
)
def test_parent_archive_includes_submitted_input(tmp_path, input_filename, label, directory):
    retrieved = _create_retrieved_folder(tmp_path / "outputs", {"OUTPUT": b"result"})
    repository = _create_retrieved_folder(
        tmp_path / "inputs", {input_filename: b"submitted input\n", "_aiidasubmit.sh": b"script"}
    )
    calc = DummyCalcJobNode(uuid="calc", label=label, retrieved=retrieved)
    calc.base = SimpleNamespace(repository=repository)
    calc.get_option = lambda name: input_filename
    child = DummyProcessNode(uuid="child", called=[calc])
    parent = DummyProcessNode(uuid="parent", label="NaCl", called=[child])

    def inspect_archive(source_dir, **kwargs):
        folder = source_dir / directory
        assert (folder / "INPUT").read_bytes() == b"submitted input\n"
        assert (folder / "OUTPUT").read_bytes() == b"result"
        assert not (folder / "_aiidasubmit.sh").exists()
        return _fake_archive_and_save(source_dir, **kwargs)

    with patch("aiida_mpds_monitor.generate_archive.load_node", return_value=parent), patch(
        "aiida_mpds_monitor.generate_archive.CalcJobNode", DummyCalcJobNode
    ), patch("aiida_mpds_monitor.generate_archive.archive_and_save", side_effect=inspect_archive):
        assert generate_parent_archive("parent", tmp_root=tmp_path / "tmp") is not None


@pytest.mark.parametrize("retrieved_input", [False, True])
def test_single_archive_preserves_retrieved_input(tmp_path, retrieved_input):
    files = {"OUTPUT": b"result"}
    if retrieved_input:
        files["INPUT"] = b"retrieved input"
    calc = DummyCalcJobNode(
        uuid="calc", retrieved=_create_retrieved_folder(tmp_path / "outputs", files)
    )
    calc.base = SimpleNamespace(repository=_create_retrieved_folder(
        tmp_path / "inputs", {"INPUT": b"submitted input"}
    ))
    calc.get_option = lambda name: "INPUT"

    def inspect_archive(source_dir, **kwargs):
        expected = b"retrieved input" if retrieved_input else b"submitted input"
        assert (source_dir / "INPUT").read_bytes() == expected
        return _fake_archive_and_save(source_dir, **kwargs)

    with patch("aiida_mpds_monitor.generate_archive.load_node", return_value=calc), patch(
        "aiida_mpds_monitor.generate_archive.archive_and_save", side_effect=inspect_archive
    ):
        assert generate_archive("calc", tmp_root=tmp_path / "tmp") is not None


def test_archive_without_saved_input_is_skipped(tmp_path, caplog):
    calc = DummyCalcJobNode(
        uuid="calc",
        retrieved=_create_retrieved_folder(tmp_path / "outputs", {"OUTPUT": b"result"}),
    )
    calc.base = SimpleNamespace(repository=_create_retrieved_folder(tmp_path / "inputs", {}))
    calc.get_option = lambda name: "INPUT"
    with patch("aiida_mpds_monitor.generate_archive.load_node", return_value=calc), patch(
        "aiida_mpds_monitor.generate_archive.archive_and_save", side_effect=_fake_archive_and_save
    ) as compress:
        assert generate_archive("calc", tmp_root=tmp_path / "tmp") is None
    compress.assert_not_called()
    assert "no saved INPUT" in caplog.text
    assert not (tmp_path / "tmp" / "calc").exists()


@pytest.mark.parametrize("label", ["STRUCT", "PHON", "ELAST"])
@pytest.mark.parametrize("retrieved_input", [False, True])
def test_parent_archive_requires_input(tmp_path, caplog, label, retrieved_input):
    files = {"OUTPUT": b"result"}
    if retrieved_input:
        files["INPUT"] = b"retrieved input"
    calc = DummyCalcJobNode(
        uuid="calc", label=label,
        retrieved=_create_retrieved_folder(tmp_path / "outputs", files),
    )
    calc.base = SimpleNamespace(repository=_create_retrieved_folder(tmp_path / "inputs", {}))
    calc.get_option = lambda name: "INPUT"
    child = DummyProcessNode(uuid="child", called=[calc])
    parent = DummyProcessNode(uuid="parent", label="NaCl", called=[child])

    def inspect_archive(source_dir, **kwargs):
        assert (source_dir / label / "INPUT").read_bytes() == b"retrieved input"
        return _fake_archive_and_save(source_dir, **kwargs)

    with patch("aiida_mpds_monitor.generate_archive.load_node", return_value=parent), patch(
        "aiida_mpds_monitor.generate_archive.CalcJobNode", DummyCalcJobNode
    ), patch(
        "aiida_mpds_monitor.generate_archive.archive_and_save", side_effect=inspect_archive
    ) as compress:
        result = generate_parent_archive("parent", tmp_root=tmp_path / "tmp")

    assert (result is not None) is retrieved_input
    assert compress.call_count == int(retrieved_input)
    assert not (tmp_path / "tmp" / "parent").exists()
    if not retrieved_input:
        assert "no saved INPUT" in caplog.text


def test_single_archive_falls_back_to_repository_input(tmp_path):
    calc = DummyCalcJobNode(
        uuid="calc",
        retrieved=_create_retrieved_folder(tmp_path / "outputs", {"OUTPUT": b"result"}),
    )
    calc.base = SimpleNamespace(repository=_create_retrieved_folder(
        tmp_path / "inputs", {"INPUT": b"submitted input"}
    ))
    calc.get_option = lambda name: "missing.d12"

    def inspect_archive(source_dir, **kwargs):
        assert (source_dir / "INPUT").read_bytes() == b"submitted input"
        return _fake_archive_and_save(source_dir, **kwargs)

    with patch("aiida_mpds_monitor.generate_archive.load_node", return_value=calc), patch(
        "aiida_mpds_monitor.generate_archive.archive_and_save", side_effect=inspect_archive
    ):
        assert generate_archive("calc", tmp_root=tmp_path / "tmp") is not None
