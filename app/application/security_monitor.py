from datetime import datetime

from app.domain.models import ClassifiedEmailEvent
from app.domain.states import EmailMessageType
from app.persistence.repositories import Repository


class SecurityMonitor:
    """Fail closed on uncorrelated security mail during an active rental."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def handle(self, event: ClassifiedEmailEvent, now: datetime) -> bool:
        if event.message_type == EmailMessageType.LOGIN_OTP:
            return False
        if (
            event.message_type == EmailMessageType.PASSWORD_CHANGE
            and event.correlation_operation_id is not None
        ):
            return False
        if event.routing_account_id is None:
            return False
        return self._repository.record_active_security_event(
            event.routing_account_id, event.message_type, event.gmail_message_id, now
        )
