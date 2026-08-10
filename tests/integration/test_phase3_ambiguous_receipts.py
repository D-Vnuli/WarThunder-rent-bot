from datetime import timedelta

from app.adapters.fake import FakeGaijinController, FakeOwnerNotifier, FakeSecureStore
from app.application.rental_manager import RentalManager
from app.application.startup_reconciliation import StartupReconciliation
from app.domain.models import OrderInput
from app.domain.states import AccountStatus, OperationKind, OperationStatus, RentalStatus
from app.persistence.models import OperationRow
from tests.helpers import create_test_account


def test_send_credentials_ambiguous_receipt_never_resends_and_notifies_owner(core, now):
    repository, _, funpay, _ = core
    notifier = FakeOwnerNotifier()
    account_id = create_test_account(repository, funpay, "WT01", now)
    secrets = FakeSecureStore()
    secrets.set_current_credentials(account_id, "safe-login", "safe-password")
    manager = RentalManager(repository, funpay, FakeGaijinController(), secrets, owner_notifier=notifier)
    manager.accept_order(OrderInput("credentials", "buyer", "1H", 60), now)
    manager.run_operations(now)
    operation = repository.pending_operations()[0]
    operation = repository.claim_operation(operation.id, now)
    assert operation is not None
    funpay.fail_next.add("message_ambiguous")
    assert not manager._send_credentials(operation, now)
    assert funpay.message_send_count == 1
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(seconds=31))
    assert funpay.message_send_count == 1
    assert repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert any(item.category == "SEND_CREDENTIALS_AMBIGUOUS" for item in notifier.notifications)


def test_send_otp_ambiguous_receipt_never_resends_and_notifies_owner(core, now):
    repository, _, funpay, _ = core
    notifier = FakeOwnerNotifier()
    account_id = create_test_account(repository, funpay, "WT01", now)
    secrets = FakeSecureStore()
    secrets.set_current_credentials(account_id, "safe-login", "safe-password")
    manager = RentalManager(repository, funpay, FakeGaijinController(), secrets, owner_notifier=notifier)
    started = manager.accept_order(OrderInput("otp", "buyer", "1H", 60), now)
    manager.run_operations(now)
    manager.run_operations(now)
    rental = repository.get_rental(started.rental_id or "")
    with repository.db.session() as session, session.begin():
        operation = OperationRow(
            kind=OperationKind.SEND_OTP,
            idempotency_key="SEND_OTP:ambiguous",
            status=OperationStatus.RUNNING,
            account_id=account_id,
            rental_id=rental.id,
            order_id=rental.order_id,
            correlation_id="ambiguous",
            created_at=now,
            started_at=now,
            lease_until=now,
        )
        session.add(operation)
        session.flush()
        operation_id = operation.id
    funpay.fail_next.add("message_ambiguous")
    before = funpay.message_send_count
    funpay.send_message("buyer", "654321", idempotency_key="SEND_OTP:ambiguous", now=now)
    assert funpay.message_send_count == before + 1
    StartupReconciliation(repository, manager, funpay).run(now + timedelta(seconds=31))
    assert funpay.message_send_count == before + 1
    assert repository.get_rental(rental.id).status == RentalStatus.MANUAL_REVIEW
    assert any(item.category == "SEND_OTP_AMBIGUOUS" for item in notifier.notifications)
    assert operation_id
