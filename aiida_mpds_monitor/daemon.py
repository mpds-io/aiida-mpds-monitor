import logging
import logging.handlers
import os
import sys
import time

from aiida import load_profile
from aiida.orm import QueryBuilder, WorkChainNode

from .config import get_auth_key, load_config
from .status import (
    EXTRA_PARENT_PROCESSED,
    get_node_status,
)
from .webhook import send_webhook


def setup_logger(config):
    logger = logging.getLogger("aiida_mpds_monitor")
    logger.setLevel(getattr(logging, config.log_level.upper()))
    # Clear existing handlers
    logger.handlers.clear()
    # File handler with rotation
    log_dir = os.path.dirname(config.log_file)
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        config.log_file,
        maxBytes=config.get("log_max_bytes", 10 * 1024 * 1024),
        backupCount=config.get("log_backup_count", 3),
    )
    file_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    # Console handler (optional, can be removed in production)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(file_formatter)
    logger.addHandler(console_handler)
    return logger


def process_base_workchain(
    base_node,
    webhook_url,
    webhook_key,
    logger,
    hierarchy,
    parent_label,
    no_commit=False,
    force=False,
):
    """Handle one base workchain node.

    When ``force`` is True we ignore any ``EXTRA_PARENT_PROCESSED`` flag and always
    attempt to send a webhook.  This is used by the ``--resend-all`` CLI
    option to re‑deliver hooks regardless of previous marks.
    """
    label = base_node.label
    if not label or not label.strip():
        logger.debug(
            f"Skipping {base_node.process_label} {base_node.pk} — empty label"
        )
        return
    
    label = label.strip()
    # Get grandchild types to check from hierarchy
    node_type = base_node.process_label
    grandchild_types = hierarchy.get(parent_label, {}).get(node_type, [])
    
    # Send webhook when state changes or when terminal state is reached
    already_finished = base_node.base.extras.get(EXTRA_PARENT_PROCESSED, False)

    if force:
        already_finished = False

    if not already_finished:
        status = get_node_status(
            base_node, child_types=grandchild_types, logger=logger
        )

        if send_webhook(webhook_url, label, status, key=webhook_key):
            if not no_commit:
                base_node.set_extra(EXTRA_PARENT_PROCESSED, True)
            logger.info(f"Webhook sent for '{label}' (status: {status})")
        else:
            logger.warning(f"Failed to send webhook for '{label}'")


def scan_and_process(config, logger, no_commit=False, force=False):
    webhook_url = config.webhook_url
    # Get parent workchain types from hierarchy keys
    hierarchy = config.get("workchain_hierarchy", {})
    workchain_types = list(hierarchy.keys())
    
    # Request ALL parent nodes of the specified type that have not yet been processed.
    # Including those that failed!
    qb = QueryBuilder()
    qb.append(
        WorkChainNode,
        filters={"attributes.process_label": {"in": workchain_types}},
        tag="parent",
    )
    
    # We process only those that are not yet marked as processed
    if not force:
        qb.add_filter("parent", {"extras": {"!has_key": EXTRA_PARENT_PROCESSED}})
    
    for parent_node in qb.iterall():
        parent_node = parent_node[0]
        logger.debug(f"Processing parent workchain {parent_node.pk}")
        parent_is_broken = (
            parent_node.is_failed
            or parent_node.is_excepted
            or parent_node.is_killed
        )
        called_nodes = parent_node.called
        # Get child workchain types to search for from hierarchy
        parent_label = parent_node.process_label
        child_types = list(hierarchy.get(parent_label, {}).keys())
        base_nodes = [
            n
            for n in called_nodes
            if isinstance(n, WorkChainNode) and n.process_label in child_types
        ]
        
        if parent_is_broken:
            if force or not parent_node.base.extras.get(EXTRA_PARENT_PROCESSED, False):
                if base_nodes:
                    # If parent is broken, send actual status for each base workchain
                    for base in base_nodes:
                        label = base.label
                        if label and label.strip():
                            # Get grandchild types to check from hierarchy
                            parent_type = base.process_label
                            grandchild_types = hierarchy.get(
                                parent_label, {}
                            ).get(parent_type, [])
                            status = get_node_status(
                                base,
                                child_types=grandchild_types,
                                logger=logger,
                            )

                            if send_webhook(
                                webhook_url,
                                label.strip(),
                                status,
                                key=get_auth_key(),
                            ):
                                logger.warning(
                                    f"ERROR webhook sent for subtask '{label}' (status: {status}, parent {parent_node.pk} failed)"
                                )
                                if not no_commit:
                                    parent_node.set_extra(EXTRA_PARENT_PROCESSED, True)
                            else:
                                logger.error(
                                    f"Failed to send ERROR webhook for '{label}'"
                                )
                    # else: skip empty label
                else:
                    logger.debug(
                        f"Parent {parent_node.pk} failed but has no children — nothing to report"
                    )
                # Mark the parent as processed (if allowed)
                if not no_commit:
                    parent_node.set_extra(EXTRA_PARENT_PROCESSED, True)
                continue
        
        # Normal processing
        for base_node in base_nodes:
            process_base_workchain(
                base_node,
                webhook_url,
                get_auth_key(),
                logger,
                hierarchy,
                parent_label,
                no_commit=no_commit,
            )
        
        if not no_commit:
            parent_node.set_extra(EXTRA_PARENT_PROCESSED, True)
        logger.info(f"Parent {parent_node.pk} marked as processed")


