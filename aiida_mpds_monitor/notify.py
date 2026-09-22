"""Send an on-demand Telegram report for the current AiiDA profile."""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

import yaml
from aiida import load_profile

from .config import DEFAULT_CONFIG, DEFAULT_CONFIG_PATH, read_config
from .daemon import scan_notifications
from .notifications import create_notifier
from .running import RunningNotifications, resolve_allocated_servers

logger = logging.getLogger(__name__)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send current AiiDA calculations and statistics to the configured Telegram chat."
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help="YAML config path (default: ~/.aiida/aiida_mpds_monitor/conf.yaml).",
    )
    parser.add_argument(
        "--overdue-only", action="store_true",
        help="Include calculation details only above running_alert_hours, like scheduled reports.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        config = read_config(args.config) if args.config.exists() else DEFAULT_CONFIG.copy()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # Parser errors can quote the YAML line containing a bot token.
        print(f"Cannot read Telegram configuration ({type(exc).__name__})", file=sys.stderr)
        return 2

    notifier = create_notifier(config)
    if notifier is None:
        print(
            "Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment "
            "or YAML configuration.",
            file=sys.stderr,
        )
        return 2

    try:
        load_profile()
        running = RunningNotifications(
            notifier, config.get("running_alert_hours"),
            user_names=config.get("notification_user_names"),
            user_name=config.get("notification_user_name"),
        )
        running.begin_scan()
        scan_notifications(config, logger, running)
        details = running.overdue_report() if args.overdue_only else running.report()
        statistics = running.statistics_report(resolve_allocated_servers(logger))
        details_sent = notifier.send_message(details) if details else True
        statistics_sent = notifier.send_message(statistics)
        return 0 if details_sent and statistics_sent else 1
    except Exception as exc:
        # Profile, database, or provider exception text may contain credentials.
        print(f"Could not send calculation report ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
