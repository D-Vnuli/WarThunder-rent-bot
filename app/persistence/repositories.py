from datetime import UTC, datetime, timedelta
from time import sleep
from uuid import uuid4

from sqlalchemy import exists, or_, select, true, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.domain.models import OrderInput, StartResult
from app.domain.states import (
    AccountStatus,
    FulfillmentStatus,
    OperationKind,
    OperationStatus,
    RentalStatus,
)
from app.domain.transitions import require_account_transition, require_rental_transition
from app.persistence.database import Database
from app.persistence.models import (
    AccountLotRow,
    AccountRow,
    AuditEventRow,
    OperationRow,
    OrderRow,
    RentalRow,
    SecurityEventRow,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class StateConflictError(RuntimeError):
    """The durable account state changed before its expected CAS transition."""


class Repository:
    def __init__(
        self,
        database: Database,
        maintenance_otp_correlation_seconds: int = 300,
        password_change_correlation_seconds: int = 900,
    ) -> None:
        self.db = database
        self._maintenance_otp_correlation_seconds = maintenance_otp_correlation_seconds
        self._password_change_correlation_seconds = password_change_correlation_seconds

    def add_account(self, code: str, now: datetime | None = None) -> str:
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Datetime must be timezone-aware")
        with self.db.session() as session, session.begin():
            row = AccountRow(
                code=code, status=AccountStatus.AVAILABLE, created_at=now, updated_at=now
            )
            session.add(row)
            session.flush()
            return row.id

    def add_account_lot(self, account_id: str, external_lot_id: str, now: datetime) -> str:
        with self.db.session() as session, session.begin():
            row = AccountLotRow(
                account_id=account_id,
                external_lot_id=external_lot_id,
                enabled_expected=True,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return row.id

    def account_lot_ids(self, account_id: str) -> list[str]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(AccountLotRow.external_lot_id)
                    .where(AccountLotRow.account_id == account_id)
                    .order_by(AccountLotRow.external_lot_id)
                )
            )

    def reserve_order(self, order: OrderInput, now: datetime) -> StartResult:
        for attempt in range(5):
            try:
                return self._reserve_order_once(order, now)
            except (IntegrityError, OperationalError):
                # A UNIQUE race or SQLite busy conflict must be resolved by rereading durable state.
                sleep(0.01 * (attempt + 1))
                with self.db.session() as session:
                    existing = session.scalar(
                        select(OrderRow).where(OrderRow.funpay_order_id == order.funpay_order_id)
                    )
                    if existing:
                        rental = session.scalar(
                            select(RentalRow).where(RentalRow.order_id == existing.id)
                        )
                        return StartResult(
                            existing.fulfillment_status != FulfillmentStatus.FULFILLMENT_BLOCKED,
                            existing.fulfillment_status,
                            rental.id if rental else None,
                        )
        raise RuntimeError("SQLite remained unavailable; order was not accepted")

    def _reserve_order_once(self, order: OrderInput, now: datetime) -> StartResult:
        with self.db.session() as session, session.begin():
            existing = session.scalar(
                select(OrderRow).where(OrderRow.funpay_order_id == order.funpay_order_id)
            )
            if existing:
                rental = session.scalar(select(RentalRow).where(RentalRow.order_id == existing.id))
                return StartResult(
                    existing.fulfillment_status != FulfillmentStatus.FULFILLMENT_BLOCKED,
                    existing.fulfillment_status,
                    rental.id if rental else None,
                )
            account = session.scalar(
                select(AccountRow)
                .where(AccountRow.status == AccountStatus.AVAILABLE)
                .order_by(AccountRow.code)
                .limit(1)
            )
            if account is None:
                session.add(
                    OrderRow(
                        funpay_order_id=order.funpay_order_id,
                        buyer_id=order.buyer_id,
                        tariff_code=order.tariff_code,
                        duration_seconds=order.duration_seconds,
                        account_id=None,
                        fulfillment_status=FulfillmentStatus.FULFILLMENT_BLOCKED,
                        received_at=now,
                    )
                )
                self._audit(session, "FULFILLMENT_BLOCKED", None, None, order.funpay_order_id, now)
                return StartResult(False, FulfillmentStatus.FULFILLMENT_BLOCKED)
            try:
                self._transition_account(session, account, AccountStatus.RESERVED, now)
            except StateConflictError as exc:
                raise OperationalError("CAS conflict; reread available accounts", {}, exc) from exc
            db_order = OrderRow(
                funpay_order_id=order.funpay_order_id,
                buyer_id=order.buyer_id,
                tariff_code=order.tariff_code,
                duration_seconds=order.duration_seconds,
                account_id=account.id,
                fulfillment_status=FulfillmentStatus.PENDING,
                received_at=now,
            )
            session.add(db_order)
            session.flush()
            rental = RentalRow(
                order_id=db_order.id,
                buyer_id=order.buyer_id,
                account_id=account.id,
                tariff_code=order.tariff_code,
                started_at=None,
                expires_at=now,
                status=RentalStatus.RESERVED,
                credential_version=account.credential_version,
                created_at=now,
                updated_at=now,
            )
            session.add(rental)
            session.flush()
            self._operation(session, OperationKind.DISABLE_LOTS, account.id, rental.id, now)
            self._audit(
                session, "RENTAL_CREATED", account.id, rental.id, order.funpay_order_id, now
            )
            return StartResult(True, FulfillmentStatus.PENDING, rental.id)

    def pending_operations(self) -> list[OperationRow]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(OperationRow)
                    .where(OperationRow.status == OperationStatus.PENDING)
                    .order_by(OperationRow.created_at)
                )
            )

    def recoverable_message_operations(self, now: datetime) -> list[OperationRow]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(OperationRow).where(
                        OperationRow.kind.in_([OperationKind.SEND_CREDENTIALS, OperationKind.SEND_OTP]),
                        OperationRow.status == OperationStatus.RUNNING,
                        or_(OperationRow.lease_until.is_(None), OperationRow.lease_until < now),
                    )
                )
            )

    def expired_running_operations(self, now: datetime) -> list[OperationRow]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(OperationRow).where(
                        OperationRow.status == OperationStatus.RUNNING,
                        OperationRow.lease_until < now,
                    )
                )
            )

    def running_operations(self) -> list[OperationRow]:
        with self.db.session() as session:
            return list(session.scalars(select(OperationRow).where(OperationRow.status == OperationStatus.RUNNING)))

    def waiting_security_operations(self) -> list[OperationRow]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(OperationRow).where(
                        OperationRow.status == OperationStatus.RUNNING,
                        OperationRow.security_state.in_(["WAITING_LOGIN_OTP", "WAITING_PASSWORD_CHANGE_EMAIL"]),
                    )
                )
            )

    def claim_startup_recovery(self, operation_id: str, now: datetime) -> OperationRow | None:
        """CAS ownership for destructive startup recovery; no Python lock."""
        token = str(uuid4())
        with self.db.session() as session, session.begin():
            claimed = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    # An active normal-worker lease is durable evidence that
                    # startup must not issue a second destructive request.
                    or_(OperationRow.lease_until.is_(None), OperationRow.lease_until < now),
                )
                .values(recovery_claim_token=token, lease_until=now + timedelta(minutes=5))
            )
            if (getattr(claimed, "rowcount", 0) or 0) != 1:
                return None
            operation = session.get(OperationRow, operation_id)
            assert operation is not None
            self._audit(
                session,
                "STARTUP_RECOVERY_CLAIMED",
                operation.account_id,
                operation.rental_id,
                f"{operation_id}:{token}",
                now,
            )
            return operation

    def release_recovery_claim(
        self, operation_id: str, recovery_claim_token: str, now: datetime
    ) -> bool:
        """Release ownership when recovery has durably entered a waiting state."""
        with self.db.session() as session, session.begin():
            released = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.recovery_claim_token == recovery_claim_token,
                    OperationRow.security_state.in_(
                        ["WAITING_LOGIN_OTP", "WAITING_PASSWORD_CHANGE_EMAIL"]
                    ),
                )
                .values(recovery_claim_token=None, lease_until=None)
            )
            return (getattr(released, "rowcount", 0) or 0) == 1

    def claim_operation(
        self, operation_id: str, now: datetime, lease_seconds: int = 30
    ) -> OperationRow | None:
        with self.db.session() as session, session.begin():
            claimed = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.PENDING,
                )
                .values(
                    status=OperationStatus.RUNNING,
                    started_at=now,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    attempt_count=OperationRow.attempt_count + 1,
                    normal_claim_token=str(uuid4()),
                )
            )
            if (getattr(claimed, "rowcount", 0) or 0) != 1:
                return None
            return session.get(OperationRow, operation_id)

    def claim_waiting_security_operation(
        self, operation_id: str, now: datetime, lease_seconds: int = 30
    ) -> OperationRow | None:
        """Assign a fresh normal-worker claim when correlated email work resumes."""
        with self.db.session() as session, session.begin():
            claimed = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.normal_claim_token.is_(None),
                    OperationRow.recovery_claim_token.is_(None),
                    OperationRow.security_state.in_(
                        ["WAITING_LOGIN_OTP", "WAITING_PASSWORD_CHANGE_EMAIL"]
                    ),
                )
                .values(
                    lease_until=now + timedelta(seconds=lease_seconds),
                    attempt_count=OperationRow.attempt_count + 1,
                    normal_claim_token=str(uuid4()),
                )
            )
            if (getattr(claimed, "rowcount", 0) or 0) != 1:
                return None
            return session.get(OperationRow, operation_id)

    def fence_normal_side_effect(
        self, operation_id: str, normal_claim_token: str | None, now: datetime, lease_seconds: int = 30
    ) -> bool:
        """Renew and verify durable ownership immediately before an external call."""
        if normal_claim_token is None:
            return False
        with self.db.session() as session, session.begin():
            fenced = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.normal_claim_token == normal_claim_token,
                    OperationRow.recovery_claim_token.is_(None),
                    OperationRow.lease_until.is_not(None),
                    OperationRow.lease_until >= now,
                )
                .values(lease_until=now + timedelta(seconds=lease_seconds))
            )
            return (getattr(fenced, "rowcount", 0) or 0) == 1

    def fence_recovery_side_effect(
        self, operation_id: str, recovery_claim_token: str | None, now: datetime
    ) -> bool:
        """Verify startup-recovery ownership immediately before an external call."""
        if recovery_claim_token is None:
            return False
        with self.db.session() as session, session.begin():
            fenced = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.recovery_claim_token == recovery_claim_token,
                    OperationRow.lease_until.is_not(None),
                    OperationRow.lease_until >= now,
                )
                .values(lease_until=now + timedelta(minutes=5))
            )
            return (getattr(fenced, "rowcount", 0) or 0) == 1

    def recover_expired_leases(self, now: datetime) -> int:
        """Expired critical work is never blindly retried: quarantine it fail-closed."""
        for attempt in range(5):
            try:
                return self._recover_expired_leases_once(now)
            except OperationalError:
                if attempt == 4:
                    raise
                sleep(0.01 * (attempt + 1))
        raise AssertionError("unreachable")

    def _recover_expired_leases_once(self, now: datetime) -> int:
        with self.db.session() as session, session.begin():
            rows = list(
                session.scalars(
                    select(OperationRow).where(OperationRow.status == OperationStatus.RUNNING)
                )
            )
            recovered_count = 0
            for row in rows:
                wait_deadline = self._external_wait_deadline(row)
                waiting_external = row.security_state in {
                    "WAITING_LOGIN_OTP",
                    "WAITING_PASSWORD_CHANGE_EMAIL",
                }
                if waiting_external and wait_deadline is not None and wait_deadline > now:
                    continue
                if not waiting_external and (row.lease_until is None or row.lease_until >= now):
                    continue
                claimed = session.execute(
                    update(OperationRow)
                    .where(
                        OperationRow.id == row.id,
                        OperationRow.status == OperationStatus.RUNNING,
                        (
                            OperationRow.security_state.in_(
                                ["WAITING_LOGIN_OTP", "WAITING_PASSWORD_CHANGE_EMAIL"]
                            )
                            if waiting_external
                            else OperationRow.lease_until < now
                        ),
                    )
                    .values(
                        status=OperationStatus.FAILED,
                        completed_at=now,
                        lease_until=None,
                        normal_claim_token=None,
                    )
                )
                if (getattr(claimed, "rowcount", 0) or 0) != 1:
                    continue
                recovered_count += 1
                account = session.get(AccountRow, row.account_id)
                rental = session.get(RentalRow, row.rental_id) if row.rental_id else None
                if account:
                    self._transition_account(session, account, AccountStatus.MANUAL_REVIEW, now)
                if rental:
                    self._transition_rental(rental, RentalStatus.MANUAL_REVIEW, now)
                self._audit(
                    session,
                    "LEASE_RECOVERY_MANUAL_REVIEW",
                    row.account_id,
                    row.rental_id,
                    row.id,
                    now,
                )
            return recovered_count

    def prepare_operation(self, operation_id: str, now: datetime) -> OperationRow | None:
        """Persist pre-side-effect state changes before executing critical work."""
        with self.db.session() as session, session.begin():
            operation = session.get(OperationRow, operation_id)
            if operation is None or operation.status != OperationStatus.RUNNING:
                return None
            if operation.kind != OperationKind.REVOKE_SESSIONS:
                return operation
            account = session.get(AccountRow, operation.account_id)
            rental = session.get(RentalRow, operation.rental_id) if operation.rental_id else None
            if account is None or rental is None:
                raise StateConflictError("Revoke operation has no durable rental and account")
            if account.status != AccountStatus.REVOKING:
                self._transition_account(session, account, AccountStatus.REVOKING, now)
            if rental.status != RentalStatus.REVOKING:
                self._transition_rental(rental, RentalStatus.REVOKING, now)
            return operation

    def record_maintenance_login_requested(self, operation_id: str, now: datetime) -> bool:
        with self.db.session() as session, session.begin():
            changed = session.execute(
                update(OperationRow)
                .where(OperationRow.id == operation_id, OperationRow.status == OperationStatus.RUNNING)
                .values(maintenance_login_requested_at=now)
            )
            return (getattr(changed, "rowcount", 0) or 0) == 1

    def record_password_change_requested(self, operation_id: str, now: datetime) -> bool:
        """Durable non-secret request intent, written before the external request."""
        with self.db.session() as session, session.begin():
            changed = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.password_change_requested_at.is_(None),
                )
                .values(password_change_requested_at=now)
            )
            return (getattr(changed, "rowcount", 0) or 0) == 1

    def set_security_state(self, operation_id: str, state: str, now: datetime) -> bool:
        with self.db.session() as session, session.begin():
            changed = session.execute(
                update(OperationRow)
                .where(OperationRow.id == operation_id, OperationRow.status == OperationStatus.RUNNING)
                .values(
                    security_state=state,
                    # External waits deliberately have no active execution
                    # lease; their own bounded deadlines are enforced below.
                    lease_until=(
                        None
                        if state in {"WAITING_LOGIN_OTP", "WAITING_PASSWORD_CHANGE_EMAIL"}
                        else now + timedelta(minutes=5)
                    ),
                    normal_claim_token=(
                        None
                        if state in {"WAITING_LOGIN_OTP", "WAITING_PASSWORD_CHANGE_EMAIL"}
                        else OperationRow.normal_claim_token
                    ),
                )
            )
            return (getattr(changed, "rowcount", 0) or 0) == 1

    def wait_for_buyer_otp(self, operation_id: str, now: datetime) -> bool:
        """Release a buyer-OTP delivery intent until Gmail supplies its secret."""
        with self.db.session() as session, session.begin():
            changed = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.kind == OperationKind.SEND_OTP,
                )
                .values(
                    status=OperationStatus.PENDING,
                    security_state="WAITING_BUYER_OTP",
                    lease_until=None,
                    normal_claim_token=None,
                    completed_at=None,
                )
            )
            return (getattr(changed, "rowcount", 0) or 0) == 1

    def operation_completed(
        self,
        operation_id: str,
        now: datetime,
        recovery_claim_token: str | None = None,
        normal_claim_token: str | None = None,
    ) -> bool:
        if recovery_claim_token is not None:
            return self.complete_recovery_operation(operation_id, recovery_claim_token, now)
        with self.db.session() as session, session.begin():
            claimed = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.recovery_claim_token.is_(None),
                    (
                        OperationRow.normal_claim_token == normal_claim_token
                        if normal_claim_token is not None
                        else true()
                    ),
                )
                .values(
                    status=OperationStatus.COMPLETED,
                    completed_at=now,
                    lease_until=None,
                    normal_claim_token=None,
                    recovery_claim_token=None,
                )
            )
            if (getattr(claimed, "rowcount", 0) or 0) != 1:
                return False
            op = session.get(OperationRow, operation_id)
            assert op is not None
            return self._complete_operation_row(session, op, now)

    def complete_recovery_operation(
        self, operation_id: str, recovery_claim_token: str, now: datetime
    ) -> bool:
        """Finalize recovery only while this worker still owns its durable lease."""
        with self.db.session() as session, session.begin():
            changed = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.recovery_claim_token == recovery_claim_token,
                )
                .values(
                    status=OperationStatus.COMPLETED,
                    completed_at=now,
                    lease_until=None,
                    recovery_claim_token=None,
                )
            )
            if (getattr(changed, "rowcount", 0) or 0) != 1:
                return False
            operation = session.get(OperationRow, operation_id)
            assert operation is not None
            return self._complete_operation_row(session, operation, now)

    def operation_failed(
        self,
        operation_id: str,
        now: datetime,
        recovery_claim_token: str | None = None,
        normal_claim_token: str | None = None,
    ) -> bool:
        if recovery_claim_token is not None:
            return self.fail_recovery_operation(operation_id, recovery_claim_token, now)
        with self.db.session() as session, session.begin():
            changed = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.recovery_claim_token.is_(None),
                    (
                        OperationRow.normal_claim_token == normal_claim_token
                        if normal_claim_token is not None
                        else true()
                    ),
                )
                .values(
                    status=OperationStatus.FAILED,
                    completed_at=now,
                    lease_until=None,
                    normal_claim_token=None,
                    recovery_claim_token=None,
                )
            )
            if (getattr(changed, "rowcount", 0) or 0) != 1:
                return False
            operation = session.get(OperationRow, operation_id)
            assert operation is not None
            return self._fail_operation_row(session, operation, now)

    def fail_recovery_operation(
        self, operation_id: str, recovery_claim_token: str, now: datetime
    ) -> bool:
        """Fail recovery only while this worker still owns its durable lease."""
        with self.db.session() as session, session.begin():
            changed = session.execute(
                update(OperationRow)
                .where(
                    OperationRow.id == operation_id,
                    OperationRow.status == OperationStatus.RUNNING,
                    OperationRow.recovery_claim_token == recovery_claim_token,
                )
                .values(status=OperationStatus.FAILED, completed_at=now, recovery_claim_token=None)
            )
            if (getattr(changed, "rowcount", 0) or 0) != 1:
                return False
            operation = session.get(OperationRow, operation_id)
            assert operation is not None
            return self._fail_operation_row(session, operation, now)

    def expire_due(self, now: datetime) -> int:
        with self.db.session() as session, session.begin():
            due = list(
                session.scalars(
                    select(RentalRow.id).where(
                        RentalRow.status == RentalStatus.ACTIVE, RentalRow.expires_at <= now
                    )
                )
            )
            expired = 0
            for rental_id in due:
                # The conditional write is the durable expiry claim.  A second
                # scheduler can observe the same due row, but cannot transition
                # it (or create another revoke operation) after this succeeds.
                claimed = session.execute(
                    update(RentalRow)
                    .where(RentalRow.id == rental_id, RentalRow.status == RentalStatus.ACTIVE)
                    .values(status=RentalStatus.EXPIRING, updated_at=now)
                )
                if (getattr(claimed, "rowcount", 0) or 0) != 1:
                    continue
                rental = session.get(RentalRow, rental_id)
                assert rental is not None
                account = session.get(AccountRow, rental.account_id)
                if account is None or account.status != AccountStatus.ACTIVE:
                    continue
                self._transition_account(session, account, AccountStatus.EXPIRING, now)
                self._operation(session, OperationKind.REVOKE_SESSIONS, account.id, rental.id, now)
                self._audit(session, "RENTAL_EXPIRED", account.id, rental.id, rental.id, now)
                expired += 1
            return expired

    def reconcile(self, now: datetime) -> int:
        with self.db.session() as session, session.begin():
            rows = list(
                session.scalars(
                    select(AccountRow).where(
                        AccountRow.status.in_(
                            [
                                AccountStatus.RESERVED,
                                AccountStatus.REVOKING,
                                AccountStatus.ROTATING_PASSWORD,
                                AccountStatus.SECURITY_ALERT,
                                AccountStatus.AVAILABLE_OFFLINE,
                            ]
                        )
                    )
                )
            )
            for account in rows:
                has_pending = session.scalar(
                    select(OperationRow)
                    .where(
                        OperationRow.account_id == account.id,
                        OperationRow.status.in_([OperationStatus.PENDING, OperationStatus.RUNNING]),
                    )
                    .limit(1)
                )
                if has_pending is None:
                    self._transition_account(session, account, AccountStatus.MANUAL_REVIEW, now)
                    self._audit(session, "MANUAL_REVIEW", account.id, None, account.id, now)
            return len(rows)

    def transition_account(
        self,
        account_id: str,
        expected_status: AccountStatus,
        expected_state_version: int,
        target: AccountStatus,
        now: datetime,
    ) -> None:
        """Public CAS boundary for application services and state-machine tests."""
        require_account_transition(expected_status, target)
        with self.db.session() as session, session.begin():
            claimed = session.execute(
                update(AccountRow)
                .where(
                    AccountRow.id == account_id,
                    AccountRow.status == expected_status,
                    AccountRow.state_version == expected_state_version,
                )
                .values(
                    status=target,
                    state_version=AccountRow.state_version + 1,
                    updated_at=now,
                )
            )
            if (getattr(claimed, "rowcount", 0) or 0) != 1:
                raise StateConflictError("Stale account status or state_version")

    def extend_active_rental(
        self, rental_id: str, buyer_id: str, extension_seconds: int, now: datetime
    ) -> bool:
        if extension_seconds <= 0:
            raise ValueError("extension_seconds must be positive")
        with self.db.session() as session, session.begin():
            rental = session.get(RentalRow, rental_id)
            if rental is None:
                return False
            account = session.get(AccountRow, rental.account_id)
            if (
                rental.buyer_id != buyer_id
                or rental.status != RentalStatus.ACTIVE
                or account is None
                or account.status != AccountStatus.ACTIVE
            ):
                return False
            previous_expiry = rental.expires_at
            new_expiry = previous_expiry + timedelta(seconds=extension_seconds)
            extended = session.execute(
                update(RentalRow)
                .where(
                    RentalRow.id == rental_id,
                    RentalRow.buyer_id == buyer_id,
                    RentalRow.status == RentalStatus.ACTIVE,
                    RentalRow.expires_at == previous_expiry,
                    exists(
                        select(AccountRow.id).where(
                            AccountRow.id == RentalRow.account_id,
                            AccountRow.status == AccountStatus.ACTIVE,
                        )
                    ),
                )
                .values(expires_at=new_expiry, updated_at=now)
            )
            if (getattr(extended, "rowcount", 0) or 0) != 1:
                return False
            self._audit(
                session,
                "RENTAL_EXTENDED",
                account.id,
                rental.id,
                f"RENTAL_EXTENDED:{rental.id}:{new_expiry.isoformat()}",
                now,
            )
            return True

    def record_active_security_event(
        self, account_id: str, event_type: str, correlation_id: str, now: datetime
    ) -> bool:
        """Route unexpected email security events into the existing fail-closed boundary."""
        with self.db.session() as session, session.begin():
            account = session.get(AccountRow, account_id)
            if account is None:
                return False
            rental = session.scalar(
                select(RentalRow)
                .where(
                    RentalRow.account_id == account_id, RentalRow.status == RentalStatus.ACTIVE
                )
                .limit(1)
            )
            if rental is None or account.status != AccountStatus.ACTIVE:
                return False
            session.add(
                SecurityEventRow(
                    account_id=account_id,
                    rental_id=rental.id,
                    event_type=event_type,
                    severity="CRITICAL",
                    occurred_at=now,
                    safe_metadata="{}",
                )
            )
            self._transition_account(session, account, AccountStatus.SECURITY_ALERT, now)
            self._transition_rental(rental, RentalStatus.SECURITY_TERMINATED, now)
            # The durable security recovery intent is created in the same
            # transaction as the alert; lots intentionally remain disabled.
            self._operation(session, OperationKind.REVOKE_SESSIONS, account.id, rental.id, now)
            self._audit(session, "EMAIL_SECURITY_ALERT", account_id, rental.id, correlation_id, now)
            return True

    def get_account(self, account_id: str) -> AccountRow:
        with self.db.session() as session:
            row = session.get(AccountRow, account_id)
            assert row is not None
            return row

    def get_rental(self, rental_id: str) -> RentalRow:
        with self.db.session() as session:
            row = session.get(RentalRow, rental_id)
            assert row is not None
            return row

    def get_operation(self, operation_id: str) -> OperationRow:
        with self.db.session() as session:
            row = session.get(OperationRow, operation_id)
            assert row is not None
            return row

    def _external_wait_deadline(self, operation: OperationRow) -> datetime | None:
        if operation.security_state == "WAITING_LOGIN_OTP" and operation.maintenance_login_requested_at:
            return operation.maintenance_login_requested_at + timedelta(
                seconds=self._maintenance_otp_correlation_seconds
            )
        if (
            operation.security_state == "WAITING_PASSWORD_CHANGE_EMAIL"
            and operation.password_change_requested_at
        ):
            return operation.password_change_requested_at + timedelta(
                seconds=self._password_change_correlation_seconds
            )
        return None

    def _complete_operation_row(self, session: Session, op: OperationRow, now: datetime) -> bool:
        rental = session.get(RentalRow, op.rental_id) if op.rental_id else None
        account = session.get(AccountRow, op.account_id)
        if account is None:
            return False
        if op.kind == OperationKind.DISABLE_LOTS and rental:
            self._operation(session, OperationKind.SEND_CREDENTIALS, account.id, rental.id, now)
        elif op.kind == OperationKind.SEND_CREDENTIALS and rental:
            self._transition_rental(rental, RentalStatus.ACTIVE, now)
            rental.started_at = now
            order = session.get(OrderRow, rental.order_id)
            if order:
                rental.expires_at = now + timedelta(seconds=order.duration_seconds)
                order.fulfillment_status = FulfillmentStatus.ACTIVATED
            self._transition_account(session, account, AccountStatus.ACTIVE, now)
            self._audit(session, "RENTAL_STARTED", account.id, rental.id, op.idempotency_key, now)
        elif op.kind == OperationKind.REVOKE_SESSIONS and rental:
            self._transition_account(session, account, AccountStatus.ROTATING_PASSWORD, now)
            self._transition_rental(rental, RentalStatus.PASSWORD_ROTATION, now)
            self._operation(session, OperationKind.ROTATE_PASSWORD, account.id, rental.id, now)
        elif op.kind == OperationKind.ROTATE_PASSWORD and rental:
            self._transition_account(session, account, AccountStatus.AVAILABLE_OFFLINE, now)
            account.credential_version += 1
            self._operation(session, OperationKind.ENABLE_LOTS, account.id, rental.id, now)
        elif op.kind == OperationKind.ENABLE_LOTS and rental:
            self._transition_account(session, account, AccountStatus.AVAILABLE, now)
            self._transition_rental(rental, RentalStatus.FINISHED, now)
            self._audit(session, "RENTAL_FINISHED", account.id, rental.id, op.idempotency_key, now)
        return True

    def _fail_operation_row(self, session: Session, operation: OperationRow, now: datetime) -> bool:
        account = session.get(AccountRow, operation.account_id)
        rental = session.get(RentalRow, operation.rental_id) if operation.rental_id else None
        if account:
            self._transition_account(session, account, AccountStatus.MANUAL_REVIEW, now)
        if rental:
            self._transition_rental(rental, RentalStatus.MANUAL_REVIEW, now)
        self._audit(
            session,
            "OPERATION_FAILED",
            operation.account_id,
            operation.rental_id,
            operation.idempotency_key,
            now,
        )
        return True

    @staticmethod
    def _transition_rental(rental: RentalRow, target: RentalStatus, now: datetime) -> None:
        require_rental_transition(rental.status, target)
        rental.status = target
        rental.updated_at = now

    @staticmethod
    def _transition_account(
        session: Session, account: AccountRow, target: AccountStatus, now: datetime
    ) -> None:
        expected_status = AccountStatus(account.status)
        expected_state_version = account.state_version
        require_account_transition(expected_status, target)
        claimed = session.execute(
            update(AccountRow)
            .where(
                AccountRow.id == account.id,
                AccountRow.status == expected_status,
                AccountRow.state_version == expected_state_version,
            )
            .values(
                status=target,
                state_version=AccountRow.state_version + 1,
                updated_at=now,
            )
        )
        if (getattr(claimed, "rowcount", 0) or 0) != 1:
            raise StateConflictError("Stale account status or state_version")
        account.status = target
        account.state_version = expected_state_version + 1
        account.updated_at = now

    def _operation(
        self, session: Session, kind: OperationKind, account_id: str, rental_id: str, now: datetime
    ) -> None:
        session.add(
            OperationRow(
                kind=kind,
                idempotency_key=f"{kind}:{rental_id}",
                status=OperationStatus.PENDING,
                account_id=account_id,
                rental_id=rental_id,
                order_id=None,
                correlation_id=f"{kind}:{rental_id}",
                created_at=now,
            )
        )

    def _audit(
        self,
        session: Session,
        event_type: str,
        account_id: str | None,
        rental_id: str | None,
        correlation_id: str,
        now: datetime,
    ) -> None:
        session.add(
            AuditEventRow(
                event_type=event_type,
                account_id=account_id,
                rental_id=rental_id,
                correlation_id=correlation_id,
                occurred_at=now,
                safe_metadata="{}",
            )
        )
