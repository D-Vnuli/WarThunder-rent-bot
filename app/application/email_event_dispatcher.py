from datetime import datetime

from app.application.security_monitor import SecurityMonitor
from app.persistence.classified_email_events import ClassifiedEmailRepository


class EmailEventDispatcher:
    """Durably dispatches security events after Gmail ingestion commits."""

    def __init__(self, events: ClassifiedEmailRepository, security_monitor: SecurityMonitor) -> None:
        self._events = events
        self._security_monitor = security_monitor

    def dispatch_pending(self, now: datetime) -> int:
        processed = 0
        for claim in self._events.claim_security_events(now):
            self._security_monitor.handle(claim.event, now)
            if self._events.mark_security_processed(claim.event.id, claim.claim_token):
                processed += 1
        return processed
