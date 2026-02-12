# Status constants
STATUS_EXC = "excepted"
STATUS_DONE = "finished"
STATUS_WAITING = "waiting"

# Extra field names for tracking workflow state
EXTRA_STARTED = "webhook_started"
EXTRA_FINISHED = "webhook_finished"
EXTRA_PARENT_PROCESSED = "webhook_parent_processed"
EXTRA_PARENT_ERROR_SENT = "webhook_parent_error_sent"


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
        child_calcs = [
            n
            for n in called_nodes
            if hasattr(n, "process_label") and n.process_label in child_types
        ]
        if not child_calcs:
            return False

        # Check the last (most recent) child calculation by PK
        # If the calculation was retried, we only care about the final attempt
        last_calc = max(child_calcs, key=lambda n: n.pk)
        is_broken = (
            last_calc.is_failed or last_calc.is_excepted or last_calc.is_killed
        )
        if is_broken and logger:
            logger.warning(
                f"BaseCrystalWorkChain {base_node.pk} finished but child "
                f"{last_calc.process_label} {last_calc.pk} failed"
            )
        return is_broken
    except Exception:
        return False


def get_node_status(node, child_types=None, logger=None):
    """Determine the status of an AiiDA node.

    Args:
        node: The AiiDA node to check
        child_types (list, optional): List of child process labels to check for failures.
                                     Defaults to ["CrystalParallelCalculation"]
        logger (logging.Logger, optional): Logger for warning messages

    Returns:
        str: One of STATUS_DONE, STATUS_EXC, STATUS_WAITING, or "excepted-{exit_code}"
    """
    if child_types is None:
        child_types = ["CrystalParallelCalculation"]

    state = node.process_state.value
    if state.lower() == "finished":
        # Check if any child calculation failed
        if check_child_calculation(
            node, child_types=child_types, logger=logger
        ):
            return STATUS_EXC

        excepted = node.is_excepted
        exit_code = node.exit_code.status if node.exit_code else 0
        if exit_code == 0 and not excepted:
            return STATUS_DONE
        # if node broke due to unexpected error (in code, for example)
        if excepted and not node.is_failed:
            return STATUS_EXC
        else:
            return f"excepted-{exit_code}"
    elif state.lower() in ["running", "submitting", "created"]:
        return STATUS_WAITING
    elif state.lower() in ["excepted"]:
        exit_code = node.exit_code.status if node.exit_code else 1
        return f"{STATUS_EXC}-{exit_code}"
    else:
        # For any other error states
        return STATUS_EXC
