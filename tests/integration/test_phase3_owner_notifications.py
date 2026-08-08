from datetime import UTC, datetime, timedelta

from app.adapters.fake import (
    FakeEphemeralEmailSecretStore,
    FakeFunPayAdapter,
    FakeGaijinController,
    FakeOwnerNotifier,
    FakeSecureStore,
)
from app.application.funpay_dispatcher import FunPayEventDispatcher
from app.application.otp_service import OTPService
from app.application.rental_manager import RentalManager
from app.domain.funpay import FunPayEvent, FunPayEventType, FunPayHealth
from app.domain.models import OrderInput
from app.domain.notifications import OwnerNotification
from app.persistence.classified_email_events import ClassifiedEmailRepository
from app.persistence.funpay_events import FunPayEventRepository
from tests.helpers import create_test_account


def _dispatcher(repository, funpay, notifier):
    events = FunPayEventRepository(repository.db)
    manager = RentalManager(repository, funpay, FakeGaijinController(), FakeSecureStore(), owner_notifier=notifier)
    otp = OTPService(ClassifiedEmailRepository(repository.db), FakeEphemeralEmailSecretStore(), 120, 0)
    return events, FunPayEventDispatcher(events, manager, otp, funpay, owner_notifier=notifier)


def test_blocked_order_and_health_states_notify_owner(core, now):
    repository, _, funpay, _ = core
    notifier = FakeOwnerNotifier()
    manager = RentalManager(repository, funpay, FakeGaijinController(), FakeSecureStore(), owner_notifier=notifier)
    assert not manager.accept_order(OrderInput("blocked", "buyer", "1H", 60), now).accepted
    assert notifier.notifications[-1].category == "PAID_ORDER_BLOCKED"

    events, dispatcher = _dispatcher(repository, funpay, notifier)
    for health, event_id in ((FunPayHealth.AUTH_REQUIRED, "auth"), (FunPayHealth.DEGRADED, "degraded"), (FunPayHealth.UNAVAILABLE, "unavailable")):
        assert events.ingest(FunPayEvent(event_id, FunPayEventType.PAID_ORDER, now, event_id, "buyer", tariff_code="1H", duration_seconds=60), now)
        funpay.set_health(health)
        dispatcher.dispatch_pending(now)
    assert {item.category for item in notifier.notifications} >= {"FUNPAY_AUTH_REQUIRED", "FUNPAY_DEGRADED", "FUNPAY_UNAVAILABLE"}


def test_lot_failures_and_unknown_event_notify_owner(core, now):
    repository, _, funpay, _ = core
    notifier = FakeOwnerNotifier()
    manager = RentalManager(repository, funpay, FakeGaijinController(), FakeSecureStore(), owner_notifier=notifier)
    account_id = create_test_account(repository, funpay, "WT01", now)
    funpay.fail_next.add("disable")
    manager.accept_order(OrderInput("disable", "buyer", "1H", 60), now)
    manager.run_operations(now)
    assert any(item.category == "DISABLE_LOTS_VERIFICATION_FAILED" for item in notifier.notifications)

    enable_funpay = FakeFunPayAdapter()
    enable_account = create_test_account(repository, enable_funpay, "WT02", now)
    enable_secrets = FakeSecureStore()
    enable_secrets.set_current_credentials(enable_account, "safe-login", "safe-password")
    enable_manager = RentalManager(repository, enable_funpay, FakeGaijinController(), enable_secrets, owner_notifier=notifier)
    enable_manager.accept_order(OrderInput("enable", "buyer-2", "1H", 1), now)
    enable_manager.run_operations(now)
    enable_manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    enable_manager.run_operations(now + timedelta(seconds=2))
    enable_manager.run_operations(now + timedelta(seconds=2))
    enable_funpay.fail_next.add("enable")
    enable_manager.run_operations(now + timedelta(seconds=2))
    assert any(item.category == "ENABLE_LOTS_VERIFICATION_FAILED" for item in notifier.notifications)

    events, dispatcher = _dispatcher(repository, funpay, notifier)
    assert events.ingest(FunPayEvent("unknown", FunPayEventType.UNKNOWN, now), now)
    dispatcher.dispatch_pending(now)
    assert any(item.category == "UNKNOWN_FUNPAY_EVENT" for item in notifier.notifications)
    assert account_id and enable_account


def test_owner_notification_is_safe_and_deduplicated():
    notifier = FakeOwnerNotifier()
    notification = OwnerNotification(
        "SAFE", "same", datetime.now(UTC), safe_error_category="UNAVAILABLE"
    )
    notifier.notify(notification)
    notifier.notify(notification)
    assert len(notifier.notifications) == 1
    rendered = repr(notifier.notifications[0])
    for marker in ("PASSWORD", "OTP", "cookie", "token", "login"):
        assert marker not in rendered
