import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import py7zr
import pytest
import yaml
from ase import Atoms

from aiida_mpds_monitor import crystal_validation as validation
from aiida_mpds_monitor import repair_crystal as repair


INPUT = "PBE0\nXLGRID\nTOLLDENS\n8\nTOLLGRID\n16\nTOLDEE\n9\n"
OUTPUT = "CRYSTAL output\nEEEEEEEEEE TERMINATION DATE 01 10 2026\n"


def stage(root, name, output=OUTPUT):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "INPUT").write_text(INPUT)
    (folder / "OUTPUT").write_text(output)
    return folder


def parsed():
    return {
        "finished": True, "energy": -10.0,
        "structures": [Atoms("NaCl", positions=[[0, 0, 0], [1, 1, 1]], cell=[3, 3, 3])],
        "phonons": {"modes": {"0 0 0": [1., 2.]},
                    "ph_eigvecs": {"0 0 0": [[1., 0.], [0., 1.]]},
                    "ir_active": [True, False], "raman_active": [False, True],
                    "irreps": {}, "zpe": 0., "born_charges": {},
                    "td": {key: [0.] for key in ("et", "pv", "t", "p", "ts", "S", "C")}},
        "elastic": {"elastic_constants": np.eye(6).tolist(),
                    "elastic_moduli": np.eye(6).tolist(),
                    **{key: 1. for key in ("K_V", "G_V", "K_R", "G_R", "K", "G", "E", "v")}},
    }


@pytest.fixture
def parser(monkeypatch):
    parser = MagicMock()
    parser.acceptable.return_value = True
    parser.return_value.info = parsed()
    monkeypatch.setattr(validation, "CRYSTOUT", parser)
    return parser


@pytest.mark.parametrize("name", list(validation.STAGES))
def test_every_stage_alias_is_parsed(tmp_path, parser, name):
    result = validation.validate_stage(stage(tmp_path, name))
    assert result.status == "valid"
    assert result.energy_ev == -10
    parser.assert_called_once()
    parser.acceptable.assert_called_once()


@pytest.mark.parametrize("failure", [
    "parser", "unfinished", "missing input", "metadata", "settings", "energy",
    "structure", "positions", "lattice", "phonon keys", "phonon nan", "phonon counts",
    "elastic", "fort34", "termination", "scf", "crash",
])
def test_invalid_data_is_recorded(tmp_path, parser, failure):
    folder = stage(tmp_path, "STRUCT")
    info = parser.return_value.info
    if failure == "parser":
        parser.side_effect = validation.CRYSTOUT_Error("parser domain error")
    elif failure == "unfinished":
        info["finished"] = False
    elif failure == "missing input":
        (folder / "INPUT").unlink()
    elif failure == "metadata":
        (folder / "INPUT").write_text(INPUT + "pk: 123\n")
    elif failure == "settings":
        (folder / "INPUT").write_text("PBE\n")
    elif failure == "energy":
        info["energy"] = float("nan")
    elif failure == "structure":
        info["structures"] = []
    elif failure == "positions":
        info["structures"][0].positions[0, 0] = float("inf")
    elif failure == "lattice":
        info["structures"][0].cell = np.zeros((3, 3))
    elif failure.startswith("phonon"):
        folder.rename(tmp_path / "PHON")
        folder = tmp_path / "PHON"
        if failure == "phonon keys":
            info["phonons"]["ph_eigvecs"] = {}
        elif failure == "phonon nan":
            info["phonons"]["modes"]["0 0 0"][0] = float("nan")
        else:
            info["phonons"]["ph_eigvecs"]["0 0 0"].pop()
    elif failure == "elastic":
        folder.rename(tmp_path / "ELAST")
        folder = tmp_path / "ELAST"
        info["elastic"]["elastic_moduli"] = None
    elif failure == "fort34":
        (folder / "fort.34").write_text("corrupted structure")
    elif failure == "termination":
        (folder / "OUTPUT").write_text("EEEEEEEEEE INT_CALC TERMINATION DATE 01\n")
    elif failure == "scf":
        (folder / "OUTPUT").write_text("== SCF ENDED - TOO MANY CYCLES\n" + OUTPUT)
    else:
        (folder / "OUTPUT").write_text("forrtl: severe (38): error during write\n")
    result = validation.validate_stage(folder)
    assert result.status in ("broken", "corrupted")
    assert result.reason
    # A report must serialize even if the parser returned NaNs or arrays.
    validation.ValidationReport([result]).write(tmp_path / "report.json")


def test_real_pycrystal_rejects_non_crystal_output(tmp_path):
    result = validation.validate_stage(stage(tmp_path, "STRUCT", "Some other software output"))
    assert result.status == "corrupted"
    assert "not a CRYSTAL output" in result.reason


