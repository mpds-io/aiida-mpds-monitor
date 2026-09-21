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
from aiida_mpds_monitor.scheduling import DailyReports


@pytest.fixture
def telegram_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)

    def advance(seconds):
        clock.now += seconds

    sleep = MagicMock(side_effect=advance)
    monkeypatch.setattr("aiida_mpds_monitor.notifications.time.monotonic", lambda: clock.now)
    monkeypatch.setattr("aiida_mpds_monitor.notifications.time.sleep", sleep)
    return sleep


def telegram_response(status=200, data=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"ok": True} if data is None else data
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError("secret")
    return response


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
            delete=lambda key: extras.pop(key, None),
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
        post.return_value = telegram_response()
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
        post.return_value = telegram_response()
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
    assert post.call_count == 1  # Never retry an ambiguous network/HTTP failure.
    assert caplog.records[-1].levelname == "ERROR"


@pytest.mark.parametrize("status,code,description", [
    (400, 400, "Bad Request: chat not found"),
    (403, 403, "Forbidden: bot was kicked from the supergroup chat"),
    (401, 401, "Unauthorized"),
    (200, 400, "Bad Request: not enough rights to send text messages to the chat"),
    (502, None, None),
])
def test_telegram_rejections_include_diagnostic_details(status, code, description, caplog):
    data = {"ok": False}
    if code is not None:
        data.update(error_code=code, description=description)
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value = telegram_response(status, data)
        TelegramNotifier("secret", "-123").notify("private message")
    assert "chat_id=-123" in caplog.text
    assert f"HTTP {status}" in caplog.text
    assert f"error_code={code if code is not None else 'unknown'}" in caplog.text
    assert (description or "description=not provided") in caplog.text
    assert "secret" not in caplog.text
    assert "private message" not in caplog.text
    post.assert_called_once()


def test_group_migration_error_shows_new_chat_id(caplog):
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value = telegram_response(400, {
            "ok": False, "error_code": 400,
            "description": "Bad Request: group chat was upgraded to a supergroup chat",
            "parameters": {"migrate_to_chat_id": -1001234567890},
        })
        TelegramNotifier("secret", "-123").notify("test")
    assert "update TELEGRAM_CHAT_ID" in caplog.text
    assert "-1001234567890" in caplog.text
    post.assert_called_once()


@pytest.mark.parametrize("failure", ["api", "network"])
def test_telegram_external_details_redact_raw_and_encoded_token(failure, caplog):
    detail = (
        "Cannot access https://api.telegram.org/bot123:secret/sendMessage "
        "or bot123%3Asecret/sendMessage\nextra detail"
    )
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        if failure == "network":
            post.side_effect = requests.ConnectionError(detail)
        else:
            post.return_value = telegram_response(403, {
                "ok": False, "error_code": 403, "description": detail,
            })
        TelegramNotifier("123:secret", "-123").notify("test")
    assert "Cannot access" in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "secret" not in caplog.text
    assert "\n" not in caplog.records[-1].message
    if failure == "network":
        assert "ConnectionError" in caplog.text


@pytest.mark.parametrize("data", [None, [], "error", 42])
def test_telegram_invalid_json_shape_logs_status(data, caplog):
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value = telegram_response(502)
        post.return_value.json.return_value = data
        TelegramNotifier("secret", "-123").notify("test")
    assert "HTTP 502" in caplog.text
    assert "expected a JSON object" in caplog.text
    post.assert_called_once()


def test_telegram_non_json_error_logs_http_status_without_body(caplog):
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value = telegram_response(502)
        post.return_value.json.side_effect = ValueError("secret")
        post.return_value.text = "private proxy error with secret"
        TelegramNotifier("secret", "-123").notify("test")
    assert "HTTP 502" in caplog.text
    assert "response is not valid JSON" in caplog.text
    assert "secret" not in caplog.text
    post.assert_called_once()


def test_message_spacing_is_preserved_between_details_and_summary(telegram_clock):
    notifier = TelegramNotifier("secret", "-123")
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value = telegram_response()
        notifier.notify("x" * 2500)
        notifier.notify("statistics")
    assert [call.kwargs["json"]["text"] for call in post.call_args_list] == [
        "x" * 2000, "x" * 500, "statistics",
    ]
    assert [call.args[0] for call in telegram_clock.call_args_list] == pytest.approx([3.1, 3.1])