# For dry-run testing
def scan_and_process_dry_run(config, logger, force=False):
    # Get parent workchain types from hierarchy keys
    hierarchy = config.get("workchain_hierarchy", {})
    workchain_types = list(hierarchy.keys())

    qb = QueryBuilder()
    qb.append(
        WorkChainNode,
        filters={"attributes.process_label": {"in": workchain_types}},
        tag="parent",
    )
    if not force:
        qb.add_filter("parent", {"extras": {"!has_key": EXTRA_PARENT_PROCESSED}})

    for parent_node in qb.iterall():
        parent_node = parent_node[0]
        logger.debug(f"[TEST] Would process parent {parent_node.pk}")

        parent_is_broken = parent_node.is_failed or parent_node.is_excepted or parent_node.is_killed

        called_nodes = parent_node.called
        # Get child workchain types to search for from hierarchy
        parent_label = parent_node.process_label
        child_types = list(hierarchy.get(parent_label, {}).keys())

        base_nodes = [n for n in called_nodes if isinstance(n, WorkChainNode) and n.process_label in child_types]

        if parent_is_broken:
            if base_nodes:
                for base in base_nodes:
                    label = base.label
                if not label or not label.strip():
                    logger.info(f"Skipping {base.pk} — empty label")
                    continue
                    # Get grandchild types to check from hierarchy
                    parent_type = base.process_label
                    grandchild_types = hierarchy.get(parent_label, {}).get(parent_type, [])
                    status = get_node_status(base, child_types=grandchild_types, logger=logger)
                    logger.info(f"[TEST] Would send webhook for '{label}' (status: {status}, parent failed)")
            logger.info(f"[TEST] Would mark parent {parent_node.pk} as processed")
            continue

        for base_node in base_nodes:
            label = base_node.label
            if not label or not label.strip():
                continue
            label = label.strip()
            # Get grandchild types to check from hierarchy
            parent_type = base_node.process_label
            grandchild_types = hierarchy.get(parent_label, {}).get(parent_type, [])
            status = get_node_status(base_node, child_types=grandchild_types, logger=logger)
            logger.info(f"[TEST] Would send webhook for '{label}' (status: {status})")

        logger.info(f"[TEST] Would mark parent {parent_node.pk} as processed")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="AiiDA MPDS Monitor Daemon")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run: show what would be done, but DO NOT send webhooks or set marks.",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Send webhooks, but DO NOT set any extras on nodes (useful for recovery or one-off runs).",
    )
    parser.add_argument(
        "--resend-all",
        action="store_true",
        help="Ignore existing extras/markers and resend webhooks for every eligible workchain.",
    )
    parser.add_argument(
        "--logging-level",
        "-l",
        dest="logging_level",
        help="Logging level (DEBUG, INFO, WARNING, ERROR). Defaults to ERROR if not provided.",
        default="ERROR",
    )
    args = parser.parse_args()

    # In --test mode, no marks are set and webhooks are not sent.
    # In --no-commit mode, webhooks are sent, but no extras are set on nodes.
    dry_run = args.dry_run
    no_commit = args.no_commit
    force = args.resend_all

    if dry_run and no_commit:
        print("--dry-run and --no-commit are mutually exclusive. Using --test.")
        no_commit = False

    load_profile()
    config = load_config()
    # Use CLI logging level explicitly, default to ERROR if omitted
    level_map = {
        "DEBUG": "DEBUG",
        "INFO": "INFO",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "CRITICAL": "CRITICAL",
    }
    level_name = (args.logging_level or "ERROR").upper()
    config.log_level = level_map.get(level_name, "ERROR")
    logger = setup_logger(config)

    if dry_run:
        mode = "TEST (dry-run, no webhooks, no marks)"
    elif no_commit:
        mode = "NO-COMMIT (webhooks sent, no extras set)"
    else:
        mode = "NORMAL"

    if force:
        mode += " [FORCE]"

    logger.info(f"Starting AiiDA MPDS Monitor daemon [{mode}]")
    logger.info(f"Webhook URL: {config.webhook_url}")
    logger.info(f"Poll interval: {config.poll_interval}s")
    logger.info(f"Log file: {config.log_file}")
    hierarchy = config.get("workchain_hierarchy", {})
    logger.info(f"Monitoring workchains: {list(hierarchy.keys())}")

    while True:
        try:
            if dry_run:
                # In test mode, we emulate the behavior without sending
                scan_and_process_dry_run(config, logger, force=force)
            else:
                scan_and_process(config, logger, no_commit=no_commit, force=force)
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
            break
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
        time.sleep(config.poll_interval)
