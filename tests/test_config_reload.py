import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from aiida_mpds_monitor import daemon
from aiida_mpds_monitor.config import read_config


def write_config(path, **settings):
    path.write_text(yaml.safe_dump(settings))


@pytest.mark.parametrize("contents", [
    'archive_key: "private-token\n',
    "- private-token\n",
    "poll_interval: 0\n",
    "poll_interval: .inf\n",
    "poll_interval: true\n",
    "workchain_hierarchy: []\n",
    "workchain_hierarchy: {Parent: {Child: invalid}}\n",
    "monitor_filters: {element_counts: [-1]}\n",
    "log_backup_count: -1\n",
])
def test_bad_reload_keeps_previous_config_without_exposing_secrets(contents, tmp_path, caplog):
    path = tmp_path / "conf.yaml"
    write_config(path, archive_key="old-token")
    previous = read_config(path)
    path.write_text(contents)
    assert daemon.reload_monitor_config(previous, path, logging.getLogger(__name__)) is previous
    assert "keeping previous settings" in caplog.text
    assert "private-token" not in caplog.text


def test_missing_config_is_not_recreated_during_reload(tmp_path):
    path = tmp_path / "conf.yaml"
    write_config(path, archive_key="old-token")
    previous = read_config(path)
    path.unlink()
    assert daemon.reload_monitor_config(previous, path, MagicMock()) is previous
    assert not path.exists()


def test_removed_keys_return_to_defaults_and_cli_log_level_remains(tmp_path):
    path = tmp_path / "conf.yaml"
    write_config(path, poll_interval=2, archive_key="old-token", log_level="DEBUG")
    previous = read_config(path)
    write_config(path, log_level="WARNING")
    with patch.object(daemon, "setup_logger") as setup:
        updated = daemon.reload_monitor_config(previous, path, MagicMock(), logging_level="DEBUG")
    assert updated.poll_interval == 30
    assert updated.archive_key == ""
    assert updated.log_level == "DEBUG"
    setup.assert_not_called()


def test_failed_log_reconfiguration_keeps_working_handlers(tmp_path):
    path = tmp_path / "conf.yaml"
    write_config(path, log_file=str(tmp_path / "old.log"), log_level="WARNING")
    previous = read_config(path)
    logger = logging.getLogger("aiida_mpds_monitor")
    handler = logging.NullHandler()
    original_handlers = logger.handlers[:]
    logger.handlers = [handler]
    write_config(path, log_file=str(tmp_path / "new.log"), log_level="DEBUG")
    try:
        with patch.object(daemon.logging.handlers, "RotatingFileHandler", side_effect=OSError):
            assert daemon.reload_monitor_config(previous, path, logger) is previous
        assert logger.handlers == [handler]
    finally:
        logger.handlers = original_handlers


def test_reloaded_log_file_receives_new_messages(tmp_path):
    path = tmp_path / "conf.yaml"
    write_config(path, log_file=str(tmp_path / "old.log"), log_level="INFO")
    previous = read_config(path)
    logger = logging.getLogger("aiida_mpds_monitor")
    original_handlers, original_level = logger.handlers[:], logger.level
    logger.handlers = []
    try:
        daemon.setup_logger(previous)
        logger.info("before reload")
        write_config(path, log_file=str(tmp_path / "new.log"), log_level="INFO")
        updated = daemon.reload_monitor_config(previous, path, logger)
        logger.info("after reload")
        assert updated.log_file == str(tmp_path / "new.log")
        assert "before reload" in (tmp_path / "old.log").read_text()
        assert "after reload" not in (tmp_path / "old.log").read_text()
        assert "after reload" in (tmp_path / "new.log").read_text()
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers = original_handlers
        logger.setLevel(original_level)


def test_loop_rotates_yaml_archive_key_and_poll_interval_without_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("MPDS_ARCHIVE_KEY", raising=False)
    monkeypatch.delenv("MPDS_MONITOR_KEY", raising=False)
    path = tmp_path / "conf.yaml"
    write_config(path, archive_key="old-token", poll_interval=1, archive_keep=True)
    config = read_config(path)
    archive = tmp_path / "result.7z"
    archive.write_bytes(b"archive")
    expired = MagicMock(status_code=401, text='{"detail":"Token has expired"}')
    expired.json.return_value = {"detail": "Token has expired"}
    scans = 0
    trackers = []

    def scan(config, logger, **options):
        nonlocal scans
        scans += 1
        if scans == 3:
            raise KeyboardInterrupt
        trackers.append(options["archive_errors"])
        uploaded = daemon.generate_and_upload_archive(
            SimpleNamespace(pk=1, uuid="1"), [], config, logger,
            archive_errors=options["archive_errors"],
        )
        assert uploaded is (scans == 2)
        if scans == 1:
            write_config(path, archive_key="new-token", poll_interval=2, archive_keep=True)

    with patch.object(daemon, "create_notifier", return_value=MagicMock()), patch.object(
        daemon, "scan_notifications"
    ), patch.object(daemon, "scan_and_process", side_effect=scan), patch.object(
        daemon, "generate_parent_archive", return_value=archive
    ), patch.object(daemon, "resolve_allocated_servers", return_value=5), patch.object(
        daemon.time, "sleep"
    ) as sleep, patch(
        "aiida_mpds_monitor.webhook.requests.post", side_effect=[expired, MagicMock(status_code=200)],
    ) as post:
        daemon.run_monitor_loop(config, MagicMock(), config_path=path)

    assert [call.kwargs["headers"] for call in post.call_args_list] == [
        {"Authorization": "Bearer old-token"}, {"Authorization": "Bearer new-token"},
    ]
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    assert trackers[0] is trackers[1]
    assert trackers[0].consume() == ""


