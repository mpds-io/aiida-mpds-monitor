from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from aiida.common.extendeddicts import AttributeDict

from aiida_mpds_monitor import daemon
from aiida_mpds_monitor.config import DEFAULT_CONFIG
from aiida_mpds_monitor.notifications import (
    EXTRA_NOTIFICATION_STATE,
    StateNotifications,
    TelegramNotifier,
    create_notifier,
    format_notification,
)


def make_node(state="running", exit_status=None, pk=123):
    extras = {}
    return SimpleNamespace(
        pk=pk, uuid=str(pk), label="Si", process_label="Calculation",
        process_state=SimpleNamespace(value=state), exit_status=exit_status,
        is_killed=state == "killed", is_excepted=state == "excepted",
        is_failed=state == "finished" and exit_status != 0,
        is_finished_ok=state == "finished" and exit_status == 0,
        computer=SimpleNamespace(label="cluster01"), called=[],
        base=SimpleNamespace(extras=SimpleNamespace(
            get=extras.get, set=lambda key, value: extras.__setitem__(key, value),
        )),
    )


def test_telegram_message_and_send():
    node = make_node("excepted", 401)
    message = format_notification(node, "excepted")
    assert message == (
        "🚨 AiiDA process excepted\n\nPK: 123\nProcess: Calculation\n"
        "Computer: cluster01\nState: excepted\nExit status: 401"
    )
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        TelegramNotifier("secret", "-123").notify(message)
    post.assert_called_once_with(
        "https://api.telegram.org/botsecret/sendMessage",
        json={"chat_id": "-123", "text": message}, timeout=10,
    )


def test_missing_optional_fields():
    node = make_node("killed")
    del node.computer
    message = format_notification(node, "killed")
    assert "Computer:" not in message
    assert "Exit status:" not in message


@pytest.mark.parametrize("failure", ["network", "http", "api", "json"])
def test_telegram_failures_are_safe_and_redacted(failure, caplog):
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        if failure == "network":
            post.side_effect = requests.Timeout("secret")
        elif failure == "http":
            post.return_value.raise_for_status.side_effect = requests.HTTPError("secret")
        elif failure == "api":
            post.return_value.json.return_value = {"ok": False, "description": "secret"}
        else:
            post.return_value.json.side_effect = ValueError("secret")
        TelegramNotifier("secret", "123").notify("test")
    assert "Telegram notification" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("state,code,event", [
    ("finished", 0, "finished"), ("finished", 401, "failed"),
    ("excepted", None, "excepted"), ("killed", None, "killed"),
])
def test_transition_and_restart_deduplication(state, code, event):
    notifier = MagicMock()
    tracker = StateNotifications(notifier)
    running = make_node()
    tracker.observe(running)
    notifier.notify.assert_not_called()
    terminal = make_node(state, code)
    terminal.base = running.base
    tracker.observe(terminal)
    tracker.observe(terminal)
    StateNotifications(notifier).observe(terminal)
    notifier.notify.assert_called_once()
    assert terminal.base.extras.get(EXTRA_NOTIFICATION_STATE) == event


def test_failed_attempt_is_not_repeated():
    notifier = MagicMock()
    notifier.notify.side_effect = RuntimeError("provider unavailable")
    node = make_node("killed")
    StateNotifications(notifier).observe(node)
    StateNotifications(notifier).observe(node)
    notifier.notify.assert_called_once()


def test_storage_failure_does_not_send_or_raise():
    node = make_node("killed")
    node.base.extras.set = MagicMock(side_effect=RuntimeError("storage unavailable"))
    notifier = MagicMock()
    StateNotifications(notifier).observe(node)
    notifier.notify.assert_not_called()


def test_no_commit_deduplicates_without_extras():
    node = make_node("finished", 0)
    notifier = MagicMock()
    tracker = StateNotifications(notifier, no_commit=True)
    tracker.observe(node)
    tracker.observe(node)
    notifier.notify.assert_called_once()
    assert node.base.extras.get(EXTRA_NOTIFICATION_STATE) is None


@pytest.mark.parametrize("token,chat", [(None, None), ("secret", None), (None, "123")])
def test_missing_configuration_disables(monkeypatch, caplog, token, chat):
    for key, value in [("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)]:
        monkeypatch.delenv(key, raising=False)
        if value:
            monkeypatch.setenv(key, value)
    assert create_notifier() is None
    if token or chat:
        assert "notifications disabled" in caplog.text


def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert isinstance(create_notifier(), TelegramNotifier)


@pytest.mark.parametrize("uppercase", [False, True])
def test_yaml_telegram_configuration(monkeypatch, uppercase):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = {"telegram_bot_token": "yaml-token", "telegram_chat_id": -123}
    if uppercase:
        config = {key.upper(): value for key, value in config.items()}
    with patch("aiida_mpds_monitor.notifications.TelegramNotifier") as notifier:
        create_notifier({**DEFAULT_CONFIG, **config})
    notifier.assert_called_once_with("yaml-token", "-123")


def test_environment_overrides_yaml_per_setting(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " ")
    with patch("aiida_mpds_monitor.notifications.TelegramNotifier") as notifier:
        create_notifier({"telegram_bot_token": "yaml-token", "telegram_chat_id": 123})
    notifier.assert_called_once_with("env-token", "123")


def test_report_scan_tracks_only_webhook_children_even_if_parent_processed():
    parent, child, calc = make_node("finished", 0, 1), make_node("killed", pk=2), make_node(
        "excepted", pk=3
    )
    parent.process_label, child.process_label = "Parent", "Child"
    parent.called, child.called = [child], [calc, make_node(pk=4)]
    child.called[1].process_label = "Unconfigured"
    parent.base.extras.set("webhook_parent_processed", True)
    config = AttributeDict({**DEFAULT_CONFIG, "workchain_hierarchy": {
        "Parent": {"Child": ["Calculation"]},
    }})
    running = MagicMock()
    with patch.object(daemon, "QueryBuilder") as qb, patch.object(
        daemon, "WorkChainNode", SimpleNamespace
    ):
        qb.return_value.iterall.side_effect = lambda: iter([(parent,)])
        daemon.scan_notifications(config, MagicMock(), running)
        daemon.scan_notifications(config, MagicMock(), running)
    assert [call.args[0] for call in running.observe.call_args_list] == [
        child, child,
    ]
    qb.return_value.add_filter.assert_not_called()


def test_notification_scan_failure_does_not_stop_monitoring():
    config = AttributeDict(DEFAULT_CONFIG)
    with patch.object(daemon, "create_notifier", return_value=MagicMock()), patch.object(
        daemon, "scan_notifications", side_effect=RuntimeError("unavailable")
    ), patch.object(daemon, "scan_and_process", side_effect=[None, KeyboardInterrupt]) as scan, patch.object(
        daemon.time, "sleep"
    ) as sleep:
        daemon.run_monitor_loop(config, MagicMock())
    assert scan.call_count == 2
    sleep.assert_called_once_with(30)


def test_dry_run_does_not_initialize_or_send_notifications():
    with patch.object(daemon, "create_notifier") as create, patch.object(
        daemon, "scan_and_process_dry_run", side_effect=KeyboardInterrupt
    ), patch.object(daemon, "scan_notifications") as scan:
        daemon.run_monitor_loop(AttributeDict(DEFAULT_CONFIG), MagicMock(), dry_run=True)
    create.assert_not_called()
    scan.assert_not_called()
