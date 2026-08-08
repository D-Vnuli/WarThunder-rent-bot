from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path

from sqlalchemy import func, select

from app.adapters.fake import FakeFunPayAdapter, FakeGaijinController, FakeSecureStore
from app.application.rental_manager import RentalManager
from app.domain.models import OrderInput
from app.domain.states import FulfillmentStatus, OperationStatus
from app.persistence.database import Database
from app.persistence.models import AuditEventRow, OperationRow, OrderRow, RentalRow
from app.persistence.repositories import Repository

NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def _worker(path: str, order_id: str) -> tuple[bool, str]:
    repo = Repository(Database(f"sqlite:///{Path(path).as_posix()}"))
    manager = RentalManager(repo, FakeFunPayAdapter(), FakeGaijinController(), FakeSecureStore())
    result = manager.accept_order(OrderInput(order_id, "buyer", "1H", 60), NOW)
    return result.accepted, result.fulfillment_status


def _run(path: Path, order_ids: list[str]) -> list[tuple[bool, str]]:
    context = get_context("spawn")
    with context.Pool(len(order_ids)) as pool:
        return pool.starmap(_worker, [(str(path), order_id) for order_id in order_ids])


def _recover_expired_lease_worker(path: str) -> int:
    repo = Repository(Database(f"sqlite:///{Path(path).as_posix()}"))
    return repo.recover_expired_leases(NOW + timedelta(seconds=31))


def _counts(database: Database) -> tuple[int, int, int]:
    with database.session() as session:
        orders = session.scalar(select(func.count()).select_from(OrderRow)) or 0
        rentals = session.scalar(select(func.count()).select_from(RentalRow)) or 0
        blocked = (
            session.scalar(
                select(func.count())
                .select_from(OrderRow)
                .where(OrderRow.fulfillment_status == FulfillmentStatus.FULFILLMENT_BLOCKED)
            )
            or 0
        )
        return orders, rentals, blocked


def test_multiprocess_eight_orders_one_account(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'one.db').as_posix()}")
    database.create_schema()
    Repository(database).add_account("WT01", NOW)
    _run(tmp_path / "one.db", [f"order-{number}" for number in range(8)])
    assert _counts(database) == (8, 1, 7)


def test_multiprocess_same_order_is_idempotent(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'same.db').as_posix()}")
    database.create_schema()
    Repository(database).add_account("WT01", NOW)
    results = _run(tmp_path / "same.db", ["same-order"] * 6)
    assert _counts(database) == (1, 1, 0)
    assert all(result[0] for result in results)


def test_multiprocess_six_orders_two_accounts(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'two.db').as_posix()}")
    database.create_schema()
    repository = Repository(database)
    repository.add_account("WT01", NOW)
    repository.add_account("WT02", NOW)
    _run(tmp_path / "two.db", [f"order-{number}" for number in range(6)])
    assert _counts(database) == (6, 2, 4)


def test_multiprocess_expired_lease_recovery_reports_one_winner(tmp_path):
    context = get_context("spawn")
    for repeat in range(10):
        path = tmp_path / f"lease-{repeat}.db"
        database = Database(f"sqlite:///{path.as_posix()}")
        database.create_schema()
        repository = Repository(database)
        repository.add_account("WT01", NOW)
        result = repository.reserve_order(OrderInput("order", "buyer", "1H", 60), NOW)
        assert result.rental_id is not None
        operation = repository.pending_operations()[0]
        assert repository.claim_operation(operation.id, NOW, lease_seconds=1) is not None

        with context.Pool(4) as pool:
            recovered = pool.map(_recover_expired_lease_worker, [str(path)] * 4)

        assert sum(recovered) == 1
        assert recovered.count(1) == 1
        assert recovered.count(0) == 3
        with database.session() as session:
            assert session.scalar(
                select(func.count())
                .select_from(OperationRow)
                .where(OperationRow.status == OperationStatus.FAILED)
            ) == 1
            assert session.scalar(
                select(func.count())
                .select_from(AuditEventRow)
                .where(AuditEventRow.event_type == "LEASE_RECOVERY_MANUAL_REVIEW")
            ) == 1
