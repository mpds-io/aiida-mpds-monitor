# aiida_mpds_monitor/status.py
"""Status checking and child calculation validation utilities."""

import logging

# Status constants
BASE_CRYSTAL_TYPE = "BaseCrystalWorkChain"
STATUS_EXC = "excepted"
STATUS_DONE = "finished"
STATUS_WAITING = "waiting"

# Extra field names for tracking workflow state
EXTRA_STARTED = "webhook_started"
EXTRA_FINISHED = "webhook_finished"
EXTRA_PARENT_PROCESSED = "webhook_parent_processed"
EXTRA_PARENT_ERROR_SENT = "webhook_parent_error_sent"


def check_child_calculation(base_node, logger=None):
    """Check if the last CrystalParallelCalculation child failed.
    
    Returns True if the child is broken, False otherwise.
    
    Note: If CrystalParallelCalculation was retried, we check only the LAST attempt.
    If the last attempt succeeded, we return False even if earlier attempts failed.
    
    Args:
        base_node: The AiiDA node to check
        logger (logging.Logger, optional): Logger for warning messages
        
    Returns:
        bool: True if the last child calculation failed, False otherwise
    """
    try:
        called_nodes = base_node.called
        crystal_calcs = [
            n for n in called_nodes
            if hasattr(n, 'process_label') and n.process_label == "CrystalParallelCalculation"
        ]
        if not crystal_calcs:
            return False
        
        # Check the last (most recent) CrystalParallelCalculation by PK
        # If the calculation was retried, we only care about the final attempt
        last_calc = max(crystal_calcs, key=lambda n: n.pk)
        is_broken = last_calc.is_failed or last_calc.is_excepted or last_calc.is_killed
        if is_broken and logger:
            logger.warning(
                f"BaseCrystalWorkChain {base_node.pk} finished but child "
                f"CrystalParallelCalculation {last_calc.pk} failed"
            )
        return is_broken
    except Exception:
        return False


def get_node_status(node, logger=None):
    """Determine the status of an AiiDA node.
    
    Args:
        node: The AiiDA node to check
        logger (logging.Logger, optional): Logger for warning messages
        
    Returns:
        str: One of STATUS_DONE, STATUS_EXC, STATUS_WAITING, or "excepted-{exit_code}"
    """
    state = node.process_state.value
    if state.lower() == "finished":
        # Check if any child CrystalParallelCalculation failed
        if check_child_calculation(node, logger=logger):
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
