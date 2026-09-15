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
    resolve_running_details,
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
    assert "Name: BaMnO3/185: Geometry optimization\n" in report
    assert "Relax atomic positions and cell" not in report
    assert "PK 124" not in report
    assert "Process:" not in report
    assert "2 h 17 min" in report
    # Creation time and modification time are intentionally not used.
    assert node.base.extras.get(EXTRA_RUNNING)["since"] == NOW.isoformat()


def test_threshold_is_observed_without_sending_between_scheduled_reports():
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
    assert "🚨 LONG-RUNNING CALCULATION 🚨" in tracker.report()
    assert "Configured limit exceeded: 2 h" in tracker.report()


def test_nonrunning_resets_interval():
    node = make_node()
    notifier = MagicMock()
    tracker = RunningNotifications(notifier, hours=1)
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    node.process_state.value = "waiting"
    tracker.observe(node, NOW + timedelta(hours=3))
    assert node.base.extras.get(EXTRA_RUNNING) is None
    assert "No RUNNING calculations" in tracker.report()
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
    assert "No RUNNING calculations" in tracker.report()


@pytest.mark.parametrize("hours", [None, 0, -1, "bad", float("nan"), float("inf"), True])
def test_disabled_or_invalid_threshold(hours):
    tracker = RunningNotifications(MagicMock(), hours=hours)
    node = make_node()
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(days=10))
    tracker.notifier.notify.assert_not_called()
    assert "240 h" in tracker.report()
    assert tracker.overdue_report() is None


def test_observation_never_calls_delivery():
    notifier = MagicMock()
    notifier.notify.side_effect = RuntimeError("unavailable")
    tracker = RunningNotifications(notifier, hours=1)
    node = make_node()
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    tracker.observe(node, NOW + timedelta(hours=3))
    notifier.notify.assert_not_called()


def test_long_messages_are_split_without_losing_descriptions():
    message = "Calculation 🔬\n" * 1000
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        TelegramNotifier("secret", "123").notify(message)
    parts = [call.kwargs["json"]["text"] for call in post.call_args_list]
    assert "".join(parts) == message
    assert all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in parts)


@pytest.mark.parametrize("failure", [None, "network", "api"])
@pytest.mark.parametrize("owner_config", [
    {"notification_user_names": {"alice@example.org": "@alice"}},
    {"notification_user_name": "alice"},
    {"notification_user_name": "@alice"},
])
def test_loop_sends_only_overdue_calculations_to_shared_chat_and_continues_mpds(
    failure, owner_config
):
    notifier = TelegramNotifier("secret", "-123")
    config = AttributeDict({
        **DEFAULT_CONFIG,
        "running_alert_hours": 2,
        **owner_config,
    })

    def observe(config, logger, running):
        overdue = make_node(pk=123)
        overdue.user = SimpleNamespace(email="alice@example.org")
        running.observe(overdue, NOW, running_since=NOW - timedelta(hours=3))
        running.observe(make_node(pk=456), NOW, running_since=NOW - timedelta(hours=1))
        running.observe(make_node("finished", 0, pk=789), NOW)
        running.finish_scan()

    with patch.object(daemon, "create_notifier", return_value=notifier), patch.object(
        daemon, "scan_notifications", side_effect=observe
    ), patch.object(daemon, "scan_and_process", side_effect=KeyboardInterrupt) as mpds, patch(
        "aiida_mpds_monitor.notifications.requests.post"
    ) as post:
        if failure == "network":
            post.side_effect = requests.Timeout("secret")
        else:
            post.return_value.json.return_value = {"ok": failure is None}
        daemon.run_monitor_loop(config, MagicMock())
    mpds.assert_called_once()
    post.assert_called_once()
    assert post.call_args.args[0].endswith("/sendMessage")
    payload = post.call_args.kwargs["json"]
    assert payload["chat_id"] == "-123"
    assert "User: @alice" in payload["text"]
    assert "PK: 123" in payload["text"]
    assert "PK: 456" not in payload["text"]
    assert "PK: 789" not in payload["text"]
    assert "Running time: at least 3 h 0 min" in payload["text"]


