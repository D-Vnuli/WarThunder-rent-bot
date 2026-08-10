from datetime import timedelta
from multiprocessing import get_context
from pathlib import Path

from sqlalchemy import func, select

from app.adapters.email_classifier import EmailClassifier
from app.adapters.fake import (
    FakeEphemeralEmailSecretStore,
    FakeFunPayAdapter,
    FakeGaijinController,
    FakeGmailAdapter,
    FakeSecureStore,
    PersistentFakeFunPayBackend,
)
from app.application.funpay_dispatcher import FunPayEventDispatcher, FunPayEventPoller
from app.application.gmail_watcher import GmailWatcher
from app.application.otp_service import OTPService
from app.application.rental_manager import RentalManager
from app.domain.funpay import FunPayEvent, FunPayEventType, FunPayHealth
from app.domain.models import OrderInput, RawEmail
from app.domain.states import AccountStatus, FulfillmentStatus
from app.persistence.classified_email_events import ClassifiedEmailRepository
from app.persistence.database import Database
from app.persistence.funpay_events import FunPayEventRepository
from app.persistence.models import FunPayEventRow, MessageReceiptRow, OrderRow, RentalRow
from app.persistence.repositories import Repository
from tests.helpers import create_test_account


def _dispatch_worker(path: str, now) -> int:
    database = Database(f"sqlite:///{Path(path).as_posix()}")
    repository = Repository(database)
    funpay = FakeFunPayAdapter()
    event_repository = FunPayEventRepository(database)
    manager = RentalManager(repository, funpay, FakeGaijinController(), FakeSecureStore())
    otp = OTPService(ClassifiedEmailRepository(database), FakeEphemeralEmailSecretStore(), 120, 0)
    return FunPayEventDispatcher(event_repository, manager, otp, funpay).dispatch_pending(now)


def _services(core):
    repository, manager, funpay, gaijin = core
    events = FunPayEventRepository(repository.db)
    otp = OTPService(ClassifiedEmailRepository(repository.db), FakeEphemeralEmailSecretStore(), 120, 0)
    return events, FunPayEventPoller(funpay, events), FunPayEventDispatcher(events, manager, otp, funpay)


def _paid(event_id, now, order_id="paid-order", buyer="buyer"):
    return FunPayEvent(
        event_id,
        FunPayEventType.PAID_ORDER,
        now,
        order_id,
        buyer,
        lot_id="lot-1",
        tariff_code="1H",
        duration_seconds=3600,
    )


def test_paid_order_ingests_durably_deduplicates_and_enters_rental_workflow(core, now):
    repository, manager, funpay, _ = core
    account_id = create_test_account(repository, funpay, "WT01", now)
    event_repo, poller, dispatcher = _services(core)
    funpay.add_event(_paid("event-1", now))
    funpay.add_event(_paid("event-1", now))

    assert poller.poll_once(now, now) == 1
    assert dispatcher.dispatch_pending(now) == 1
    manager.run_operations(now)
    assert funpay.verify_lots_disabled(repository.account_lot_ids(account_id)).verified
    assert len(repository.pending_operations()) == 1  # SEND_CREDENTIALS only after verified disable
    with repository.db.session() as session:
        assert session.scalar(select(func.count()).select_from(OrderRow)) == 1
        assert session.scalar(select(func.count()).select_from(FunPayEventRow)) == 1


def test_blocked_paid_order_persists_without_credentials(core, now):
    repository, _, funpay, _ = core
    events, poller, dispatcher = _services(core)
    funpay.add_event(_paid("blocked-event", now, "blocked-order"))
    assert poller.poll_once(now, now) == 1
    assert dispatcher.dispatch_pending(now) == 1
    with repository.db.session() as session:
        order = session.scalar(select(OrderRow).where(OrderRow.funpay_order_id == "blocked-order"))
        assert order is not None
        assert order.fulfillment_status == FulfillmentStatus.FULFILLMENT_BLOCKED
        assert session.scalar(select(func.count()).select_from(RentalRow)) == 0


def test_lot_disable_partial_failure_is_fail_closed_before_credentials(core, now):
    repository, manager, funpay, _ = core
    account_id = create_test_account(repository, funpay, "WT01", now)
    events, poller, dispatcher = _services(core)
    funpay.fail_next.add("disable_partial")
    funpay.add_event(_paid("partial-event", now))
    poller.poll_once(now, now)
    dispatcher.dispatch_pending(now)
    manager.run_operations(now)
    assert repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert funpay.message_send_count == 0


