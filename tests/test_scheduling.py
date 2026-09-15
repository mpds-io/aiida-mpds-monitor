from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from aiida_mpds_monitor.scheduling import DailyReports

NOW = datetime(2026, 9, 15, 8, tzinfo=timezone.utc)


def test_startup_and_daily_delivery_once_per_date():
    notifier = MagicMock()
    reports = DailyReports(notifier, "09:00", "UTC")
    reports.notify_if_due("startup", NOW)
    reports.notify_if_due("not yet due", NOW + timedelta(minutes=59))
    reports.notify_if_due("daily", NOW + timedelta(hours=1))
    reports.notify_if_due("duplicate", NOW + timedelta(hours=2))
    reports.notify_if_due("not yet tomorrow", NOW + timedelta(days=1))
    reports.notify_if_due("tomorrow", NOW + timedelta(days=1, hours=1))
    assert [call.args[0] for call in notifier.notify.call_args_list] == [
        "startup", "daily", "tomorrow",
    ]


@pytest.mark.parametrize("hours", [1, 5])
def test_startup_at_or_after_daily_time_consumes_todays_slot(hours):
    notifier = MagicMock()
    reports = DailyReports(notifier)
    reports.notify_if_due("startup", NOW + timedelta(hours=hours))
    reports.notify_if_due("duplicate", NOW + timedelta(hours=hours + 1))
    notifier.notify.assert_called_once_with("startup")


def test_empty_startup_and_daily_reports_stay_silent_until_next_slot():
    notifier = MagicMock()
    reports = DailyReports(notifier)
    reports.notify_if_due(None, NOW)
    reports.notify_if_due("became overdue", NOW + timedelta(minutes=30))
    reports.notify_if_due(None, NOW + timedelta(hours=1))
    reports.notify_if_due("became overdue", NOW + timedelta(hours=2))
    notifier.notify.assert_not_called()
    reports.notify_if_due("next day", NOW + timedelta(days=1, hours=1))
    notifier.notify.assert_called_once_with("next day")


def test_schedule_uses_configured_timezone_and_local_date():
    notifier = MagicMock()
    reports = DailyReports(notifier, "00:30", "Asia/Tokyo")
    reports.notify_if_due(None, NOW)  # 17:00 local; consumes today's slot.
    reports.notify_if_due("too soon", NOW + timedelta(hours=7))  # Next day, 00:00.
    reports.notify_if_due("next local day", NOW + timedelta(hours=7, minutes=30))
    reports.notify_if_due("duplicate", NOW + timedelta(hours=8))
    notifier.notify.assert_called_once_with("next local day")


def test_repeated_dst_hour_does_not_repeat_daily_report():
    notifier = MagicMock()
    reports = DailyReports(notifier, "02:30", "Europe/Berlin")
    now = datetime(2026, 10, 25, 0, tzinfo=timezone.utc)
    reports.notify_if_due(None, now)
    reports.notify_if_due("daily", now + timedelta(minutes=30))
    reports.notify_if_due("duplicate", now + timedelta(hours=1, minutes=30))
    notifier.notify.assert_called_once_with("daily")


def test_skipped_dst_time_sends_on_first_scan_after_clock_jump():
    notifier = MagicMock()
    reports = DailyReports(notifier, "02:30", "Europe/Berlin")
    now = datetime(2026, 3, 29, 0, tzinfo=timezone.utc)
    reports.notify_if_due(None, now)
    reports.notify_if_due("daily", now + timedelta(hours=1))  # 03:00 local.
    notifier.notify.assert_called_once_with("daily")


def test_restart_intentionally_sends_a_new_startup_report():
    notifier = MagicMock()
    DailyReports(notifier).notify_if_due("startup", NOW)
    DailyReports(notifier).notify_if_due("startup after restart", NOW)
    assert notifier.notify.call_count == 2


def test_notification_failure_is_contained_and_does_not_repeat(caplog):
    notifier = MagicMock()
    notifier.notify.side_effect = RuntimeError("secret")
    reports = DailyReports(notifier)
    reports.notify_if_due("startup", NOW + timedelta(hours=2))
    reports.notify_if_due("duplicate", NOW + timedelta(hours=3))
    notifier.notify.assert_called_once()
    assert "Scheduled notification failed" in caplog.text
    assert "secret" not in caplog.text
    reports.notify_if_due("next day", NOW + timedelta(days=1, hours=2))
    assert notifier.notify.call_count == 2


@pytest.mark.parametrize("time,zone", [
    ("25:00", "UTC"), ("09:60", "UTC"), ("9:00", "UTC"), ("09:00:30", "UTC"),
    (None, "UTC"), (900, "UTC"), ("09:00", "No/SuchZone"), ("09:00", None),
])
def test_invalid_schedule_disables_reports_with_warning(time, zone, caplog):
    notifier = MagicMock()
    reports = DailyReports(notifier, time, zone)
    reports.notify_if_due("must not send", NOW)
    notifier.notify.assert_not_called()
    assert "Scheduled reports disabled" in caplog.text
