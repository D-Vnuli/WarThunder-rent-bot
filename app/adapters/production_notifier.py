"""Production owner-notification boundary.

The adapter deliberately owns no Telegram client.  Deployment injects a
transport, while tests use a recording transport without any network access.
"""

from typing import Protocol

from app.domain.notifications import OwnerNotification


class TelegramOwnerTransport(Protocol):
    def send_owner_notification(self, category: str, safe_context: str) -> None: ...


class TelegramOwnerNotifier:
    """Explicit production transport boundary; notification failures are caller-isolated."""

    production_safe = True

    def __init__(self, transport: TelegramOwnerTransport) -> None:
        self._transport = transport

    def notify(self, notification: OwnerNotification) -> None:
        context = notification.safe_error_category or notification.correlation_id
        self._transport.send_owner_notification(notification.category, context)
