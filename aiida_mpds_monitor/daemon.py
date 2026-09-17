import logging
import logging.handlers
import os
import sys
import time

from aiida import load_profile
from aiida.orm import ProcessNode, QueryBuilder, WorkChainNode

from .config import get_archive_key, get_auth_key, load_config, resolve_archive_upload_url
from .filters import (
    build_parent_query_filters,
    count_compound_elements,
    get_allowed_compounds,
    get_allowed_element_counts,
    get_element_filter,
    get_element_count_greater_than,
    get_time_bounds,
    matches_compound_filters,
)
from .generate_archive import generate_parent_archive
from .notifications import create_notifier
from .running import (
    EXTRA_RUNNING, RunningNotifications, resolve_allocated_servers, resolve_running_details,
)
from .scheduling import DailyReports
from .status import (
    base_has_ready_children,
    EXTRA_ARCHIVE_PROCESSED,
    EXTRA_INPROGRESS_SENT,
    EXTRA_PARENT_PROCESSED,
    STATUS_WAITING,
    get_node_status,
)
from .webhook import send_webhook, send_archive


def filter_nodes_by_element_count(
    nodes,
    allowed_counts,
    logger,
    greater_than=None,
    allowed_compounds=None,
    selected_elements=None,
    elements_match="any",
):
    """Keep nodes whose label formula satisfies all configured compound filters."""
    if (
        allowed_counts is None
        and greater_than is None
        and allowed_compounds is None
        and selected_elements is None
    ):
        return nodes

    filtered = []
    for node in nodes:
        label = node.label or ""
        element_count = count_compound_elements(label)
        if matches_compound_filters(
            label,
            allowed_counts,
            greater_than,
            allowed_compounds,
            selected_elements,
            elements_match,
        ):
            filtered.append(node)
            continue

        count_description = "unrecognized formula" if element_count is None else element_count
        logger.info(
            "Skipping %s %s due to compound filter: "
            "label=%r, count=%s, allowed=%s, greater_than=%s, "
            "compounds=%s, elements=%s, elements_match=%s",
            node.process_label,
            node.pk,
            label,
            count_description,
            sorted(allowed_counts) if allowed_counts is not None else "any",
            greater_than if greater_than is not None else "any",
            sorted(allowed_compounds) if allowed_compounds is not None else "any",
            sorted(selected_elements) if selected_elements is not None else "any",
            elements_match,
        )
    return filtered


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


