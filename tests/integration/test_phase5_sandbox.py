import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread
from time import monotonic

import pytest

from app.config.settings import Settings
from app.domain.funpay import FunPayEvent, FunPayEventType, FunPayHealth
from app.domain.models import OrderInput
from app.domain.pixelstorm import (
    PixelStormAuthResult,
    PixelStormHealth,
    PixelStormSecurityCapabilities,
)
from app.domain.states import AccountStatus, OperationKind, RentalStatus
from app.main import create_application
from app.persistence.repositories import StateConflictError
from app.sandbox import SandboxEnvironment


def _phase5_process_worker(root: str, now: datetime, cycles: int) -> None:
    environment = SandboxEnvironment(Path(root), now)
    try:
        for _ in range(cycles):
            try:
                environment.run_once()
            except StateConflictError:
                continue
    finally:
        environment.close()


def _phase5_crash_after_credentials_worker(root: str, now: datetime) -> None:
    environment = SandboxEnvironment(Path(root), now)
    original = environment.application.repository.operation_completed

    def crash(operation_id, occurred_at, recovery_claim_token=None, normal_claim_token=None):
        del normal_claim_token
        if environment.application.repository.get_operation(operation_id).kind == OperationKind.SEND_CREDENTIALS:
            os._exit(17)
        return original(operation_id, occurred_at, recovery_claim_token)

    environment.application.repository.operation_completed = crash
    environment.run_once()


def _sandbox(tmp_path) -> SandboxEnvironment:
    return SandboxEnvironment(tmp_path, datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def sandbox(tmp_path):
    environment = _sandbox(tmp_path)
    yield environment
    environment.close()


def _run(environment: SandboxEnvironment, count: int = 1) -> None:
    for _ in range(count):
        environment.run_once()


def _activate(sandbox: SandboxEnvironment, *, order_id: str = "order-1"):
    account_id = sandbox.seed_account()
    sandbox.paid_order(f"paid-{order_id}", order_id, "buyer-1", 60)
    _run(sandbox)
    rental = sandbox.application.funpay_dispatcher._events.rental_for_order(order_id, "buyer-1")
    assert rental is not None and rental.status == RentalStatus.ACTIVE
    return account_id, rental


def _running_send_credentials(sandbox: SandboxEnvironment, order_id: str):
    account_id = sandbox.seed_account()
    started = sandbox.application.manager.accept_order(
        OrderInput(order_id, "buyer-1", "SANDBOX", 60), sandbox.clock.now()
    )
    assert started.rental_id is not None
    disable = next(
        operation
        for operation in sandbox.application.repository.pending_operations()
        if operation.kind == OperationKind.DISABLE_LOTS
    )
    assert sandbox.application.repository.claim_operation(disable.id, sandbox.clock.now()) is not None
    lot_ids = sandbox.application.repository.account_lot_ids(account_id)
    assert sandbox.funpay.disable_lots(account_id, lot_ids).verified
    assert sandbox.application.repository.operation_completed(disable.id, sandbox.clock.now())
    credentials = next(
        operation
        for operation in sandbox.application.repository.pending_operations()
        if operation.kind == OperationKind.SEND_CREDENTIALS
    )
    claimed = sandbox.application.repository.claim_operation(credentials.id, sandbox.clock.now())
    assert claimed is not None
    return account_id, started.rental_id, claimed


def _crash_after_completion(monkeypatch, sandbox: SandboxEnvironment, kind: OperationKind) -> None:
    original = sandbox.application.repository.operation_completed

    def crash(operation_id, now, recovery_claim_token=None, normal_claim_token=None):
        del normal_claim_token
        if sandbox.application.repository.get_operation(operation_id).kind == kind:
            raise RuntimeError(f"simulated crash after {kind}")
        return original(operation_id, now, recovery_claim_token)

    monkeypatch.setattr(sandbox.application.repository, "operation_completed", crash)


def test_phase5_full_rental_happy_path_with_buyer_otp_and_expiry(sandbox):
    account_id = sandbox.seed_account(lots=2)
    old_credentials = sandbox.secrets.get_current_credentials(account_id)
    assert old_credentials is not None
    sandbox.paid_order("paid-1", "order-1", "buyer-1", 60)
    _run(sandbox)
    rental = sandbox.application.funpay_dispatcher._events.rental_for_order("order-1", "buyer-1")
    assert rental is not None and rental.status == RentalStatus.ACTIVE
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
    assert all(not sandbox.funpay.get_lot_state(lot) for lot in sandbox.application.repository.account_lot_ids(account_id))

    sandbox.buyer_code("buyer-code-1", "order-1", "buyer-1")
    _run(sandbox)
    waiting = [op for op in sandbox.application.repository.pending_operations() if op.kind == OperationKind.SEND_OTP]
    assert len(waiting) == 1
    sandbox.login_otp_email("login-otp-1", account_id, "123456")
    _run(sandbox)
    assert sandbox.funpay.message_count("SEND_OTP:buyer-code-1") == 1

    sandbox.clock.advance(seconds=60)
    _run(sandbox, 3)
    account = sandbox.application.repository.get_account(account_id)
    finished = sandbox.application.repository.get_rental(rental.id)
    assert account.status == AccountStatus.AVAILABLE
    assert finished.status == RentalStatus.FINISHED
    assert account.credential_version == 2
    assert sandbox.pixel_backend.counter(account_id, "revoke_count") == 1
    assert sandbox.pixel_backend.counter(account_id, "rotation_count") == 1
    assert sandbox.pixel_backend.verify(account_id, *old_credentials) is False
    assert all(sandbox.funpay.get_lot_state(lot) for lot in sandbox.application.repository.account_lot_ids(account_id))


@pytest.mark.parametrize(
    "health", [FunPayHealth.AUTH_REQUIRED, FunPayHealth.DEGRADED, FunPayHealth.UNAVAILABLE]
)
def test_phase5_duplicate_events_wrong_buyer_and_funpay_retry(sandbox, health):
    account_id = sandbox.seed_account()
    sandbox.funpay.set_health(health)
    sandbox.paid_order("paid-retry", "order-retry", "buyer", 60)
    _run(sandbox)
    assert sandbox.application.funpay_dispatcher._events.rental_for_order("order-retry", "buyer") is None
    sandbox.funpay.set_health(FunPayHealth.READY)
    sandbox.clock.advance(seconds=31)
    _run(sandbox)
    rental = sandbox.application.funpay_dispatcher._events.rental_for_order("order-retry", "buyer")
    assert rental is not None and rental.status == RentalStatus.ACTIVE
    sandbox.paid_order("paid-retry", "order-retry", "buyer", 60)
    sandbox.buyer_code("wrong-buyer-code", "order-retry", "other-buyer")
    _run(sandbox)
    assert sandbox.funpay.message_count("SEND_OTP:wrong-buyer-code") == 0
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE


def test_phase5_sandbox_rejects_non_sandbox_adapters(tmp_path):
    class Unsafe:
        pass

    with pytest.raises(RuntimeError, match="refuses non-sandbox FunPay"):
        create_application(
            settings=Settings(app_mode="SANDBOX", dry_run=True),
            funpay=Unsafe(),  # type: ignore[arg-type]
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_phase5_no_double_allocation_for_two_paid_orders(sandbox):
    account_id = sandbox.seed_account()
    sandbox.paid_order("paid-a", "order-a", "buyer-a", 60)
    sandbox.paid_order("paid-b", "order-b", "buyer-b", 60)
    _run(sandbox)

    first = sandbox.application.funpay_dispatcher._events.rental_for_order("order-a", "buyer-a")
    second = sandbox.application.funpay_dispatcher._events.rental_for_order("order-b", "buyer-b")
    accepted = [rental for rental in (first, second) if rental is not None]
    assert len(accepted) == 1
    assert accepted[0].account_id == account_id
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
    assert any(item.category == "PAID_ORDER_BLOCKED" for item in sandbox.owner.notifications)


def test_phase5_concurrent_order_acceptance_keeps_one_account_lease(sandbox):
    account_id = sandbox.seed_account()
    now = sandbox.clock.now()
    orders = [
        OrderInput("concurrent-a", "buyer-a", "SANDBOX", 60),
        OrderInput("concurrent-b", "buyer-b", "SANDBOX", 60),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda order: sandbox.application.manager.accept_order(order, now), orders))

    assert sum(result.accepted for result in results) == 1
    _run(sandbox)
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE


def test_phase5_concurrent_duplicate_order_has_one_rental(sandbox):
    sandbox.seed_account()
    order = OrderInput("concurrent-duplicate", "buyer", "SANDBOX", 60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: sandbox.application.manager.accept_order(order, sandbox.clock.now()), range(2)
            )
        )

    assert all(result.accepted for result in results)
    assert len({result.rental_id for result in results}) == 1


