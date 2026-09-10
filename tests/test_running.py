import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from aiida.common.extendeddicts import AttributeDict

from aiida_mpds_monitor import daemon
from aiida_mpds_monitor.config import DEFAULT_CONFIG
from aiida_mpds_monitor.notifications import TelegramNotifier
from aiida_mpds_monitor.running import (
    EXTRA_RUNNING,
    RunningNotifications,
    _yastatus_executable,
    resolve_running_since,
)
from tests.test_notifications import make_node

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def test_running_report_uses_exact_webhook_payload_and_observed_time():
    node = make_node()
    node.label = "  BaMnO3/185: Geometry optimization  "
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
    assert "Название: BaMnO3/185: Geometry optimization\n" in report
    assert "Relax atomic positions and cell" not in report
    assert "PK 124" not in report
    assert "Процесс:" not in report
    assert "2 ч 17 мин" in report
    # Creation time and modification time are intentionally not used.
    assert node.base.extras.get(EXTRA_RUNNING)["since"] == NOW.isoformat()


def test_threshold_only_marks_requested_report_across_polls_and_restart():
    node = make_node()
    notifier = MagicMock()
    tracker = RunningNotifications(notifier, hours=2)
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    notifier.notify.assert_not_called()
    tracker.observe(node, NOW + timedelta(hours=3))
    tracker.observe(node, NOW + timedelta(hours=4))
    RunningNotifications(notifier, hours=2).observe(node, NOW + timedelta(hours=5))
    notifier.notify.assert_not_called()
    assert "Превышен порог 2 ч" in tracker.report()


def test_nonrunning_resets_interval():
    node = make_node()
    notifier = MagicMock()
    tracker = RunningNotifications(notifier, hours=1)
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    node.process_state.value = "waiting"
    tracker.observe(node, NOW + timedelta(hours=3))
    assert node.base.extras.get(EXTRA_RUNNING) is None
    assert "нет расчётов" in tracker.report()
    node.process_state.value = "running"
    tracker.observe(node, NOW + timedelta(hours=4))
    tracker.observe(node, NOW + timedelta(hours=6))
    notifier.notify.assert_not_called()


def test_no_commit_and_new_scan():
    node = make_node()
    tracker = RunningNotifications(MagicMock(), hours=1, no_commit=True)
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    tracker.observe(node, NOW + timedelta(hours=3))
    tracker.notifier.notify.assert_not_called()
    assert node.base.extras.get(EXTRA_RUNNING) is None
    tracker.begin_scan()
    assert "PK: 123" in tracker.report()
    tracker.finish_scan()
    assert "нет расчётов" in tracker.report()


@pytest.mark.parametrize("hours", [None, 0, -1, "bad", float("nan"), float("inf"), True])
def test_disabled_or_invalid_threshold(hours):
    tracker = RunningNotifications(MagicMock(), hours=hours)
    node = make_node()
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(days=10))
    tracker.notifier.notify.assert_not_called()
    assert "240 ч" in tracker.report()


def test_observation_never_calls_delivery():
    notifier = MagicMock()
    notifier.notify.side_effect = RuntimeError("unavailable")
    tracker = RunningNotifications(notifier, hours=1)
    node = make_node()
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    tracker.observe(node, NOW + timedelta(hours=3))
    notifier.notify.assert_not_called()


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


def test_background_command_polling_starts_and_stops():
    notifier = TelegramNotifier("secret", "123")
    started = threading.Event()

    def poll(report, long_poll_timeout=0):
        assert report() == "current report"
        assert long_poll_timeout == 10
        started.set()
        notifier._stop_commands.wait(1)
        return True

    with patch.object(notifier, "poll_commands", side_effect=poll):
        notifier.start_command_polling(lambda: "current report")
        assert started.wait(1)
        notifier.stop_command_polling()
    assert notifier._command_thread is not None
    assert not notifier._command_thread.is_alive()


def test_command_long_poll_uses_matching_http_timeout():
    notifier = TelegramNotifier("secret", "123")
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value.json.return_value = {"ok": True, "result": []}
        assert notifier.poll_commands(lambda: "report", long_poll_timeout=10)
    assert post.call_args.kwargs["json"]["timeout"] == 10
    assert post.call_args.kwargs["timeout"] == 15


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
    notifier.start_command_polling.side_effect = lambda report: reports.append(report())

    def observe(config, logger, running):
        running.observe(make_node(), NOW)
        running.finish_scan()

    with patch.object(daemon, "create_notifier", return_value=notifier), patch.object(
        daemon, "scan_notifications", side_effect=observe
    ), patch.object(daemon, "scan_and_process", side_effect=KeyboardInterrupt) as mpds:
        daemon.run_monitor_loop(AttributeDict(DEFAULT_CONFIG), MagicMock())
    mpds.assert_called_once()
    assert len(reports) == 1
    assert "PK: 123" in reports[0]
    assert "RUNNING: не менее 0 ч 0 мин" in reports[0]
    notifier.stop_command_polling.assert_called_once()


@pytest.mark.parametrize("state", ["finished", "excepted", "killed", "waiting", "created"])
def test_report_excludes_nonrunning_nodes(state):
    tracker = RunningNotifications(MagicMock())
    tracker.observe(make_node(state, 0), NOW)
    assert "нет расчётов" in tracker.report()
    tracker.notifier.notify.assert_not_called()


