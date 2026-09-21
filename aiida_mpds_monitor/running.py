"""Track observed RUNNING intervals without relying on node creation/modification time."""

import json
import logging
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from aiida.orm import ProcessNode

from .notifications import Notifier

logger = logging.getLogger(__name__)
EXTRA_RUNNING = "monitor_running_interval"


@dataclass(frozen=True)
class RunningDetails:
    """Scheduler details available for a currently executing process."""

    running_since: Optional[datetime] = None
    hostname: Optional[str] = None


def _aware(value: datetime) -> datetime:
    """Return an aware datetime; AiiDA timestamps are UTC when timezone is absent."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _yastatus_executable() -> str:
    """Find yastatus beside Python or on the service PATH."""
    name = "yastatus"
    sibling = Path(sys.executable).with_name(name)
    if sibling.is_file():
        return str(sibling)
    return shutil.which(name) or name


def _yascheduler_db_config() -> Any:
    """Load connection settings through the installed yascheduler configuration API."""
    try:
        config_module = import_module("yascheduler.config")
        paths = import_module("yascheduler.variables")
    except ModuleNotFoundError as exc:
        if exc.name not in ("yascheduler.config", "yascheduler.variables"):
            raise
        parser = import_module("yascheduler.entrypoints.config_parser")
        paths = import_module("yascheduler.entrypoints.paths")
        return parser.parse_config(paths.CONFIG_FILE).db
    return config_module.Config.from_config_parser(paths.CONFIG_FILE).db


def resolve_allocated_servers(logger_: logging.Logger = logger) -> Optional[int]:
    """Count enabled servers directly in the configured yascheduler database."""
    connection = None
    cursor = None
    stage = "loading configuration"
    try:
        db = _yascheduler_db_config()
        pg8000 = import_module("pg8000")
        stage = "connecting to database"
        connection = pg8000.connect(
            user=db.user,
            password=db.password,
            database=db.database,
            host=db.host,
            port=db.port,
            timeout=15,
        )
        stage = "querying enabled servers"
        cursor = connection.cursor()
        cursor.execute("SET statement_timeout = 15000")
        cursor.execute("SELECT COUNT(*) FROM yascheduler_nodes WHERE enabled=TRUE;")
        row = cursor.fetchone()
        if not row or type(row[0]) is not int or row[0] < 0:
            raise ValueError("Invalid server count")
        return row[0]
    except Exception as exc:
        # Connection/configuration errors may contain credentials; log only the type.
        logger_.error(
            "Could not count yascheduler servers while %s (%s)", stage, type(exc).__name__
        )
        return None
    finally:
        for resource in (cursor, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    logger_.error(
                        "Could not close yascheduler database resource (%s)", type(exc).__name__
                    )


def resolve_running_details(
    nodes: Iterable[ProcessNode], logger_: logging.Logger = logger
) -> dict[str, RunningDetails]:
    """Resolve scheduler start times and hostnames when the plugin exposes them."""
    result: dict[str, RunningDetails] = {}
    yascheduler_nodes = {}
    for node in nodes:
        try:
            info_getter = getattr(node, "get_last_job_info", None)
            info = info_getter() if callable(info_getter) else None
            dispatch_time = getattr(info, "dispatch_time", None)
            if dispatch_time is not None:
                result[node.uuid] = RunningDetails(running_since=_aware(dispatch_time))

            computer = getattr(node, "computer", None)
            if getattr(computer, "scheduler_type", None) == "yascheduler":
                job_id = node.base.attributes.get("job_id", None)
                if job_id is not None:
                    yascheduler_nodes[str(job_id)] = node
        except Exception:
            logger_.warning("Could not inspect scheduler timestamps for PK %s", node.pk)

    if not yascheduler_nodes:
        return result

    try:
        completed = subprocess.run(
            [_yastatus_executable(), "--jobs", *yascheduler_nodes, "--json"],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
        if completed.returncode != 0:
            logger_.warning(
                "yascheduler timestamp query failed with exit code %s",
                completed.returncode,
            )
            return result
        for task in json.loads(completed.stdout):
            if task.get("status") != "RUNNING":
                continue
            node = yascheduler_nodes.get(str(task.get("task_id")))
            updated_at = task.get("updated_at")
            if node is None:
                continue
            previous = result.get(node.uuid, RunningDetails())
            running_since = previous.running_since
            if updated_at:
                running_since = _aware(datetime.fromisoformat(updated_at))
            task_node = task.get("node")
            hostname = task_node.get("hostname") if isinstance(task_node, dict) else None
            hostname = hostname.strip() if isinstance(hostname, str) else None
            result[node.uuid] = RunningDetails(
                running_since=running_since,
                hostname=hostname or None,
            )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        logger_.warning(
            "Could not resolve yascheduler RUNNING timestamps (%s)",
            type(exc).__name__,
        )
    return result


def resolve_running_since(
    nodes: Iterable[ProcessNode], logger_: logging.Logger = logger
) -> dict[str, datetime]:
    """Return scheduler start times for compatibility with existing callers."""
    return {
        uuid: details.running_since
        for uuid, details in resolve_running_details(nodes, logger_).items()
        if details.running_since is not None
    }


def running_source(node: ProcessNode) -> Optional[str]:
    """Recognize an executing AiiDA process or scheduler calculation."""
    state = getattr(node.process_state, "value", node.process_state)
    if state not in ("created", "waiting", "running"):
        return None
    get_scheduler_state = getattr(node, "get_scheduler_state", None)
    scheduler = get_scheduler_state() if callable(get_scheduler_state) else None
    scheduler = getattr(scheduler, "value", scheduler)
    if isinstance(scheduler, str) and scheduler.lower() == "running":
        return "scheduler"
    return "process" if state == "running" else None


class RunningNotifications:
    def __init__(self, notifier: Notifier, hours: Optional[float] = None,
                 no_commit: bool = False,
                 user_names: Optional[Mapping[str, str]] = None,
                 user_name: Optional[str] = None) -> None:
        self.notifier = notifier
        self.configure(hours, user_names, user_name)
        self.no_commit = no_commit
        self._intervals: dict[str, dict] = {}
        self._current: dict[str, tuple[bool, str]] = {}
        self._next_current: Optional[dict[str, tuple[bool, str]]] = None

    def configure(
        self, hours: Optional[float] = None,
        user_names: Optional[Mapping[str, str]] = None, user_name: Optional[str] = None,
    ) -> None:
        """Update reporting settings while retaining observed running intervals."""
        self.user_names = user_names if isinstance(user_names, Mapping) else {}
        self.user_name = user_name.strip() if isinstance(user_name, str) else ""
        # The singular setting is a default Telegram username for this monitor.
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", self.user_name):
            self.user_name = f"@{self.user_name}"
        self.hours = None
        if hours is not None:
            try:
                value = float(hours)
                if isinstance(hours, bool) or not math.isfinite(value) or value <= 0:
                    raise ValueError
                self.hours = value
            except (TypeError, ValueError):
                logger.warning("running_alert_hours must be positive; RUNNING threshold disabled")

    def begin_scan(self) -> None:
        self._next_current = {}

    def finish_scan(self) -> None:
        if self._next_current is None:
            return
        if self.no_commit:
            self._intervals = {key: value for key, value in self._intervals.items()
                               if key in self._next_current}
        self._current = self._next_current
        self._next_current = None

    def _record(self, uuid: str, message: str, overdue: bool = False) -> None:
        target = self._next_current if self._next_current is not None else self._current
        target[uuid] = (overdue, message)

    def observe(
        self,
        node: ProcessNode,
        now: Optional[datetime] = None,
        running_since: Optional[datetime] = None,
        hostname: Optional[str] = None,
    ) -> None:
        source = running_source(node)
        try:
            now = now or datetime.now(timezone.utc)
            now = _aware(now)
            running_since = _aware(running_since) if running_since is not None else None
            interval = (self._intervals.get(node.uuid) if self.no_commit else
                        node.base.extras.get(EXTRA_RUNNING, None))
            if source is None:
                if self.no_commit:
                    self._intervals.pop(node.uuid, None)
                elif interval is not None:
                    node.base.extras.delete(EXTRA_RUNNING)
                if self._next_current is None:
                    self._current.pop(node.uuid, None)
                return
            if not interval or interval.get("source", "process") != source:
                since = running_since or now
                interval = {"since": since.isoformat(), "source": source}
                self._save(node, interval)
            else:
                since = _aware(datetime.fromisoformat(interval["since"]))
                if running_since is not None and running_since != since:
                    since = running_since
                    interval = {"since": since.isoformat(), "source": source}
                    self._save(node, interval)
            seconds = max(0, (now - since).total_seconds())
            message = self._describe(node, seconds, source, hostname)
            overdue = self.hours is not None and seconds > self.hours * 3600
            if overdue:
                message = (
                    "🚨 LONG-RUNNING CALCULATION 🚨\n"
                    f"Configured limit exceeded: {self.hours:g} h\n\n"
                    f"{message}"
                )
            self._record(node.uuid, message, overdue)
        except Exception:
            logger.warning("Could not track RUNNING interval for PK %s", getattr(node, "pk", None))
            if source is not None:
                lines = [
                    f"Name: {(node.label or '').strip() or '(label not set)'}",
                    f"PK: {node.pk}",
                ]
                if hostname:
                    lines.append(f"Hostname: {hostname}")
                lines.append("Running time: unavailable")
                self._record(node.uuid, "\n".join(lines))

    def _save(self, node: ProcessNode, interval: dict) -> None:
        if self.no_commit:
            self._intervals[node.uuid] = interval
        else:
            node.base.extras.set(EXTRA_RUNNING, interval)

    def _user_name(self, node: ProcessNode) -> str:
        """Use the owner's mapping, the default name, or the AiiDA owner identity."""
        user = getattr(node, "user", None)
        email = getattr(user, "email", "") or ""
        configured = self.user_names.get(email, "")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        if self.user_name:
            return self.user_name
        name = " ".join(
            part.strip() for part in (getattr(user, "first_name", ""),
                                      getattr(user, "last_name", ""))
            if isinstance(part, str) and part.strip()
        )
        return name or email or "Unknown user"

    def _describe(
        self,
        node: ProcessNode,
        seconds: float,
        source: str = "process",
        hostname: Optional[str] = None,
    ) -> str:
        minutes = int(seconds // 60)
        lines = [
            f"User: {self._user_name(node)}",
            f"Name: {(node.label or '').strip() or '(label not set)'}",
            f"PK: {node.pk}",
        ]
        if hostname:
            lines.append(f"Hostname: {hostname}")
        lines.append(f"Running time: at least {minutes // 60} h {minutes % 60} min")
        return "\n".join(lines)

    def report(self) -> str:
        if not self._current:
            return (
                "No RUNNING calculations were found in workchain_hierarchy "
                "for the current AiiDA profile."
            )
        entries = sorted(self._current.values(), key=lambda entry: not entry[0])
        return "Current AiiDA calculations\n\n" + "\n\n".join(
            message for _, message in entries
        )

    def overdue_report(self) -> Optional[str]:
        """Return calculations confirmed over the limit, or None when there are none."""
        entries = [message for overdue, message in self._current.values() if overdue]
        return "\n\n".join(entries) if entries else None

    def statistics_report(self, allocated_servers: Optional[int] = None) -> str:
        """Summarize running processes from the same completed scan as the report."""
        entries = list(self._current.values())
        overdue_count = sum(overdue for overdue, _ in entries)
        lines = ["📊 Calculation statistics (this monitor)"]
        if self.user_name:
            lines.append(f"User: {self.user_name}")
        servers = allocated_servers if allocated_servers is not None else "unavailable"
        lines.append(f"Allocated servers (yascheduler): {servers}")
        lines.append(f"RUNNING: {len(entries)}")
        if self.hours is not None:
            lines.append(f"Running longer than {self.hours:g} h: {overdue_count}")
        else:
            lines.append("Running time limit: disabled")
        return "\n".join(lines)