def test_phase5_wrong_buyer_command_never_creates_otp_operation(sandbox):
    account_id, _ = _activate(sandbox, order_id="wrong-command")
    sandbox.funpay.add_event(
        FunPayEvent(
            "not-code",
            FunPayEventType.BUYER_MESSAGE,
            sandbox.clock.now(),
            "wrong-command",
            "buyer-1",
            message_text="not the exact command",
        )
    )
    _run(sandbox)
    assert not [
        operation
        for operation in sandbox.application.repository.pending_operations()
        if operation.kind == OperationKind.SEND_OTP
    ]
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE


def test_phase5_duplicate_exact_buyer_command_creates_one_otp_delivery(sandbox):
    account_id, rental = _activate(sandbox, order_id="duplicate-buyer-command")
    sandbox.buyer_code("duplicate-buyer-command", "duplicate-buyer-command", "buyer-1")
    sandbox.buyer_code("duplicate-buyer-command", "duplicate-buyer-command", "buyer-1")
    _run(sandbox)
    otp_operations = [
        operation
        for operation in sandbox.application.repository.pending_operations()
        if operation.kind == OperationKind.SEND_OTP and operation.rental_id == rental.id
    ]
    assert len(otp_operations) == 1
    sandbox.login_otp_email("duplicate-buyer-command-otp", account_id, "556677")
    _run(sandbox)
    assert sandbox.funpay.message_count("SEND_OTP:duplicate-buyer-command") == 1
    with sqlite3.connect(sandbox.root / "application.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM funpay_events WHERE external_event_id = ?",
            ("duplicate-buyer-command",),
        ).fetchone() == (1,)


def test_phase5_active_buyer_can_request_second_otp_after_rate_limit(sandbox):
    account_id, rental = _activate(sandbox, order_id="multiple-otp-requests")
    sandbox.buyer_code("otp-request-1", "multiple-otp-requests", "buyer-1")
    sandbox.login_otp_email("otp-request-mail-1", account_id, "111111")
    _run(sandbox)
    assert sandbox.funpay.message_count("SEND_OTP:otp-request-1") == 1
    credential_key = f"SEND_CREDENTIALS:{rental.id}"
    assert sandbox.funpay.message_count(credential_key) == 1
    disabled_before = sandbox.funpay.lot_mutation_count(enabled=False)
    enabled_before = sandbox.funpay.lot_mutation_count(enabled=True)

    sandbox.clock.advance(seconds=31)
    sandbox.buyer_code("otp-request-2", "multiple-otp-requests", "buyer-1")
    sandbox.login_otp_email("otp-request-mail-2", account_id, "222222")
    _run(sandbox)

    assert sandbox.funpay.message_count("SEND_OTP:otp-request-1") == 1
    assert sandbox.funpay.message_count("SEND_OTP:otp-request-2") == 1
    assert sandbox.funpay.message_count(credential_key) == 1
    assert sandbox.application.repository.get_rental(rental.id).status == RentalStatus.ACTIVE
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
    assert sandbox.funpay.lot_mutation_count(enabled=False) == disabled_before
    assert sandbox.funpay.lot_mutation_count(enabled=True) == enabled_before
    assert not sandbox.owner.notifications


def test_phase5_buyer_otp_rate_limit_blocks_too_soon_request(sandbox):
    account_id, rental = _activate(sandbox, order_id="otp-rate-limit")
    sandbox.buyer_code("otp-rate-limit-1", "otp-rate-limit", "buyer-1")
    sandbox.login_otp_email("otp-rate-limit-mail-1", account_id, "111111")
    _run(sandbox)
    sandbox.clock.advance(seconds=1)
    sandbox.buyer_code("otp-rate-limit-2", "otp-rate-limit", "buyer-1")
    sandbox.login_otp_email("otp-rate-limit-mail-2", account_id, "222222")
    _run(sandbox)

    assert sandbox.funpay.message_count("SEND_OTP:otp-rate-limit-1") == 1
    assert sandbox.funpay.message_count("SEND_OTP:otp-rate-limit-2") == 0
    assert sandbox.application.repository.get_rental(rental.id).status == RentalStatus.ACTIVE
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE


def test_phase5_active_disable_lots_lease_survives_startup(tmp_path):
    root = tmp_path / "active-disable-lease"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = SandboxEnvironment(root, now)
    account_id = first.seed_account()
    started = first.application.manager.accept_order(OrderInput("active-disable", "buyer", "SANDBOX", 60), now)
    assert started.rental_id is not None
    operation = next(
        item for item in first.application.repository.pending_operations() if item.kind == OperationKind.DISABLE_LOTS
    )
    assert first.application.repository.claim_operation(operation.id, now) is not None
    first.close()

    second = SandboxEnvironment(root, now.replace(second=1))
    try:
        assert second.application.repository.get_operation(operation.id).status == "RUNNING"
        assert second.application.repository.get_account(account_id).status == AccountStatus.RESERVED
        assert second.application.repository.get_rental(started.rental_id).status == RentalStatus.RESERVED
        assert second.funpay.lot_mutation_count(enabled=False) == 0
    finally:
        second.close()


def test_phase5_active_send_credentials_lease_survives_startup(tmp_path):
    root = tmp_path / "active-credentials-lease"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = SandboxEnvironment(root, now)
    account_id, rental_id, operation = _running_send_credentials(first, "active-credentials")
    first.close()

    second = SandboxEnvironment(root, now.replace(second=1))
    try:
        assert second.application.repository.get_operation(operation.id).status == "RUNNING"
        assert second.application.repository.get_account(account_id).status == AccountStatus.RESERVED
        assert second.application.repository.get_rental(rental_id).status == RentalStatus.RESERVED
        assert second.funpay.message_count(operation.idempotency_key) == 0
    finally:
        second.close()


