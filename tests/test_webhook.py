from unittest.mock import MagicMock, patch

from aiida_mpds_monitor.webhook import send_webhook


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
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "http://example.com/webhook"
        assert call_args[1]["data"] == {
            "payload": "test_payload",
            "status": "finished",
        }
        assert call_args[1]["timeout"] == 10

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
            assert "http://example.com/webhook" in args[1]
            assert "test_payload" in repr(args[2])

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
    def test_send_webhook_timeout(self, mock_post):
        """Test webhook timeout handling."""
        mock_post.side_effect = TimeoutError("Request timeout")

        result = send_webhook(
            "http://example.com/webhook", "test_payload", "finished"
        )

        assert result is False
