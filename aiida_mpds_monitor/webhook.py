import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)


def _safe_text(value: object, key: Optional[str]) -> str:
    """Servers and request exceptions can echo credentials outside the form fields."""
    text = str(value)
    if key:
        for secret in (key, quote(key, safe="")):
            text = text.replace(secret, "[REDACTED]")
    return text[:5000]


@dataclass
class ArchiveUploadResult:
    """Response details for one upload; independent of notification deduplication."""

    status_code: Optional[int] = None
    description: str = ""

    @property
    def unfinished_crystal_output(self) -> bool:
        return self.status_code == 422 and " ".join(self.description.split()) == (
            "Archive validation failed: Found not a finished output"
        )


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
        description = _safe_text(description, key)
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
        return "⚠️ Archive upload errors\n" + "\n".join(notices)


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
                _safe_text(webhook_url, key),
                log_data,
            )
            return True

        # non-200/non-409 response
        logger.error(
            "Webhook returned non-200 status %s for %s; data=%r; response=%r",
            response.status_code,
            _safe_text(webhook_url, key),
            log_data,
            _safe_text(getattr(response, "text", None), key),
        )
        return False
    except Exception as e:
        log_data = {**data, "key": "***"} if key else data

        logger.error(
            "Webhook error: %s (url=%s, data=%r)",
            _safe_text(e, key),
            _safe_text(webhook_url, key),
            log_data,
        )
        return False


def send_archive(
    upload_url, archive_path, bid: int | None = None, schema_id: int | None = None,
    key: str | None = None, timeout: int = 30, errors: Optional[ArchiveUploadErrors] = None,
    result: Optional[ArchiveUploadResult] = None,
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
    if result is not None:
        result.status_code = None
        result.description = ""
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

        if result is not None:
            result.status_code = resp.status_code
        if resp.status_code == 200:
            if errors is not None:
                errors.succeeded(upload_url)
            return True

        description = "Server did not provide an error explanation"
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("detail") or body.get("description")
                if isinstance(detail, str) and detail.strip():
                    description = detail
            elif isinstance(body, str) and body.strip():
                description = body.strip()
        except ValueError:
            if isinstance(resp.text, str) and resp.text.strip():
                description = resp.text.strip()
        if result is not None:
            result.description = description
        if errors is not None:
            errors.failed(
                upload_url, f"Archive upload failed (HTTP {resp.status_code}): {description}", key,
            )

        logger.error(
            "Archive upload returned non-200 status %s for %s; data=%r; response=%r",
            resp.status_code,
            _safe_text(upload_url, key),
            data,
            _safe_text(getattr(resp, "text", None), key),
        )
        return False
    except Exception as e:
        if errors is not None:
            errors.failed(upload_url, f"Archive upload failed ({type(e).__name__})", key)
        logger.error("Archive upload error: %s (url=%s, data=%r)",
                     _safe_text(e, key), _safe_text(upload_url, key), data)
        return False
