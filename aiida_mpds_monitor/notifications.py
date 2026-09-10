"""Optional notification delivery and persistent terminal-event deduplication."""

import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Callable, Mapping, Optional

import requests
from aiida.orm import ProcessNode

logger = logging.getLogger(__name__)
EXTRA_NOTIFICATION_STATE = "monitor_notification_state"


class Notifier(ABC):
    @abstractmethod
    def notify(self, message: str) -> None:
        """Attempt delivery of a message."""

    def start_command_polling(self, report: Callable[[], str]) -> None:
        """Optionally start serving interactive commands."""

    def stop_command_polling(self) -> None:
        """Optionally stop serving interactive commands."""

    def poll_commands(self, report: Callable[[], str], long_poll_timeout: int = 0) -> bool:
        """Optionally serve requests for a current-process report."""
        return True


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._offset = 0
        self._command_thread: Optional[threading.Thread] = None
        self._stop_commands = threading.Event()

    def notify(self, message: str) -> None:
        # Use a conservative chunk size, including for non-BMP Unicode characters.
        for start in range(0, len(message), 2000):
            self._send(message[start:start + 2000])

    def _send(self, message: str, keyboard: bool = False) -> None:
        try:
            payload = {"chat_id": self._chat_id, "text": message}
            if keyboard:
                payload["reply_markup"] = {
                    "keyboard": [[{"text": "Текущие расчёты"}]],
                    "resize_keyboard": True,
                }
            response = requests.post(
                self._url,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            if response.json().get("ok") is not True:
                logger.warning("Telegram notification rejected by API")
        except (requests.RequestException, ValueError, AttributeError):
            # Request exceptions can contain the URL (and therefore the bot token).
            logger.warning("Telegram notification failed (HTTP, network, or invalid response)")

    def start_command_polling(self, report: Callable[[], str]) -> None:
        """Serve commands independently of the slower AiiDA monitor loop."""
        if self._command_thread is not None and self._command_thread.is_alive():
            return
        self._stop_commands.clear()
        self._command_thread = threading.Thread(
            target=self._command_loop,
            args=(report,),
            name="telegram-command-poller",
            daemon=True,
        )
        self._command_thread.start()

    def stop_command_polling(self) -> None:
        self._stop_commands.set()
        if self._command_thread is not None:
            self._command_thread.join(timeout=2)

    def _command_loop(self, report: Callable[[], str]) -> None:
        while not self._stop_commands.is_set():
            if not self.poll_commands(report, long_poll_timeout=10):
                self._stop_commands.wait(1)

    def poll_commands(self, report: Callable[[], str], long_poll_timeout: int = 0) -> bool:
        """Read commands using Telegram long polling; reject requests from other chats."""
        try:
            response = requests.post(
                self._url.replace("/sendMessage", "/getUpdates"),
                json={
                    "offset": self._offset,
                    "timeout": long_poll_timeout,
                    "allowed_updates": ["message"],
                },
                timeout=long_poll_timeout + 5,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("ok") is not True:
                logger.warning("Telegram command polling rejected by API")
                return False
            for update in data["result"]:
                update_id = update["update_id"]
                if update_id < self._offset:
                    continue
                self._offset = update_id + 1
                message = update.get("message", {})
                if str(message.get("chat", {}).get("id")) != self._chat_id:
                    continue
                text = message.get("text", "").strip()
                command = text.split("@", 1)[0]
                if command in ("/start", "/help"):
                    self._send("Нажмите «Текущие расчёты» или отправьте /running.", keyboard=True)
                elif command == "/running" or text == "Текущие расчёты":
                    self.notify(report())
            return True
        except Exception:
            logger.warning("Telegram command polling failed; continuing monitoring")
            return False


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
