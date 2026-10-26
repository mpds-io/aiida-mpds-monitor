from io import BytesIO

import pytest
from aiida.common.links import LinkType
from aiida.engine import ProcessState
from aiida.orm import CalcJobNode, FolderData, WorkChainNode
from aiida.storage.sqlite_temp import SqliteTempBackend

from aiida_mpds_monitor.status import EXTRA_PARENT_PROCESSED
from tests.test_crystal_repair import OUTPUT


@pytest.fixture
def nodes():
    # Isolated, temporary AiiDA storage: never use the user's active database.
    backend = SqliteTempBackend(SqliteTempBackend.create_profile())
    try:
        parent = WorkChainNode(backend=backend).store()
        base = WorkChainNode(backend=backend)
        base.base.links.add_incoming(parent, link_type=LinkType.CALL_WORK, link_label="base")
        base.store()
        good = CalcJobNode(backend=backend)
        good.base.links.add_incoming(base, link_type=LinkType.CALL_CALC, link_label="structure")
        good.store()
        bad = CalcJobNode(backend=backend)
        bad.base.links.add_incoming(base, link_type=LinkType.CALL_CALC, link_label="phonon")
        bad.store()
        for calculation, contents in ((good, OUTPUT), (bad, "Segmentation fault")):
            retrieved = FolderData(backend=backend)
            retrieved.base.repository.put_object_from_filelike(BytesIO(contents.encode()), "OUTPUT")
            retrieved.base.links.add_incoming(calculation, link_type=LinkType.CREATE, link_label="retrieved")
            retrieved.store()
        for node, label in (
            (parent, "MPDSStructureWorkChain"), (base, "BaseCrystalWorkChain"),
            (good, "CrystalParallelCalculation"), (bad, "CrystalParallelCalculation"),
        ):
            node.set_process_label(label)
            node.set_process_state(ProcessState.FINISHED)
            node.set_exit_status(0)
            node.label = "NaCl"
            node.seal()
        parent.base.extras.set(EXTRA_PARENT_PROCESSED, True)
        base.base.extras.set(EXTRA_PARENT_PROCESSED, True)
        backend.get_session().commit()
        yield parent, base, good, bad
    finally:
        backend.close()


