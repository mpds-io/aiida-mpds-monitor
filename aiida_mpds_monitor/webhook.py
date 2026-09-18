import logging
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)


class ArchiveUploadErrors:
    """Queue one notice per failing endpoint; successful uploads clear old notices.

    State belongs to one daemon run. Consume notices before attempting Telegram
    delivery so an ambiguous delivery failure cannot cause repeated alerts.
    """

    def __init__(self) -> None:
        self._failed: set[str] = set()
        self._pending: dict[str, str] = {}

    def failed(self, url: str, description: str, key: Optional[str] = None) -> None:
        if url in self._failed:
            return
        if key:
            for secret in (key, quote(key, safe="")):
                description = description.replace(secret, "[REDACTED]")
        self._failed.add(url)
        self._pending[url] = " ".join(description.split())[:500]

    def succeeded(self, url: str) -> None:
        self._failed.discard(url)
        self._pending.pop(url, None)

    def consume(self) -> str:
        notices = list(self._pending.values())
        self._pending.clear()
        if not notices:
            return ""
        return "\n\n⚠️ Archive upload errors\n" + "\n".join(notices)


def send_webhook(webhook_url, payload, status, key=None):
    """Send a webhook notification with the given payload and status.

    Args:
        webhook_url (str): The webhook endpoint URL
        payload (str): The payload data to send
        status (str): The status string
        key (str, optional): Authorization key included in the form data

    Returns:
        bool: True if webhook was sent successfully (200) or already exists on server (409), False otherwise
    """
    data = {"payload": payload, "status": status}
    if key:
        data["key"] = key
    try:
        response = requests.post(webhook_url, data=data, timeout=10)
        if response.status_code == 200:
            return True

        log_data = {**data, "key": "***"} if key else data

        if response.status_code == 409:
            logger.info(
                "Webhook returned 409 for %s (already exists on server) — marking as done; data=%r",
                webhook_url,
                log_data,
            )
            return True

        # non-200/non-409 response
        logger.error(
            "Webhook returned non-200 status %s for %s; data=%r; response=%r",
            response.status_code,
            webhook_url,
            log_data,
            getattr(response, "text", None),
        )
        return False
    except Exception as e:
        log_data = {**data, "key": "***"} if key else data

        logger.error(
            "Webhook error: %s (url=%s, data=%r)",
            e,
            webhook_url,
            log_data,
        )
        return False


def send_archive(
    upload_url, archive_path, bid: int | None = None, schema_id: int | None = None,
    key: str | None = None, timeout: int = 30, errors: Optional[ArchiveUploadErrors] = None,
) -> bool:
    """
    Upload a 7z archive file to the given `upload_url` endpoint using multipart/form-data.

    Args:
        upload_url (str): Full URL to POST the archive to (e.g. https://host/upload/absolidix)
        archive_path (str or Path): Path to the archive file to upload
        bid (int, optional): Optional `bid` form field
        schema_id (int, optional): Optional `schema_id` form field
        key (str, optional): Optional auth key included in form data as `key`
        timeout (int): request timeout in seconds
        errors: Optional daemon tracker for one-time statistics notices.

    Returns:
        bool: True if upload returned HTTP 200, False otherwise
    """
    data = {}
    if bid is not None:
        data["bid"] = str(bid)
    if schema_id is not None:
        data["schema_id"] = str(schema_id)

    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        with open(archive_path, "rb") as fh:
            files = {"file": (Path(archive_path).name, fh, "application/x-7z-compressed")}
            resp = requests.post(
                upload_url,
                data=data,
                files=files,
                headers=headers or None,
                timeout=timeout,
            )

        if resp.status_code == 200:
            if errors is not None:
                errors.succeeded(upload_url)
            return True

        if errors is not None:
            description = "Server did not provide an error explanation"
            try:
                body = resp.json()
                if isinstance(body, dict):
                    detail = body.get("detail") or body.get("description")
                    if isinstance(detail, str) and detail.strip():
                        description = detail
            except ValueError:
                pass
            errors.failed(
                upload_url, f"Archive upload failed (HTTP {resp.status_code}): {description}", key,
            )

        logger.error(
            "Archive upload returned non-200 status %s for %s; data=%r; response=%r",
            resp.status_code,
            upload_url,
            data,
            getattr(resp, "text", None),
        )
        return False
    except Exception as e:
        if errors is not None:
            errors.failed(upload_url, f"Archive upload failed ({type(e).__name__})", key)
        logger.error("Archive upload error: %s (url=%s, data=%r)", e, upload_url, data)
        return False
