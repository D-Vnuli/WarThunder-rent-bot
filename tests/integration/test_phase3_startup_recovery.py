from datetime import timedelta

from app.adapters.fake import (
    FakeFunPayAdapter,
    FakeGaijinController,
    FakeSecureStore,
    PersistentFakeFunPayBackend,
)
from app.application.rental_manager import RentalManager
from app.application.startup_reconciliation import StartupReconciliation
from app.domain.models import OrderInput
from app.domain.states import AccountStatus, OperationKind
from app.persistence.database import Database
from app.persistence.models import OperationRow
from app.persistence.repositories import Repository
from tests.helpers import create_test_account, fake_pixelstorm_security


def _new_worker(path, now, lot_states):
    database = Database(f"sqlite:///{path.as_posix()}")
    repository = Repository(database)
    funpay = FakeFunPayAdapter()
    for lot_id, enabled in lot_states.items():
        funpay.set_lot_state(lot_id, enabled=enabled)
    secrets = FakeSecureStore()
    manager = RentalManager(repository, funpay, FakeGaijinController(), secrets, pixelstorm_security=fake_pixelstorm_security(repository, secrets))
    return repository, manager, funpay


def test_disable_lots_crash_restart_verified_state_completes_without_resend(tmp_path, now):
    path = tmp_path / "disable.db"
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repository = Repository(database)
    external = FakeFunPayAdapter()
    account_id = create_test_account(repository, external, "WT01", now)
    repository.add_account_lot(account_id, f"test-lot-2-{account_id}", now)
    external.set_lot_state(f"test-lot-2-{account_id}", enabled=True)
    secrets = FakeSecureStore()
    RentalManager(repository, external, FakeGaijinController(), secrets, pixelstorm_security=fake_pixelstorm_security(repository, secrets)).accept_order(OrderInput("disable", "buyer", "1H", 60), now)
    operation = repository.pending_operations()[0]
    assert repository.claim_operation(operation.id, now) is not None
    lots = repository.account_lot_ids(account_id)
    assert external.disable_lots(account_id, lots).verified
    database.engine.dispose()

    repo2, manager2, funpay2 = _new_worker(path, now, {lot_id: False for lot_id in lots})
    assert StartupReconciliation(repo2, manager2, funpay2).run(now + timedelta(seconds=31)) >= 1
    assert repo2.pending_operations()[0].kind == OperationKind.SEND_CREDENTIALS
    assert funpay2.lot_operations == []


def test_disable_lots_crash_restart_partial_goes_manual_review(tmp_path, now):
    path = tmp_path / "disable-partial.db"
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repository = Repository(database)
    external = FakeFunPayAdapter()
    account_id = create_test_account(repository, external, "WT01", now)
    repository.add_account_lot(account_id, f"test-lot-2-{account_id}", now)
    external.set_lot_state(f"test-lot-2-{account_id}", enabled=True)
    secrets = FakeSecureStore()
    RentalManager(repository, external, FakeGaijinController(), secrets, pixelstorm_security=fake_pixelstorm_security(repository, secrets)).accept_order(OrderInput("partial", "buyer", "1H", 60), now)
    operation = repository.pending_operations()[0]
    assert repository.claim_operation(operation.id, now) is not None
    lots = repository.account_lot_ids(account_id)
    database.engine.dispose()

    repo2, manager2, funpay2 = _new_worker(path, now, {lots[0]: False, lots[1]: True})
    StartupReconciliation(repo2, manager2, funpay2).run(now + timedelta(seconds=31))
    assert repo2.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert funpay2.lot_operations == []


