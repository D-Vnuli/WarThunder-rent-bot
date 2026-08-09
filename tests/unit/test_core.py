from datetime import timedelta

from app.application.scheduler import DurableScheduler
from app.domain.models import OrderInput
from app.domain.states import AccountStatus, FulfillmentStatus, RentalStatus
from tests.helpers import create_test_account


def test_second_paid_order_is_blocked(core, now):
    repository, manager, _, _ = core
    repository.add_account("WT01", now)
    first = manager.accept_order(OrderInput("A", "buyer-a", "6H", 3600), now)
    second = manager.accept_order(OrderInput("B", "buyer-b", "3H", 1800), now)
    assert first.accepted
    assert second.fulfillment_status == FulfillmentStatus.FULFILLMENT_BLOCKED


def test_unverified_disable_lots_enters_manual_review(core, now):
    repository, manager, funpay, _ = core
    account_id = create_test_account(repository, funpay, "WT01", now)
    funpay.fail_next.add("disable")
    result = manager.accept_order(OrderInput("A", "buyer-a", "6H", 3600), now)
    manager.run_operations(now)
    assert result.accepted
    assert repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert funpay.message_send_count == 0


def test_expiration_uses_persisted_timestamp(core, now):
    repository, manager, funpay, gaijin = core
    create_test_account(repository, funpay, "WT01", now)
    rental = manager.accept_order(OrderInput("A", "buyer-a", "1H", 60), now)
    manager.run_operations(now)
    manager.run_operations(now)
    DurableScheduler(repository, manager).tick(now + timedelta(seconds=61))
    manager.run_operations(now + timedelta(seconds=61))
    assert gaijin.revoked == []
    assert repository.get_rental(rental.rental_id or "").status == RentalStatus.PASSWORD_ROTATION
