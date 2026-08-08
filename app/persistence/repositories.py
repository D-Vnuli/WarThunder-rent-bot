from datetime import UTC, datetime, timedelta
from time import sleep

from sqlalchemy import exists, select, update
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
    def __init__(self, database: Database) -> None:
        self.db = database

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
                )
            )
            if (getattr(claimed, "rowcount", 0) or 0) != 1:
                return None
            return session.get(OperationRow, operation_id)

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
                    select(OperationRow).where(
                        OperationRow.status == OperationStatus.RUNNING,
                        OperationRow.lease_until < now,
                    )
                )
            )
            recovered_count = 0
            for row in rows:
                claimed = session.execute(
                    update(OperationRow)
                    .where(
                        OperationRow.id == row.id,
                        OperationRow.status == OperationStatus.RUNNING,
                        OperationRow.lease_until < now,
                    )
                    .values(status=OperationStatus.FAILED, completed_at=now, lease_until=None)
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
            self._transition_account(session, account, AccountStatus.REVOKING, now)
            self._transition_rental(rental, RentalStatus.REVOKING, now)
            return operation

    def operation_completed(self, operation_id: str, now: datetime) -> None:
        with self.db.session() as session, session.begin():
            op = session.get(OperationRow, operation_id)
            if op is None or op.status == OperationStatus.COMPLETED:
                return
            op.status, op.completed_at, op.lease_until = OperationStatus.COMPLETED, now, None
            rental = session.get(RentalRow, op.rental_id) if op.rental_id else None
            account = session.get(AccountRow, op.account_id)
            if account is None:
                return
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
                self._audit(
                    session, "RENTAL_STARTED", account.id, rental.id, op.idempotency_key, now
                )
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
                self._audit(
                    session, "RENTAL_FINISHED", account.id, rental.id, op.idempotency_key, now
                )

    def operation_failed(self, operation_id: str, now: datetime) -> None:
        with self.db.session() as session, session.begin():
            operation = session.get(OperationRow, operation_id)
            if operation is None or operation.status == OperationStatus.COMPLETED:
                return
            operation.status, operation.completed_at = OperationStatus.FAILED, now
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

    def expire_due(self, now: datetime) -> int:
        with self.db.session() as session, session.begin():
            due = list(
                session.scalars(
                    select(RentalRow).where(
                        RentalRow.status == RentalStatus.ACTIVE, RentalRow.expires_at <= now
                    )
                )
            )
            for rental in due:
                account = session.get(AccountRow, rental.account_id)
                if account is None:
                    continue
                self._transition_account(session, account, AccountStatus.EXPIRING, now)
                self._transition_rental(rental, RentalStatus.EXPIRING, now)
                self._operation(session, OperationKind.REVOKE_SESSIONS, account.id, rental.id, now)
                self._audit(session, "RENTAL_EXPIRED", account.id, rental.id, rental.id, now)
            return len(due)

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
                        OperationRow.status == OperationStatus.PENDING,
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