def _running_enable_operation(tmp_path, now):
    path = tmp_path / "enable.db"
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repository = Repository(database)
    funpay = FakeFunPayAdapter()
    account_id = create_test_account(repository, funpay, "WT01", now)
    secrets = FakeSecureStore()
    secrets.set_current_credentials(account_id, "safe-login", "safe-password")
    manager = RentalManager(repository, funpay, FakeGaijinController(), secrets, pixelstorm_security=fake_pixelstorm_security(repository, secrets))
    manager.accept_order(OrderInput("enable", "buyer", "1H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    repository.expire_due(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))
    operation = repository.pending_operations()[0]
    assert operation.kind == OperationKind.ENABLE_LOTS
    assert repository.claim_operation(operation.id, now + timedelta(seconds=2)) is not None
    return path, database, repository, account_id


def test_enable_lots_crash_restart_verified_state_makes_account_available(tmp_path, now):
    path, database, repository, account_id = _running_enable_operation(tmp_path, now)
    lots = repository.account_lot_ids(account_id)
    database.engine.dispose()
    repo2, manager2, funpay2 = _new_worker(path, now, {lot_id: True for lot_id in lots})
    StartupReconciliation(repo2, manager2, funpay2).run(now + timedelta(seconds=33))
    assert repo2.get_account(account_id).status == AccountStatus.AVAILABLE
    assert funpay2.lot_operations == []


def test_enable_lots_crash_restart_unknown_not_available(tmp_path, now):
    path, database, repository, account_id = _running_enable_operation(tmp_path, now)
    lots = repository.account_lot_ids(account_id)
    database.engine.dispose()
    repo2, manager2, _ = _new_worker(path, now, {lots[0]: True})
    StartupReconciliation(repo2, manager2, FakeFunPayAdapter()).run(now + timedelta(seconds=33))
    assert repo2.get_account(account_id).status != AccountStatus.AVAILABLE


def test_startup_reconciliation_recovers_send_credentials_receipt(tmp_path, now):
    path = tmp_path / "credentials.db"
    backend = PersistentFakeFunPayBackend(str(tmp_path / "external.db"))
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repository = Repository(database)
    funpay = FakeFunPayAdapter(backend)
    account_id = create_test_account(repository, funpay, "WT01", now)
    secrets = FakeSecureStore()
    secrets.set_current_credentials(account_id, "safe-login", "safe-password")
    manager = RentalManager(repository, funpay, FakeGaijinController(), secrets, pixelstorm_security=fake_pixelstorm_security(repository, secrets))
    started = manager.accept_order(OrderInput("credentials", "buyer", "1H", 60), now)
    manager.run_operations(now)
    operation = repository.pending_operations()[0]
    assert repository.claim_operation(operation.id, now) is not None
    assert manager._send_credentials(operation, now)
    database.engine.dispose()

    repo2, manager2, funpay2 = _new_worker(path, now, {})
    manager2.funpay = FakeFunPayAdapter(backend)
    assert StartupReconciliation(repo2, manager2, manager2.funpay).run(now) >= 1
    assert repo2.get_rental(started.rental_id or "").status == "ACTIVE"
    assert funpay2 is not manager2.funpay


def test_startup_reconciliation_recovers_send_otp_receipt(tmp_path, now):
    path = tmp_path / "otp.db"
    backend = PersistentFakeFunPayBackend(str(tmp_path / "otp-external.db"))
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    repository = Repository(database)
    funpay = FakeFunPayAdapter(backend)
    account_id = create_test_account(repository, funpay, "WT01", now)
    secrets = FakeSecureStore()
    manager = RentalManager(repository, funpay, FakeGaijinController(), secrets, pixelstorm_security=fake_pixelstorm_security(repository, secrets))
    started = manager.accept_order(OrderInput("otp", "buyer", "1H", 60), now)
    manager.run_operations(now)
    manager.run_operations(now)
    rental = repository.get_rental(started.rental_id or "")
    with repository.db.session() as session, session.begin():
        operation = OperationRow(
            kind=OperationKind.SEND_OTP,
            idempotency_key="SEND_OTP:startup",
            status="RUNNING",
            account_id=account_id,
            rental_id=rental.id,
            order_id=rental.order_id,
            correlation_id="startup",
            created_at=now,
            started_at=now,
            lease_until=now,
        )
        session.add(operation)
    funpay.send_message("buyer", "654321", idempotency_key="SEND_OTP:startup", now=now)
    database.engine.dispose()

    repo2, manager2, _ = _new_worker(path, now, {})
    manager2.funpay = FakeFunPayAdapter(backend)
    assert StartupReconciliation(repo2, manager2, manager2.funpay).run(now) >= 1
    with repo2.db.session() as session:
        assert session.get(OperationRow, operation.id).status == "COMPLETED"
