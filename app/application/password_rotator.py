from datetime import datetime

from app.domain.ports import EphemeralEmailSecretStore
from app.persistence.classified_email_events import ClassifiedEmailRepository


class PasswordRotator:
    """Consumes only a password-reset event correlated by GmailWatcher to a rotation operation."""

    def __init__(self, events: ClassifiedEmailRepository, secrets: EphemeralEmailSecretStore) -> None:
        self._events = events
        self._secrets = secrets

    def consume_expected_reset_url(self, rotation_operation_id: str, now: datetime) -> str | None:
        claim = self._events.claim_expected_password_change(rotation_operation_id, now)
        if claim is None:
            return None
        reset_url = self._secrets.consume_once(claim.event_id, claim_token=claim.claim_token, now=now)
        if reset_url is None:
            self._events.mark_claim_unusable(claim)
            return None
        self._events.mark_claim_consumed(claim)
        return reset_url
