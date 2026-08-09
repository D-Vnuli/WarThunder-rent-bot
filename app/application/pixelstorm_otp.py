from datetime import datetime

from app.domain.ports import EphemeralEmailSecretStore
from app.persistence.classified_email_events import ClassifiedEmailRepository


class PixelStormMaintenanceOtpService:
    """One-time correlated Pixel Storm maintenance-login OTP access."""

    def __init__(self, events: ClassifiedEmailRepository, secrets: EphemeralEmailSecretStore) -> None:
        self._events = events
        self._secrets = secrets

    def consume(self, account_id: str, operation_id: str, login_requested_at: datetime, now: datetime) -> str | None:
        claim = self._events.claim_maintenance_login_otp(account_id, operation_id, login_requested_at, now)
        if claim is None:
            return None
        value = self._secrets.consume_once(claim.event_id, claim_token=claim.claim_token, now=now)
        if value is None:
            self._events.mark_claim_unusable(claim)
            return None
        self._events.mark_claim_consumed(claim)
        return value