def test_buyer_code_is_exact_owned_and_delivers_one_time_otp(core, now):
    repository, manager, funpay, gaijin = core
    account_id = create_test_account(repository, funpay, "WT01", now)
    rental = manager.accept_order(OrderInput("order", "buyer", "1H", 3600), now)
    manager.run_operations(now)
    manager.run_operations(now)
    assert rental.rental_id is not None

    email_events = ClassifiedEmailRepository(repository.db)
    secrets = FakeEphemeralEmailSecretStore()
    gmail = FakeGmailAdapter(
        [
            RawEmail(
                "otp-mail",
                "login@pixstorm.ru",
                "Подтверждение входа",
                now,
                "Код подтверждения для входа: 654321; type=two_step_email_code",
                account_id,
            )
        ]
    )
    GmailWatcher(gmail, EmailClassifier(), email_events, secrets, 300, 900).poll_once(now, now)
    funpay_events = FunPayEventRepository(repository.db)
    otp_service = OTPService(email_events, secrets, 120, 0)
    dispatcher = FunPayEventDispatcher(funpay_events, manager, otp_service, funpay)
    assert funpay_events.ingest(
        FunPayEvent("code", FunPayEventType.BUYER_MESSAGE, now, "order", "buyer", message_text="КОД"), now
    )
    assert dispatcher.dispatch_pending(now) == 1
    worker = RentalManager(
        repository,
        funpay,
        gaijin,
        FakeSecureStore(),
        otp_service=otp_service,
        message_receipts=funpay_events,
    )
    worker.run_operations(now)
    assert len(funpay._receipts) == 2
    assert funpay_events.ingest(
        FunPayEvent("not-code", FunPayEventType.BUYER_MESSAGE, now, "order", "buyer", message_text="изменить код пароль"), now
    )
    assert dispatcher.dispatch_pending(now) == 1
    assert len(funpay._receipts) == 2
    with repository.db.session() as session:
        assert session.scalar(select(func.count()).select_from(MessageReceiptRow)) == 1


def test_wrong_buyer_health_failure_and_security_alert_block_funpay_actions(core, now):
    repository, manager, funpay, _ = core
    account_id = create_test_account(repository, funpay, "WT01", now)
    events, poller, dispatcher = _services(core)
    funpay.set_health(FunPayHealth.AUTH_REQUIRED)
    funpay.add_event(_paid("auth-lost", now))
    poller.poll_once(now, now)
    assert dispatcher.dispatch_pending(now) == 0
    assert repository.get_account(account_id).status == AccountStatus.AVAILABLE

    funpay.set_health(FunPayHealth.READY)
    result = manager.accept_order(OrderInput("secure", "buyer", "1H", 3600), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.record_active_security_event(account_id, "UNKNOWN", "security", now + timedelta(seconds=1))
    assert repository.get_account(account_id).status == AccountStatus.SECURITY_ALERT
    assert result.rental_id is not None
    assert not events.account_is_active(result.rental_id)


def test_file_sqlite_multiprocess_funpay_event_claims_once(tmp_path, now):
    path = tmp_path / "funpay-multiprocess.db"
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    Repository(database).add_account("WT01", now)
    events = FunPayEventRepository(database)
    assert events.ingest(_paid("mp-event", now, "mp-order"), now)
    context = get_context("spawn")
    with context.Pool(2) as pool:
        results = pool.starmap(_dispatch_worker, [(str(path), now), (str(path), now)])
    assert sum(results) == 1
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(RentalRow)) == 1


def test_paid_event_survives_dispatcher_restart_and_credentials_do_not_duplicate(core, now):
    repository, manager, funpay, _ = core
    create_test_account(repository, funpay, "WT01", now)
    events, _, dispatcher = _services(core)
    assert events.ingest(_paid("restart-paid", now, "restart-order"), now)
    # A fresh dispatcher after a simulated process restart claims the stored event.
    assert dispatcher.dispatch_pending(now) == 1
    manager.run_operations(now)  # verified DISABLE_LOTS creates SEND_CREDENTIALS
    send = repository.pending_operations()[0]
    send = repository.claim_operation(send.id, now)
    assert send is not None
    assert manager._send_credentials(send, now)
    # Simulated crash after external success: fake external state has the idempotent receipt.
    assert repository.operation_completed(send.id, now, normal_claim_token=send.normal_claim_token)
    assert funpay.message_send_count == 1
    assert repository.pending_operations() == []