def test_monitor_without_commands_sends_no_telegram_messages():
    notifier = MagicMock()

    def observe(config, logger, running):
        node = make_node()
        running.observe(node, NOW)
        running.observe(node, NOW + timedelta(hours=100))
        running.observe(make_node("finished", 0, pk=2), NOW)

    with patch.object(daemon, "create_notifier", return_value=notifier), patch.object(
        daemon, "scan_notifications", side_effect=observe
    ), patch.object(daemon, "scan_and_process", side_effect=KeyboardInterrupt):
        daemon.run_monitor_loop(
            AttributeDict({**DEFAULT_CONFIG, "running_alert_hours": 1}), MagicMock()
        )
    notifier.notify.assert_not_called()
    notifier.start_command_polling.assert_called_once()
    notifier.stop_command_polling.assert_called_once()


def test_timer_storage_failure_does_not_hide_running_calculation():
    node = make_node()
    node.base.extras.get = MagicMock(side_effect=RuntimeError("storage unavailable"))
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    assert "PK: 123" in tracker.report()
    assert "RUNNING: длительность недоступна" in tracker.report()
    assert "нет расчётов" not in tracker.report()


@pytest.mark.parametrize("scheduler", ["running", "RUNNING"])
def test_running_scheduler_job_is_reported_as_running(scheduler):
    node = make_node("waiting")
    node.get_scheduler_state = lambda: scheduler
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    assert "PK: 123" in tracker.report()
    assert "RUNNING: не менее 2 ч 0 мин" in tracker.report()
    assert "waiting" not in tracker.report().lower()
    tracker.notifier.notify.assert_not_called()


@pytest.mark.parametrize("state,scheduler", [
    ("waiting", "queued"), ("waiting", "done"), ("finished", "running"),
    ("excepted", "running"), ("killed", "running"),
])
def test_queued_and_terminal_jobs_with_stale_scheduler_state_are_excluded(state, scheduler):
    node = make_node(state)
    node.get_scheduler_state = lambda: scheduler
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    assert "нет расчётов" in tracker.report()


def test_timer_resets_when_execution_source_changes():
    node = make_node()
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    node.process_state.value = "waiting"
    node.get_scheduler_state = lambda: "running"
    tracker.observe(node, NOW + timedelta(hours=5))
    assert "RUNNING: не менее 0 ч 0 мин" in tracker.report()
    node.get_scheduler_state = lambda: "queued"
    tracker.observe(node, NOW + timedelta(hours=6))
    assert "нет расчётов" in tracker.report()


def test_authoritative_running_since_replaces_first_observation():
    node = make_node("running")
    node.get_scheduler_state = lambda: "running"
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    actual_start = NOW - timedelta(hours=5, minutes=12)
    tracker.observe(node, NOW, running_since=actual_start)
    assert "5 ч 12 мин" in tracker.report()
    assert node.base.extras.get(EXTRA_RUNNING)["since"] == actual_start.isoformat()


def test_resolve_running_since_uses_generic_dispatch_time():
    node = make_node("waiting")
    dispatch_time = NOW - timedelta(hours=3)
    node.get_last_job_info = lambda: SimpleNamespace(dispatch_time=dispatch_time)
    assert resolve_running_since([node]) == {node.uuid: dispatch_time}


def test_resolve_running_since_uses_yascheduler_updated_at():
    node = make_node("waiting")
    node.computer.scheduler_type = "yascheduler"
    node.base.attributes = SimpleNamespace(get=lambda key, default=None: "10050")
    node.get_last_job_info = lambda: SimpleNamespace(dispatch_time=None)
    output = (
        '[{"task_id": 10050, "status": "RUNNING", '
        '"updated_at": "2026-09-06T11:32:47+02:00"}]'
    )
    with patch(
        "aiida_mpds_monitor.running._yastatus_executable", return_value="/venv/bin/yastatus"
    ), patch("aiida_mpds_monitor.running.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout=output)
        result = resolve_running_since([node])
    assert result[node.uuid] == datetime.fromisoformat("2026-09-06T11:32:47+02:00")
    run.assert_called_once_with(
        ["/venv/bin/yastatus", "--jobs", "10050", "--json"],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("failure", ["exit", "invalid", "timeout"])
def test_yascheduler_timestamp_failure_falls_back_cleanly(failure):
    node = make_node("waiting")
    node.computer.scheduler_type = "yascheduler"
    node.base.attributes = SimpleNamespace(get=lambda key, default=None: "10050")
    node.get_last_job_info = lambda: None
    with patch("aiida_mpds_monitor.running.subprocess.run") as run:
        if failure == "exit":
            run.return_value = SimpleNamespace(returncode=1, stdout="")
        elif failure == "invalid":
            run.return_value = SimpleNamespace(returncode=0, stdout="invalid")
        else:
            run.side_effect = __import__("subprocess").TimeoutExpired("yastatus", 15)
        assert resolve_running_since([node]) == {}


def test_yastatus_is_resolved_next_to_virtualenv_python():
    with patch("aiida_mpds_monitor.running.sys.executable", "/opt/aiida/bin/python"), patch(
        "aiida_mpds_monitor.running.Path.is_file", return_value=True
    ):
        assert _yastatus_executable() == "/opt/aiida/bin/yastatus"