def test_loop_waits_for_a_successful_scan_before_startup_notification():
    notifier = MagicMock()
    attempts = 0

    def observe(config, logger, running):
        nonlocal attempts
        attempts += 1
        running.observe(make_node(), NOW, running_since=NOW - timedelta(hours=3))
        if attempts == 1:
            raise RuntimeError("scan failed")
        running.finish_scan()

    with patch.object(daemon, "create_notifier", return_value=notifier), patch.object(
        daemon, "scan_notifications", side_effect=observe
    ), patch.object(daemon, "scan_and_process", side_effect=[None, KeyboardInterrupt]), patch.object(
        daemon.time, "sleep"
    ):
        daemon.run_monitor_loop(
            AttributeDict({**DEFAULT_CONFIG, "running_alert_hours": 2}), MagicMock()
        )
    assert attempts == 2
    notifier.notify.assert_called_once()


@pytest.mark.parametrize("state", ["finished", "excepted", "killed", "waiting", "created"])
def test_report_excludes_nonrunning_nodes(state):
    tracker = RunningNotifications(MagicMock())
    tracker.observe(make_node(state, 0), NOW)
    assert "No RUNNING calculations" in tracker.report()
    tracker.notifier.notify.assert_not_called()


@pytest.mark.parametrize("hours", [None, 2])
def test_monitor_without_overdue_calculations_sends_no_telegram_messages(hours):
    notifier = MagicMock()

    def observe(config, logger, running):
        node = make_node()
        running.observe(node, NOW)
        running.observe(node, NOW + timedelta(hours=1))
        running.observe(make_node("finished", 0, pk=2), NOW)
        running.finish_scan()

    with patch.object(daemon, "create_notifier", return_value=notifier), patch.object(
        daemon, "scan_notifications", side_effect=observe
    ), patch.object(daemon, "scan_and_process", side_effect=KeyboardInterrupt):
        daemon.run_monitor_loop(
            AttributeDict({**DEFAULT_CONFIG, "running_alert_hours": hours}), MagicMock()
        )
    notifier.notify.assert_not_called()


def test_timer_storage_failure_does_not_hide_running_calculation():
    node = make_node()
    node.base.extras.get = MagicMock(side_effect=RuntimeError("storage unavailable"))
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    assert "PK: 123" in tracker.report()
    assert "Running time: unavailable" in tracker.report()
    assert "No RUNNING calculations" not in tracker.report()
    assert tracker.overdue_report() is None


@pytest.mark.parametrize("scheduler", ["running", "RUNNING"])
def test_running_scheduler_job_is_reported_as_running(scheduler):
    node = make_node("waiting")
    node.get_scheduler_state = lambda: scheduler
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    tracker.observe(node, NOW + timedelta(hours=2))
    assert "PK: 123" in tracker.report()
    assert "Running time: at least 2 h 0 min" in tracker.report()
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
    assert "No RUNNING calculations" in tracker.report()


def test_timer_resets_when_execution_source_changes():
    node = make_node()
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    node.process_state.value = "waiting"
    node.get_scheduler_state = lambda: "running"
    tracker.observe(node, NOW + timedelta(hours=5))
    assert "Running time: at least 0 h 0 min" in tracker.report()
    node.get_scheduler_state = lambda: "queued"
    tracker.observe(node, NOW + timedelta(hours=6))
    assert "No RUNNING calculations" in tracker.report()


def test_authoritative_running_since_replaces_first_observation():
    node = make_node("running")
    node.get_scheduler_state = lambda: "running"
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW)
    actual_start = NOW - timedelta(hours=5, minutes=12)
    tracker.observe(node, NOW, running_since=actual_start)
    assert "5 h 12 min" in tracker.report()
    assert node.base.extras.get(EXTRA_RUNNING)["since"] == actual_start.isoformat()


def test_running_report_includes_scheduler_hostname_when_available():
    node = make_node("waiting")
    node.get_scheduler_state = lambda: "running"
    tracker = RunningNotifications(MagicMock())
    tracker.observe(node, NOW, hostname="compute-17")
    assert "PK: 123\nHostname: compute-17\nRunning time:" in tracker.report()


def test_overdue_calculations_are_highlighted_and_listed_first():
    normal = make_node(pk=1)
    overdue = make_node(pk=2)
    tracker = RunningNotifications(MagicMock(), hours=2)
    tracker.observe(normal, NOW, running_since=NOW - timedelta(hours=1))
    tracker.observe(overdue, NOW, running_since=NOW - timedelta(hours=3))
    report = tracker.report()
    assert report.index("🚨 LONG-RUNNING CALCULATION 🚨") < report.index("PK: 1")
    assert report.index("PK: 2") < report.index("PK: 1")


