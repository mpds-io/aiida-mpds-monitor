from unittest.mock import MagicMock, patch

import pytest
import requests

from aiida_mpds_monitor.webhook import ArchiveUploadErrors, send_archive, send_webhook


class TestSendWebhook:
    """Test cases for send_webhook function."""

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_send_webhook_success(self, mock_post):
        """Test successful webhook submission."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = send_webhook(
            "http://example.com/webhook", "test_payload", "finished"
        )

        assert result is True
        mock_post.assert_called_once_with(
            "http://example.com/webhook",
            data={
                "payload": "test_payload",
                "status": "finished",
            },
            timeout=10,
        )

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_send_webhook_redacts_auth_key_in_error_log(self, mock_post):
        mock_response = MagicMock(status_code=403, text="Forbidden")
        mock_post.return_value = mock_response

        with patch("aiida_mpds_monitor.webhook.logger") as mock_logger:
            result = send_webhook(
                "http://example.com/webhook",
                "test_payload",
                "excepted",
                key="secret_key",
            )

        assert result is False
        log_args = mock_logger.error.call_args.args
        assert log_args[3]["key"] == "***"
        assert "secret_key" not in repr(log_args)

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_send_webhook_failure(self, mock_post):
        """Test webhook submission failure and logging of request data."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "internal error"
        mock_post.return_value = mock_response
        with patch("aiida_mpds_monitor.webhook.logger") as mock_logger:
            result = send_webhook(
                "http://example.com/webhook", "test_payload", "excepted"
            )
            assert result is False
            mock_logger.error.assert_called_once()
            # ensure the logged message contains the URL and data payload
            args, _ = mock_logger.error.call_args
            assert "http://example.com/webhook" in args[2]
            assert "test_payload" in repr(args[3])

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_send_webhook_with_auth_key(self, mock_post):
        """Test webhook submission with authentication key."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = send_webhook(
            "http://example.com/webhook",
            "test_payload",
            "finished",
            key="secret_key",
        )

        assert result is True
        mock_post.assert_called_once_with(
            "http://example.com/webhook",
            data={
                "payload": "test_payload",
                "status": "finished",
                "key": "secret_key",
            },
            timeout=10,
        )

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_send_webhook_exception(self, mock_post):
        """Test webhook exception handling and that the request is logged."""
        mock_post.side_effect = Exception("Connection error")
        with patch("aiida_mpds_monitor.webhook.logger") as mock_logger:
            result = send_webhook(
                "http://example.com/webhook", "test_payload", "finished"
            )
            assert result is False
            mock_logger.error.assert_called_once()
            args, _ = mock_logger.error.call_args
            # first arg is format string, second arg should be the exception
            assert "Connection error" in str(args[1])

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_send_webhook_409_treated_as_success(self, mock_post):
        """Test that 409 (already exists on server) is treated as success."""
        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_post.return_value = mock_response

        with patch("aiida_mpds_monitor.webhook.logger") as mock_logger:
            result = send_webhook(
                "http://example.com/webhook", "test_payload", "finished"
            )

        assert result is True
        mock_logger.info.assert_called_once()
        mock_logger.error.assert_not_called()

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_send_webhook_timeout(self, mock_post):
        """Test webhook timeout handling."""
        mock_post.side_effect = TimeoutError("Request timeout")

        result = send_webhook(
            "http://example.com/webhook", "test_payload", "finished"
        )

        assert result is False


class TestSendArchive:
    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_expired_token_notice_once_until_upload_recovers(self, mock_post, tmp_path):
        archive = tmp_path / "result.7z"
        archive.write_bytes(b"archive")
        errors = ArchiveUploadErrors()
        response = MagicMock(status_code=401, text='{"detail":"Token has expired"}')
        response.json.return_value = {"detail": "Token has expired"}
        mock_post.return_value = response

        for _ in range(3):
            assert not send_archive("https://example.com/upload", archive, errors=errors)
        notice = errors.consume()
        assert notice.count("Token has expired") == 1
        assert "HTTP 401" in notice
        assert not send_archive("https://example.com/upload", archive, errors=errors)
        assert errors.consume() == ""

        mock_post.return_value = MagicMock(status_code=200)
        assert send_archive("https://example.com/upload", archive, errors=errors)
        assert errors.consume() == ""
        mock_post.return_value = response
        assert not send_archive("https://example.com/upload", archive, errors=errors)
        assert errors.consume() == notice

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_notice_cleared_if_upload_recovers_before_statistics(self, mock_post, tmp_path):
        archive = tmp_path / "result.7z"
        archive.write_bytes(b"archive")
        errors = ArchiveUploadErrors()
        response = MagicMock(status_code=401)
        response.json.return_value = {"detail": "Token has expired"}
        new_failure = MagicMock(status_code=503)
        new_failure.json.return_value = {"detail": "Service unavailable"}
        mock_post.side_effect = [response, MagicMock(status_code=200), new_failure]
        assert not send_archive("https://example.com/upload", archive, errors=errors)
        assert send_archive("https://example.com/upload", archive, errors=errors)
        assert errors.consume() == ""
        assert not send_archive("https://example.com/upload", archive, errors=errors)
        notice = errors.consume()
        assert "Service unavailable" in notice
        assert "Token has expired" not in notice
        assert errors.consume() == ""

    @pytest.mark.parametrize("body", [None, [], {"detail": []}, "not JSON"])
    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_unusable_error_body_still_reports_status(self, mock_post, body, tmp_path):
        archive = tmp_path / "result.7z"
        archive.write_bytes(b"archive")
        errors = ArchiveUploadErrors()
        response = MagicMock(status_code=502)
        if body == "not JSON":
            response.json.side_effect = ValueError("bad JSON")
        else:
            response.json.return_value = body
        mock_post.return_value = response
        assert not send_archive("https://example.com/upload", archive, errors=errors)
        assert "HTTP 502" in errors.consume()

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_error_notice_redacts_credentials_and_bounds_text(self, mock_post, tmp_path):
        archive = tmp_path / "result.7z"
        archive.write_bytes(b"archive")
        errors = ArchiveUploadErrors()
        mock_post.return_value = MagicMock(status_code=401)
        mock_post.return_value.json.return_value = {
            "detail": "Invalid secret:key / secret%3Akey\n" + "x" * 1000,
        }
        assert not send_archive(
            "https://example.com/upload", archive, key="secret:key", errors=errors,
        )
        notice = errors.consume()
        assert "secret" not in notice
        assert "[REDACTED]" in notice
        assert len(notice.splitlines()[-1]) == 500

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_network_error_notice_is_safe_and_not_repeated(self, mock_post, tmp_path):
        archive = tmp_path / "result.7z"
        archive.write_bytes(b"archive")
        errors = ArchiveUploadErrors()
        mock_post.side_effect = requests.Timeout("secret")
        assert not send_archive("https://example.com/upload", archive, errors=errors)
        assert "Timeout" in errors.consume()
        assert not send_archive("https://example.com/upload", archive, errors=errors)
        assert errors.consume() == ""

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_successful_multipart_upload(self, mock_post, tmp_path):
        archive = tmp_path / "BaMnO3.7z"
        archive.write_bytes(b"archive contents")
        mock_post.return_value = MagicMock(status_code=200)

        result = send_archive(
            "http://example.com/upload",
            archive,
            bid=42,
            schema_id=7,
            key="archive-secret",
            timeout=15,
        )

        assert result is True
        mock_post.assert_called_once()
        kwargs = mock_post.call_args.kwargs
        assert mock_post.call_args.args == ("http://example.com/upload",)
        assert kwargs["data"] == {"bid": "42", "schema_id": "7"}
        assert kwargs["headers"] == {"Authorization": "Bearer archive-secret"}
        assert kwargs["timeout"] == 15
        filename, file_handle, content_type = kwargs["files"]["file"]
        assert filename == "BaMnO3.7z"
        assert content_type == "application/x-7z-compressed"
        assert file_handle.closed

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_non_200_upload_is_failure_and_logs_response(self, mock_post, tmp_path):
        archive = tmp_path / "result.7z"
        archive.write_bytes(b"archive")
        mock_post.return_value = MagicMock(status_code=403, text="Forbidden")

        with patch("aiida_mpds_monitor.webhook.logger") as mock_logger:
            result = send_archive("http://example.com/upload", archive)

        assert result is False
        assert mock_logger.error.call_count == 1
        assert mock_logger.error.call_args.args[1] == 403
        assert mock_logger.error.call_args.args[4] == "Forbidden"

    @patch("aiida_mpds_monitor.webhook.requests.post")
    def test_missing_archive_is_failure_without_http_request(self, mock_post, tmp_path):
        result = send_archive(
            "http://example.com/upload", tmp_path / "missing.7z"
        )

        assert result is False
        mock_post.assert_not_called()