def generate_and_upload_archive(parent_node, base_nodes, config, logger) -> bool:
    """Generate and upload one parent archive.

    A disabled archive upload is considered complete.  Otherwise ``True`` is
    returned only after the archive has been uploaded successfully.
    """
    if not config.get("send_archive", True):
        return True

    try:
        archive_path = generate_parent_archive(
            parent_node.uuid,
            base_nodes=base_nodes,
            require_all_subnodes=config.get("send_archives_all_stages_ready", False),
        )
    except Exception as exc:
        logger.exception(
            f"Error generating archive for parent {parent_node.pk}: {exc}"
        )
        return False

    if not archive_path:
        logger.warning(f"Failed to generate archive for parent {parent_node.pk}")
        return False

    logger.info(f"Generated archive for parent {parent_node.pk}: {archive_path}")
    try:
        upload_url = resolve_archive_upload_url(config, logger=logger)
        uploaded = send_archive(
            upload_url,
            archive_path,
            bid=config.get("archive_bid"),
            schema_id=config.get("archive_schema_id"),
            key=get_archive_key(config),
        )
    except Exception as exc:
        logger.exception(
            f"Error uploading archive for parent {parent_node.pk}: {exc}"
        )
        return False

    if not uploaded:
        logger.warning(
            f"Failed to upload archive for parent {parent_node.pk} to "
            f"{upload_url}; keeping {archive_path} for manual recovery"
        )
        return False

    logger.info(f"Uploaded archive for parent {parent_node.pk} to {upload_url}")
    if config.get("archive_keep", False):
        logger.info(f"Kept archive {archive_path} (archive_keep=true)")
        return True

    try:
        archive_path.unlink()
        logger.info(f"Deleted archive {archive_path} after successful upload")
    except OSError as exc:
        logger.warning(f"Could not delete archive {archive_path}: {exc}")
    return True


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

    ``force`` makes the parent eligible for a scan, but successful per-node
    delivery marks are still respected.  This lets ``--resend-all`` retry only
    deliveries that were not completed previously.
    """
    label = base_node.label
    if not label or not label.strip():
        logger.debug(f"Skipping {base_node.process_label} {base_node.pk} — empty label")
        return True

    label = label.strip()
    # Get grandchild types to check from hierarchy
    node_type = base_node.process_label
    grandchild_types = hierarchy.get(parent_label, {}).get(node_type, [])

    # Send webhook when state changes or when terminal state is reached
    already_finished = base_node.base.extras.get(EXTRA_PARENT_PROCESSED, False)

    inprogress_sent = base_node.base.extras.get(EXTRA_INPROGRESS_SENT, False)
    if not already_finished:
        status = get_node_status(base_node, child_types=grandchild_types, logger=logger)

        # still running
        if status == STATUS_WAITING:
            if not inprogress_sent:
                if send_webhook(webhook_url, label, status, key=webhook_key):
                    if not no_commit:
                        base_node.base.extras.set(EXTRA_INPROGRESS_SENT, True)
                    logger.info(f"In-progress webhook sent for '{label}'")
                else:
                    logger.warning(f"Failed to send in-progress webhook for '{label}'")
            return None

        # finished or excepted
        if send_webhook(webhook_url, label, status, key=webhook_key):
            if not no_commit:
                base_node.base.extras.set(EXTRA_PARENT_PROCESSED, True)
            logger.info(f"Webhook sent for '{label}' (status: {status})")
            return True
        else:
            logger.warning(f"Failed to send webhook for '{label}'")
            return False
    return True


def scan_and_process(config, logger, no_commit=False, force=False):
    webhook_url = config.webhook_url
    webhook_key = get_auth_key(config)
    # Get parent workchain types from hierarchy keys
    hierarchy = config.get("workchain_hierarchy", {})
    workchain_types = list(hierarchy.keys())
    allowed_element_counts = get_allowed_element_counts(config)
    element_count_greater_than = get_element_count_greater_than(config)
    allowed_compounds = get_allowed_compounds(config)
    selected_elements, elements_match = get_element_filter(config)

    # Request ALL parent nodes of the specified type that have not yet been processed.
    # Including those that failed!
    qb = QueryBuilder()
    qb.append(
        WorkChainNode,
        filters=build_parent_query_filters(config, workchain_types),
        tag="parent",
    )

    # We process only those that are not yet marked as processed
    if not force:
        qb.add_filter("parent", {"extras": {"!has_key": EXTRA_PARENT_PROCESSED}})

    for parent_node in qb.iterall():
        parent_node = parent_node[0]
        logger.debug(f"Processing parent workchain {parent_node.pk}")
        parent_is_broken = parent_node.is_failed or parent_node.is_excepted or parent_node.is_killed
        called_nodes = parent_node.called
        # Get child workchain types to search for from hierarchy
        parent_label = parent_node.process_label
        child_types = list(hierarchy.get(parent_label, {}).keys())
        base_candidates = [
            node
            for node in called_nodes
            if isinstance(node, WorkChainNode) and node.process_label in child_types
        ]
        base_nodes = filter_nodes_by_element_count(
            base_candidates,
            allowed_element_counts,
            logger,
            greater_than=element_count_greater_than,
            allowed_compounds=allowed_compounds,
            selected_elements=selected_elements,
            elements_match=elements_match,
        )
        archive_base_nodes = [
            base
            for base in base_nodes
            if base_has_ready_children(
                base,
                child_types=hierarchy.get(parent_label, {}).get(base.process_label, []),
                all_stages_ready=config.get("send_archives_all_stages_ready", False),
            )
        ]

        if parent_is_broken:
            if force or not parent_node.base.extras.get(EXTRA_PARENT_PROCESSED, False):
                if base_nodes:
                    all_webhooks_sent = True
                    # If parent is broken, send actual status for each base workchain
                    for base in base_nodes:
                        label = base.label
                        if label and label.strip():
                            if base.base.extras.get(EXTRA_PARENT_PROCESSED, False):
                                continue
                            # Get grandchild types to check from hierarchy
                            parent_type = base.process_label
                            grandchild_types = hierarchy.get(parent_label, {}).get(parent_type, [])
                            status = get_node_status(
                                base,
                                child_types=grandchild_types,
                                logger=logger,
                            )

                            if send_webhook(
                                webhook_url,
                                label.strip(),
                                status,
                                key=webhook_key,
                            ):
                                logger.warning(
                                    f"ERROR webhook sent for subtask '{label}' (status: {status}, parent {parent_node.pk} failed)"
                                )
                                if not no_commit:
                                    base.base.extras.set(EXTRA_PARENT_PROCESSED, True)
                            else:
                                all_webhooks_sent = False
                                logger.error(f"Failed to send ERROR webhook for '{label}'")
                    archive_uploaded = parent_node.base.extras.get(
                        EXTRA_ARCHIVE_PROCESSED, False
                    )
                    if all_webhooks_sent and not archive_uploaded:
                        archive_uploaded = generate_and_upload_archive(
                            parent_node,
                            base_nodes,
                            config,
                            logger,
                        )
                    if all_webhooks_sent and archive_uploaded and not no_commit:
                        parent_node.base.extras.set(EXTRA_ARCHIVE_PROCESSED, True)
                        parent_node.base.extras.set(EXTRA_PARENT_PROCESSED, True)
                elif not base_candidates:
                    # Parent failed before spawning any children — report using parent's own label
                    webhook_sent = parent_node.base.extras.get(
                        EXTRA_PARENT_PROCESSED, False
                    )
                    label = parent_node.label
                    if label and label.strip() and matches_compound_filters(
                        label,
                        allowed_element_counts,
                        element_count_greater_than,
                        allowed_compounds,
                        selected_elements,
                        elements_match,
                    ):
                        status = get_node_status(parent_node, child_types=[], logger=logger)
                        if send_webhook(webhook_url, label.strip(), status, key=webhook_key):
                            logger.warning(
                                f"ERROR webhook sent for parent '{label}' (status: {status}, no children spawned)"
                            )
                            webhook_sent = True
                        else:
                            logger.error(f"Failed to send ERROR webhook for parent '{label}' (no children)")
                    else:
                        logger.debug(
                            f"Parent {parent_node.pk} failed but its label is empty or excluded — skipping"
                        )
                    # No archive is applicable when the parent spawned no children.
                    if webhook_sent and not no_commit:
                        parent_node.base.extras.set(EXTRA_PARENT_PROCESSED, True)
                        parent_node.base.extras.set(EXTRA_ARCHIVE_PROCESSED, True)
                continue

        # Normal processing
        all_terminal = True
        any_failed = False
        for base_node in base_nodes:
            result = process_base_workchain(
                base_node,
                webhook_url,
                webhook_key,
                logger,
                hierarchy,
                parent_label,
                no_commit=no_commit,
                force=force,
            )
            if result is False:
                any_failed = True
                all_terminal = False
            elif result is None:
                all_terminal = False

        processing_complete = bool(base_nodes) and all_terminal and not any_failed
        archive_ready = bool(archive_base_nodes) and (
            config.get("send_archives_all_stages_ready", False)
            and len(archive_base_nodes) == len(base_nodes)
            or not config.get("send_archives_all_stages_ready", False)
        )
        archive_uploaded = parent_node.base.extras.get(
            EXTRA_ARCHIVE_PROCESSED, False
        )
        if processing_complete and archive_ready and not archive_uploaded:
            archive_uploaded = generate_and_upload_archive(
                parent_node,
                archive_base_nodes,
                config,
                logger,
            )

        if processing_complete and archive_uploaded:
            if not no_commit:
                parent_node.base.extras.set(EXTRA_ARCHIVE_PROCESSED, True)
                parent_node.base.extras.set(EXTRA_PARENT_PROCESSED, True)
                logger.info(f"Parent {parent_node.pk} marked as processed")
        else:
            logger.debug(
                f"Parent {parent_node.pk} remains pending: "
                f"all_terminal={all_terminal}, webhook_failed={any_failed}, "
                f"archive_uploaded={archive_uploaded}"
            )


# For dry-run testing
def scan_and_process_dry_run(config, logger, force=False):
    # Get parent workchain types from hierarchy keys
    hierarchy = config.get("workchain_hierarchy", {})
    workchain_types = list(hierarchy.keys())
    allowed_element_counts = get_allowed_element_counts(config)
    element_count_greater_than = get_element_count_greater_than(config)
    allowed_compounds = get_allowed_compounds(config)
    selected_elements, elements_match = get_element_filter(config)

    qb = QueryBuilder()
    qb.append(
        WorkChainNode,
        filters=build_parent_query_filters(config, workchain_types),
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

        base_candidates = [
            node
            for node in called_nodes
            if isinstance(node, WorkChainNode) and node.process_label in child_types
        ]
        base_nodes = filter_nodes_by_element_count(
            base_candidates,
            allowed_element_counts,
            logger,
            greater_than=element_count_greater_than,
            allowed_compounds=allowed_compounds,
            selected_elements=selected_elements,
            elements_match=elements_match,
        )

        if parent_is_broken:
            if base_nodes:
                for base in base_nodes:
                    label = base.label
                    if not label or not label.strip():
                        logger.info(f"Skipping {base.pk} — empty label")
                        continue
                    parent_type = base.process_label
                    grandchild_types = hierarchy.get(parent_label, {}).get(
                        parent_type, []
                    )
                    status = get_node_status(
                        base, child_types=grandchild_types, logger=logger
                    )
                    logger.info(
                        f"[TEST] Would send webhook for '{label}' "
                        f"(status: {status}, parent failed)"
                    )
            elif not base_candidates:
                label = parent_node.label
                if label and label.strip() and matches_compound_filters(
                    label,
                    allowed_element_counts,
                    element_count_greater_than,
                    allowed_compounds,
                    selected_elements,
                    elements_match,
                ):
                    status = get_node_status(parent_node, child_types=[], logger=logger)
                    logger.info(
                        f"[TEST] Would send webhook for '{label}' "
                        f"(status: {status}, parent failed with no children)"
                    )
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


def scan_notifications(config, logger, running: RunningNotifications) -> None:
    """Find configured process types directly, regardless of their call-link depth."""
    hierarchy = config.get("workchain_hierarchy", {})
    labels = set(hierarchy)
    for children in hierarchy.values():
        labels.update(children)
        for calculations in children.values():
            labels.update(calculations)
    qb = QueryBuilder()
    qb.append(ProcessNode, filters={"and": [
        {"attributes.process_label": {"in": sorted(labels)}},
        {"or": [
            {"attributes.process_state": "running"},
            {"and": [
                {"attributes.process_state": {"in": ["created", "waiting", "running"]}},
                {"attributes.scheduler_state": {"in": ["running", "RUNNING"]}},
            ]},
            # Revisit tracked nodes to reset intervals when they leave RUNNING.
            {"extras": {"has_key": EXTRA_RUNNING}},
        ]},
    ]})
    nodes = [node for (node,) in qb.iterall()]
    running_details = resolve_running_details(nodes, logger)
    for node in nodes:
        details = running_details.get(node.uuid)
        running.observe(
            node,
            running_since=details.running_since if details else None,
            hostname=details.hostname if details else None,
        )
    running.finish_scan()


def run_monitor_loop(config, logger, dry_run=False, no_commit=False, force=False):
    """Run monitor scans continuously.

    ``force`` applies only to the first completed scan.  This makes
    ``--resend-all`` a one-shot replay instead of resending the same webhooks
    after every poll interval.
    """
    notifier = None if dry_run else create_notifier(config)
    running = (RunningNotifications(
        notifier, config.get("running_alert_hours"), no_commit,
        user_names=config.get("notification_user_names"),
        user_name=config.get("notification_user_name"),
    ) if notifier else None)
    reports = (DailyReports(
        notifier, config.get("notification_time", "09:00"),
        config.get("notification_timezone", "UTC"),
    ) if notifier else None)
    if running is not None and running.hours is None:
        logger.warning("Scheduled reports disabled: set a positive running_alert_hours limit")
    while True:
        try:
            if dry_run:
                # In test mode, we emulate the behavior without sending
                scan_and_process_dry_run(config, logger, force=force)
            else:
                if running is not None:
                    try:
                        running.begin_scan()
                        scan_notifications(config, logger, running)
                        reports.notify_if_due(
                            running.overdue_report(),
                            summary=lambda: running.statistics_report(
                                allocated_servers=resolve_allocated_servers(logger)
                            ),
                        )
                    except Exception:
                        logger.warning("Notification scan failed; continuing MPDS monitoring")
                scan_and_process(config, logger, no_commit=no_commit, force=force)

            if force:
                force = False
                logger.info(
                    "Forced resend scan completed; continuing in normal monitor mode"
                )
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
            break
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
        time.sleep(config.poll_interval)


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
        help=(
            "Ignore existing extras/markers during the first scan and resend "
            "webhooks for every eligible workchain."
        ),
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
    try:
        created_after, created_before = get_time_bounds(config)
        allowed_element_counts = get_allowed_element_counts(config)
        element_count_greater_than = get_element_count_greater_than(config)
        allowed_compounds = get_allowed_compounds(config)
        selected_elements, elements_match = get_element_filter(config)
    except ValueError as exc:
        parser.error(f"Invalid monitor_filters configuration: {exc}")
    logger.info(
        "Monitor filters: created_after=%s, created_before=%s, "
        "element_counts=%s, element_count_greater_than=%s, compounds=%s, "
        "elements=%s, elements_match=%s",
        created_after or "any",
        created_before or "any",
        sorted(allowed_element_counts) if allowed_element_counts else "any",
        element_count_greater_than
        if element_count_greater_than is not None
        else "any",
        sorted(allowed_compounds) if allowed_compounds is not None else "any",
        sorted(selected_elements) if selected_elements is not None else "any",
        elements_match,
    )

    run_monitor_loop(
        config,
        logger,
        dry_run=dry_run,
        no_commit=no_commit,
        force=force,
    )
