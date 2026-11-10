import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)
EXTRA_ARCHIVE_SERVER_ERRORS = "monitor_archive_server_errors"


def _safe_text(value: object, key: Optional[str]) -> str:
    """Servers and request exceptions can echo credentials outside the form fields."""
    text = str(value)
    if key:
        for secret in (key, quote(key, safe="")):
            text = text.replace(secret, "[REDACTED]")
    return text[:5000]


def _archive_error_description(response) -> str:
    """Extract useful text from common plain-text and structured API errors."""
    try:
        body = response.json()
    except ValueError:
        body = getattr(response, "text", None)
    if isinstance(body, dict):
        for field in ("detail", "description", "message", "error", "errors"):
            value = body.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (dict, list)) and value:
                return json.dumps(value, ensure_ascii=False)
        if body:
            return json.dumps(body, ensure_ascii=False)
    elif isinstance(body, (dict, list)) and body:
        return json.dumps(body, ensure_ascii=False)
    elif isinstance(body, str) and body.strip():
        return body.strip()
    return "Server did not provide an error explanation"


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
    """Queue new server errors once; successful uploads clear errors for that node.

    State belongs to one daemon run. Consume notices before attempting Telegram
    delivery so an ambiguous delivery failure cannot cause repeated alerts.
    """

    def __init__(self) -> None:
        self._failed: dict[tuple[str, Optional[str], str], str] = {}
        self._pending: dict[tuple[str, Optional[str], str, str], str] = {}

    @staticmethod
    def _records(node) -> list[dict[str, str]]:
        records = node.base.extras.get(EXTRA_ARCHIVE_SERVER_ERRORS, [])
        if not isinstance(records, list):
            return []
        return [
            record for record in records
            if isinstance(record, dict)
            and isinstance(record.get("system"), str)
            and isinstance(record.get("message"), str)
        ]

    def failed(
        self,
        url: str,
        description: str,
        key: Optional[str] = None,
        system_labels: Optional[list[str]] = None,
        node=None,
    ) -> None:
        description = _safe_text(description, key)
        description = " ".join(description.split())
        labels = system_labels or ["(label not set)"]
        scope = getattr(node, "uuid", None) if node is not None else None
        records = self._records(node) if node is not None else []
        for label in labels:
            label = " ".join(_safe_text(label, key).split())[:200] or "(label not set)"
            existing = next(
                (record for record in records if record["system"] == label), None
            )
            record = {"system": label, "message": description}
            already_current = existing == record
            if node is not None and not already_current:
                records = [item for item in records if item["system"] != label]
                records.append(record)
                node.base.extras.set(EXTRA_ARCHIVE_SERVER_ERRORS, records)

            identity = (url, scope, label)
            failure = (*identity, description)
            if self._failed.get(identity) == description or already_current:
                self._failed[identity] = description
                continue
            self._failed[identity] = description
            calculation = (
                f"Calculation PK: {node.pk}\n"
                if node is not None and getattr(node, "pk", None) is not None else ""
            )
            self._pending[failure] = f"System: {label}\n{calculation}{description}"

    def succeeded(self, url: str, node=None) -> None:
        scope = getattr(node, "uuid", None) if node is not None else None

        def is_resolved(identity) -> bool:
            return identity[1] == scope if node is not None else identity[0] == url

        self._failed = {
            identity: description for identity, description in self._failed.items()
            if not is_resolved(identity)
        }
        self._pending = {
            failure: notice for failure, notice in self._pending.items()
            if not is_resolved(failure)
        }
        if (
            node is not None
            and node.base.extras.get(EXTRA_ARCHIVE_SERVER_ERRORS, None) is not None
        ):
            node.base.extras.delete(EXTRA_ARCHIVE_SERVER_ERRORS)

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
    result: Optional[ArchiveUploadResult] = None, system_labels: Optional[list[str]] = None,
    error_node=None,
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
                try:
                    errors.succeeded(upload_url, node=error_node)
                except Exception as exc:
                    # A delivered archive must not be retried because bookkeeping failed.
                    logger.warning(
                        "Archive upload succeeded, but clearing error records failed: %s",
                        _safe_text(exc, key),
                    )
            return True

        description = _archive_error_description(resp)
        if result is not None:
            result.description = description
        if errors is not None:
            errors.failed(
                upload_url,
                f"Archive upload failed (HTTP {resp.status_code}): {description}",
                key,
                system_labels=system_labels,
                node=error_node,
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
            errors.failed(
                upload_url,
                f"Archive upload failed ({type(e).__name__})",
                key,
                system_labels=system_labels,
            )
        logger.error("Archive upload error: %s (url=%s, data=%r)",
                     _safe_text(e, key), _safe_text(upload_url, key), data)
        return False
