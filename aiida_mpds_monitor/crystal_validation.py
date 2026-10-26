"""Validate CRYSTAL stage folders against the MPDS archive requirements."""

import hashlib
import json
import logging
import math
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from aiida_crystal_dft.io.f34 import Fort34
from pycrystal import CRYSTOUT, CRYSTOUT_Error
from pyparsing import ParseBaseException

from .archive_files import write_report

logger = logging.getLogger(__name__)

STAGES = {"STRUCT": "STRUCT", "PHON": "PHONON", "PHONON": "PHONON",
          "ELAST": "ELASTIC", "ELASTIC": "ELASTIC"}
_FATAL = re.compile(
    r"segmentation fault|forrtl:\s*severe|MPI_ABORT|job has been aborted|"
    r"processes exited with non-zero status", re.IGNORECASE,
)
_TERMINATION = re.compile(r"^\s*E{5,}\s+TERMINATION\s+DATE\b", re.MULTILINE)
_SCF = re.compile(r"SCF ENDED\s*-\s*([^\r\n]+)")


class StageValidationError(ValueError):
    """Known invalid calculation data, distinct from failures of the validator."""


@dataclass
class StageResult:
    folder: str
    stage: str
    status: str = "valid"
    reason: str = ""
    energy_ev: Optional[float] = None
    elements: List[str] = field(default_factory=list)
    output_sha256: Optional[str] = None
    process_failure: bool = False


@dataclass
class ValidationReport:
    stages: List[StageResult]
    error: Optional[str] = None

    @property
    def uploadable(self) -> bool:
        return self.error is None and any(
            result.stage == "STRUCT" and result.status == "valid" for result in self.stages
        )

    def write(self, path: Path, overwrite: bool = True) -> None:
        write_report(path, json.dumps(asdict(self), indent=2, allow_nan=False) + "\n", overwrite)


def _finite_values(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_finite_values(item) for item in value.values())
    if isinstance(value, (list, tuple, np.ndarray)):
        return all(_finite_values(item) for item in value)
    if isinstance(value, (int, float, np.number)):
        return math.isfinite(float(value))
    return False


def _completion_error(output: str) -> Optional[str]:
    """A completion record from an older appended run does not finish a new run."""
    termination = None
    for match in _TERMINATION.finditer(output):
        termination = match
    scf = None
    for match in _SCF.finditer(output):
        scf = match
    if termination is None or (scf is not None and scf.start() > termination.start()):
        return "missing final CRYSTAL termination record"
    if scf is not None and "TOO MANY CYCLES" in scf.group(1):
        return "final SCF did not converge"
    return None


def _validate_properties(info: Dict[str, Any], stage: str) -> None:
    if stage == "PHONON":
        phonons = info["phonons"]
        modes, vectors = phonons["modes"], phonons.get("ph_eigvecs", {})
        if not modes or modes.keys() != vectors.keys():
            raise StageValidationError("phonon modes and eigenvector wavevectors are missing or inconsistent")
        if not _finite_values(modes) or not _finite_values(vectors):
            raise StageValidationError("non-finite phonon modes or eigenvectors")
        for wavevector, frequencies in modes.items():
            eigenvectors = np.asarray(vectors[wavevector])
            if eigenvectors.ndim < 2 or len(eigenvectors) != len(frequencies):
                raise StageValidationError("phonon mode and eigenvector counts differ")
        for key in ("ir_active", "raman_active", "irreps", "zpe", "born_charges"):
            if key not in phonons:
                raise StageValidationError(f"missing phonon {key}")
        thermodynamics = phonons.get("td")
        if not isinstance(thermodynamics, dict):
            raise StageValidationError("missing phonon thermodynamics")
        lengths = set()
        for key in ("et", "pv", "t", "p", "ts", "S", "C"):
            values = np.asarray(thermodynamics[key], dtype=float)
            if values.ndim != 1 or not values.size or np.isnan(values).any():
                raise StageValidationError(f"missing or invalid phonon thermodynamics: {key}")
            # The server normalizes infinity in these three fields to zero.
            if key not in ("p", "ts", "S") and not np.isfinite(values).all():
                raise StageValidationError(f"non-finite phonon thermodynamics: {key}")
            lengths.add(len(values))
        if len(lengths) != 1:
            raise StageValidationError("phonon thermodynamic array lengths differ")
    elif stage == "ELASTIC":
        elastic = info["elastic"]
        for key in ("elastic_constants", "elastic_moduli"):
            matrix = np.asarray(elastic.get(key), dtype=float)
            if matrix.shape != (6, 6) or not np.isfinite(matrix).all():
                raise StageValidationError(f"missing or invalid 6x6 {key}")
        for key in ("K_V", "G_V", "K_R", "G_R", "K", "G", "E", "v"):
            value = float(elastic[key])
            # The server explicitly handles NaN effective moduli as unavailable.
            if math.isinf(value):
                raise StageValidationError(f"infinite effective elastic modulus: {key}")


