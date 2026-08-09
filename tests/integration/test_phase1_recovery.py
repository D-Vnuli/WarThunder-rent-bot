from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from app.adapters.fake import FakeFunPayAdapter, FakeGaijinController, FakeSecureStore
from app.application.rental_manager import RentalManager
from app.application.startup_reconciliation import StartupReconciliation
from app.domain.models import OrderInput
from app.domain.states import AccountStatus, FulfillmentStatus
from app.persistence.database import Database
from app.persistence.repositories import Repository
from tests.helpers import create_test_account, fake_pixelstorm_security


def _manager(path: Path):
    db = Database(f"sqlite:///{path.as_posix()}")
    db.create_schema()
    repo = Repository(db)
    secrets = FakeSecureStore()
    repo._test_secret_store = secrets  # type: ignore[attr-defined]
    manager = RentalManager(repo, FakeFunPayAdapter(), FakeGaijinController(), secrets, pixelstorm_security=fake_pixelstorm_security(repo, secrets))
    return repo, manager


def test_file_sqlite_persists_blocked_orders_under_competing_orders(tmp_path, now):
    repo, manager = _manager(tmp_path / "concurrency.db")
    create_test_account(repo, manager.funpay, "WT01", now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda n: manager.accept_order(
                    OrderInput(f"order-{n}", f"buyer-{n}", "1H", 60), now
                ),
                range(8),
            )
        )
    assert sum(result.accepted for result in results) == 1
    assert (
        sum(
            result.fulfillment_status == FulfillmentStatus.FULFILLMENT_BLOCKED for result in results
        )
        == 7
    )


def test_file_sqlite_uses_all_available_accounts(tmp_path, now):
    repo, manager = _manager(tmp_path / "multi-account.db")
    create_test_account(repo, manager.funpay, "WT01", now)
    repo.add_account("WT02", now)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda n: manager.accept_order(
                    OrderInput(f"order-{n}", f"buyer-{n}", "1H", 60), now
                ),
                range(4),
            )
        )
    assert sum(result.accepted for result in results) == 2
    assert (
        sum(
            result.fulfillment_status == FulfillmentStatus.FULFILLMENT_BLOCKED for result in results
        )
        == 2
    )


def test_file_sqlite_duplicate_delivery_is_idempotent(tmp_path, now):
    repo, manager = _manager(tmp_path / "duplicate.db")
    repo.add_account("WT01", now)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: manager.accept_order(OrderInput("same", "buyer", "1H", 60), now),
                range(6),
            )
        )
    assert {result.rental_id for result in results} == {results[0].rental_id}
    assert all(result.accepted for result in results)


def test_active_lease_cannot_be_claimed_twice_and_expired_is_fail_closed(tmp_path, now):
    repo, manager = _manager(tmp_path / "lease.db")
    repo.add_account("WT01", now)
    result = manager.accept_order(OrderInput("order", "buyer", "1H", 60), now)
    operation = repo.pending_operations()[0]
    assert repo.claim_operation(operation.id, now) is not None
    assert repo.claim_operation(operation.id, now) is None
    assert StartupReconciliation(repo).run(now + timedelta(seconds=31)) == 1
    assert (
        repo.get_account(repo.get_rental(result.rental_id or "").account_id).status
        == AccountStatus.MANUAL_REVIEW
    )


def test_expired_lease_only_one_worker_reclaims(tmp_path, now):
    repo, manager = _manager(tmp_path / "lease-race.db")
    repo.add_account("WT01", now)
    manager.accept_order(OrderInput("order", "buyer", "1H", 60), now)
    operation = repo.pending_operations()[0]
    assert repo.claim_operation(operation.id, now) is not None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: repo.claim_operation(operation.id, now + timedelta(seconds=31)), range(2)
            )
        )
    assert sum(result is not None for result in results) == 0


def test_restart_after_completed_effect_does_not_replay_credentials(tmp_path, now):
    repo, manager = _manager(tmp_path / "restart.db")
    create_test_account(repo, manager.funpay, "WT01", now)
    result = manager.accept_order(OrderInput("order", "buyer", "1H", 60), now)
    manager.run_operations(now)
    manager.run_operations(now)
    # A new application worker only sees completed durable operations.
    manager.run_operations(now + timedelta(seconds=1))
    assert manager.funpay.message_send_count == 1
    assert result.rental_id is not None


def test_crash_after_credentials_send_is_fail_closed_without_second_send(tmp_path, now):
    repo, manager = _manager(tmp_path / "credentials-crash.db")
    create_test_account(repo, manager.funpay, "WT01", now)
    manager.accept_order(OrderInput("order", "buyer", "1H", 60), now)
    manager.run_operations(now)  # disable lots -> durable SEND_CREDENTIALS
    operation = repo.pending_operations()[0]
    assert manager._send_credentials(operation, now)  # external success, no DB completion
    assert repo.claim_operation(operation.id, now) is not None
    assert StartupReconciliation(repo).run(now + timedelta(seconds=31)) == 1
    assert manager.funpay.message_send_count == 1


def test_rotation_crash_preserves_historical_rental_version(tmp_path, now):
    repo, manager = _manager(tmp_path / "rotation-crash.db")
    account_id = create_test_account(repo, manager.funpay, "WT01", now)
    result = manager.accept_order(OrderInput("order", "buyer", "1H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    manager.repository.expire_due(now + timedelta(seconds=2))
    manager.run_operations(now + timedelta(seconds=2))  # revoke -> rotate operation
    rotation = repo.pending_operations()[0]
    assert manager.gaijin.rotate_password(account_id)  # external success before durable completion
    assert repo.claim_operation(rotation.id, now + timedelta(seconds=2)) is not None
    StartupReconciliation(repo).run(now + timedelta(seconds=33))
    assert repo.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert repo.get_rental(result.rental_id or "").credential_version == 1