def test_notification_edits_preserve_running_timers_and_daily_delivery_history(tmp_path):
    path = tmp_path / "conf.yaml"
    settings = dict(
        telegram_bot_token="old-token", telegram_chat_id="-1",
        running_alert_hours=1, notification_user_name="@old",
        notification_time="09:00", notification_timezone="UTC",
    )
    write_config(path, **settings)
    config = read_config(path)
    old_notifier, new_notifier = MagicMock(), MagicMock()
    now = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    times = [now, now + timedelta(minutes=30), now + timedelta(days=1)]
    node = SimpleNamespace(
        uuid="1", pk=1, label="Si", process_state="running", user=None,
    )
    scans = 0
    runners = []

    def observe(config, logger, running):
        runners.append(running)
        running.observe(node, now=times[len(runners) - 1])
        running.finish_scan()

    def scan(config, logger, **options):
        nonlocal scans
        scans += 1
        if scans == 3:
            raise KeyboardInterrupt
        settings.update(
            telegram_bot_token="new-token", telegram_chat_id="-2",
            running_alert_hours=0.5, notification_user_name="@new", notification_time="10:00",
        )
        write_config(path, **settings)

    with patch.object(daemon, "create_notifier", side_effect=[old_notifier, new_notifier]), patch.object(
        daemon, "scan_notifications", side_effect=observe
    ), patch.object(daemon, "scan_and_process", side_effect=scan), patch.object(
        daemon, "resolve_allocated_servers", return_value=5
    ), patch.object(daemon.time, "sleep"), patch(
        "aiida_mpds_monitor.scheduling.datetime", wraps=datetime
    ) as clock:
        clock.now.side_effect = times
        daemon.run_monitor_loop(config, MagicMock(), config_path=path, no_commit=True)

    assert runners[0] is runners[1] is runners[2]
    assert runners[-1].hours == 0.5
    assert runners[-1]._intervals["1"]["since"] == now.isoformat()
    old_notifier.notify.assert_called_once()
    assert new_notifier.notify.call_count == 2  # Next day's details and statistics only.
    details, summary = [call.args[0] for call in new_notifier.notify.call_args_list]
    assert "User: @new" in details
    assert "at least 24 h 0 min" in details
    assert "Running longer than 0.5 h: 1" in summary


def test_enabling_disabling_and_reenabling_telegram_does_not_repeat_startup(tmp_path):
    path = tmp_path / "conf.yaml"
    write_config(path)
    config = read_config(path)
    first_notifier, second_notifier = MagicMock(), MagicMock()
    scans = 0

    def scan(config, logger, **options):
        nonlocal scans
        scans += 1
        if scans == 4:
            raise KeyboardInterrupt
        if scans == 2:
            write_config(path)
        else:
            write_config(path, telegram_bot_token="token", telegram_chat_id="-1")

    with patch.object(
        daemon, "create_notifier", side_effect=[None, first_notifier, None, second_notifier],
    ), patch.object(daemon, "scan_notifications"), patch.object(
        daemon, "scan_and_process", side_effect=scan
    ), patch.object(daemon, "resolve_allocated_servers", return_value=5), patch.object(
        daemon.time, "sleep"
    ), patch("aiida_mpds_monitor.scheduling.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
        daemon.run_monitor_loop(config, MagicMock(), config_path=path)
    first_notifier.notify.assert_called_once()
    second_notifier.notify.assert_not_called()


def test_cli_passes_config_path_and_logging_override_to_loop(tmp_path, monkeypatch):
    path = tmp_path / "conf.yaml"
    write_config(path)
    config = read_config(path)
    monkeypatch.setattr(daemon, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr("sys.argv", ["aiida-mpds-monitor", "-l", "DEBUG"])
    logger = MagicMock()
    with patch.object(daemon, "load_profile"), patch.object(
        daemon, "load_config", return_value=config
    ), patch.object(daemon, "setup_logger", return_value=logger), patch.object(
        daemon, "run_monitor_loop"
    ) as loop:
        daemon.main()
    loop.assert_called_once_with(
        config, logger, dry_run=False, no_commit=False, force=False,
        config_path=path, logging_level="DEBUG",
    )