def validate_stage(folder: Path) -> StageResult:
    """Check one recognized stage; a parser exception invalidates only this folder."""
    result = StageResult(folder=folder.name, stage=STAGES[folder.name.upper()])
    try:
        for filename in ("INPUT", "OUTPUT"):
            path = folder / filename
            if not path.is_file() or path.stat().st_size == 0:
                raise StageValidationError(f"missing or empty {filename}")
        raw_output = (folder / "OUTPUT").read_bytes()
        result.output_sha256 = hashlib.sha256(raw_output).hexdigest()
        output = raw_output.decode("ascii")
        del raw_output  # Avoid retaining another full copy throughout parsing.
        fatal = _FATAL.search(output)
        if fatal:
            result.status = "broken"
            result.process_failure = True
            raise StageValidationError(f"fatal runtime error: {fatal.group(0)}")
        # Give pycrystal the path instead of another full in-memory OUTPUT copy.
        if not CRYSTOUT.acceptable(str(folder / "OUTPUT")):
            raise StageValidationError("not a CRYSTAL output")
        completion_error = _completion_error(output)
        if completion_error is not None:
            result.status = "broken"
            result.process_failure = True
            raise StageValidationError(completion_error)
        del output  # Release the scan buffer before pycrystal loads its own copy.
        # A domain parser error can mean an unsupported feature, not a broken process.
        info = CRYSTOUT(str(folder / "OUTPUT")).info
        if info.get("finished") is not True:
            result.status = "broken"
            result.process_failure = True
            raise StageValidationError("CRYSTAL calculation did not finish")
        energy = float(info["energy"])
        if not math.isfinite(energy):
            raise StageValidationError("missing or non-finite energy")
        structures = info.get("structures")
        if not structures:
            raise StageValidationError("no parsed atomic structure")
        atoms = structures[-1]
        if not len(atoms) or not np.isfinite(atoms.positions).all():
            raise StageValidationError("invalid atomic positions")
        result.elements = sorted(set(atoms.get_chemical_symbols()))
        cell = np.asarray(atoms.cell)
        if not np.isfinite(cell).all() or abs(np.linalg.det(cell)) < 1e-12:
            raise StageValidationError("invalid lattice")
        inp = (folder / "INPUT").read_text(encoding="ascii").replace("\r\n", "\n")
        if "uuid" in inp or "pk:" in inp:
            raise StageValidationError("INPUT contains AiiDA metadata")
        if not all(token in inp for token in (
            "PBE0", "XLGRID", "TOLLDENS\n8", "TOLLGRID\n16", "TOLDEE\n9",
        )):
            raise StageValidationError("INPUT does not conform to the MPDS CRYSTAL settings")
        if (folder / "fort.34").exists():
            try:
                structure = Fort34().read(str(folder / "fort.34")).to_ase()
            except (ValueError, TypeError, KeyError, IndexError, ParseBaseException) as exc:
                raise StageValidationError(f"invalid fort.34: {exc}") from exc
            if sorted(set(structure.get_chemical_symbols())) != result.elements:
                raise StageValidationError("fort.34 and OUTPUT contain different chemical elements")
            if (not np.isfinite(structure.positions).all()
                    or not np.isfinite(np.asarray(structure.cell)).all()
                    or abs(np.linalg.det(structure.cell)) < 1e-12):
                raise StageValidationError("invalid fort.34 geometry")
        try:
            _validate_properties(info, result.stage)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise StageValidationError(str(exc)) from exc
        result.energy_ev = energy
    except (StageValidationError, CRYSTOUT_Error, UnicodeError) as exc:
        if result.status == "valid":
            result.status = "corrupted"
        result.reason = f"{type(exc).__name__}: {exc}"
        logger.warning("CRYSTAL folder %s is %s: %s", folder, result.status, result.reason)
    return result


def validate_crystal_tree(root: Path, remove_invalid: bool = False) -> ValidationReport:
    """Check every stage, then compare valid stages with STRUCT as the reference.

    Deletion is intended for disposable staging directories, never AiiDA data.
    Stage aliases retain their original names in the resulting archive.
    """
    folders = sorted(path for path in root.iterdir()
                     if path.is_dir() and path.name.upper() in STAGES)
    report = ValidationReport([validate_stage(path) for path in folders])
    if len({result.stage for result in report.stages}) != len(report.stages):
        report.error = "Duplicate stage aliases are ambiguous; archive will not be uploaded"
        return report
    structures = [r for r in report.stages if r.stage == "STRUCT" and r.status == "valid"]
    if len(structures) != 1:
        report.error = "Exactly one valid STRUCT folder is required; archive will not be uploaded"
    else:
        reference = structures[0]
        for result in report.stages:
            if result.status != "valid" or result is reference:
                continue
            if result.elements != reference.elements:
                result.status, result.reason = "corrupted", "different chemical system from STRUCT"
            elif abs(result.energy_ev - reference.energy_ev) > 0.01:
                result.status, result.reason = "corrupted", "energy differs from STRUCT by > 0.01 eV"
    for result in report.stages:
        if result.status != "valid":
            logger.warning("CRYSTAL folder %s excluded: %s", result.folder, result.reason)
            if remove_invalid:
                shutil.rmtree(root / result.folder)
    return report
