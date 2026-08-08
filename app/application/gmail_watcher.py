from datetime import datetime, timedelta
from uuid import uuid4

from app.adapters.email_classifier import EmailClassifier
from app.domain.models import ClassifiedEmailEvent
from app.domain.ports import EphemeralEmailSecretStore, GmailPort
from app.domain.states import EmailMessageType, EmailPayloadState
from app.persistence.classified_email_events import ClassifiedEmailRepository


class GmailWatcher:
    """The sole raw-Gmail consumer; it deduplicates before publishing safe metadata."""

    def __init__(
        self,
        gmail: GmailPort,
        classifier: EmailClassifier,
        events: ClassifiedEmailRepository,
        secrets: EphemeralEmailSecretStore,
        secret_ttl_seconds: int,
        password_reset_ttl_seconds: int,
    ) -> None:
        self._gmail = gmail
        self._classifier = classifier
        self._events = events
        self._secrets = secrets
        self._secret_ttl_seconds = secret_ttl_seconds
        self._password_reset_ttl_seconds = password_reset_ttl_seconds

    def poll_once(self, after: datetime, now: datetime) -> list[ClassifiedEmailEvent]:
        published: list[ClassifiedEmailEvent] = []
        for message in self._gmail.get_new_messages(after=after):
            classification = self._classifier.classify(message)
            event_id = str(uuid4())
            correlation = (
                self._events.expected_rotation_operation(message.routing_account_id, message.received_at)
                if classification.message_type == EmailMessageType.PASSWORD_CHANGE
                else None
            )
            state = (
                EmailPayloadState.AVAILABLE
                if classification.sensitive_payload is not None
                else EmailPayloadState.UNUSABLE_EXPIRED
            )
            event = ClassifiedEmailEvent(
                id=event_id,
                gmail_message_id=message.gmail_message_id,
                message_type=classification.message_type,
                received_at=message.received_at,
                routing_account_id=message.routing_account_id,
                correlation_operation_id=correlation,
                payload_state=state,
            )
            if classification.sensitive_payload is not None:
                ttl = (
                    self._password_reset_ttl_seconds
                    if classification.message_type == EmailMessageType.PASSWORD_CHANGE
                    else self._secret_ttl_seconds
                )
                if not self._secrets.put(
                    event_id, classification.sensitive_payload, expires_at=now + timedelta(seconds=ttl)
                ):
                    continue
            if not self._events.store_event(event, now):
                self._secrets.discard(event_id)
                continue
            published.append(event)
        return published
