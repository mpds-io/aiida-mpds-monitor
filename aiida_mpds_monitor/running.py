"""Track observed RUNNING intervals without relying on node creation/modification time."""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from aiida.orm import ProcessNode

from .notifications import Notifier

logger = logging.getLogger(__name__)
EXTRA_RUNNING = "monitor_running_interval"


class RunningNotifications:
    def __init__(self, notifier: Notifier, hours: Optional[float] = None,
                 no_commit: bool = False) -> None:
        self.notifier = notifier
        self.hours = None
        if hours is not None:
            try:
                value = float(hours)
                if isinstance(hours, bool) or not math.isfinite(value) or value <= 0:
                    raise ValueError
                self.hours = value
            except (TypeError, ValueError):
                logger.warning("running_alert_hours must be positive; RUNNING threshold disabled")
        self.no_commit = no_commit
        self._intervals: dict[str, dict] = {}
        self._current: dict[str, str] = {}

    def begin_scan(self) -> None:
        self._current.clear()

    def finish_scan(self) -> None:
        if self.no_commit:
            self._intervals = {key: value for key, value in self._intervals.items()
                               if key in self._current}

    def observe(self, node: ProcessNode, now: Optional[datetime] = None) -> None:
        try:
            now = now or datetime.now(timezone.utc)
            interval = (self._intervals.get(node.uuid) if self.no_commit else
                        node.base.extras.get(EXTRA_RUNNING, None))
            state = getattr(node.process_state, "value", node.process_state)
            if state != "running":
                if self.no_commit:
                    self._intervals.pop(node.uuid, None)
                elif interval is not None:
                    node.base.extras.delete(EXTRA_RUNNING)
                self._current.pop(node.uuid, None)
                return
            if not interval:
                interval = {"since": now.isoformat(), "alerted": False}
                self._save(node, interval)
            since = datetime.fromisoformat(interval["since"])
            seconds = max(0, (now - since).total_seconds())
            message = self._describe(node, seconds)
            if self.hours is not None and seconds > self.hours * 3600:
                message += f"\n⏳ Превышен порог {self.hours:g} ч"
            self._current[node.uuid] = message
        except Exception:
            logger.warning("Could not track RUNNING interval for PK %s", getattr(node, "pk", None))

    def _save(self, node: ProcessNode, interval: dict) -> None:
        if self.no_commit:
            self._intervals[node.uuid] = interval
        else:
            node.base.extras.set(EXTRA_RUNNING, interval)

    @staticmethod
    def _describe(node: ProcessNode, seconds: float) -> str:
        minutes = int(seconds // 60)
        lines = [f"Название: {(node.label or '').strip() or '(label не задан)'}", f"PK: {node.pk}"]
        lines.append(f"RUNNING: не менее {minutes // 60} ч {minutes % 60} мин")
        return "\n".join(lines)

    def report(self) -> str:
        if not self._current:
            return "В workchain_hierarchy текущего профиля AiiDA нет расчётов со статусом RUNNING."
        return "Текущие расчёты AiiDA\n\n" + "\n\n".join(self._current.values())
