import argparse
import logging
import sys

from aiida import load_profile
from aiida.orm import WorkChainNode, load_node

from .config import get_archive_key, get_auth_key, load_config, resolve_archive_upload_url
from .generate_archive import generate_parent_archive
from .status import (
    base_has_ready_children,
    get_node_status,
)
from .webhook import send_webhook, send_archive


def submit_parent(
    parent_pk: int,
    webhook_url: str,
    webhook_key: str = "",
    dry_run: bool = False,
    config=None,
):
    parent_node = load_node(parent_pk)

    if not isinstance(parent_node, WorkChainNode):
        raise ValueError(f"Node {parent_pk} is not a WorkChain")

    # Get hierarchy
    hierarchy = config.get("workchain_hierarchy", {}) if config else {}

    # Get child workchain types to search for from hierarchy
    parent_label = parent_node.process_label
    child_types = list(hierarchy.get(parent_label, {}).keys())

    called_nodes = parent_node.called
    base_nodes = [
        n
        for n in called_nodes
        if isinstance(n, WorkChainNode) and n.process_label in child_types
    ]

    # Check if parent is in a failed state
    parent_is_broken = (
        parent_node.is_failed
        or parent_node.is_excepted
        or parent_node.is_killed
    )

    if parent_is_broken:
        if base_nodes:
            for base in base_nodes:
                label = base.label
                if label and label.strip():
                    # Get grandchild types to check from hierarchy
                    node_type = base.process_label
                    grandchild_types = hierarchy.get(parent_label, {}).get(
                        node_type, []
                    )
                    status = get_node_status(
                        base, child_types=grandchild_types
                    )
                    payload = label.strip()
                    if dry_run:
                        print(
                            f"[DRY-RUN] Parent {parent_pk} failed — would send: payload='{payload}', status='{status}'"
                        )
                    else:
                        if send_webhook(
                            webhook_url, payload, status, key=webhook_key
                        ):
                            print(
                                f"Sent ERROR webhook for last subtask '{payload}' ({status})"
                            )
                        else:
                            print(
                                f"Failed to send webhook for '{payload}'",
                                file=sys.stderr,
                            )
                else:
                    print(
                        f"Parent {parent_pk} failed, but child workchain has no label — skipping"
                    )
        else:
            # Parent failed before spawning any children — report using parent's own label
            label = parent_node.label
            if label and label.strip():
                status = get_node_status(parent_node, child_types=[])
                payload = label.strip()
                if dry_run:
                    print(f"[DRY-RUN] Parent {parent_pk} failed with no children — would send: payload='{payload}', status='{status}'")
                else:
                    if send_webhook(webhook_url, payload, status, key=webhook_key):
                        print(f"Sent ERROR webhook for parent '{payload}' ({status}, no children spawned)")
                    else:
                        print(f"Failed to send webhook for parent '{payload}'", file=sys.stderr)
            else:
                print(f"Parent {parent_pk} failed but has no children and no label — skipping")
        return

    # Normal case: parent is OK — process all subnodes
    if not base_nodes:
        print(f"No child workchains found under parent {parent_pk}")
        return

    for base_node in base_nodes:
        label = base_node.label
        if not label or not label.strip():
            print(f"Skipping {base_node.pk} — empty label")
            continue

        label = label.strip()
        # Get grandchild types to check from hierarchy
        node_type = base_node.process_label
        grandchild_types = hierarchy.get(parent_label, {}).get(node_type, [])
        status = get_node_status(base_node, child_types=grandchild_types)

        if dry_run:
            print(
                f"[DRY-RUN] Would send: payload='{label}', status='{status}'"
            )
        else:
            if send_webhook(webhook_url, label, status, key=webhook_key):
                print(f"Sent webhook for '{label}' ({status})")
            else:
                print(f"Failed to send webhook for '{label}'", file=sys.stderr)

    # Create parent archive with all child results
    if not dry_run and config and config.get("send_archive", True):
        archive_base_nodes = [
            base
            for base in base_nodes
            if base_has_ready_children(
                base,
                child_types=hierarchy.get(parent_label, {}).get(base.process_label, []),
                all_stages_ready=config.get("send_archives_all_stages_ready", False),
            )
        ]
        all_stages_ready = config.get("send_archives_all_stages_ready", False)
        archive_ready = bool(archive_base_nodes) and (
            all_stages_ready and len(archive_base_nodes) == len(base_nodes)
            or not all_stages_ready
        )
        if not archive_ready:
            return
        print(f"Generating parent archive for {parent_pk}...")
        archive_path = generate_parent_archive(
            parent_node.uuid,
            base_nodes=archive_base_nodes,
            require_all_subnodes=all_stages_ready,
        )
        if archive_path:
            print(f"Parent archive successfully created: {archive_path}")
            try:
                upload_url = resolve_archive_upload_url(config, logger=logging.getLogger(__name__))
                bid = config.get("archive_bid", None)
                schema_id = config.get("archive_schema_id", None)
                if send_archive(upload_url, archive_path, bid=bid, schema_id=schema_id, key=get_archive_key(config)):
                    print(f"Archive uploaded successfully to {upload_url}")
                    if not config.get("archive_keep", False):
                        try:
                            archive_path.unlink()
                            print(f"Deleted archive {archive_path} after successful upload")
                        except OSError as exc:
                            print(f"Warning: could not delete archive {archive_path}: {exc}", file=sys.stderr)
                    else:
                        print(f"Kept archive {archive_path} (archive_keep=true)")
                else:
                    print(f"Failed to upload archive to {upload_url}; keeping {archive_path} for manual recovery", file=sys.stderr)
            except Exception as e:
                print(f"Error uploading archive: {e}", file=sys.stderr)
        else:
            print(f"Failed to create parent archive for {parent_pk}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Submit results of a parent workchain to webhook",
        usage="aiida-mpds-submit PARENT_PK [--dry-run]",
    )
    parser.add_argument(
        "parent_pk", type=int, help="PK of the parent WorkChain"
    )
    parser.add_argument(
        "--logging-level",
        "-l",
        dest="logging_level",
        help="Logging level (DEBUG, INFO, WARNING, ERROR). Defaults to ERROR if not provided.",
        default="ERROR",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Only show what would be sent"
    )
    args = parser.parse_args()

    load_profile()
    config = load_config()

    # Determine logging level from CLI only; default to ERROR
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    level = level_map.get(args.logging_level.upper(), logging.ERROR)

    logging.basicConfig(level=level)
    webhook_key = get_auth_key(config)
    if not webhook_key:
        logging.warning(
            "Cannot find authentication key in MPDS_MONITOR_KEY or config auth_key."
        )

    try:
        submit_parent(
            args.parent_pk,
            config.webhook_url,
            webhook_key=webhook_key,
            dry_run=args.dry_run,
            config=config,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
