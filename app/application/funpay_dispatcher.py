from datetime import datetime

from app.application.otp_service import OTPService
from app.application.rental_manager import RentalManager
from app.domain.funpay import FunPayEventType, FunPayHealth
from app.domain.models import OrderInput
from app.domain.notifications import OwnerNotification
from app.domain.ports import FunPayPort, OwnerNotifier
from app.persistence.funpay_events import FunPayEventRepository


class FunPayEventPoller:
    """Durably ingest normalized external events before any business action."""

    def __init__(
        self,
        funpay: FunPayPort,
        events: FunPayEventRepository,
        owner_notifier: OwnerNotifier | None = None,
    ) -> None:
        self._funpay = funpay
        self._events = events
        self._owner_notifier = owner_notifier
        self._notified_health: set[FunPayHealth] = set()

    def poll_once(self, after: datetime, now: datetime) -> int:
        health = self._funpay.health()
        if health != FunPayHealth.READY:
            if health not in self._notified_health and self._owner_notifier is not None:
                self._owner_notifier.notify(
                    OwnerNotification(f"FUNPAY_{health}", f"POLL:{health}", now)
                )
                self._notified_health.add(health)
            return 0
        stored = 0
        for event in self._funpay.poll_events(after=after):
            stored += int(self._events.ingest(event, now))
        return stored


class FunPayEventDispatcher:
    """Claims durable events; credentials and OTP are never written to the event store."""

    def __init__(
        self,
        events: FunPayEventRepository,
        rentals: RentalManager,
        otp_service: OTPService,
        funpay: FunPayPort,
        owner_notifier: OwnerNotifier | None = None,
    ) -> None:
        self._events = events
        self._rentals = rentals
        self._otp_service = otp_service
        self._funpay = funpay
        self._owner_notifier = owner_notifier

    def dispatch_pending(self, now: datetime) -> int:
        completed = 0
        for claim in self._events.claim_events(now):
            event = claim.event
            if event.event_type == FunPayEventType.PAID_ORDER:
                outcome = self._handle_paid_order(event, now)
            elif event.event_type == FunPayEventType.BUYER_MESSAGE:
                outcome = "processed" if self._handle_buyer_message(event, now) else "failed"
            else:
                outcome = "processed"
                self._notify("UNKNOWN_FUNPAY_EVENT", event.external_event_id, now, event_id=event.id)
            if outcome == "processed":
                completed += int(self._events.mark_processed(event.id, claim.claim_token, now))
            elif outcome == "retryable":
                self._events.mark_retryable(event.id, claim.claim_token, now)
            else:
                self._events.mark_failed_closed(event.id, claim.claim_token, now)
        return completed

    def _handle_paid_order(self, event, now: datetime) -> str:
        if self._funpay.health() != FunPayHealth.READY:
            self._notify_health(self._funpay.health(), event, now)
            return "retryable"
        if (
            event.funpay_order_id is None
            or event.buyer_id is None
            or event.tariff_code is None
            or event.duration_seconds is None
        ):
            return "failed"
        self._rentals.accept_order(
            OrderInput(event.funpay_order_id, event.buyer_id, event.tariff_code, event.duration_seconds), now
        )
        return "processed"

    def _notify_health(self, health: FunPayHealth, event, now: datetime) -> None:
        self._notify(f"FUNPAY_{health}", f"FUNPAY:{health}:{event.external_event_id}", now, event_id=event.id)

    def _notify(self, category: str, correlation_id: str, now: datetime, *, event_id: str | None = None) -> None:
        if self._owner_notifier is not None:
            self._owner_notifier.notify(OwnerNotification(category, correlation_id, now, event_id=event_id))

    def _handle_buyer_message(self, event, now: datetime) -> bool:
        if (event.message_text or "").strip().casefold() != "код":
            return True
        if self._funpay.health() != FunPayHealth.READY:
            return False
        if event.funpay_order_id is None or event.buyer_id is None:
            return False
        rental = self._events.rental_for_order(event.funpay_order_id, event.buyer_id)
        if rental is None or not self._events.account_is_active(rental.id):
            return True
        del now
        return self._events.create_send_otp(event, rental, event.received_at)
