from datetime import timedelta

from sqlalchemy import func, select

from app.adapters.fake import (
    FakeEphemeralEmailSecretStore,
    FakeFunPayAdapter,
    FakeGaijinController,
    FakeSecureStore,
)
from app.application.funpay_dispatcher import FunPayEventDispatcher
from app.application.otp_service import OTPService
from app.application.rental_manager import RentalManager
from app.domain.funpay import FunPayEvent, FunPayEventType, FunPayHealth, FunPayProcessingStatus
from app.persistence.classified_email_events import ClassifiedEmailRepository
from app.persistence.database import Database
from app.persistence.funpay_events import FunPayEventRepository
from app.persistence.models import FunPayEventRow, OrderRow, RentalRow
from app.persistence.repositories import Repository
from tests.helpers import create_test_account


def _paid(event_id: str, now, order_id: str = "paid") -> FunPayEvent:
    return FunPayEvent(event_id, FunPayEventType.PAID_ORDER, now, order_id, "buyer", tariff_code="1H", duration_seconds=60)


def _dispatcher(repository, funpay):
    events = FunPayEventRepository(repository.db)
    manager = RentalManager(repository, funpay, FakeGaijinController(), FakeSecureStore())
    otp = OTPService(ClassifiedEmailRepository(repository.db), FakeEphemeralEmailSecretStore(), 120, 0)
    return events, FunPayEventDispatcher(events, manager, otp, funpay)


def _status(events, event_id: str) -> str:
    with events.db.session() as session:
        return session.scalar(select(FunPayEventRow.processing_status).where(FunPayEventRow.external_event_id == event_id))


def test_paid_order_auth_required_is_retryable(core, now):
    repository, _, funpay, _ = core
    create_test_account(repository, funpay, "WT01", now)
    events, dispatcher = _dispatcher(repository, funpay)
    assert events.ingest(_paid("auth", now), now)
    funpay.set_health(FunPayHealth.AUTH_REQUIRED)
    assert dispatcher.dispatch_pending(now) == 0
    assert _status(events, "auth") == FunPayProcessingStatus.RETRY_PENDING


def test_paid_order_degraded_and_unavailable_are_retryable(core, now):
    repository, _, funpay, _ = core
    create_test_account(repository, funpay, "WT01", now)
    events, dispatcher = _dispatcher(repository, funpay)
    for health, event_id in ((FunPayHealth.DEGRADED, "degraded"), (FunPayHealth.UNAVAILABLE, "unavailable")):
        assert events.ingest(_paid(event_id, now, event_id), now)
        funpay.set_health(health)
        assert dispatcher.dispatch_pending(now) == 0
        assert _status(events, event_id) == FunPayProcessingStatus.RETRY_PENDING


def test_paid_order_unavailable_is_retryable(core, now):
    repository, _, funpay, _ = core
    create_test_account(repository, funpay, "WT01", now)
    events, dispatcher = _dispatcher(repository, funpay)
    assert events.ingest(_paid("unavailable-explicit", now), now)
    funpay.set_health(FunPayHealth.UNAVAILABLE)
    assert dispatcher.dispatch_pending(now) == 0
    assert _status(events, "unavailable-explicit") == FunPayProcessingStatus.RETRY_PENDING


def test_paid_order_retry_respects_delay_and_creates_one_order(core, now):
    repository, _, funpay, _ = core
    create_test_account(repository, funpay, "WT01", now)
    events, dispatcher = _dispatcher(repository, funpay)
    assert events.ingest(_paid("retry", now), now)
    funpay.set_health(FunPayHealth.AUTH_REQUIRED)
    dispatcher.dispatch_pending(now)
    funpay.set_health(FunPayHealth.READY)
    assert dispatcher.dispatch_pending(now + timedelta(seconds=29)) == 0
    assert dispatcher.dispatch_pending(now + timedelta(seconds=30)) == 1
    with repository.db.session() as session:
        assert session.scalar(select(func.count()).select_from(OrderRow)) == 1
        assert session.scalar(select(func.count()).select_from(RentalRow)) == 1


def test_paid_order_retry_after_restart_new_dispatcher(tmp_path, now):
    path = tmp_path / "retry.db"
    db1 = Database(f"sqlite:///{path.as_posix()}")
    db1.create_schema()
    repo1 = Repository(db1)
    funpay1 = FakeFunPayAdapter()
    create_test_account(repo1, funpay1, "WT01", now)
    events1, dispatcher1 = _dispatcher(repo1, funpay1)
    assert events1.ingest(_paid("restart", now), now)
    funpay1.set_health(FunPayHealth.AUTH_REQUIRED)
    dispatcher1.dispatch_pending(now)
    db1.engine.dispose()

    db2 = Database(f"sqlite:///{path.as_posix()}")
    repo2 = Repository(db2)
    funpay2 = FakeFunPayAdapter()
    funpay2.set_health(FunPayHealth.READY)
    events2, dispatcher2 = _dispatcher(repo2, funpay2)
    assert dispatcher2.dispatch_pending(now + timedelta(seconds=30)) == 1
    with db2.session() as session:
        assert session.scalar(select(func.count()).select_from(OrderRow)) == 1
        assert session.scalar(select(func.count()).select_from(RentalRow)) == 1


def test_paid_order_retry_creates_exactly_one_order_and_rental(core, now):
    repository, _, funpay, _ = core
    create_test_account(repository, funpay, "WT01", now)
    events, dispatcher = _dispatcher(repository, funpay)
    assert events.ingest(_paid("one", now), now)
    funpay.set_health(FunPayHealth.AUTH_REQUIRED)
    dispatcher.dispatch_pending(now)
    funpay.set_health(FunPayHealth.READY)
    assert dispatcher.dispatch_pending(now + timedelta(seconds=30)) == 1
    assert dispatcher.dispatch_pending(now + timedelta(seconds=60)) == 0
    with repository.db.session() as session:
        assert session.scalar(select(func.count()).select_from(OrderRow)) == 1
        assert session.scalar(select(func.count()).select_from(RentalRow)) == 1


def test_malformed_paid_order_remains_failed_closed(core, now):
    repository, _, funpay, _ = core
    events, dispatcher = _dispatcher(repository, funpay)
    assert events.ingest(FunPayEvent("bad", FunPayEventType.PAID_ORDER, now), now)
    assert dispatcher.dispatch_pending(now) == 0
    assert _status(events, "bad") == FunPayProcessingStatus.FAILED_CLOSED
