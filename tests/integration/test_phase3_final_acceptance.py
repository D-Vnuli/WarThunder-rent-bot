from datetime import timedelta

from app.adapters.fake import (
    FakeFunPayAdapter,
    FakeFunPayTransport,
    FakeGaijinController,
    FakeOwnerNotifier,
    FakeSecureStore,
)
from app.adapters.session_backed_funpay import SessionBackedFunPayAdapter
from app.application.funpay_dispatcher import FunPayEventPoller
from app.application.rental_manager import RentalManager
from app.domain.funpay import FunPayHealth
from app.domain.models import OrderInput
from app.domain.states import AccountStatus, RentalStatus
from app.main import create_application
from app.persistence.database import Database
from app.persistence.funpay_events import FunPayEventRepository
from app.persistence.repositories import Repository
from tests.helpers import create_test_account


def test_missing_secure_credentials_never_activates_rental(core, now):
    repository, _, funpay, _ = core
    notifier = FakeOwnerNotifier()
    account_id = repository.add_account("WT01", now)
    repository.add_account_lot(account_id, "missing-credential-lot", now)
    funpay.set_lot_state("missing-credential-lot", enabled=True)
    manager = RentalManager(repository, funpay, FakeGaijinController(), FakeSecureStore(), owner_notifier=notifier)
    started = manager.accept_order(OrderInput("missing-credentials", "buyer", "1H", 60), now)
    manager.run_operations(now)
    manager.run_operations(now)

    assert funpay.message_send_count == 0
    assert repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert repository.get_rental(started.rental_id or "").status == RentalStatus.MANUAL_REVIEW
    assert any(item.category == "SEND_CREDENTIALS_FAIL_CLOSED" for item in notifier.notifications)


def test_application_startup_immediately_recovers_running_disable_lots(tmp_path, now):
    path = tmp_path / "startup-disable.db"
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repository = Repository(database)
    external = FakeFunPayAdapter()
    account_id = create_test_account(repository, external, "WT01", now)
    manager = RentalManager(repository, external, FakeGaijinController(), FakeSecureStore())
    manager.secrets.set_current_credentials(account_id, "safe-login", "safe-password")
    manager.accept_order(OrderInput("startup-disable", "buyer", "1H", 60), now)
    operation = repository.pending_operations()[0]
    assert repository.claim_operation(operation.id, now) is not None
    lots = repository.account_lot_ids(account_id)
    assert external.disable_lots(account_id, lots).verified
    database.engine.dispose()

    restarted = Database(f"sqlite:///{path.as_posix()}")
    funpay = FakeFunPayAdapter()
    for lot_id in lots:
        funpay.set_lot_state(lot_id, enabled=False)
    create_application(database=restarted, funpay=funpay, now=now + timedelta(seconds=1))
    repo2 = Repository(restarted)
    assert repo2.pending_operations()[0].kind == "SEND_CREDENTIALS"
    assert repo2.get_account(account_id).status != AccountStatus.MANUAL_REVIEW


def test_application_startup_immediately_recovers_running_enable_lots(tmp_path, now):
    path = tmp_path / "startup-enable.db"
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repository = Repository(database)
    external = FakeFunPayAdapter()
    account_id = create_test_account(repository, external, "WT01", now)
    secrets = FakeSecureStore()
    secrets.set_current_credentials(account_id, "safe-login", "safe-password")
    manager = RentalManager(repository, external, FakeGaijinController(), secrets)
    manager.accept_order(OrderInput("startup-enable", "buyer", "1H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    operation = repository.pending_operations()[0]
    assert operation.kind == "ENABLE_LOTS"
    assert repository.claim_operation(operation.id, now + timedelta(seconds=2)) is not None
    lots = repository.account_lot_ids(account_id)
    assert external.enable_lots(account_id, lots).verified
    database.engine.dispose()

    restarted = Database(f"sqlite:///{path.as_posix()}")
    funpay = FakeFunPayAdapter()
    for lot_id in lots:
        funpay.set_lot_state(lot_id, enabled=True)
    create_application(database=restarted, funpay=funpay, now=now + timedelta(seconds=3))
    assert Repository(restarted).get_account(account_id).status == AccountStatus.AVAILABLE


def test_startup_lot_failure_notifies_owner_and_polling_notifies_auth_loss(core, now):
    repository, _, funpay, _ = core
    notifier = FakeOwnerNotifier()
    account_id = create_test_account(repository, funpay, "WT01", now)
    repository.add_account_lot(account_id, "startup-notification-lot", now)
    funpay.set_lot_state("startup-notification-lot", enabled=True)
    manager = RentalManager(repository, funpay, FakeGaijinController(), FakeSecureStore(), owner_notifier=notifier)
    manager.accept_order(OrderInput("partial", "buyer", "1H", 60), now)
    operation = repository.pending_operations()[0]
    assert repository.claim_operation(operation.id, now) is not None
    lots = repository.account_lot_ids(account_id)
    funpay.set_lot_state(lots[0], enabled=False)
    from app.application.startup_reconciliation import StartupReconciliation

    StartupReconciliation(repository, manager, funpay).run(now + timedelta(seconds=1))
    assert any(item.category == "DISABLE_LOTS_VERIFICATION_FAILED" for item in notifier.notifications)

    transport = FakeFunPayTransport(FakeFunPayAdapter(), valid_sessions={"valid"})
    sessions = FakeSecureStore()
    adapter = SessionBackedFunPayAdapter("owner", sessions, transport)
    poller = FunPayEventPoller(adapter, FunPayEventRepository(repository.db), notifier)
    assert poller.poll_once(now, now) == 0
    assert transport.poll_calls == 0
    assert any(item.category == "FUNPAY_AUTH_REQUIRED" for item in notifier.notifications)

    sessions.set_funpay_session("owner", "invalid")
    assert poller.poll_once(now, now) == 0
    assert sessions.get_funpay_session("owner") is None

    unavailable = FakeFunPayAdapter()
    unavailable.set_health(FunPayHealth.UNAVAILABLE)
    assert FunPayEventPoller(unavailable, FunPayEventRepository(repository.db), notifier).poll_once(now, now) == 0
    assert any(item.category == "FUNPAY_UNAVAILABLE" for item in notifier.notifications)
