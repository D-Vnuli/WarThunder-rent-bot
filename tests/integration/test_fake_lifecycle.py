from datetime import timedelta

from app.application.scheduler import DurableScheduler
from app.domain.models import OrderInput
from app.domain.states import AccountStatus, RentalStatus
from tests.helpers import create_test_account


def test_full_fake_lifecycle(core, now):
    repository, manager, funpay, gaijin = core
    account_id = create_test_account(repository, funpay, "WT01", now)
    result = manager.accept_order(OrderInput("order-1", "buyer-1", "6H", 6 * 3600), now)
    manager.run_operations(now)
    manager.run_operations(now)
    rental = repository.get_rental(result.rental_id or "")
    assert rental.status == RentalStatus.ACTIVE
    assert funpay.message_send_count == 1
    scheduler = DurableScheduler(repository, manager)
    scheduler.tick(now + timedelta(hours=6, seconds=1))
    manager.run_operations(now + timedelta(hours=6, seconds=1))
    manager.run_operations(now + timedelta(hours=6, seconds=1))
    manager.run_operations(now + timedelta(hours=6, seconds=1))
    assert repository.get_account(account_id).status == AccountStatus.AVAILABLE
    assert repository.get_rental(rental.id).status == RentalStatus.FINISHED
    assert funpay.lots_enabled
    assert gaijin.revoked == [account_id]