def test_phase5_active_enable_lots_lease_survives_startup(tmp_path):
    root = tmp_path / "active-enable-lease"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = SandboxEnvironment(root, now)
    try:
        account_id, rental = _activate(first, order_id="active-enable")
        first.clock.advance(seconds=60)
        first.application.repository.expire_due(first.clock.now())
        first.application.manager.run_operations(first.clock.now())
        first.application.manager.run_operations(first.clock.now())
        operation = next(
            item
            for item in first.application.repository.pending_operations()
            if item.kind == OperationKind.ENABLE_LOTS
        )
        assert first.application.repository.claim_operation(operation.id, first.clock.now()) is not None
        assert first.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE_OFFLINE
        assert all(
            first.funpay.get_lot_state(lot) is False
            for lot in first.application.repository.account_lot_ids(account_id)
        )
    finally:
        first.close()

    second = SandboxEnvironment(root, now.replace(minute=1, second=1))
    try:
        assert second.application.repository.get_operation(operation.id).status == "RUNNING"
        assert second.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE_OFFLINE
        assert second.application.repository.get_rental(rental.id).status == RentalStatus.PASSWORD_ROTATION
        assert second.funpay.lot_mutation_count(enabled=True) == 0
    finally:
        second.close()


def test_phase5_expired_credentials_lease_recovers_confirmed_receipt_once(tmp_path):
    root = tmp_path / "expired-receipt-recovery"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = SandboxEnvironment(root, now)
    account_id, rental_id, operation = _running_send_credentials(first, "expired-receipt")
    assert first.application.manager._send_credentials(operation, now)
    assert first.funpay.message_count(operation.idempotency_key) == 1
    first.close()

    before_expiry = SandboxEnvironment(root, now.replace(second=1))
    try:
        assert before_expiry.application.repository.get_operation(operation.id).status == "RUNNING"
        assert before_expiry.application.repository.get_account(account_id).status == AccountStatus.RESERVED
        assert before_expiry.funpay.message_count(operation.idempotency_key) == 1
    finally:
        before_expiry.close()

    after_expiry = SandboxEnvironment(root, now.replace(second=31))
    try:
        assert after_expiry.application.repository.get_operation(operation.id).status == "COMPLETED"
        assert after_expiry.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
        assert after_expiry.application.repository.get_rental(rental_id).status == RentalStatus.ACTIVE
        assert after_expiry.funpay.message_count(operation.idempotency_key) == 1
    finally:
        after_expiry.close()


def test_phase5_operation_completion_has_one_durable_winner(sandbox):
    account_id, rental_id, operation = _running_send_credentials(sandbox, "completion-cas")
    now = sandbox.clock.now()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: sandbox.application.repository.operation_completed(operation.id, now), range(2)))
    assert sorted(results) == [False, True]
    assert sandbox.application.repository.get_operation(operation.id).status == "COMPLETED"
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
    assert sandbox.application.repository.get_rental(rental_id).status == RentalStatus.ACTIVE


def test_phase5_failed_operation_cannot_be_resurrected_by_stale_completion(sandbox):
    account_id, rental_id, operation = _running_send_credentials(sandbox, "failed-cannot-resurrect")
    now = sandbox.clock.now()
    assert sandbox.application.repository.operation_failed(operation.id, now)
    assert sandbox.application.repository.operation_completed(operation.id, now) is False
    assert sandbox.application.repository.get_operation(operation.id).status == "FAILED"
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert sandbox.application.repository.get_rental(rental_id).status == RentalStatus.MANUAL_REVIEW


@pytest.mark.parametrize("kind", [OperationKind.SEND_CREDENTIALS, OperationKind.SEND_OTP])
def test_phase5_startup_security_recovery_never_claims_expired_nonsecurity_operation(tmp_path, kind):
    root = tmp_path / f"nonsecurity-startup-{kind}"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = SandboxEnvironment(root, now)
    if kind == OperationKind.SEND_CREDENTIALS:
        _account_id, _rental_id, operation = _running_send_credentials(first, "nonsecurity-credentials")
    else:
        account_id, rental = _activate(first, order_id="nonsecurity-otp")
        first.buyer_code("nonsecurity-otp-command", "nonsecurity-otp", "buyer-1")
        first.application.funpay_poller.poll_once(first.application.last_poll_at, now)
        first.application.funpay_dispatcher.dispatch_pending(now)
        operation = next(
            item
            for item in first.application.repository.pending_operations()
            if item.kind == OperationKind.SEND_OTP and item.rental_id == rental.id
        )
        assert first.application.repository.claim_operation(operation.id, now) is not None
        assert first.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
    first.close()

    restarted = SandboxEnvironment(root, now.replace(second=31))
    try:
        recovered = restarted.application.repository.get_operation(operation.id)
        assert recovered.status == "FAILED"
        assert recovered.recovery_claim_token is None
        assert recovered.normal_claim_token is None
    finally:
        restarted.close()


def test_phase5_stale_send_credentials_claim_is_fenced_before_delivery(sandbox):
    account_id, rental_id, operation = _running_send_credentials(sandbox, "stale-credentials")
    old_token = operation.normal_claim_token
    assert old_token is not None
    stale_now = sandbox.clock.advance(seconds=31)
    assert sandbox.application.repository.recover_expired_leases(stale_now) == 1
    assert not sandbox.application.manager._send_credentials(
        operation, stale_now, normal_claim_token=old_token
    )
    assert sandbox.funpay.message_count(operation.idempotency_key) == 0
    assert sandbox.application.repository.operation_completed(
        operation.id, stale_now, normal_claim_token=old_token
    ) is False
    assert sandbox.application.repository.operation_failed(
        operation.id, stale_now, normal_claim_token=old_token
    ) is False
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert sandbox.application.repository.get_rental(rental_id).status == RentalStatus.MANUAL_REVIEW


def _wait_for_lease_after(sandbox: SandboxEnvironment, operation_id: str, now: datetime) -> None:
    deadline = monotonic() + 2
    while monotonic() < deadline:
        lease_until = sandbox.application.repository.get_operation(operation_id).lease_until
        if lease_until is not None and lease_until > now:
            return
        Event().wait(0.01)
    raise AssertionError("in-flight side-effect guard did not renew the durable lease")


def test_phase5_stale_cycle_time_cannot_renew_expired_normal_lease(sandbox):
    _account_id, _rental_id, operation = _running_send_credentials(sandbox, "stale-cycle-time")
    assert operation.normal_claim_token is not None
    cycle_now = sandbox.clock.now()
    sandbox.clock.advance(seconds=31)

    assert not sandbox.application.manager._send_credentials(
        operation, cycle_now, normal_claim_token=operation.normal_claim_token
    )
    assert sandbox.funpay.message_count(operation.idempotency_key) == 0
    current = sandbox.application.repository.get_operation(operation.id)
    assert current.lease_until == cycle_now.replace(second=30)


