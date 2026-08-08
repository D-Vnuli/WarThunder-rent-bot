from datetime import datetime

from app.domain.ports import EphemeralEmailSecretStore
from app.persistence.classified_email_events import ClassifiedEmailRepository


class OTPService:
    """Returns an OTP only after the event store atomically claims it."""

    def __init__(
        self,
        events: ClassifiedEmailRepository,
        secrets: EphemeralEmailSecretStore,
        lookback_seconds: int,
        min_request_interval_seconds: int,
    ) -> None:
        self._events = events
        self._secrets = secrets
        self._lookback_seconds = lookback_seconds
        self._min_request_interval_seconds = min_request_interval_seconds

    def request_otp(
        self, rental_id: str, buyer_id: str, request_started_at: datetime, now: datetime
    ) -> str | None:
        claim = self._events.claim_login_otp(
            rental_id,
            buyer_id,
            request_started_at,
            now,
            self._lookback_seconds,
            self._min_request_interval_seconds,
        )
        if claim is None:
            return None
        otp = self._secrets.consume_once(claim.event_id, claim_token=claim.claim_token, now=now)
        if otp is None:
            self._events.mark_claim_unusable(claim)
            return None
        self._events.mark_claim_consumed(claim)
        return otp
