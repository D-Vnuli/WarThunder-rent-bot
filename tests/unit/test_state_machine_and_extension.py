from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.domain.models import OrderInput
from app.domain.states import AccountStatus, OperationKind, RentalStatus
from app.domain.transitions import require_account_transition
from app.persistence.models import OperationRow
from app.persistence.repositories import StateConflictError


def _start_active_rental(core, now):
    repository, manager, _, _ = core
    account_id = repository.add_account("WT01", now)
    result = manager.accept_order(OrderInput("order", "buyer", "1H", 60), now)
    manager.run_operations(now)
    manager.run_operations(now)
    assert result.rental_id is not None
    return repository, manager, account_id, result.rental_id


def test_domain_and_durable_account_transition_cas(core, now):
    repository, _, _, _ = core
    account_id = repository.add_account("WT01", now)
    account = repository.get_account(account_id)

    require_account_transition(AccountStatus.AVAILABLE, AccountStatus.RESERVED)
    repository.transition_account(
        account_id,
        AccountStatus.AVAILABLE,
        account.state_version,
        AccountStatus.RESERVED,
        now,
    )
    reserved = repository.get_account(account_id)
    assert (reserved.status, reserved.state_version) == (AccountStatus.RESERVED, 1)


def test_illegal_and_stale_account_transitions_leave_database_unchanged(core, now):
    repository, _, _, _ = core
    account_id = repository.add_account("WT01", now)
    account = repository.get_account(account_id)

    with pytest.raises(ValueError, match="Illegal account transition"):
        repository.transition_account(
            account_id,
            AccountStatus.AVAILABLE,
            account.state_version,
            AccountStatus.ACTIVE,
            now,
        )
    with pytest.raises(StateConflictError, match="Stale"):
        repository.transition_account(
            account_id,
            AccountStatus.AVAILABLE,
            account.state_version + 1,
            AccountStatus.RESERVED,
            now,
        )
    unchanged = repository.get_account(account_id)
    assert (unchanged.status, unchanged.state_version) == (AccountStatus.AVAILABLE, 0)


def test_expiration_passes_through_expiring_then_revoking(core, now):
    repository, _, account_id, rental_id = _start_active_rental(core, now)
    repository.expire_due(now + timedelta(seconds=61))
    assert repository.get_account(account_id).status == AccountStatus.EXPIRING
    assert repository.get_rental(rental_id).status == RentalStatus.EXPIRING

    operation = repository.pending_operations()[0]
    assert operation.kind == OperationKind.REVOKE_SESSIONS
    assert repository.claim_operation(operation.id, now + timedelta(seconds=61)) is not None
    assert repository.prepare_operation(operation.id, now + timedelta(seconds=61)) is not None
    assert repository.get_account(account_id).status == AccountStatus.REVOKING
    assert repository.get_rental(rental_id).status == RentalStatus.REVOKING


def test_same_buyer_extension_is_atomic_and_creates_no_operations(core, now):
    repository, manager, account_id, rental_id = _start_active_rental(core, now)
    before_rental = repository.get_rental(rental_id)
    before_account = repository.get_account(account_id)
    with repository.db.session() as session:
        before_operations = session.scalar(select(func.count()).select_from(OperationRow))

    assert manager.extend_rental(rental_id, "buyer", 3600, now)

    rental = repository.get_rental(rental_id)
    account = repository.get_account(account_id)
    with repository.db.session() as session:
        after_operations = session.scalar(select(func.count()).select_from(OperationRow))
    assert rental.expires_at == before_rental.expires_at + timedelta(hours=1)
    assert (rental.status, account.status) == (RentalStatus.ACTIVE, AccountStatus.ACTIVE)
    assert rental.credential_version == before_rental.credential_version
    assert account.credential_version == before_account.credential_version
    assert after_operations == before_operations


def test_extension_rejects_wrong_buyer_and_non_active_rental(core, now):
    repository, manager, _, rental_id = _start_active_rental(core, now)
    previous_expiry = repository.get_rental(rental_id).expires_at
    assert not manager.extend_rental(rental_id, "other-buyer", 60, now)
    assert repository.get_rental(rental_id).expires_at == previous_expiry

    repository.expire_due(now + timedelta(seconds=61))
    assert not manager.extend_rental(rental_id, "buyer", 60, now)
    assert repository.get_rental(rental_id).expires_at == previous_expiry