@pytest.mark.parametrize("status", [200, 429])
def test_final_statistics_retry_after_explicit_rate_limit(status, telegram_clock, caplog):
    notifier = TelegramNotifier("secret", "-123")
    limited = telegram_response(status, {
        "ok": False, "error_code": 429, "description": "secret",
        "parameters": {"retry_after": 7},
    })
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.side_effect = [telegram_response() for _ in range(3)] + [limited, telegram_response()]
        DailyReports(notifier).notify_if_due("x" * 5000, summary="RUNNING: 29; over limit: 23")
    texts = [call.kwargs["json"]["text"] for call in post.call_args_list]
    assert texts == ["x" * 2000, "x" * 2000, "x" * 1000] + [
        "RUNNING: 29; over limit: 23",
    ] * 2
    assert telegram_clock.call_args.args[0] == pytest.approx(7)
    assert post.call_args.kwargs["timeout"] == 10
    limited.raise_for_status.assert_not_called()
    assert "retrying rejected message" in caplog.text
    assert "secret" not in caplog.text


def test_rate_limit_retries_are_bounded_and_log_failure(telegram_clock, caplog):
    notifier = TelegramNotifier("secret", "-123")
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value = telegram_response(429, {
            "ok": False, "error_code": 429, "parameters": {"retry_after": 60},
        })
        notifier.notify("statistics")
    assert post.call_count == 3
    assert [call.args[0] for call in telegram_clock.call_args_list] == [60, 60]
    assert caplog.records[-1].levelname == "ERROR"
    assert "rate limit" in caplog.records[-1].message


@pytest.mark.parametrize("retry_after", [None, -1, 61, "7", True, [], float("inf")])
def test_invalid_or_excessive_retry_delay_does_not_block_monitoring(
    retry_after, telegram_clock, caplog
):
    with patch("aiida_mpds_monitor.notifications.requests.post") as post:
        post.return_value = telegram_response(429, {
            "ok": False, "error_code": 429, "parameters": {"retry_after": retry_after},
        })
        TelegramNotifier("secret", "-123").notify("statistics")
    post.assert_called_once()
    telegram_clock.assert_not_called()
    assert caplog.records[-1].levelname == "ERROR"


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


def test_report_scan_queries_types_from_all_hierarchy_levels_directly():
    from aiida_mpds_monitor.running import EXTRA_RUNNING, RunningNotifications

    calc = make_node(pk=123)
    calc.process_label = "CrystalParallelCalculation"
    calc.label = ""
    # No parent or call links: selection must not depend on their presence.
    finished = make_node("finished", 0, pk=124)
    finished.base.extras.set(EXTRA_RUNNING, {"since": "2026-09-07T00:00:00+00:00"})
    config = AttributeDict({**DEFAULT_CONFIG, "workchain_hierarchy": {
        "Parent": {"Child": ["CrystalParallelCalculation"]},
    }, "monitor_filters": {"compounds": ["BaMnO3"]}})
    running = RunningNotifications(MagicMock())
    with patch.object(daemon, "QueryBuilder") as qb:
        qb.return_value.iterall.return_value = iter([(calc,), (finished,)])
        daemon.scan_notifications(config, MagicMock(), running)
    qb.return_value.append.assert_called_once_with(daemon.ProcessNode, filters={"and": [
        {"attributes.process_label": {"in": ["Child", "CrystalParallelCalculation", "Parent"]}},
        {"or": [
            {"attributes.process_state": "running"},
            {"and": [
                {"attributes.process_state": {"in": ["created", "waiting", "running"]}},
                {"attributes.scheduler_state": {"in": ["running", "RUNNING"]}},
            ]},
            {"extras": {"has_key": EXTRA_RUNNING}},
        ]},
    ]})
    assert "PK: 123" in running.report()
    assert "PK: 124" not in running.report()
    assert "label not set" in running.report()
    assert finished.base.extras.get(EXTRA_RUNNING) is None
    running.notifier.notify.assert_not_called()


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