@pytest.mark.parametrize("mismatch", ["energy", "elements"])
def test_property_stage_is_compared_to_struct(tmp_path, parser, mismatch):
    stage(tmp_path, "STRUCT")
    stage(tmp_path, "PHON")
    info = parsed()
    if mismatch == "energy":
        info["energy"] = -11.
    else:
        info["structures"] = [Atoms("Si", cell=[3, 3, 3])]
    # Folder sorting parses PHON before STRUCT.
    parser.side_effect = [SimpleNamespace(info=info), SimpleNamespace(info=parsed())]
    report = validation.validate_crystal_tree(tmp_path, remove_invalid=True)
    assert report.uploadable
    assert not (tmp_path / "PHON").exists()
    assert (tmp_path / "STRUCT").exists()
    assert report.stages[0].status == "corrupted"


@pytest.mark.parametrize("archived", [False, True])
def test_repaired_7z_contains_only_valid_stages_and_preserves_source(tmp_path, parser, archived):
    source = tmp_path / "material"
    stage(source, "STRUCT")
    stage(source, "PHONON", "Segmentation fault")
    stage(source, "ELAST")
    (source / "stale-summary.json").write_text("obsolete")
    if archived:
        with py7zr.SevenZipFile(tmp_path / "material.7z", "w") as archive:
            archive.writeall(source, arcname=source.name)
        source = tmp_path / "material.7z"
    original = source.read_bytes() if archived else None
    output = tmp_path / "repaired.7z"
    report = repair.repair_archive(source, output)
    assert report.uploadable
    with py7zr.SevenZipFile(output) as archive:
        assert set(archive.getnames()) == {"STRUCT/INPUT", "STRUCT/OUTPUT", "ELAST/INPUT", "ELAST/OUTPUT"}
    saved = json.loads(output.with_suffix(".validation.json").read_text())
    assert next(r for r in saved["stages"] if r["stage"] == "PHONON")["status"] == "broken"
    if archived:
        assert source.read_bytes() == original
    else:
        assert (source / "PHONON" / "OUTPUT").exists()


def test_invalid_struct_produces_report_but_no_archive(tmp_path, parser):
    source = tmp_path / "material"
    stage(source, "STRUCT", "Segmentation fault")
    stage(source, "PHON")
    output = tmp_path / "clean.7z"
    report = repair.repair_archive(source, output)
    assert not report.uploadable
    assert not output.exists()
    assert output.with_suffix(".validation.json").exists()


def test_dry_run_writes_nothing(tmp_path, parser):
    source = tmp_path / "material"
    stage(source, "STRUCT")
    stage(source, "PHON", "Segmentation fault")
    output = tmp_path / "clean.7z"
    assert repair.repair_archive(source, output, dry_run=True).uploadable
    assert not output.exists()
    assert not output.with_suffix(".validation.json").exists()
    assert (source / "PHON").is_dir()


@pytest.mark.parametrize("upload_success", [True, False])
def test_cli_uploads_rebuilt_archive_with_configured_auth(tmp_path, parser, monkeypatch, upload_success):
    source = tmp_path / "material"
    stage(source, "STRUCT")
    stage(source, "ELAST", "Segmentation fault")
    output = tmp_path / "clean.7z"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"archive_upload_url": "https://example.invalid/upload",
                                     "archive_key": "test-key", "archive_bid": 2,
                                     "archive_schema_id": 3}))
    monkeypatch.delenv("MPDS_ARCHIVE_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["repair", str(source), "--output", str(output),
                                    "--upload", "--config", str(config)])
    with patch.object(repair, "send_archive", return_value=upload_success) as upload:
        assert repair.main() == (0 if upload_success else 3)
    upload.assert_called_once_with("https://example.invalid/upload", output,
                                   bid=2, schema_id=3, key="test-key")
    assert output.exists()


def test_cli_does_not_upload_without_valid_struct(tmp_path, parser, monkeypatch):
    source = tmp_path / "material"
    stage(source, "STRUCT", "Segmentation fault")
    monkeypatch.setattr("sys.argv", ["repair", str(source), "--upload"])
    with patch.object(repair, "send_archive") as upload:
        assert repair.main() == 2
    upload.assert_not_called()


def test_existing_output_is_not_overwritten(tmp_path, parser):
    source = tmp_path / "material"
    stage(source, "STRUCT")
    output = tmp_path / "existing.7z"
    output.write_bytes(b"existing archive")
    with pytest.raises(FileExistsError):
        repair.repair_archive(source, output)
    assert output.read_bytes() == b"existing archive"