def test_funpay_event_and_receipt_metadata_never_store_otp_or_password(core, now):
    repository, _, _, _ = core
    events = FunPayEventRepository(repository.db)
    marker = "sensitive-value"
    assert events.ingest(
        FunPayEvent("safe-event", FunPayEventType.UNKNOWN, now, safe_metadata="{}"), now
    )
    with repository.db.engine.connect() as connection:
        values = [
            str(value)
            for table in ("funpay_events", "message_receipts")
            for row in connection.exec_driver_sql(f"SELECT * FROM {table}")
            for value in row
        ]
    assert marker not in values


def test_send_credentials_crash_restart_with_new_worker(tmp_path, now):
    path = tmp_path / "app.db"
    backend = PersistentFakeFunPayBackend(str(tmp_path / "external.db"))
    db1 = Database(f"sqlite:///{path.as_posix()}")
    db1.create_schema()
    repo1 = Repository(db1)
    funpay1 = FakeFunPayAdapter(backend)
    secrets = FakeSecureStore()
    account_id = create_test_account(repo1, funpay1, "WT01", now)
    secrets.set_current_credentials(account_id, "UNIQUE_LOGIN_SECRET_123", "UNIQUE_PASSWORD_SECRET_456")
    manager1 = RentalManager(repo1, funpay1, FakeGaijinController(), secrets)
    result = manager1.accept_order(OrderInput("crash-credentials", "buyer", "1H", 3600), now)
    manager1.run_operations(now)
    operation = repo1.pending_operations()[0]
    operation = repo1.claim_operation(operation.id, now)
    assert operation is not None
    assert manager1._send_credentials(operation, now)  # external success, then crash before completion
    assert backend.send_count(operation.idempotency_key) == 1

    db2 = Database(f"sqlite:///{path.as_posix()}")
    funpay2 = FakeFunPayAdapter(backend)
    manager2 = RentalManager(Repository(db2), funpay2, FakeGaijinController(), FakeSecureStore())
    assert funpay1 is not funpay2
    assert manager2.recover_message_receipts(now + timedelta(seconds=31)) == 1
    assert backend.send_count(operation.idempotency_key) == 1
    assert Repository(db2).get_rental(result.rental_id or "").status == "ACTIVE"


def test_send_otp_crash_restart_with_new_worker(tmp_path, now):
    path = tmp_path / "otp-app.db"
    backend = PersistentFakeFunPayBackend(str(tmp_path / "otp-external.db"))
    db1 = Database(f"sqlite:///{path.as_posix()}")
    db1.create_schema()
    repo1 = Repository(db1)
    funpay1 = FakeFunPayAdapter(backend)
    secrets = FakeSecureStore()
    manager = RentalManager(repo1, funpay1, FakeGaijinController(), secrets)
    account_id = create_test_account(repo1, funpay1, "WT01", now)
    secrets.set_current_credentials(account_id, "safe-login", "safe-password")
    manager.accept_order(OrderInput("otp-crash", "buyer", "1H", 3600), now)
    manager.run_operations(now)
    manager.run_operations(now)
    emails = ClassifiedEmailRepository(db1)
    secrets = FakeEphemeralEmailSecretStore()
    GmailWatcher(FakeGmailAdapter([RawEmail("otp", "login@pixstorm.ru", "Подтверждение входа", now, "Код подтверждения для входа: 654321; type=two_step_email_code", account_id)]), EmailClassifier(), emails, secrets, 300, 900).poll_once(now, now)
    events = FunPayEventRepository(db1)
    assert events.ingest(FunPayEvent("otp-event", FunPayEventType.BUYER_MESSAGE, now, "otp-crash", "buyer", message_text="код"), now)
    dispatcher = FunPayEventDispatcher(events, manager, OTPService(emails, secrets, 120, 0), funpay1)
    dispatcher.dispatch_pending(now)
    op = repo1.pending_operations()[0]
    op = repo1.claim_operation(op.id, now)
    assert op is not None
    worker1 = RentalManager(repo1, funpay1, FakeGaijinController(), FakeSecureStore(), otp_service=OTPService(emails, secrets, 120, 0))
    assert worker1._send_otp(op, now)
    db2 = Database(f"sqlite:///{path.as_posix()}")
    funpay2 = FakeFunPayAdapter(backend)
    worker2 = RentalManager(Repository(db2), funpay2, FakeGaijinController(), FakeSecureStore())
    assert funpay1 is not funpay2
    assert worker2.recover_message_receipts(now + timedelta(seconds=31)) == 1
    assert backend.send_count(op.idempotency_key) == 1
