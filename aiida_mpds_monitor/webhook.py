import logging

import requests


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
        return response.status_code == 200
    except Exception as e:
        logging.debug(f"Webhook error: {e}")
        return False