@pytest.mark.parametrize("upload_success", [True, False])
def test_monitor_checks_stages_and_uploads_only_clean_archive(tmp_path, parser, upload_success):
    from aiida_mpds_monitor import daemon, generate_archive as generation
    from aiida_mpds_monitor import crystal_recovery

    class Calc(SimpleNamespace):
        pass

    nodes = []
    for index, (label, output) in enumerate([
        ("STRUCT", OUTPUT), ("PHONON", "Segmentation fault"), ("ELASTIC", OUTPUT),
    ]):
        folder = stage(tmp_path / "retrieved", label, output)
        retrieved = SimpleNamespace(
            list_object_names=lambda: ["INPUT", "OUTPUT"],
            open=lambda name, mode, folder=folder: (folder / name).open(mode),
        )
        nodes.append(Calc(
            pk=index, uuid=f"calc-{index}", label=label, called=[], is_finished_ok=True,
            process_label="CrystalParallelCalculation", outputs=SimpleNamespace(retrieved=retrieved),
        ))
    child = SimpleNamespace(uuid="child", pk=100, called=nodes, process_label="BaseCrystalWorkChain")
    parent = SimpleNamespace(pk=42, uuid="parent", label="NaCl", called=[child])
    marks = {}
    extras = MagicMock()
    extras.get.side_effect = lambda key, default=None: marks.get(key, default)
    extras.set.side_effect = marks.__setitem__
    extras.delete.side_effect = marks.pop
    parent.base = SimpleNamespace(extras=extras)
    archive_path = tmp_path / "archives" / "parent" / "NaCl.7z"

    def generate(*args, **kwargs):
        assert kwargs.get("validate_crystal", False) is False
        return generation.generate_parent_archive(*args, tmp_root=tmp_path / "tmp", **kwargs)

    def compress(root, **options):
        assert (root / "PHONON").exists()
        with py7zr.SevenZipFile(str(root) + ".7z", "w") as archive:
            archive.writeall(root, arcname=root.name)

    uploads = 0

    def upload(url, path, **options):
        nonlocal uploads
        uploads += 1
        with py7zr.SevenZipFile(path) as archive:
            assert any("PHONON/OUTPUT" in name for name in archive.getnames()) is (uploads == 1)
            assert any("STRUCT/OUTPUT" in name for name in archive.getnames())
        if uploads == 1:
            options["result"].status_code = 422
            options["result"].description = "Archive validation failed: Found not a finished output"
            return False
        return upload_success

    with patch.object(generation, "load_node", return_value=parent), patch.object(
        generation, "CalcJobNode", Calc
    ), patch.object(generation, "archive_and_save", side_effect=compress), patch.object(
        daemon, "generate_parent_archive", side_effect=generate
    ), patch.object(daemon, "send_archive", side_effect=upload) as send, patch.object(
        crystal_recovery, "mark_broken_processes",
        side_effect=lambda *args, **options: options["before_commit"](),
    ) as mark:
        assert daemon.generate_and_upload_archive(parent, [child], {}, MagicMock()) is upload_success
    assert send.call_count == 2
    mark.assert_called_once()
    assert mark.call_args.args[1][0][0] is nodes[1]
    assert archive_path.exists() is (not upload_success)
    saved = json.loads(archive_path.with_suffix(".validation.json").read_text())
    assert next(r for r in saved["stages"] if r["folder"] == "PHONON")["status"] == "broken"


def test_cli_dry_run_does_not_upload(tmp_path, parser, monkeypatch):
    source = tmp_path / "material"
    stage(source, "STRUCT")
    monkeypatch.setattr("sys.argv", ["repair", str(source), "--dry-run", "--upload"])
    with patch.object(repair, "send_archive") as upload:
        assert repair.main() == 0
    upload.assert_not_called()
    assert not source.with_name("material.clean.7z").exists()


def test_symlinks_are_rejected_without_following_them(tmp_path, parser):
    source = tmp_path / "material"
    stage(source, "STRUCT")
    (source / "PHON").symlink_to(source / "STRUCT", target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        repair.repair_archive(source, tmp_path / "clean.7z")


def test_invalid_thermodynamics_rejects_phonon_stage(tmp_path, parser):
    folder = stage(tmp_path, "PHONON")
    parser.return_value.info["phonons"]["td"] = None
    assert validation.validate_stage(folder).status == "corrupted"


def test_server_supported_nonfinite_property_values_are_accepted(tmp_path, parser):
    folder = stage(tmp_path, "PHONON")
    for key in ("p", "S", "ts"):
        parser.return_value.info["phonons"]["td"][key] = [float("inf")]
    assert validation.validate_stage(folder).status == "valid"
    folder.rename(tmp_path / "ELASTIC")
    parser.return_value.info["elastic"]["E"] = float("nan")
    assert validation.validate_stage(tmp_path / "ELASTIC").status == "valid"
