import logging

import requests

logger = logging.getLogger(__name__)


def send_webhook(webhook_url, payload, status, key=None):
    """Send a webhook notification with the given payload and status.

    Args:
        webhook_url (str): The webhook endpoint URL
        payload (str): The payload data to send
        status (str): The status string
        key (str, optional): Authorization key for Bearer token authentication

    Returns:
        bool: True if webhook was sent successfully (status code 200), False otherwise
    """
    data = {"payload": payload, "status": status}
    if key:
        data["key"] = key
    try:
        response = requests.post(
            webhook_url, data=data, timeout=10
        )
        if response.status_code == 200:
            return True

        # non-200 response
        logger.error(
            "Webhook returned non-200 status %s for %s; data=%r; response=%r",
            response.status_code,
            webhook_url,
            data,
            # include response text if available
            getattr(response, "text", None),
        )
        return False
    except Exception as e:
        logger.error(
            "Webhook error: %s (url=%s, data=%r)",
            e,
            webhook_url,
            data,
        )
        return False
