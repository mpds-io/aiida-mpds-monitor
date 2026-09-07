from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests
from aiida.common.extendeddicts import AttributeDict

from aiida_mpds_monitor import daemon
from aiida_mpds_monitor.config import DEFAULT_CONFIG
from aiida_mpds_monitor.notifications import TelegramNotifier
from aiida_mpds_monitor.running import EXTRA_RUNNING, RunningNotifications
from tests.test_notifications import make_node

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def test_running_report_uses_child_labels_descriptions_and_observed_time():
    node = make_node()
    node.label = "BaMnO3/185"
    node.description = "Structure workflow"
    child = make_node(pk=124)
    child.label = "BaMnO3/185: Geometry optimization"
    child.description = "Relax atomic positions and cell"
    node.called = [child]
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2, minutes=17))
    report = tracker.report()
    assert "BaMnO3/185" in report
    assert "Geometry optimization" in report
    assert "Relax atomic positions and cell" in report
    assert "PK 124" in report
    assert "2 ч 17 мин" in report
    # Creation time and modification time are intentionally not used.
    assert node.base.extras.get(EXTRA_RUNNING)["since"] == NOW.isoformat()


def test_threshold_one_attempt_across_polls_and_restart():
    node = make_node()
    notifier = MagicMock()
    tracker = RunningNotifications(notifier, hours=2)
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    notifier.notify.assert_not_called()
    tracker.observe(node, NOW + timedelta(hours=3))
    tracker.observe(node, NOW + timedelta(hours=4))
    RunningNotifications(notifier, hours=2).observe(node, NOW + timedelta(hours=5))
    notifier.notify.assert_called_once()
    assert "RUNNING дольше 2 ч" in notifier.notify.call_args.args[0]


def test_nonrunning_resets_interval_and_new_run_can_alert():
    node = make_node()
    notifier = MagicMock()
    tracker = RunningNotifications(notifier, hours=1)
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    node.process_state.value = "waiting"
    tracker.observe(node, NOW + timedelta(hours=3))
    assert node.base.extras.get(EXTRA_RUNNING) == {}
    assert "нет расчётов" in tracker.report()
    node.process_state.value = "running"
    tracker.observe(node, NOW + timedelta(hours=4))
    tracker.observe(node, NOW + timedelta(hours=6))
    assert notifier.notify.call_count == 2


def test_no_commit_and_new_scan():
    node = make_node()
    tracker = RunningNotifications(MagicMock(), hours=1, no_commit=True)
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    tracker.observe(node, NOW + timedelta(hours=3))
    tracker.notifier.notify.assert_called_once()
    assert node.base.extras.get(EXTRA_RUNNING) is None
    tracker.begin_scan()
    assert "нет расчётов" in tracker.report()


@pytest.mark.parametrize("hours", [None, 0, -1, "bad", float("nan"), float("inf"), True])
def test_disabled_or_invalid_threshold(hours):
    tracker = RunningNotifications(MagicMock(), hours=hours)
    node = make_node()
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(days=10))
    tracker.notifier.notify.assert_not_called()
    assert "240 ч" in tracker.report()


def test_running_notification_failure_is_contained():
    notifier = MagicMock()
    notifier.notify.side_effect = RuntimeError("unavailable")
    tracker = RunningNotifications(notifier, hours=1)
    node = make_node()
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    tracker.observe(node, NOW + timedelta(hours=3))
    notifier.notify.assert_called_once()


def update(identifier, text, chat=123):
    return {"update_id": identifier, "message": {"chat": {"id": chat}, "text": text}}


def test_commands_button_chat_authorization_and_offset():
    notifier = TelegramNotifier("secret", "123")
    report = MagicMock(return_value="current report")
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value.json.return_value = {"ok": True, "result": [
            update(1, "/running", chat=999), update(2, "/start"),
            update(3, "/running"), update(4, "Текущие расчёты"),
        ]}
        notifier.poll_commands(report)
        notifier.poll_commands(report)
    assert report.call_count == 2
    calls = post.call_args_list
    assert calls[1].kwargs["json"]["reply_markup"]["keyboard"] == [
        [{"text": "Текущие расчёты"}]
    ]
    assert calls[-1].kwargs["json"]["offset"] == 5
    assert calls[0].kwargs["json"]["timeout"] == 0
    assert calls[0].args[0].endswith("/getUpdates")


@pytest.mark.parametrize("failure", ["network", "api", "malformed"])
def test_command_failure_is_contained_and_redacted(failure, caplog):
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        if failure == "network":
            post.side_effect = requests.Timeout("secret")
        else:
            post.return_value.json.return_value = (
                {"ok": False, "description": "secret"} if failure == "api" else None
            )
        TelegramNotifier("secret", "123").poll_commands(lambda: "report")
    assert "Telegram command polling" in caplog.text
    assert "secret" not in caplog.text


def test_long_messages_are_split_without_losing_descriptions():
    message = "Расчёт 🔬\n" * 1000
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        TelegramNotifier("secret", "123").notify(message)
    parts = [call.kwargs["json"]["text"] for call in post.call_args_list]
    assert "".join(parts) == message
    assert all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in parts)


def test_loop_serves_current_scan_report_and_continues_mpds():
    notifier = MagicMock()
    reports = []
    notifier.poll_commands.side_effect = lambda report: reports.append(report())

    def observe(config, logger, terminal, running):
        running.observe(make_node(), NOW)

    with patch.object(daemon, "create_notifier", return_value=notifier), patch.object(
        daemon, "scan_notifications", side_effect=observe
    ), patch.object(daemon, "scan_and_process", side_effect=KeyboardInterrupt) as mpds:
        daemon.run_monitor_loop(AttributeDict(DEFAULT_CONFIG), MagicMock())
    mpds.assert_called_once()
    assert len(reports) == 1
    assert "PK: 123" in reports[0]
    assert "RUNNING: не менее 0 ч 0 мин" in reports[0]