def test_phase5_inflight_send_credentials_keeps_durable_ownership(sandbox, monkeypatch):
    account_id, rental_id, operation = _running_send_credentials(sandbox, "inflight-credentials")
    assert operation.normal_claim_token is not None
    started, release = Event(), Event()
    original = sandbox.funpay.send_message

    def blocking_send(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(sandbox.funpay, "send_message", blocking_send)
    result: list[bool] = []
    cycle_now = sandbox.clock.now()
    worker = Thread(
        target=lambda: result.append(
            sandbox.application.manager._send_credentials(
                operation, cycle_now, normal_claim_token=operation.normal_claim_token
            )
        )
    )
    worker.start()
    assert started.wait(2)
    future = sandbox.clock.advance(seconds=31)
    _wait_for_lease_after(sandbox, operation.id, future)

    assert sandbox.application.repository.recover_expired_leases(future) == 0
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert result == [True]
    assert sandbox.application.repository.operation_completed(
        operation.id, future, normal_claim_token=operation.normal_claim_token
    )
    assert sandbox.funpay.message_count(operation.idempotency_key) == 1
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
    assert sandbox.application.repository.get_rental(rental_id).status == RentalStatus.ACTIVE


def test_phase5_inflight_enable_lots_cannot_expose_manual_review_account(sandbox, monkeypatch):
    account_id, rental = _activate(sandbox, order_id="inflight-enable")
    sandbox.clock.advance(seconds=60)
    sandbox.application.repository.expire_due(sandbox.clock.now())
    sandbox.application.manager.run_operations(sandbox.clock.now())
    sandbox.application.manager.run_operations(sandbox.clock.now())
    operation = next(
        item
        for item in sandbox.application.repository.pending_operations()
        if item.kind == OperationKind.ENABLE_LOTS
    )
    claimed = sandbox.application.repository.claim_operation(operation.id, sandbox.clock.now())
    assert claimed is not None and claimed.normal_claim_token is not None
    started, release = Event(), Event()
    original = sandbox.funpay.enable_lots

    def blocking_enable(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(sandbox.funpay, "enable_lots", blocking_enable)
    result: list[bool] = []
    cycle_now = sandbox.clock.now()
    worker = Thread(
        target=lambda: result.append(
            sandbox.application.manager._enable_lots(
                claimed, cycle_now, normal_claim_token=claimed.normal_claim_token
            )
        )
    )
    worker.start()
    assert started.wait(2)
    future = sandbox.clock.advance(seconds=31)
    _wait_for_lease_after(sandbox, claimed.id, future)

    assert sandbox.application.repository.recover_expired_leases(future) == 0
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert result == [True]
    assert sandbox.application.repository.operation_completed(
        claimed.id, future, normal_claim_token=claimed.normal_claim_token
    )
    lot_ids = sandbox.application.repository.account_lot_ids(account_id)
    assert all(sandbox.funpay.get_lot_state(lot) is True for lot in lot_ids)
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE
    assert sandbox.application.repository.get_rental(rental.id).status == RentalStatus.FINISHED


def test_phase5_stale_enable_lots_claim_cannot_expose_manual_review_account(sandbox):
    account_id, rental = _activate(sandbox, order_id="stale-enable")
    sandbox.clock.advance(seconds=60)
    sandbox.application.repository.expire_due(sandbox.clock.now())
    sandbox.application.manager.run_operations(sandbox.clock.now())
    sandbox.application.manager.run_operations(sandbox.clock.now())
    operation = next(
        item
        for item in sandbox.application.repository.pending_operations()
        if item.kind == OperationKind.ENABLE_LOTS
    )
    claimed = sandbox.application.repository.claim_operation(operation.id, sandbox.clock.now())
    assert claimed is not None and claimed.normal_claim_token is not None
    stale_now = sandbox.clock.advance(seconds=31)
    assert sandbox.application.repository.recover_expired_leases(stale_now) == 1
    lot_ids = sandbox.application.repository.account_lot_ids(account_id)
    if sandbox.application.repository.fence_normal_side_effect(
        operation.id, claimed.normal_claim_token, stale_now
    ):
        sandbox.funpay.enable_lots(account_id, lot_ids)
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert all(sandbox.funpay.get_lot_state(lot) is False for lot in lot_ids)
    assert sandbox.application.repository.get_rental(rental.id).status == RentalStatus.MANUAL_REVIEW


@pytest.mark.parametrize("kind", [OperationKind.DISABLE_LOTS, OperationKind.SEND_OTP])
def test_phase5_stale_normal_funpay_claims_are_fenced(sandbox, kind):
    now = sandbox.clock.now()
    if kind == OperationKind.DISABLE_LOTS:
        account_id = sandbox.seed_account()
        started = sandbox.application.manager.accept_order(
            OrderInput("stale-disable", "buyer", "SANDBOX", 60), now
        )
        assert started.rental_id is not None
        operation = next(
            item for item in sandbox.application.repository.pending_operations() if item.kind == kind
        )
        claimed = sandbox.application.repository.claim_operation(operation.id, now)
        assert claimed is not None and claimed.normal_claim_token is not None
        stale_now = sandbox.clock.advance(seconds=31)
        sandbox.application.repository.recover_expired_leases(stale_now)
        lot_ids = sandbox.application.repository.account_lot_ids(account_id)
        if sandbox.application.repository.fence_normal_side_effect(
            operation.id, claimed.normal_claim_token, stale_now
        ):
            sandbox.funpay.disable_lots(account_id, lot_ids)
        assert all(sandbox.funpay.get_lot_state(lot) is True for lot in lot_ids)
    else:
        account_id, rental = _activate(sandbox, order_id="stale-otp")
        sandbox.buyer_code("stale-otp-command", "stale-otp", "buyer-1")
        sandbox.application.funpay_poller.poll_once(sandbox.application.last_poll_at, now)
        sandbox.application.funpay_dispatcher.dispatch_pending(now)
        operation = next(
            item
            for item in sandbox.application.repository.pending_operations()
            if item.kind == kind and item.rental_id == rental.id
        )
        claimed = sandbox.application.repository.claim_operation(operation.id, now)
        assert claimed is not None and claimed.normal_claim_token is not None
        sandbox.login_otp_email("stale-otp-mail", account_id, "445566")
        sandbox.application.gmail_watcher.poll_once(sandbox.application.last_poll_at, now)
        stale_now = sandbox.clock.advance(seconds=31)
        sandbox.application.repository.recover_expired_leases(stale_now)
        assert sandbox.application.manager._send_otp(
            operation, stale_now, normal_claim_token=claimed.normal_claim_token
        ) is not True
        assert sandbox.funpay.message_count(operation.idempotency_key) == 0


def test_phase5_current_normal_owner_is_fenced_and_can_execute_side_effects(sandbox):
    _account_id, _rental_id, credentials = _running_send_credentials(sandbox, "current-owner")
    assert credentials.normal_claim_token is not None
    assert sandbox.application.manager._send_credentials(
        credentials, sandbox.clock.now(), normal_claim_token=credentials.normal_claim_token
    )
    assert sandbox.funpay.message_count(credentials.idempotency_key) == 1


def test_phase5_current_normal_owner_can_enable_lots(sandbox):
    account_id, rental = _activate(sandbox, order_id="current-owner-enable")
    sandbox.clock.advance(seconds=60)
    sandbox.application.repository.expire_due(sandbox.clock.now())
    sandbox.application.manager.run_operations(sandbox.clock.now())
    sandbox.application.manager.run_operations(sandbox.clock.now())
    operation = next(
        item
        for item in sandbox.application.repository.pending_operations()
        if item.kind == OperationKind.ENABLE_LOTS
    )
    claimed = sandbox.application.repository.claim_operation(operation.id, sandbox.clock.now())
    assert claimed is not None and claimed.normal_claim_token is not None
    lot_ids = sandbox.application.repository.account_lot_ids(account_id)
    assert sandbox.application.repository.fence_normal_side_effect(
        operation.id, claimed.normal_claim_token, sandbox.clock.now()
    )
    assert sandbox.funpay.enable_lots(account_id, lot_ids).verified
    assert sandbox.application.repository.operation_completed(
        operation.id, sandbox.clock.now(), normal_claim_token=claimed.normal_claim_token
    )
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE
    assert sandbox.application.repository.get_rental(rental.id).status == RentalStatus.FINISHED


def test_phase5_partial_lot_failure_is_fail_closed_and_notified(sandbox):
    account_id = sandbox.seed_account(lots=2)
    lots = sandbox.application.repository.account_lot_ids(account_id)
    sandbox.funpay.block_lot_transition(lots[-1], target_enabled=False)
    sandbox.paid_order("partial-disable", "order-partial", "buyer", 60)
    _run(sandbox)

    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert sandbox.funpay.get_lot_state(lots[0]) is False
    assert sandbox.funpay.get_lot_state(lots[-1]) is True
    assert any(item.category == "DISABLE_LOTS_VERIFICATION_FAILED" for item in sandbox.owner.notifications)


def test_phase5_partial_lot_enable_is_fail_closed(sandbox):
    account_id, _ = _activate(sandbox, order_id="partial-enable")
    lots = sandbox.application.repository.account_lot_ids(account_id)
    sandbox.funpay.block_lot_transition(lots[-1], target_enabled=True)
    sandbox.clock.advance(seconds=60)
    _run(sandbox, 3)
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert sandbox.funpay.get_lot_state(lots[-1]) is False
    assert any(item.category == "ENABLE_LOTS_VERIFICATION_FAILED" for item in sandbox.owner.notifications)


def test_phase5_same_buyer_extension_preserves_active_lifecycle(sandbox):
    account_id, rental = _activate(sandbox, order_id="extension")
    before = sandbox.application.repository.get_rental(rental.id).expires_at
    credentials = sandbox.funpay.message_count(f"SEND_CREDENTIALS:{rental.id}")
    disables = sandbox.funpay.lot_mutation_count(enabled=False)
    assert sandbox.application.manager.extend_rental(rental.id, "buyer-1", 120, sandbox.clock.now())
    after = sandbox.application.repository.get_rental(rental.id)
    assert after.expires_at > before
    assert after.account_id == account_id
    assert sandbox.application.repository.get_account(account_id).credential_version == 1
    assert sandbox.funpay.message_count(f"SEND_CREDENTIALS:{rental.id}") == credentials
    assert sandbox.funpay.lot_mutation_count(enabled=False) == disables


def test_phase5_expired_rental_buyer_otp_is_denied_through_event_pipeline(sandbox):
    account_id, rental = _activate(sandbox, order_id="expired-otp")
    sandbox.clock.advance(seconds=60)
    _run(sandbox)
    sandbox.login_otp_email("expired-otp-mail", account_id, "123456")
    sandbox.buyer_code("expired-otp-command", "expired-otp", "buyer-1")
    _run(sandbox)
    assert sandbox.application.repository.get_rental(rental.id).status != RentalStatus.ACTIVE
    assert sandbox.funpay.message_count("SEND_OTP:expired-otp-command") == 0
    assert not [op for op in sandbox.application.repository.pending_operations() if op.kind == OperationKind.SEND_OTP]
    assert all(
        sandbox.funpay.get_lot_state(lot) is False
        for lot in sandbox.application.repository.account_lot_ids(account_id)
    ) or sandbox.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE


def test_phase5_security_terminated_buyer_otp_is_denied_through_event_pipeline(sandbox):
    account_id, rental = _activate(sandbox, order_id="security-otp")
    sandbox.password_change_email("security-otp-alert", account_id, "safe-token")
    _run(sandbox)
    sandbox.login_otp_email("security-otp-mail", account_id, "654321")
    sandbox.buyer_code("security-otp-command", "security-otp", "buyer-1")
    _run(sandbox)
    assert sandbox.application.repository.get_rental(rental.id).status != RentalStatus.ACTIVE
    assert sandbox.funpay.message_count("SEND_OTP:security-otp-command") == 0
    assert not [op for op in sandbox.application.repository.pending_operations() if op.kind == OperationKind.SEND_OTP]


def test_phase5_owner_notifications_are_redacted_for_integrated_failures(sandbox):
    markers = ("PHASE5_LOGIN_SECRET", "PHASE5_PASSWORD_SECRET", "PHASE5_OTP_SECRET", "PHASE5_RESET_TOKEN")
    account_id, _ = _activate(sandbox, order_id="owner-redaction")
    sandbox.pixelstorm.set_health(account_id, PixelStormHealth.CHALLENGE)
    sandbox.clock.advance(seconds=60)
    _run(sandbox, 2)
    sandbox.funpay.set_health(FunPayHealth.AUTH_REQUIRED)
    sandbox.paid_order("owner-auth", "owner-auth-order", "buyer-2", 60)
    _run(sandbox)
    categories = {item.category for item in sandbox.owner.notifications}
    assert "PIXEL_STORM_CHALLENGE" in categories
    assert any(category.startswith("FUNPAY_AUTH_REQUIRED") for category in categories)
    for notification in sandbox.owner.notifications:
        rendered = repr(notification)
        assert all(marker not in rendered for marker in markers)


def test_phase5_unexpected_password_mail_revokes_active_access(sandbox):
    account_id, rental = _activate(sandbox)
    sandbox.password_change_email("unexpected-reset", account_id, "reset-token")
    _run(sandbox)

    assert sandbox.application.repository.get_rental(rental.id).status != RentalStatus.ACTIVE
    assert sandbox.application.repository.get_account(account_id).status != AccountStatus.ACTIVE
    assert all(
        sandbox.funpay.get_lot_state(lot) is False
        for lot in sandbox.application.repository.account_lot_ids(account_id)
    )


def test_phase5_disable_lots_crash_restart_verifies_without_second_mutation(sandbox, monkeypatch):
    account_id = sandbox.seed_account()
    sandbox.paid_order("crash-disable", "order-disable", "buyer", 60)
    _crash_after_completion(monkeypatch, sandbox, OperationKind.DISABLE_LOTS)
    with pytest.raises(RuntimeError, match="DISABLE_LOTS"):
        sandbox.run_once()
    assert sandbox.funpay.lot_mutation_count(enabled=False) == 1
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.RESERVED

    sandbox.clock.advance(seconds=31)
    sandbox.restart()
    _run(sandbox, 2)
    assert sandbox.funpay.lot_mutation_count(enabled=False) == 1
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE


def test_phase5_send_credentials_crash_restart_uses_confirmed_receipt(sandbox, monkeypatch):
    account_id = sandbox.seed_account()
    sandbox.paid_order("crash-credentials", "order-credentials", "buyer", 60)
    _crash_after_completion(monkeypatch, sandbox, OperationKind.SEND_CREDENTIALS)
    with pytest.raises(RuntimeError, match="SEND_CREDENTIALS"):
        sandbox.run_once()
    operation = next(
        item
        for item in sandbox.application.repository.running_operations()
        if item.kind == OperationKind.SEND_CREDENTIALS
    )
    assert sandbox.funpay.message_count(operation.idempotency_key) == 1

    sandbox.clock.advance(seconds=31)
    sandbox.restart()
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
    assert sandbox.funpay.message_count(operation.idempotency_key) == 1


def test_phase5_send_otp_crash_restart_uses_confirmed_receipt(sandbox, monkeypatch):
    account_id, _ = _activate(sandbox, order_id="otp-crash")
    sandbox.login_otp_email("otp-before-request", account_id, "654321")
    sandbox.buyer_code("otp-crash-command", "otp-crash", "buyer-1")
    _crash_after_completion(monkeypatch, sandbox, OperationKind.SEND_OTP)
    with pytest.raises(RuntimeError, match="SEND_OTP"):
        sandbox.run_once()
    assert sandbox.funpay.message_count("SEND_OTP:otp-crash-command") == 1

    sandbox.clock.advance(seconds=31)
    sandbox.restart()
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
    assert sandbox.funpay.message_count("SEND_OTP:otp-crash-command") == 1


def test_phase5_enable_lots_crash_restart_verifies_without_second_mutation(sandbox, monkeypatch):
    account_id, _ = _activate(sandbox, order_id="enable-crash")
    sandbox.clock.advance(seconds=60)
    sandbox.run_once()
    _crash_after_completion(monkeypatch, sandbox, OperationKind.ENABLE_LOTS)
    with pytest.raises(RuntimeError, match="ENABLE_LOTS"):
        sandbox.run_once()
    assert sandbox.funpay.lot_mutation_count(enabled=True) == 1

    sandbox.clock.advance(seconds=31)
    sandbox.restart()
    _run(sandbox, 2)
    assert sandbox.funpay.lot_mutation_count(enabled=True) == 1
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE


@pytest.mark.parametrize("kind", [OperationKind.SEND_CREDENTIALS, OperationKind.SEND_OTP])
def test_phase5_ambiguous_message_receipt_recovery_is_manual_review(sandbox, kind):
    if kind == OperationKind.SEND_CREDENTIALS:
        account_id = sandbox.seed_account()
        sandbox.paid_order("ambiguous-credentials", "ambiguous-credentials", "buyer-1", 60)
        now = sandbox.clock.now()
        sandbox.application.funpay_poller.poll_once(sandbox.application.last_poll_at, now)
        sandbox.application.funpay_dispatcher.dispatch_pending(now)
        sandbox.application.manager.run_operations(now)
        rental = sandbox.application.funpay_dispatcher._events.rental_for_order(
            "ambiguous-credentials", "buyer-1"
        )
        assert rental is not None
    else:
        account_id, rental = _activate(sandbox, order_id=f"ambiguous-{kind}")
        sandbox.buyer_code("ambiguous-otp", f"ambiguous-{kind}", "buyer-1")
        _run(sandbox)
    operation = next(
        item
        for item in sandbox.application.repository.pending_operations()
        if item.kind == kind
    )
    assert sandbox.application.repository.claim_operation(operation.id, sandbox.clock.now()) is not None
    sandbox.funpay.inject_ambiguous_receipt(operation.idempotency_key, rental.buyer_id, sandbox.clock.now())
    sandbox.clock.advance(seconds=31)
    sandbox.restart()
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert sandbox.application.repository.get_rental(rental.id).status == RentalStatus.MANUAL_REVIEW


def test_phase5_database_never_contains_credentials_or_otp(sandbox):
    account_id = sandbox.seed_account()
    sandbox.paid_order("secret-scan", "order-secret-scan", "buyer", 60)
    _run(sandbox)
    sandbox.login_otp_email("secret-otp", account_id, "123456")
    sandbox.buyer_code("secret-command", "order-secret-scan", "buyer")
    _run(sandbox)

    contents = (sandbox.root / "application.db").read_bytes()
    assert b"sandbox-password" not in contents
    assert b"sandbox-login" not in contents
    assert b"123456" not in contents


def test_phase5_system_nonsecure_artifacts_redact_unique_secret_markers(sandbox, caplog, capsys, recwarn):
    """Exercise every secret-bearing sandbox flow, then scan every unsafe sink."""
    markers = {
        "login": "PHASE5_LOGIN_SECRET_marker",
        "current_password": "PHASE5_CURRENT_PASSWORD_marker",
        "pending_password": "PHASE5_PENDING_PASSWORD_marker",
        "buyer_otp": "778899",
        "maintenance_otp": "112233",
        "reset_token": "PHASE5_RESET_TOKEN_marker",
        "web_session": "PHASE5_WEB_SESSION_marker",
    }
    account_id = sandbox.seed_account(login=markers["login"], password=markers["current_password"])
    # The session values are confined to the secure-store boundary, exactly as
    # real browser/session adapters require; neither reaches business storage.
    sandbox.secrets.set_funpay_session(account_id, markers["web_session"])
    sandbox.secrets.set_pixelstorm_session(account_id, markers["web_session"])
    sandbox.paid_order("marker-paid", "marker-order", "buyer", 60)
    _run(sandbox)
    sandbox.buyer_code("marker-code", "marker-order", "buyer")
    _run(sandbox)
    sandbox.login_otp_email("marker-buyer-otp", account_id, markers["buyer_otp"])
    _run(sandbox)

    # The expiry branch uses a maintenance OTP and a correlated reset URL.
    sandbox.secrets.set_pending_credentials(account_id, markers["login"], markers["pending_password"])
    sandbox.pixelstorm.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
    sandbox.pixelstorm.set_auth_results(
        account_id, [PixelStormAuthResult.EMAIL_OTP_REQUIRED, PixelStormAuthResult.SUCCESS]
    )
    sandbox.pixelstorm.require_password_email_confirmation(account_id)
    sandbox.clock.advance(seconds=60)
    _run(sandbox)
    sandbox.login_otp_email("marker-maintenance-otp", account_id, markers["maintenance_otp"])
    _run(sandbox, 2)
    sandbox.password_change_email("marker-reset", account_id, markers["reset_token"])
    _run(sandbox, 3)
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE

    marker_bytes = [value.encode() for value in markers.values()]
    business_path = sandbox.root / "application.db"
    assert all(marker not in business_path.read_bytes() for marker in marker_bytes)

    # Explicitly include all durable business projections, not merely raw DB bytes.
    with sqlite3.connect(business_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "accounts",
            "rentals",
            "operations",
            "funpay_events",
            "classified_email_events",
            "audit_events",
        } <= table_names
        for table_name in table_names:
            if table_name.startswith("sqlite_"):
                continue
            rows = connection.execute(f'SELECT * FROM "{table_name}"').fetchall()
            assert all(
                marker.decode() not in repr(row)
                for row in rows
                for marker in marker_bytes
            )

    # Make each normally non-secure browser-artifact family visible to the
    # scan; the recursive runtime scan below covers real artifacts identically.
    artifacts = sandbox.root / "playwright-artifacts"
    for relative in ("trace/trace.zip", "video/video.webm", "screenshots/page.png", "storage_state/state.json", "browser/session.json"):
        target = artifacts / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("non-secret sandbox artifact", encoding="utf-8")

    excluded_secure_or_external = {
        "secure-store.vault",
        "email-secrets.vault",
        "funpay.db",
        "gmail.inbox",
        "pixelstorm.db",
    }
    for path in sandbox.root.rglob("*"):
        if not path.is_file() or path.name in excluded_secure_or_external:
            continue
        contents = path.read_bytes()
        assert all(marker not in contents for marker in marker_bytes), path

    for item in sandbox.owner.notifications:
        assert all(marker not in repr(item).encode() for marker in marker_bytes)
    captured = capsys.readouterr()
    log_and_output = "\n".join(
        [caplog.text, captured.out, captured.err]
        + [str(warning.message) for warning in recwarn]
    ).encode()
    assert all(marker not in log_and_output for marker in marker_bytes)


def test_phase5_external_side_effects_run_without_application_db_transaction(sandbox, monkeypatch):
    account_id = sandbox.seed_account()
    observed: list[int] = []
    original = sandbox.funpay.disable_lots

    def observed_disable(*args, **kwargs):
        observed.append(sandbox.database.active_transactions)
        return original(*args, **kwargs)

    monkeypatch.setattr(sandbox.funpay, "disable_lots", observed_disable)
    sandbox.paid_order("transaction-boundary", "order-boundary", "buyer", 60)
    _run(sandbox)
    assert observed == [0]
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.ACTIVE


def test_phase5_all_critical_external_calls_are_outside_business_transactions(sandbox):
    calls: list[tuple[str, int]] = []

    def probe(kind):
        calls.append((kind, sandbox.database.active_transactions))

    sandbox.funpay.business_transaction_probe = probe
    sandbox.pixelstorm.business_transaction_probe = probe
    account_id = sandbox.seed_account()
    sandbox.paid_order("probe-paid", "probe-order", "buyer", 60)
    _run(sandbox)
    sandbox.login_otp_email("probe-otp", account_id, "445566")
    sandbox.buyer_code("probe-code", "probe-order", "buyer")
    _run(sandbox)
    sandbox.clock.advance(seconds=60)
    _run(sandbox, 3)
    kinds = {kind for kind, _ in calls}
    assert {"DISABLE_LOTS", "SEND_CREDENTIALS", "SEND_OTP", "REVOKE_SESSIONS", "CHANGE_PASSWORD", "ENABLE_LOTS"} <= kinds
    assert all(active == 0 for _, active in calls)


def test_phase5_password_confirmation_external_calls_are_outside_business_transactions(sandbox):
    calls: list[tuple[str, int]] = []

    def probe(kind):
        calls.append((kind, sandbox.database.active_transactions))

    account_id, _ = _activate(sandbox, order_id="probe-confirmation")
    sandbox.pixelstorm.business_transaction_probe = probe
    sandbox.pixelstorm.require_password_email_confirmation(account_id)
    sandbox.clock.advance(seconds=60)
    _run(sandbox, 2)
    sandbox.password_change_email("probe-confirmation-mail", account_id, "safe-token")
    _run(sandbox, 3)
    kinds = {kind for kind, _ in calls}
    assert {"REQUEST_PASSWORD_CHANGE", "COMPLETE_PASSWORD_CHANGE", "VERIFY_CREDENTIALS"} <= kinds
    assert all(active == 0 for _, active in calls)


def test_phase5_readiness_and_safe_cli(tmp_path):
    sandbox = _sandbox(tmp_path / "readiness")
    try:
        with sqlite3.connect(sandbox.root / "application.db") as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0006_phase5_normal_worker_fencing",
            )
        assert sandbox.readiness()["ACCOUNT"] == "NOT_READY:ACCOUNT"
        sandbox.seed_account()
        readiness = sandbox.readiness()
        assert all(value == "READY" for value in readiness.values())
    finally:
        sandbox.close()

    result = subprocess.run(
        [sys.executable, "-m", "app.sandbox", "--scenario", "happy-path"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "sandbox=READY rental=FINISHED account=AVAILABLE dry_run=true" in result.stdout


def test_phase5_production_mode_is_hard_refused():
    with pytest.raises(RuntimeError, match="production runtime is not enabled"):
        create_application(settings=Settings(app_mode="PRODUCTION", dry_run=False))


def test_phase5_clean_shutdown_preserves_waiting_work_for_restart(tmp_path):
    root = tmp_path / "clean-shutdown"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    environment = SandboxEnvironment(root, now)
    try:
        account_id, _ = _activate(environment, order_id="clean-shutdown")
        environment.buyer_code("clean-shutdown-command", "clean-shutdown", "buyer-1")
        _run(environment)
        assert any(
            operation.kind == OperationKind.SEND_OTP
            for operation in environment.application.repository.pending_operations()
        )
    finally:
        environment.close()

    resumed = SandboxEnvironment(root, now)
    try:
        resumed.login_otp_email("clean-shutdown-otp", account_id, "332211")
        _run(resumed)
        assert resumed.funpay.message_count("SEND_OTP:clean-shutdown-command") == 1
    finally:
        resumed.close()


def test_phase5_restart_recovers_persisted_buyer_otp_secret(sandbox):
    account_id, _ = _activate(sandbox, order_id="otp-payload-restart")
    sandbox.buyer_code("otp-payload-command", "otp-payload-restart", "buyer-1")
    _run(sandbox)
    sandbox.login_otp_email("otp-payload-email", account_id, "654321")
    sandbox.application.gmail_watcher.poll_once(sandbox.application.last_poll_at, sandbox.clock.now())
    event = sandbox.application.gmail_watcher._events.get_event("otp-payload-email")
    assert event is not None
    old_email_store = sandbox.email_secrets
    sandbox.restart()
    assert sandbox.email_secrets is not old_email_store
    _run(sandbox)
    assert sandbox.funpay.message_count("SEND_OTP:otp-payload-command") == 1
    assert sandbox.application.gmail_watcher._events.get_event("otp-payload-email").payload_state == "CONSUMED"
    assert b"654321" not in (sandbox.root / "application.db").read_bytes()


def test_phase5_restart_recovers_persisted_password_reset_secret(sandbox):
    account_id, _ = _activate(sandbox, order_id="reset-payload-restart")
    sandbox.pixelstorm.require_password_email_confirmation(account_id)
    sandbox.clock.advance(seconds=60)
    _run(sandbox, 2)
    waiting = next(
        operation
        for operation in sandbox.application.repository.waiting_security_operations()
        if operation.kind == OperationKind.ROTATE_PASSWORD
    )
    assert sandbox.pixelstorm.password_change_requests.count(account_id) == 1
    sandbox.password_change_email("reset-payload-email", account_id, "PHASE5_RESET_TOKEN")
    sandbox.application.gmail_watcher.poll_once(sandbox.application.last_poll_at, sandbox.clock.now())
    old_adapter = sandbox.pixelstorm
    sandbox.restart()
    assert sandbox.pixelstorm is not old_adapter
    _run(sandbox, 3)
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE
    assert sandbox.pixel_backend.counter(account_id, "rotation_count") == 1
    assert b"PHASE5_RESET_TOKEN" not in (sandbox.root / "application.db").read_bytes()
    assert sandbox.application.repository.get_operation(waiting.id).status == "COMPLETED"


@pytest.mark.parametrize("health", [PixelStormHealth.UNAVAILABLE, PixelStormHealth.CHALLENGE])
def test_phase5_readiness_refuses_unrecoverable_pixelstorm(sandbox, health):
    account_id = sandbox.seed_account()
    sandbox.pixelstorm.set_health(account_id, health)
    readiness = sandbox.readiness()
    assert readiness["PIXELSTORM"] == "NOT_READY:PIXELSTORM"
    assert readiness["ACCOUNT"] == "NOT_READY:ACCOUNT"


def test_phase5_readiness_refuses_missing_credentials_lots_and_funpay_auth(sandbox):
    account_id = sandbox.seed_account()
    sandbox.secrets._write({})
    assert sandbox.readiness()["SECURESTORE"] == "NOT_READY:MISSING_CREDENTIALS"
    assert sandbox.readiness()["ACCOUNT"] == "NOT_READY:ACCOUNT"
    sandbox.secrets.set_current_credentials(account_id, "safe", "safe")
    lots = sandbox.application.repository.account_lot_ids(account_id)
    for lot in lots:
        sandbox.funpay.block_lot_transition(lot, target_enabled=True)
        sandbox.funpay.set_lot_state(lot, enabled=False)
    assert sandbox.readiness()["LOTS"] == "NOT_READY:LOTS"
    sandbox.funpay.set_health(FunPayHealth.AUTH_REQUIRED)
    assert sandbox.readiness()["FUNPAY"] == "NOT_READY:AUTH_REQUIRED"


def test_phase5_security_alert_completes_safe_recovery_and_denies_buyer_otp(sandbox):
    account_id, rental = _activate(sandbox, order_id="security-alert-full")
    old = sandbox.secrets.get_current_credentials(account_id)
    assert old is not None
    sandbox.password_change_email("security-alert-mail", account_id, "safe-reset")
    _run(sandbox)
    sandbox.buyer_code("security-alert-otp", "security-alert-full", "buyer-1")
    _run(sandbox, 3)
    account = sandbox.application.repository.get_account(account_id)
    assert account.status == AccountStatus.AVAILABLE
    assert sandbox.application.repository.get_rental(rental.id).status == RentalStatus.FINISHED
    assert account.credential_version == 2
    assert sandbox.pixel_backend.counter(account_id, "revoke_count") == 1
    assert sandbox.pixel_backend.counter(account_id, "rotation_count") == 1
    assert sandbox.pixel_backend.verify(account_id, *old) is False
    assert sandbox.funpay.message_count("SEND_OTP:security-alert-otp") == 0
    assert all(
        sandbox.funpay.get_lot_state(lot) is True
        for lot in sandbox.application.repository.account_lot_ids(account_id)
    )


@pytest.mark.parametrize(
    "health",
    [
        PixelStormHealth.PIXEL_PASS_REQUIRED,
        PixelStormHealth.CHALLENGE,
        PixelStormHealth.WRONG_REGION,
        PixelStormHealth.UNKNOWN_UI,
    ],
)
def test_phase5_pixelstorm_health_matrix_fails_closed(sandbox, health):
    account_id, _ = _activate(sandbox, order_id=f"pixel-{health}")
    sandbox.pixelstorm.set_health(account_id, health)
    sandbox.clock.advance(seconds=60)
    _run(sandbox, 2)
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert sandbox.pixel_backend.counter(account_id, "rotation_count") == 0
    assert all(
        sandbox.funpay.get_lot_state(lot) is False
        for lot in sandbox.application.repository.account_lot_ids(account_id)
    )
    assert sandbox.owner.notifications


def test_phase5_unsupported_revoke_cannot_unlock_lots(sandbox):
    account_id, _ = _activate(sandbox, order_id="unsupported-revoke")
    sandbox.pixelstorm.set_capabilities(
        account_id,
        PixelStormSecurityCapabilities(True, False, False, True),
    )
    sandbox.clock.advance(seconds=60)
    _run(sandbox, 2)
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW
    assert sandbox.pixel_backend.counter(account_id, "rotation_count") == 0
    assert any(item.category == "SESSION_REVOCATION_UNSUPPORTED" for item in sandbox.owner.notifications)


def test_phase5_delayed_maintenance_login_otp_and_deadline(sandbox):
    account_id, _ = _activate(sandbox, order_id="maintenance-otp")
    sandbox.pixelstorm.set_health(account_id, PixelStormHealth.AUTH_REQUIRED)
    sandbox.pixelstorm.set_auth_results(
        account_id, [PixelStormAuthResult.EMAIL_OTP_REQUIRED, PixelStormAuthResult.SUCCESS]
    )
    sandbox.clock.advance(seconds=60)
    _run(sandbox)
    waiting = next(
        operation
        for operation in sandbox.application.repository.waiting_security_operations()
        if operation.kind == OperationKind.REVOKE_SESSIONS
    )
    assert sandbox.pixelstorm.authentication_calls.count(account_id) == 1
    _run(sandbox, 2)
    assert sandbox.pixelstorm.authentication_calls.count(account_id) == 1
    sandbox.login_otp_email("maintenance-otp-mail", account_id, "112233")
    _run(sandbox, 3)
    assert sandbox.application.repository.get_operation(waiting.id).status == "COMPLETED"
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE


def test_phase5_waiting_password_change_deadline_fails_closed(sandbox):
    account_id, _ = _activate(sandbox, order_id="password-deadline")
    sandbox.pixelstorm.require_password_email_confirmation(account_id)
    sandbox.clock.advance(seconds=60)
    _run(sandbox, 2)
    assert sandbox.application.repository.waiting_security_operations()
    sandbox.clock.advance(seconds=901)
    _run(sandbox)
    assert sandbox.application.repository.get_account(account_id).status == AccountStatus.MANUAL_REVIEW


def test_phase5_os_process_paid_order_lifecycle(tmp_path):
    root = tmp_path / "process-paid"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    environment = SandboxEnvironment(root, now)
    account_id = environment.seed_account()
    environment.paid_order("process-paid-event", "process-paid-order", "buyer", 60)
    environment.close()
    context = get_context("spawn")
    with context.Pool(2) as pool:
        pool.starmap(_phase5_process_worker, [(str(root), now, 3), (str(root), now, 3)])
    inspected = SandboxEnvironment(root, now)
    try:
        rental = inspected.application.funpay_dispatcher._events.rental_for_order("process-paid-order", "buyer")
        assert rental is not None and rental.status == RentalStatus.ACTIVE
        assert inspected.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
        assert inspected.funpay.lot_mutation_count(enabled=False) == 1
        assert inspected.funpay.message_count(f"SEND_CREDENTIALS:{rental.id}") == 1
    finally:
        inspected.close()


def test_phase5_os_process_expiry_lifecycle(tmp_path):
    root = tmp_path / "process-expiry"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    environment = SandboxEnvironment(root, now)
    account_id = environment.seed_account()
    old = environment.secrets.get_current_credentials(account_id)
    environment.paid_order("process-expiry-event", "process-expiry-order", "buyer", 60)
    for _ in range(3):
        environment.run_once()
    environment.close()
    expired = now.replace(minute=1)
    context = get_context("spawn")
    with context.Pool(2) as pool:
        pool.starmap(_phase5_process_worker, [(str(root), expired, 4), (str(root), expired, 4)])
    inspected = SandboxEnvironment(root, expired)
    try:
        assert inspected.application.repository.get_account(account_id).status == AccountStatus.AVAILABLE
        assert inspected.pixel_backend.counter(account_id, "revoke_count") == 1
        assert inspected.pixel_backend.counter(account_id, "rotation_count") == 1
        assert inspected.funpay.lot_mutation_count(enabled=True) == 1
        assert old is not None and inspected.pixel_backend.verify(account_id, *old) is False
    finally:
        inspected.close()


def test_phase5_os_process_death_recovers_confirmed_credentials_delivery(tmp_path):
    root = tmp_path / "process-death"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    seeded = SandboxEnvironment(root, now)
    account_id = seeded.seed_account()
    seeded.paid_order("death-event", "death-order", "buyer", 60)
    seeded.close()
    context = get_context("spawn")
    process = context.Process(target=_phase5_crash_after_credentials_worker, args=(str(root), now))
    process.start()
    process.join(30)
    assert process.exitcode == 17
    recovery = context.Process(
        target=_phase5_process_worker, args=(str(root), now.replace(second=31), 3)
    )
    recovery.start()
    recovery.join(30)
    assert recovery.exitcode == 0
    inspected = SandboxEnvironment(root, now.replace(second=31))
    try:
        rental = inspected.application.funpay_dispatcher._events.rental_for_order("death-order", "buyer")
        assert rental is not None and rental.status == RentalStatus.ACTIVE
        assert inspected.application.repository.get_account(account_id).status == AccountStatus.ACTIVE
        assert inspected.funpay.message_count(f"SEND_CREDENTIALS:{rental.id}") == 1
    finally:
        inspected.close()