def test_overdue_report_excludes_boundary_and_resolves_each_owner_separately():
    tracker = RunningNotifications(MagicMock(), hours=0.5, user_names={
        "alice@example.org": "@alice", "bob@example.org": "Bob Smith (@bob)",
    })
    alice = make_node(pk=1)
    alice.user = SimpleNamespace(email="alice@example.org")
    bob = make_node(pk=2)
    bob.user = SimpleNamespace(email="bob@example.org")
    boundary = make_node(pk=3)
    tracker.observe(alice, NOW, running_since=NOW - timedelta(hours=1), hostname="worker-1")
    tracker.observe(bob, NOW, running_since=NOW - timedelta(hours=2), hostname="worker-2")
    tracker.observe(boundary, NOW, running_since=NOW - timedelta(minutes=30))
    report = tracker.overdue_report()
    assert "User: @alice\nName: Si\nPK: 1\nHostname: worker-1" in report
    assert "User: Bob Smith (@bob)\nName: Si\nPK: 2\nHostname: worker-2" in report
    assert "Configured limit exceeded: 0.5 h" in report
    assert "PK: 3" not in report


@pytest.mark.parametrize("first,last,expected", [
    ("Alice", "Smith", "Alice Smith"), ("", "", "alice@example.org"),
])
def test_unmapped_owner_falls_back_to_aiida_identity(first, last, expected):
    node = make_node()
    node.user = SimpleNamespace(email="alice@example.org", first_name=first, last_name=last)
    tracker = RunningNotifications(MagicMock(), hours=1)
    tracker.observe(node, NOW, running_since=NOW - timedelta(hours=2))
    assert f"User: {expected}\n" in tracker.overdue_report()


def test_owner_mapping_takes_priority_over_default_username():
    alice = make_node(pk=1)
    alice.user = SimpleNamespace(email="alice@example.org")
    bob = make_node(pk=2)
    bob.user = SimpleNamespace(email="bob@example.org")
    tracker = RunningNotifications(
        MagicMock(), hours=1, user_names={"alice@example.org": "Alice (@alice)"},
        user_name=" bob ",
    )
    for node in (alice, bob):
        tracker.observe(node, NOW, running_since=NOW - timedelta(hours=2))
    report = tracker.overdue_report()
    assert "User: Alice (@alice)\nName: Si\nPK: 1" in report
    assert "User: @bob\nName: Si\nPK: 2" in report


@pytest.mark.parametrize("user_name", [None, "", "   ", 123, {}])
def test_empty_or_invalid_default_name_keeps_aiida_owner_fallback(user_name):
    node = make_node()
    node.user = SimpleNamespace(email="alice@example.org")
    tracker = RunningNotifications(MagicMock(), hours=1, user_name=user_name)
    tracker.observe(node, NOW, running_since=NOW - timedelta(hours=2))
    assert "User: alice@example.org\n" in tracker.overdue_report()


def test_overdue_report_drops_finished_calculations_after_scan():
    node = make_node()
    tracker = RunningNotifications(MagicMock(), hours=1)
    tracker.observe(node, NOW, running_since=NOW - timedelta(hours=2))
    assert tracker.overdue_report() is not None
    tracker.begin_scan()
    node.process_state.value = "finished"
    tracker.observe(node, NOW)
    tracker.finish_scan()
    assert tracker.overdue_report() is None


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
        '"updated_at": "2026-09-06T11:32:47+02:00", '
        '"node": {"hostname": "worker-05"}}]'
    )
    with patch(
        "aiida_mpds_monitor.running._yastatus_executable", return_value="/venv/bin/yastatus"
    ), patch("aiida_mpds_monitor.running.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout=output)
        result = resolve_running_details([node])
    assert result[node.uuid].running_since == datetime.fromisoformat(
        "2026-09-06T11:32:47+02:00"
    )
    assert result[node.uuid].hostname == "worker-05"
    run.assert_called_once_with(
        ["/venv/bin/yastatus", "--jobs", "10050", "--json"],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )


def test_resolve_running_details_omits_unassigned_yascheduler_hostname():
    node = make_node("waiting")
    node.computer.scheduler_type = "yascheduler"
    node.base.attributes = SimpleNamespace(get=lambda key, default=None: "10050")
    node.get_last_job_info = lambda: None
    output = (
        '[{"task_id": 10050, "status": "RUNNING", '
        '"updated_at": "2026-09-06T11:32:47+02:00", "node": null}]'
    )
    with patch("aiida_mpds_monitor.running.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout=output)
        details = resolve_running_details([node])[node.uuid]
    assert details.hostname is None


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
