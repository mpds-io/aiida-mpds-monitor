"""Startup and daily report delivery, independent of the notification transport."""

import logging
import re
from datetime import date, datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .notifications import Notifier

logger = logging.getLogger(__name__)


class DailyReports:
    """Attempt a report at startup and once per local date at the configured time.

    Call only after a successful calculation scan. Empty reports also consume
    their time slot. Delivery failures are not retried until the next slot to
    avoid duplicates when the remote service accepted a request before a timeout.
    """

    def __init__(
        self, notifier: Notifier, report_time: str = "09:00", report_timezone: str = "UTC"
    ) -> None:
        self.notifier = notifier
        self._started = False
        self._last_daily_date: Optional[date] = None
        self._time: Optional[time] = None
        self._timezone = timezone.utc
        try:
            if not isinstance(report_time, str) or not re.fullmatch(r"\d{2}:\d{2}", report_time):
                raise ValueError
            scheduled_time = time.fromisoformat(report_time)
            self._timezone = ZoneInfo(report_timezone)
            self._time = scheduled_time
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            logger.warning(
                "Scheduled reports disabled: notification_time must be HH:MM and "
                "notification_timezone must be a valid IANA timezone"
            )

    def notify_if_due(
        self, report: Optional[str], now: Optional[datetime] = None,
        summary: Optional[str] = None,
    ) -> None:
        if self._time is None:
            return
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local_now = now.astimezone(self._timezone)
        daily_due = (
            local_now.time() >= self._time
            and (self._last_daily_date is None or local_now.date() > self._last_daily_date)
        )
        if self._started and not daily_due:
            return
        self._started = True
        if daily_due:
            self._last_daily_date = local_now.date()
        if report:
            try:
                self.notifier.notify(report)
                if summary:
                    self.notifier.notify(summary)
            except Exception:
                logger.warning("Scheduled notification failed; continuing monitoring")
