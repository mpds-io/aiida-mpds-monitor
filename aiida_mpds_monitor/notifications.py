"""Optional notification delivery and persistent terminal-event deduplication."""

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Mapping, Optional
from urllib.parse import quote

import requests
from aiida.orm import ProcessNode

logger = logging.getLogger(__name__)
EXTRA_NOTIFICATION_STATE = "monitor_notification_state"
TELEGRAM_SEND_INTERVAL = 3.1  # Stay below the group limit of 20 messages per minute.
TELEGRAM_SEND_ATTEMPTS = 3
TELEGRAM_MAX_RETRY_AFTER = 60


class Notifier(ABC):
    @abstractmethod
    def notify(self, message: str) -> None:
        """Attempt delivery of a message."""


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._next_send_at = 0.0

    def _safe_detail(self, value: object) -> str:
        """Redact credentials before bounding and flattening external log text."""
        detail = str(value)
        for secret in (self._token, quote(self._token, safe="")):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        return " ".join(detail.split())[:500]

    def _response_detail(self, status: int, data: Mapping) -> str:
        details = [
            f"chat_id={self._safe_detail(self._chat_id)}",
            f"HTTP {status}",
            f"error_code={self._safe_detail(data.get('error_code', 'unknown'))}",
            f"description={self._safe_detail(data.get('description', 'not provided'))}",
        ]
        parameters = data.get("parameters")
        if isinstance(parameters, Mapping):
            if "retry_after" in parameters:
                details.append(f"retry_after={self._safe_detail(parameters['retry_after'])}")
            if "migrate_to_chat_id" in parameters:
                details.append(
                    "group migrated: update TELEGRAM_CHAT_ID (or telegram_chat_id in YAML) to "
                    + self._safe_detail(parameters["migrate_to_chat_id"])
                )
        return "; ".join(details)

    def notify(self, message: str) -> None:
        # Use a conservative chunk size, including for non-BMP Unicode characters.
        for start in range(0, len(message), 2000):
            self._send(message[start:start + 2000])

    def _send(self, message: str) -> None:
        for attempt in range(TELEGRAM_SEND_ATTEMPTS):
            try:
                delay = self._next_send_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._next_send_at = time.monotonic() + TELEGRAM_SEND_INTERVAL
                response = requests.post(
                    self._url,
                    json={"chat_id": self._chat_id, "text": message},
                    timeout=10,
                )
                try:
                    data = response.json()
                except ValueError:
                    logger.error(
                        "Telegram notification failed: chat_id=%s; HTTP %s; "
                        "response is not valid JSON",
                        self._safe_detail(self._chat_id), response.status_code,
                    )
                    return
                if not isinstance(data, Mapping):
                    logger.error(
                        "Telegram notification failed: chat_id=%s; HTTP %s; "
                        "expected a JSON object, received %s",
                        self._safe_detail(self._chat_id), response.status_code,
                        type(data).__name__,
                    )
                    return
                if response.status_code == 429 or data.get("error_code") == 429:
                    parameters = data.get("parameters")
                    retry_after = (
                        parameters.get("retry_after") if isinstance(parameters, Mapping) else None
                    )
                    if (
                        type(retry_after) is int
                        and 0 <= retry_after <= TELEGRAM_MAX_RETRY_AFTER
                        and attempt + 1 < TELEGRAM_SEND_ATTEMPTS
                    ):
                        self._next_send_at = time.monotonic() + max(
                            TELEGRAM_SEND_INTERVAL, retry_after
                        )
                        logger.warning(
                            "Telegram rate limit reached; retrying rejected message in %s seconds; %s",
                            max(TELEGRAM_SEND_INTERVAL, retry_after),
                            self._response_detail(response.status_code, data),
                        )
                        continue
                    logger.error(
                        "Telegram notification rejected by rate limit; "
                        "retry limit reached or retry_after missing, invalid, or over %s seconds; %s",
                        TELEGRAM_MAX_RETRY_AFTER,
                        self._response_detail(response.status_code, data),
                    )
                    return
                if response.status_code >= 400:
                    logger.error(
                        "Telegram notification rejected: %s",
                        self._response_detail(response.status_code, data),
                    )
                    return
                response.raise_for_status()
                if data.get("ok") is not True:
                    logger.error(
                        "Telegram notification rejected by API: %s",
                        self._response_detail(response.status_code, data),
                    )
                return
            except requests.RequestException as exc:
                # Do not retry ambiguous failures: Telegram may have accepted the message.
                logger.error(
                    "Telegram notification failed: chat_id=%s; %s: %s",
                    self._safe_detail(self._chat_id), type(exc).__name__, self._safe_detail(exc),
                )
                return
            except (ValueError, AttributeError, TypeError) as exc:
                logger.error(
                    "Telegram notification failed: chat_id=%s; invalid response (%s): %s",
                    self._safe_detail(self._chat_id), type(exc).__name__, self._safe_detail(exc),
                )
                return


def create_notifier(config: Optional[Mapping] = None) -> Optional[Notifier]:
    """Resolve Telegram settings from the environment, then YAML configuration."""
    config = config or {}

    def setting(name: str) -> str:
        for value in (os.environ.get(name), config.get(name.lower()), config.get(name)):
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    token = setting("TELEGRAM_BOT_TOKEN")
    chat_id = setting("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        if token or chat_id:
            logger.warning(
                "Telegram notifications disabled: both TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID are required (environment or YAML configuration)"
            )
        else:
            logger.info("Telegram notifications disabled: no configuration")
        return None
    return TelegramNotifier(token, chat_id)


def terminal_event(node: ProcessNode) -> Optional[str]:
    """Keep native failure categories separate from the MPDS webhook vocabulary."""
    if node.is_killed:
        return "killed"
    if node.is_excepted:
        return "excepted"
    if node.is_failed:
        return "failed"
    if node.is_finished_ok:
        return "finished"
    return None


def format_notification(node: ProcessNode, event: str) -> str:
    title = "✅ AiiDA process finished successfully" if event == "finished" else (
        f"🚨 AiiDA process {event}"
    )
    state = getattr(node, "process_state", None)
    computer = getattr(node, "computer", None)
    fields = [
        ("PK", node.pk),
        ("Process", node.process_label),
        ("Computer", getattr(computer, "label", None)),
        ("State", getattr(state, "value", state)),
        ("Exit status", getattr(node, "exit_status", None)),
    ]
    return title + "\n\n" + "\n".join(
        f"{name}: {value}" for name, value in fields if value is not None and value != ""
    )


class StateNotifications:
    """One attempt per terminal event, for a single daemon per AiiDA profile.

    Persist before sending to avoid duplicates following ambiguous network failures
    or restarts. This favors duplicate prevention over guaranteed delivery.
    """

    def __init__(self, notifier: Notifier, no_commit: bool = False) -> None:
        self.notifier = notifier
        self.no_commit = no_commit
        self._attempted: dict[str, str] = {}

    def observe(self, node: ProcessNode) -> None:
        try:
            event = terminal_event(node)
            if event is None:
                return
            previous = (
                self._attempted.get(node.uuid)
                if self.no_commit
                else node.base.extras.get(EXTRA_NOTIFICATION_STATE, None)
            )
            if previous == event:
                return
            message = format_notification(node, event)
            if self.no_commit:
                self._attempted[node.uuid] = event
            else:
                node.base.extras.set(EXTRA_NOTIFICATION_STATE, event)
            self.notifier.notify(message)
        except Exception:
            # Isolate provider and node/storage failures from calculation monitoring.
            # Do not log exception text: providers may include credentials in it.
            logger.warning("Could not process notification for PK %s", getattr(node, "pk", None))
