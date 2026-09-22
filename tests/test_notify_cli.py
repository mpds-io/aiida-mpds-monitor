"""One-shot Telegram reports without a live AiiDA profile or Telegram bot."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aiida_mpds_monitor.notify import main
from aiida_mpds_monitor.notifications import TelegramNotifier


NOW = datetime(2026, 9, 22, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clear_telegram_environment(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def response(status=200, data=None):
    result = MagicMock()
    result.status_code = status
    result.json.return_value = {"ok": True} if data is None else data
    return result


def running_node():
    extras = {}
    return SimpleNamespace(
        uuid="node-123", pk=123, label="BaMnO3: Relax", process_state="running",
        user=SimpleNamespace(email="alice@example.org"),
        base=SimpleNamespace(extras=SimpleNamespace(
            get=extras.get, set=lambda key, value: extras.__setitem__(key, value),
        )),
    )


def scan_with_duration(hours):
    def scan(config, logger, running):
        running.observe(running_node(), now=NOW, running_since=NOW - timedelta(hours=hours))
        running.finish_scan()
    return scan


def config_file(tmp_path):
    config = tmp_path / "conf.yaml"
    config.write_text(
        'telegram_bot_token: yaml-token\ntelegram_chat_id: "-123"\n'
        'running_alert_hours: 1\nnotification_user_name: "@alice"\n'
    )
    return config


def test_cli_sends_calculation_details_then_statistics(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = config_file(tmp_path)
    with patch("aiida_mpds_monitor.notify.load_profile") as profile, patch(
        "aiida_mpds_monitor.notify.scan_notifications", side_effect=scan_with_duration(0.5)
    ) as scan, patch(
        "aiida_mpds_monitor.notify.resolve_allocated_servers", return_value=5
    ), patch("aiida_mpds_monitor.notifications.requests.post", return_value=response()) as post, patch(
        "aiida_mpds_monitor.notifications.time.sleep"
    ):
        assert main(["--config", str(config)]) == 0

    profile.assert_called_once_with()
    scan.assert_called_once()
    assert post.call_count == 2
    assert all(call.args[0].endswith("/sendMessage") for call in post.call_args_list)
    assert all(call.kwargs["json"]["chat_id"] == "-123" for call in post.call_args_list)
    details, statistics = [call.kwargs["json"]["text"] for call in post.call_args_list]
    assert "Current AiiDA calculations" in details
    assert "PK: 123" in details
    assert "User: @alice" in details
    assert "Running time: at least 0 h 30 min" in details
    assert "📊 Calculation statistics" in statistics
    assert "Allocated servers (yascheduler): 5" in statistics
    assert "RUNNING: 1" in statistics
    assert "Running longer than 1 h: 0" in statistics


@pytest.mark.parametrize("hours,details_expected", [(0.5, False), (2, True)])
def test_overdue_only_matches_scheduled_report(tmp_path, hours, details_expected):
    config = config_file(tmp_path)
    with patch("aiida_mpds_monitor.notify.load_profile"), patch(
        "aiida_mpds_monitor.notify.scan_notifications", side_effect=scan_with_duration(hours)
    ), patch("aiida_mpds_monitor.notify.resolve_allocated_servers", return_value=5), patch(
        "aiida_mpds_monitor.notifications.requests.post", return_value=response()
    ) as post, patch("aiida_mpds_monitor.notifications.time.sleep"):
        assert main(["--config", str(config), "--overdue-only"]) == 0
    texts = [call.kwargs["json"]["text"] for call in post.call_args_list]
    assert len(texts) == (2 if details_expected else 1)
    assert any("Calculation statistics" in text for text in texts)
    assert any("PK: 123" in text for text in texts) is details_expected


def test_empty_scan_sends_no_calculations_notice_and_statistics(tmp_path):
    config = config_file(tmp_path)
    with patch("aiida_mpds_monitor.notify.load_profile"), patch(
        "aiida_mpds_monitor.notify.scan_notifications",
        side_effect=lambda config, logger, running: running.finish_scan(),
    ), patch("aiida_mpds_monitor.notify.resolve_allocated_servers", return_value=None), patch(
        "aiida_mpds_monitor.notifications.requests.post", return_value=response()
    ) as post, patch("aiida_mpds_monitor.notifications.time.sleep"):
        assert main(["--config", str(config)]) == 0
    texts = [call.kwargs["json"]["text"] for call in post.call_args_list]
    assert "No RUNNING calculations" in texts[0]
    assert "RUNNING: 0" in texts[1]
    assert "unavailable" in texts[1]


def test_env_credentials_override_yaml(tmp_path, monkeypatch):
    config = config_file(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-456")
    with patch("aiida_mpds_monitor.notify.load_profile"), patch(
        "aiida_mpds_monitor.notify.scan_notifications",
        side_effect=lambda config, logger, running: running.finish_scan(),
    ), patch("aiida_mpds_monitor.notify.resolve_allocated_servers", return_value=5), patch(
        "aiida_mpds_monitor.notifications.requests.post", return_value=response()
    ) as post, patch("aiida_mpds_monitor.notifications.time.sleep"):
        assert main(["--config", str(config)]) == 0
    assert all(call.args[0].endswith("botenv-token/sendMessage") for call in post.call_args_list)
    assert all(call.kwargs["json"]["chat_id"] == "-456" for call in post.call_args_list)


def test_failed_details_do_not_prevent_statistics(tmp_path):
    config = config_file(tmp_path)
    rejected = response(403, {"ok": False, "error_code": 403, "description": "Forbidden"})
    with patch("aiida_mpds_monitor.notify.load_profile"), patch(
        "aiida_mpds_monitor.notify.scan_notifications", side_effect=scan_with_duration(2)
    ), patch("aiida_mpds_monitor.notify.resolve_allocated_servers", return_value=5), patch(
        "aiida_mpds_monitor.notifications.requests.post", side_effect=[rejected, response()]
    ) as post, patch("aiida_mpds_monitor.notifications.time.sleep"):
        assert main(["--config", str(config)]) == 1
    assert post.call_count == 2
    assert "Calculation statistics" in post.call_args_list[1].kwargs["json"]["text"]


def test_scan_failure_sends_nothing(tmp_path, capsys):
    config = config_file(tmp_path)
    with patch("aiida_mpds_monitor.notify.load_profile"), patch(
        "aiida_mpds_monitor.notify.scan_notifications", side_effect=RuntimeError("secret")
    ), patch("aiida_mpds_monitor.notifications.requests.post") as post:
        assert main(["--config", str(config)]) == 1
    stderr = capsys.readouterr().err
    assert "RuntimeError" in stderr
    assert "secret" not in stderr
    post.assert_not_called()


def test_missing_credentials_does_not_load_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with patch("aiida_mpds_monitor.notify.load_profile") as profile:
        assert main(["--config", str(tmp_path / "missing.yaml")]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    profile.assert_not_called()


def test_invalid_yaml_does_not_expose_token(tmp_path, capsys):
    config = tmp_path / "conf.yaml"
    config.write_text('telegram_bot_token: "private-token\n')
    assert main(["--config", str(config)]) == 2
    stderr = capsys.readouterr().err
    assert "Cannot read Telegram configuration" in stderr
    assert "private-token" not in stderr


def test_send_message_checks_all_chunks_even_when_one_fails():
    notifier = TelegramNotifier("secret", "-123")
    rejected = response(403, {"ok": False, "error_code": 403, "description": "Forbidden"})
    with patch("aiida_mpds_monitor.notifications.requests.post") as post, patch(
        "aiida_mpds_monitor.notifications.time.sleep"
    ):
        post.side_effect = [rejected, response()]
        assert notifier.send_message("x" * 2500) is False
    assert [call.kwargs["json"]["text"] for call in post.call_args_list] == [
        "x" * 2000, "x" * 500,
    ]
