from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.domain.models import ClassifiedEmailEvent
from app.domain.states import (
    AccountStatus,
    EmailMessageType,
    EmailPayloadState,
    OperationKind,
    OperationStatus,
    RentalStatus,
)
from app.persistence.database import Database
from app.persistence.models import (
    AccountRow,
    ClassifiedEmailEventRow,
    OperationRow,
    OtpRequestRow,
    ProcessedMessageRow,
    RentalRow,
)


@dataclass(frozen=True)
class EmailClaim:
    event_id: str
    claim_token: str
    request_id: str | None = None


@dataclass(frozen=True)
class SecurityEventClaim:
    event: ClassifiedEmailEvent
    claim_token: str


class ClassifiedEmailRepository:
    def __init__(self, database: Database, password_change_correlation_seconds: int = 900, maintenance_otp_correlation_seconds: int = 300) -> None:
        self.db = database
        self._password_change_correlation_seconds = password_change_correlation_seconds
        self._maintenance_otp_correlation_seconds = maintenance_otp_correlation_seconds

    def store_event(self, event: ClassifiedEmailEvent, now: datetime) -> bool:
        """Atomically publish metadata and Gmail ingestion ledger; no payload enters SQLite."""
        try:
            with self.db.session() as session, session.begin():
                session.add(
                    ProcessedMessageRow(
                        source="gmail", external_message_id=event.gmail_message_id, processed_at=now
                    )
                )
                session.add(
                    ClassifiedEmailEventRow(
                        id=event.id,
                        gmail_message_id=event.gmail_message_id,
                        message_type=event.message_type,
                        received_at=event.received_at,
                        routing_account_id=event.routing_account_id,
                        correlation_operation_id=event.correlation_operation_id,
                        payload_state=event.payload_state,
                        security_processing_state=(
                            "NOT_REQUIRED"
                            if event.message_type == EmailMessageType.LOGIN_OTP
                            else "PENDING"
                        ),
                        safe_metadata="{}",
                    )
                )
                session.flush()
                return True
        except IntegrityError:
            return False

    def expected_rotation_operation(
        self, account_id: str | None, received_at: datetime
    ) -> str | None:
        if account_id is None:
            return None
        with self.db.session() as session:
            candidates = list(
                session.scalars(
                    select(OperationRow.id).where(
                        OperationRow.account_id == account_id,
                        OperationRow.kind == OperationKind.ROTATE_PASSWORD,
                        OperationRow.status == OperationStatus.RUNNING,
                        OperationRow.password_change_requested_at.is_not(None),
                        OperationRow.password_change_requested_at <= received_at,
                        OperationRow.password_change_requested_at
                        >= received_at - timedelta(seconds=self._password_change_correlation_seconds),
                    )
                )
            )
            return candidates[0] if len(candidates) == 1 else None

    def claim_security_events(
        self, now: datetime, claim_lease_seconds: int = 30
    ) -> list[SecurityEventClaim]:
        """Claim durable security work; an expired claim is safely recoverable."""
        with self.db.session() as session, session.begin():
            rows = list(
                session.scalars(
                    select(ClassifiedEmailEventRow)
                    .where(
                        ClassifiedEmailEventRow.message_type != EmailMessageType.LOGIN_OTP,
                        (
                            (ClassifiedEmailEventRow.security_processing_state == "PENDING")
                            | (
                                (ClassifiedEmailEventRow.security_processing_state == "CLAIMED")
                                & (ClassifiedEmailEventRow.security_claimed_at < now - timedelta(seconds=claim_lease_seconds))
                            )
                        ),
                    )
                    .order_by(ClassifiedEmailEventRow.received_at)
                )
            )
            claims: list[SecurityEventClaim] = []
            for row in rows:
                token = str(uuid4())
                claimed = session.execute(
                    update(ClassifiedEmailEventRow)
                    .where(
                        ClassifiedEmailEventRow.id == row.id,
                        (
                            (ClassifiedEmailEventRow.security_processing_state == "PENDING")
                            | (
                                (ClassifiedEmailEventRow.security_processing_state == "CLAIMED")
                                & (ClassifiedEmailEventRow.security_claimed_at < now - timedelta(seconds=claim_lease_seconds))
                            )
                        ),
                    )
                    .values(
                        security_processing_state="CLAIMED",
                        security_claim_token=token,
                        security_claimed_at=now,
                    )
                )
                if (getattr(claimed, "rowcount", 0) or 0) != 1:
                    continue
                claims.append(
                    SecurityEventClaim(
                        ClassifiedEmailEvent(
                            id=row.id,
                            gmail_message_id=row.gmail_message_id,
                            message_type=row.message_type,
                            received_at=row.received_at,
                            routing_account_id=row.routing_account_id,
                            correlation_operation_id=row.correlation_operation_id,
                            payload_state=row.payload_state,
                        ),
                        token,
                    )
                )
            return claims

    def mark_security_processed(self, event_id: str, claim_token: str) -> bool:
        with self.db.session() as session, session.begin():
            updated = session.execute(
                update(ClassifiedEmailEventRow)
                .where(
                    ClassifiedEmailEventRow.id == event_id,
                    ClassifiedEmailEventRow.security_processing_state == "CLAIMED",
                    ClassifiedEmailEventRow.security_claim_token == claim_token,
                )
                .values(security_processing_state="PROCESSED")
            )
            return (getattr(updated, "rowcount", 0) or 0) == 1

    def claim_login_otp(
        self,
        rental_id: str,
        buyer_id: str,
        requested_at: datetime,
        now: datetime,
        lookback_seconds: int,
        min_request_interval_seconds: int,
    ) -> EmailClaim | None:
        with self.db.session() as session, session.begin():
            rental = session.get(RentalRow, rental_id)
            if (
                rental is None
                or rental.buyer_id != buyer_id
                or rental.status != RentalStatus.ACTIVE
                or rental.started_at is None
                or now >= rental.expires_at
            ):
                return None
            account = session.get(AccountRow, rental.account_id)
            if account is None or account.status != AccountStatus.ACTIVE:
                return None
            recent_request = session.scalar(
                select(OtpRequestRow.id).where(
                    OtpRequestRow.rental_id == rental_id,
                    OtpRequestRow.buyer_id == buyer_id,
                    OtpRequestRow.requested_at
                    > now - timedelta(seconds=min_request_interval_seconds),
                )
            )
            if recent_request is not None:
                return None
            event = session.scalar(
                select(ClassifiedEmailEventRow)
                .where(
                    ClassifiedEmailEventRow.message_type == EmailMessageType.LOGIN_OTP,
                    ClassifiedEmailEventRow.routing_account_id == rental.account_id,
                    ClassifiedEmailEventRow.claim_token.is_(None),
                    ClassifiedEmailEventRow.payload_state == EmailPayloadState.AVAILABLE,
                    ClassifiedEmailEventRow.received_at >= rental.started_at,
                    ClassifiedEmailEventRow.received_at
                    >= requested_at - timedelta(seconds=lookback_seconds),
                )
                .order_by(ClassifiedEmailEventRow.received_at)
                .limit(1)
            )
            if event is None:
                return None
            claim_token = str(uuid4())
            claimed = session.execute(
                update(ClassifiedEmailEventRow)
                .where(
                    ClassifiedEmailEventRow.id == event.id,
                    ClassifiedEmailEventRow.claim_token.is_(None),
                    ClassifiedEmailEventRow.payload_state == EmailPayloadState.AVAILABLE,
                )
                .values(claim_token=claim_token, claimed_by="otp", claimed_at=now)
            )
            if (getattr(claimed, "rowcount", 0) or 0) != 1:
                return None
            request = OtpRequestRow(
                rental_id=rental_id,
                buyer_id=buyer_id,
                requested_at=requested_at,
                outcome="CLAIMED",
                gmail_message_id=event.gmail_message_id,
            )
            session.add(request)
            session.flush()
            return EmailClaim(event.id, claim_token, request.id)

    def claim_maintenance_login_otp(
        self, account_id: str, operation_id: str, login_requested_at: datetime, now: datetime
    ) -> EmailClaim | None:
        """Claim a fresh maintenance OTP without touching buyer-rental OTPs."""
        with self.db.session() as session, session.begin():
            operation = session.get(OperationRow, operation_id)
            if (
                operation is None
                or operation.account_id != account_id
                or operation.status != OperationStatus.RUNNING
                or operation.kind
                not in {OperationKind.REVOKE_SESSIONS, OperationKind.ROTATE_PASSWORD}
            ):
                return None
            event = session.scalar(
                select(ClassifiedEmailEventRow)
                .where(
                    ClassifiedEmailEventRow.message_type == EmailMessageType.LOGIN_OTP,
                    ClassifiedEmailEventRow.routing_account_id == account_id,
                    ClassifiedEmailEventRow.claim_token.is_(None),
                    ClassifiedEmailEventRow.payload_state == EmailPayloadState.AVAILABLE,
                    ClassifiedEmailEventRow.received_at >= login_requested_at,
                    ClassifiedEmailEventRow.received_at <= now,
                    ClassifiedEmailEventRow.received_at <= login_requested_at + timedelta(seconds=self._maintenance_otp_correlation_seconds),
                )
                .order_by(ClassifiedEmailEventRow.received_at)
                .limit(1)
            )
            if event is None:
                return None
            token = str(uuid4())
            claimed = session.execute(
                update(ClassifiedEmailEventRow)
                .where(
                    ClassifiedEmailEventRow.id == event.id,
                    ClassifiedEmailEventRow.claim_token.is_(None),
                    ClassifiedEmailEventRow.payload_state == EmailPayloadState.AVAILABLE,
                )
                .values(claim_token=token, claimed_by="pixelstorm_maintenance", claimed_at=now)
            )
            if (getattr(claimed, "rowcount", 0) or 0) != 1:
                return None
            return EmailClaim(event.id, token)

    def claim_expected_password_change(
        self, rotation_operation_id: str, now: datetime
    ) -> EmailClaim | None:
        with self.db.session() as session, session.begin():
            event = session.scalar(
                select(ClassifiedEmailEventRow)
                .where(
                    ClassifiedEmailEventRow.message_type == EmailMessageType.PASSWORD_CHANGE,
                    ClassifiedEmailEventRow.correlation_operation_id == rotation_operation_id,
                    ClassifiedEmailEventRow.claim_token.is_(None),
                    ClassifiedEmailEventRow.payload_state == EmailPayloadState.AVAILABLE,
                )
                .order_by(ClassifiedEmailEventRow.received_at)
                .limit(1)
            )
            if event is None:
                return None
            token = str(uuid4())
            claimed = session.execute(
                update(ClassifiedEmailEventRow)
                .where(
                    ClassifiedEmailEventRow.id == event.id,
                    ClassifiedEmailEventRow.claim_token.is_(None),
                    ClassifiedEmailEventRow.payload_state == EmailPayloadState.AVAILABLE,
                )
                .values(claim_token=token, claimed_by="password_rotator", claimed_at=now)
            )
            if (getattr(claimed, "rowcount", 0) != 1):
                return None
            return EmailClaim(event.id, token)

    def mark_claim_consumed(self, claim: EmailClaim) -> None:
        with self.db.session() as session, session.begin():
            session.execute(
                update(ClassifiedEmailEventRow)
                .where(
                    ClassifiedEmailEventRow.id == claim.event_id,
                    ClassifiedEmailEventRow.claim_token == claim.claim_token,
                )
                .values(payload_state=EmailPayloadState.CONSUMED)
            )
            if claim.request_id:
                request = session.get(OtpRequestRow, claim.request_id)
                if request is not None:
                    request.outcome = "DELIVERED"

    def mark_claim_unusable(self, claim: EmailClaim) -> None:
        with self.db.session() as session, session.begin():
            session.execute(
                update(ClassifiedEmailEventRow)
                .where(
                    ClassifiedEmailEventRow.id == claim.event_id,
                    ClassifiedEmailEventRow.claim_token == claim.claim_token,
                )
                .values(payload_state=EmailPayloadState.UNUSABLE_EXPIRED)
            )
            if claim.request_id:
                request = session.get(OtpRequestRow, claim.request_id)
                if request is not None:
                    request.outcome = "UNUSABLE_EXPIRED"

    def get_event(self, gmail_message_id: str) -> ClassifiedEmailEventRow | None:
        with self.db.session() as session:
            return session.scalar(
                select(ClassifiedEmailEventRow).where(
                    ClassifiedEmailEventRow.gmail_message_id == gmail_message_id
                )
            )
