# aiida_mpds_monitor/submit.py
import argparse
import sys
import logging
from aiida import load_profile
from aiida.orm import load_node, WorkChainNode
from .config import load_config

BASE_CRYSTAL_TYPE = "BaseCrystalWorkChain"


def send_webhook(webhook_url, payload, status):
    import requests
    data = {"payload": payload, "status": status}
    try:
        response = requests.post(webhook_url, json=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Webhook error: {e}", file=sys.stderr)
        return False


def check_child_calculation(base_node):
    """Check if the last CrystalParallelCalculation child failed.
    Returns True if the child is broken, False otherwise.
    
    Note: If CrystalParallelCalculation was retried, we check only the LAST attempt.
    If the last attempt succeeded, we return False even if earlier attempts failed.
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
        if is_broken:
            logging.warning(f"BaseCrystalWorkChain {base_node.pk} finished but child CrystalParallelCalculation {last_calc.pk} failed")
        return is_broken
    except Exception:
        return False


def get_node_status(node):
    state = node.process_state.value
    if state.lower() == "finished":
        # Check if any child CrystalParallelCalculation failed
        if check_child_calculation(node):
            return "excepted"
        
        excepted = node.is_excepted
        exit_code = node.exit_code.status if node.exit_code else 0
        if not excepted and exit_code == 0:
            return "finished"
        # if node broke due to unexpected error (in code, for example)
        if excepted and not node.is_failed:
            return "excepted"
        else:
            return f"excepted-{exit_code}"
    elif state.lower() in ["running", "submitting", "created"]:
        return "waiting"
    elif state.lower() in ["excepted"]:
        exit_code = node.exit_code.status if node.exit_code else 1
        return f"excepted-{exit_code}"
    else:
        # For any other error states
        return "excepted"


def submit_parent(parent_pk: int, webhook_url: str, dry_run: bool = False):
    parent_node = load_node(parent_pk)

    if not isinstance(parent_node, WorkChainNode):
        raise ValueError(f"Node {parent_pk} is not a WorkChain")

    called_nodes = parent_node.called
    base_nodes = [
        n for n in called_nodes
        if isinstance(n, WorkChainNode) and n.process_label == BASE_CRYSTAL_TYPE
    ]

    # Check if parent is in a failed state
    parent_is_broken = (
        parent_node.is_failed or
        parent_node.is_excepted or
        parent_node.is_killed
    )

    if parent_is_broken:
        if base_nodes:
            for base in base_nodes:
                label = base.label
                if label and label.strip():
                    status = get_node_status(base)
                    payload = label.strip()
                    if dry_run:
                        print(f"[DRY-RUN] Parent {parent_pk} failed — would send: payload='{payload}', status='{status}'")
                    else:
                        if send_webhook(webhook_url, payload, status):
                            print(f"Sent ERROR webhook for last subtask '{payload}' ({status})")
                        else:
                            print(f"Failed to send webhook for '{payload}'", file=sys.stderr)
                else:
                    print(f"Parent {parent_pk} failed, but last BaseCrystalWorkChain has no label — skipping")
        else:
            print(f"Parent {parent_pk} failed but launched no BaseCrystalWorkChain — nothing to report")
        return

    # Normal case: parent is OK — process all subnodes
    if not base_nodes:
        print(f"No BaseCrystalWorkChain found under parent {parent_pk}")
        return

    for base_node in base_nodes:
        label = base_node.label
        if not label or not label.strip():
            print(f"Skipping {base_node.pk} — empty label")
            continue

        label = label.strip()
        status = get_node_status(base_node)

        if dry_run:
            print(f"[DRY-RUN] Would send: payload='{label}', status='{status}'")
        else:
            if send_webhook(webhook_url, label, status):
                print(f"Sent webhook for '{label}' ({status})")
            else:
                print(f"Failed to send webhook for '{label}'", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Submit results of a parent workchain to webhook",
        usage="aiida-mpds-submit PARENT_PK [--dry-run]"
    )
    parser.add_argument(
        "parent_pk",
        type=int,
        help="PK of the parent WorkChain"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what would be sent"
    )
    args = parser.parse_args()

    load_profile()
    config = load_config()

    logging.basicConfig(level=logging.INFO)
    try:
        submit_parent(args.parent_pk, config.webhook_url, dry_run=args.dry_run)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)