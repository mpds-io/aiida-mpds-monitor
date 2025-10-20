# aiida_mpds_monitor/daemon.py
import os
import sys
import logging
import logging.handlers
import time
from aiida import load_profile
from aiida.orm import QueryBuilder, WorkChainNode
from .config import load_config

BASE_CRYSTAL_TYPE = "BaseCrystalWorkChain"
EXTRA_STARTED = "webhook_started"
EXTRA_FINISHED = "webhook_finished"
EXTRA_PARENT_PROCESSED = "webhook_parent_processed"
EXTRA_PARENT_ERROR_SENT = "webhook_parent_error_sent"


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
        backupCount=config.get("log_backup_count", 3)
    )
    file_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Console handler (optional, можно убрать в продакшене)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(file_formatter)
    logger.addHandler(console_handler)

    return logger


def send_webhook(webhook_url, payload, status):
    import requests
    params = {"payload": payload, "status": status}
    try:
        response = requests.get(webhook_url, params=params, timeout=10)
        return response.status_code == 200
    except Exception as _:
        return False


def get_node_status(node):
    state = node.process_state.value
    if state.lower() == "finished":
        code = node.exit_code.status if node.exit_code else 0
        return f"{state}-{code}"
    return state


def process_base_workchain(base_node, webhook_url, logger, dry_run=False):
    label = base_node.label
    if not label or not label.strip():
        logger.debug(f"⏭Skipping BaseCrystalWorkChain {base_node.pk} — empty label")
        return

    label = label.strip()

    # === START ===
    already_started = base_node.base.extras.get(EXTRA_STARTED, False)
    if not already_started:
        if base_node.process_state.value != "created":
            if dry_run:
                logger.info(f"[TEST] Would send START webhook for '{label}' ({base_node.pk})")
            else:
                if send_webhook(webhook_url, label, "started"):
                    base_node.set_extra(EXTRA_STARTED, True)
                    logger.info(f"START webhook sent for '{label}' ({base_node.pk})")
                else:
                    logger.warning(f"Failed to send START webhook for '{label}'")

    # === FINISH ===
    already_finished = base_node.base.extras.get(EXTRA_FINISHED, False)
    if not already_finished:
        if base_node.is_finished:
            status = get_node_status(base_node)
            if dry_run:
                logger.info(f"[TEST] Would send FINISH webhook for '{label}' ({status})")
            else:
                if send_webhook(webhook_url, label, status):
                    base_node.set_extra(EXTRA_FINISHED, True)
                    logger.info(f"FINISH webhook sent for '{label}' ({status})")
                else:
                    logger.warning(f"Failed to send FINISH webhook for '{label}'")


def scan_and_process(config, logger, dry_run=False):
    webhook_url = config.webhook_url

    qb = QueryBuilder()
    qb.append(
        WorkChainNode,
        filters={"attributes.process_label": {"in": config.workchain_types}},
        tag="parent"
    )
    if not dry_run:
        qb.add_filter("parent", {"extras": {"!has_key": EXTRA_PARENT_PROCESSED}})

    for parent_node in qb.iterall():
        parent_node = parent_node[0]
        logger.debug(f"Processing parent workchain {parent_node.pk}")

        # Check if parent is in a failed state
        parent_is_broken = (
            parent_node.is_failed or
            parent_node.is_excepted or
            parent_node.is_killed
        )

        called_nodes = parent_node.called
        base_nodes = [
            n for n in called_nodes
            if isinstance(n, WorkChainNode) and n.process_label == BASE_CRYSTAL_TYPE
        ]

        # If parent is broken, send error for the LAST BaseCrystalWorkChain (if any)
        if parent_is_broken:
            if not dry_run and not parent_node.get_extra(EXTRA_PARENT_ERROR_SENT, False):
                if base_nodes:
                    # Sort by PK (or ctime) to get the most recent
                    for base in base_nodes:
                        label = base.label
                        if label and label.strip():
                            status = "finished-500"
                            if send_webhook(webhook_url, label.strip(), status):
                                logger.warning(f"ERROR webhook sent for last subtask '{label}' (parent {parent_node.pk} failed)")
                                parent_node.set_extra(EXTRA_PARENT_ERROR_SENT, True)
                            else:
                                logger.error(f"Failed to send ERROR webhook for '{label}'")
                        else:
                            logger.debug(f"Parent {parent_node.pk} failed, but last BaseCrystalWorkChain has no label — skipping webhook")
                            parent_node.set_extra(EXTRA_PARENT_ERROR_SENT, True)  # still mark to avoid retry
                else:
                    logger.debug(f"Parent {parent_node.pk} failed but launched no BaseCrystalWorkChain — nothing to report")
                    parent_node.set_extra(EXTRA_PARENT_ERROR_SENT, True)

            # Mark parent as processed regardless
            if not dry_run:
                parent_node.set_extra(EXTRA_PARENT_PROCESSED, True)
            continue

        # Normal case: parent is OK
        for base_node in base_nodes:
            process_base_workchain(base_node, webhook_url, logger, dry_run=dry_run)

        if not dry_run:
            parent_node.set_extra(EXTRA_PARENT_PROCESSED, True)
            logger.info(f"Parent {parent_node.pk} marked as processed")
        else:
            logger.info(f"[TEST] Would mark parent {parent_node.pk} as processed")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AiiDA MPDS Monitor Daemon")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run in test mode: scan and log actions, but DO NOT send webhooks or mark nodes."
    )
    args = parser.parse_args()

    load_profile()
    config = load_config()
    logger = setup_logger(config)

    mode = "TEST (dry-run)" if args.test else "NORMAL"
    logger.info(f"Starting AiiDA MPDS Monitor daemon [{mode}]")
    logger.info(f"Webhook URL: {config.webhook_url}")
    logger.info(f"Poll interval: {config.poll_interval}s")
    logger.info(f"Log file: {config.log_file}")
    logger.info(f"Monitoring workchains: {config.workchain_types}")

    while True:
        try:
            scan_and_process(config, logger, dry_run=args.test)
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
            break
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
        time.sleep(config.poll_interval)