from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiida.common.extendeddicts import AttributeDict

from aiida_mpds_monitor import daemon
from aiida_mpds_monitor.config import DEFAULT_CONFIG


@pytest.mark.parametrize("telegram_fails", [False, True])
def test_loop_includes_upload_error_in_statistics_once(telegram_fails, tmp_path):
    notifier = MagicMock()
    if telegram_fails:
        notifier.notify.side_effect = [None, RuntimeError("delivery failure"), None]
    config = AttributeDict({**DEFAULT_CONFIG, "archive_keep": True})
    archive = tmp_path / "result.7z"
    archive.write_bytes(b"archive")
    response = MagicMock(status_code=401, text='{"detail":"Token has expired"}')
    response.json.return_value = {"detail": "Token has expired"}
    now = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    scans = 0

    def upload(config, logger, **options):
        nonlocal scans
        scans += 1
        if scans == 3:
            raise KeyboardInterrupt
        # Multiple parents and polling attempts must share one error notice.
        for pk in (1, 2, 3):
            assert not daemon.generate_and_upload_archive(
                SimpleNamespace(pk=pk, uuid=str(pk)), [], config, logger,
                archive_errors=options["archive_errors"],
            )

    with patch.object(daemon, "create_notifier", return_value=notifier), patch.object(
        daemon, "scan_notifications"
    ), patch.object(daemon, "scan_and_process", side_effect=upload), patch.object(
        daemon, "generate_parent_archive", return_value=archive
    ), patch.object(daemon, "resolve_allocated_servers", return_value=5), patch.object(
        daemon.time, "sleep"
    ), patch("aiida_mpds_monitor.webhook.requests.post", return_value=response), patch(
        "aiida_mpds_monitor.scheduling.datetime", wraps=datetime
    ) as clock:
        clock.now.side_effect = [now, now + timedelta(days=1), now + timedelta(days=2)]
        daemon.run_monitor_loop(config, MagicMock())

    summaries = [call.args[0] for call in notifier.notify.call_args_list]
    assert len(summaries) == 3
    assert all("📊 Calculation statistics" in summary for summary in summaries)
    assert "Archive upload errors" not in summaries[0]
    assert summaries[1].count("Token has expired") == 1
    assert "HTTP 401" in summaries[1]
    assert "Archive upload errors" not in summaries[2]


def test_disabled_telegram_does_not_create_upload_error_tracker():
    config = AttributeDict(DEFAULT_CONFIG)
    logger = MagicMock()
    with patch.object(daemon, "create_notifier", return_value=None), patch.object(
        daemon, "ArchiveUploadErrors"
    ) as errors, patch.object(
        daemon, "scan_and_process", side_effect=KeyboardInterrupt
    ) as scan:
        daemon.run_monitor_loop(config, logger)
    errors.assert_not_called()
    scan.assert_called_once_with(config, logger, no_commit=False, force=False)


def test_loop_drops_expired_token_notice_after_successful_upload(monkeypatch, tmp_path):
    notifier = MagicMock()
    config = AttributeDict({**DEFAULT_CONFIG, "archive_keep": True})
    archive = tmp_path / "result.7z"
    archive.write_bytes(b"archive")
    expired = MagicMock(status_code=401, text='{"detail":"Token has expired"}')
    expired.json.return_value = {"detail": "Token has expired"}
    now = datetime(2026, 9, 18, 8, tzinfo=timezone.utc)
    monkeypatch.setenv("MPDS_ARCHIVE_KEY", "old-token")
    scans = 0

    def upload(config, logger, **options):
        nonlocal scans
        scans += 1
        if scans == 3:
            raise KeyboardInterrupt
        if scans == 2:
            # Simulate changing this process's environment, not a parent shell's.
            monkeypatch.setenv("MPDS_ARCHIVE_KEY", "new-token")
        uploaded = daemon.generate_and_upload_archive(
            SimpleNamespace(pk=1, uuid="1"), [], config, logger,
            archive_errors=options["archive_errors"],
        )
        assert uploaded is (scans == 2)

    with patch.object(daemon, "create_notifier", return_value=notifier), patch.object(
        daemon, "scan_notifications"
    ), patch.object(daemon, "scan_and_process", side_effect=upload), patch.object(
        daemon, "generate_parent_archive", return_value=archive
    ), patch.object(daemon, "resolve_allocated_servers", return_value=5), patch.object(
        daemon.time, "sleep"
    ), patch(
        "aiida_mpds_monitor.webhook.requests.post",
        side_effect=[expired, MagicMock(status_code=200)],
    ) as post, patch("aiida_mpds_monitor.scheduling.datetime", wraps=datetime) as clock:
        clock.now.side_effect = [now, now + timedelta(minutes=30), now + timedelta(hours=1)]
        daemon.run_monitor_loop(config, MagicMock())

    assert [call.kwargs["headers"] for call in post.call_args_list] == [
        {"Authorization": "Bearer old-token"}, {"Authorization": "Bearer new-token"},
    ]
    summaries = [call.args[0] for call in notifier.notify.call_args_list]
    assert len(summaries) == 2
    assert all("📊 Calculation statistics" in summary for summary in summaries)
    assert all("Archive upload errors" not in summary for summary in summaries)
