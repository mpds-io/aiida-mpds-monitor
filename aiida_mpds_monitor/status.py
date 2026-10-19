from typing import Any


# Status constants
STATUS_EXC = "excepted"
STATUS_DONE = "finished"
STATUS_WAITING = "waiting"

EXTRA_PARENT_PROCESSED = "webhook_parent_processed"
EXTRA_INPROGRESS_SENT = "webhook_inprogress_sent"
EXTRA_ARCHIVE_PROCESSED = "webhook_archive_processed"
EXTRA_ARCHIVE_RECOVERY = "mpds_archive_recovery"

EXTRA_VALIDATION = "mpds_archive_validation"


def excluded_from_archive(node: Any) -> bool:
    """Honor CRYSTAL validation exclusions without importing the output parser."""
    if getattr(node, "process_label", None) != "CrystalParallelCalculation":
        return False
    base = getattr(node, "base", None)
    if base is None:
        return False
    record = base.extras.get(EXTRA_VALIDATION, None)
    return isinstance(record, dict) and (
        record.get("status") in ("broken", "corrupted")
        and record.get("exclude_from_archive") is True
    )


def check_child_calculation(base_node, child_types=None, logger=None):
    """Check if the last child calculation (of specified types) failed.
    Returns True if the child is broken, False otherwise.
    Note: If child calculation was retried, we check only the LAST attempt.
    If the last attempt succeeded, we return False even if earlier attempts failed.

    Args:
        base_node: The AiiDA node to check
        child_types (list, optional): List of child process labels to check.
            Defaults to ["CrystalParallelCalculation"]
        logger (logging.Logger, optional): Logger for warning messages

    Returns:
        bool: True if the last child calculation failed, False otherwise
    """
    if child_types is None:
        child_types = ["CrystalParallelCalculation"]
    try:
        called_nodes = base_node.called
        child_calcs = [n for n in called_nodes if hasattr(n, "process_label") and n.process_label in child_types]
        if not child_calcs:
            return False
        # Check the most recent child calculation by PK
        # If the calculation was retried, we only care about the final attempt
        last_calc = max(child_calcs, key=lambda n: n.pk)
        is_broken = last_calc.is_failed or last_calc.is_excepted or last_calc.is_killed
        if is_broken and logger:
            logger.warning(
                f"BaseCrystalWorkChain {base_node.pk} finished but child "
                f"{last_calc.process_label} {last_calc.pk} failed"
            )
        return is_broken
    except Exception:
        return False


def get_child_calculations(base_node, child_types=None):
    """Return direct child calculations matching the configured process labels."""
    if child_types is None:
        child_types = ["CrystalParallelCalculation"]
    try:
        return [
            node
            for node in base_node.called
            if hasattr(node, "process_label") and node.process_label in child_types
        ]
    except Exception:
        return []


def base_has_ready_children(base_node, child_types=None, all_stages_ready=False):
    """Return whether a base structure has enough successful child calculations."""
    child_calcs = [
        calc for calc in get_child_calculations(base_node, child_types)
        if not excluded_from_archive(calc)
    ]
    if not child_calcs:
        return False

    successful = [
        calc for calc in child_calcs if getattr(calc, "is_finished_ok", False) is True
    ]
    if all_stages_ready:
        return len(successful) == len(child_calcs)
    return bool(successful)


def get_node_status(node, child_types=None, logger=None):
    """Determine the status of an AiiDA node."""
    if child_types is None:
        child_types = ["CrystalParallelCalculation"]

    if getattr(node, "process_label", None) in (
        "BaseCrystalWorkChain", "CrystalParallelCalculation",
    ):
        base = getattr(node, "base", None)
        record = base.extras.get(EXTRA_VALIDATION, None) if base is not None else None
        if isinstance(record, dict) and record.get("status") == "broken":
            return STATUS_EXC

    state = node.process_state.value

    if state.lower() == "finished":
        if check_child_calculation(node, child_types=child_types, logger=logger):
            # Child failed — get its exit code if available
            child_exit_code = _get_child_exit_code(node, child_types)
            if child_exit_code and child_exit_code != 0:
                return f"{STATUS_EXC}-{child_exit_code}"
            return STATUS_EXC

        excepted = node.is_excepted
        exit_code = node.exit_code.status if node.exit_code is not None else 0
        if exit_code == 0 and not excepted:
            return STATUS_DONE
        if excepted:
            if exit_code != 0:
                return f"{STATUS_EXC}-{exit_code}"
            else:
                return STATUS_EXC
        if exit_code != 0:
            return f"{STATUS_EXC}-{exit_code}"
        return STATUS_EXC

    elif state.lower() in ["running", "submitting", "created", "waiting", "paused"]:
        return STATUS_WAITING

    elif state.lower() == "excepted":
        # Checking child exit codes
        child_exit_code = _get_child_exit_code(node, child_types)
        if child_exit_code and child_exit_code != 0:
            # Exit code of child process
            return f"{STATUS_EXC}-{child_exit_code}"

        return STATUS_EXC

    else:
        # failed, killed, etc.
        return STATUS_EXC


def _get_child_exit_code(node, child_types):
    """Get exit_code of last gradnchild process."""
    try:
        called_nodes = node.called
        child_calcs = [n for n in called_nodes if hasattr(n, "process_label") and n.process_label in child_types]
        if not child_calcs:
            return None

        last_calc = max(child_calcs, key=lambda n: n.pk)

        if last_calc.exit_code is not None:
            return last_calc.exit_code.status
        return None
    except Exception:
        return None
