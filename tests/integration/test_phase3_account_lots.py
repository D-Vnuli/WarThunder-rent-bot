from datetime import timedelta

from app.application.scheduler import DurableScheduler
from app.domain.models import OrderInput
from app.domain.states import AccountStatus, RentalStatus


def _add_durable_lots(repository, funpay, account_id, now, *lot_ids: str) -> None:
    for lot_id in lot_ids:
        repository.add_account_lot(account_id, lot_id, now)
        funpay.set_lot_state(lot_id, enabled=True)


def test_disable_uses_only_durable_account_lots(core, now):
    repository, manager, funpay, _ = core
    account_id = repository.add_account("WT01", now)
    _add_durable_lots(repository, funpay, account_id, now, "real-lot-1", "real-lot-2")

    result = manager.accept_order(OrderInput("order", "buyer", "1H", 60), now)
    manager.run_operations(now)

    assert result.accepted
    assert funpay.verify_lots_disabled(["real-lot-1", "real-lot-2"]).verified
    assert funpay.lot_operations == [("disable", ("real-lot-1", "real-lot-2"))]
    assert funpay.get_lot_state("fake-lot") is None


def test_missing_durable_lots_fails_closed_before_credentials(core, now):
    repository, manager, funpay, _ = core
    account_id = repository.add_account("WT01", now)

    result = manager.accept_order(OrderInput("order", "buyer", "1H", 60), now)
    manager.run_operations(now)

    assert result.accepted
    assert repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert repository.get_rental(result.rental_id or "").status == RentalStatus.MANUAL_REVIEW
    assert repository.pending_operations() == []
    assert funpay.message_send_count == 0
    assert not any(call.startswith("disable:") for call in funpay.calls)


def test_partial_and_unknown_durable_lot_results_fail_closed(core, now):
    repository, manager, funpay, _ = core
    partial_account = repository.add_account("WT01", now)
    _add_durable_lots(repository, funpay, partial_account, now, "partial-1", "partial-2")
    funpay.fail_next.add("disable_partial")
    manager.accept_order(OrderInput("partial", "buyer", "1H", 60), now)
    manager.run_operations(now)
    assert repository.get_account(partial_account).status == AccountStatus.MANUAL_REVIEW
    assert funpay.message_send_count == 0

    unknown_account = repository.add_account("WT02", now)
    repository.add_account_lot(unknown_account, "known-lot", now)
    repository.add_account_lot(unknown_account, "unknown-lot", now)
    funpay.set_lot_state("known-lot", enabled=True)
    unknown = manager.accept_order(OrderInput("unknown", "buyer-2", "1H", 60), now)
    manager.run_operations(now)
    assert repository.get_account(unknown_account).status == AccountStatus.MANUAL_REVIEW
    assert repository.get_rental(unknown.rental_id or "").status == RentalStatus.MANUAL_REVIEW
    assert funpay.message_send_count == 0


def test_enable_uses_exact_durable_lot_ids(core, now):
    repository, manager, funpay, _ = core
    account_id = repository.add_account("WT01", now)
    _add_durable_lots(repository, funpay, account_id, now, "enable-1", "enable-2")
    manager.secrets.set_current_credentials(account_id, "test-login", "test-password")
    result = manager.accept_order(OrderInput("order", "buyer", "1H", 1), now)
    manager.run_operations(now)
    manager.run_operations(now)
    assert repository.get_rental(result.rental_id or "").status == RentalStatus.ACTIVE

    DurableScheduler(repository, manager).tick(now + timedelta(seconds=2))
    for _ in range(3):
        manager.run_operations(now + timedelta(seconds=2))

    assert funpay.verify_lots_enabled(["enable-1", "enable-2"]).verified
    assert funpay.lot_operations[-1] == ("enable", ("enable-1", "enable-2"))
    assert funpay.get_lot_state("fake-lot") is None
