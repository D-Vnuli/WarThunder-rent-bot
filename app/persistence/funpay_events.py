from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.domain.funpay import FunPayEvent, FunPayProcessingStatus, MessageReceipt
from app.domain.states import AccountStatus, RentalStatus
from app.persistence.database import Database
from app.persistence.models import (
    AccountLotRow,
    AccountRow,
    FunPayEventRow,
    MessageReceiptRow,
    OperationRow,
    OrderRow,
    RentalRow,
)


@dataclass(frozen=True)
class FunPayEventClaim:
    event: FunPayEventRow
    claim_token: str


class FunPayEventRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def ingest(self, event: FunPayEvent, now: datetime) -> bool:
        try:
            with self.db.session() as session, session.begin():
                session.add(
                    FunPayEventRow(
                        external_event_id=event.external_event_id,
                        event_type=event.event_type,
                        funpay_order_id=event.funpay_order_id,
                        buyer_id=event.buyer_id,
                        buyer_handle=event.buyer_handle,
                        lot_id=event.lot_id,
                        offer_id=event.offer_id,
                        tariff_code=event.tariff_code,
                        duration_seconds=event.duration_seconds,
                        message_text=event.message_text,
                        received_at=event.received_at,
                        processing_status=FunPayProcessingStatus.PENDING,
                        correlation_id=event.external_event_id,
                        safe_metadata=event.safe_metadata,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
                return True
        except IntegrityError:
            return False

    def claim_events(self, now: datetime, lease_seconds: int = 30) -> list[FunPayEventClaim]:
        retry_after = now - timedelta(seconds=lease_seconds)
        with self.db.session() as session, session.begin():
            rows = list(
                session.scalars(
                    select(FunPayEventRow)
                    .where(
                        (FunPayEventRow.processing_status == FunPayProcessingStatus.PENDING)
                        | (
                            (FunPayEventRow.processing_status == FunPayProcessingStatus.RETRY_PENDING)
                            & (FunPayEventRow.updated_at <= retry_after)
                        )
                        | (
                            (FunPayEventRow.processing_status == FunPayProcessingStatus.CLAIMED)
                            & (FunPayEventRow.claimed_at < now - timedelta(seconds=lease_seconds))
                        )
                    )
                    .order_by(FunPayEventRow.received_at)
                )
            )
            claims: list[FunPayEventClaim] = []
            for row in rows:
                token = str(uuid4())
                result = session.execute(
                    update(FunPayEventRow)
                    .where(
                        FunPayEventRow.id == row.id,
                        (FunPayEventRow.processing_status == FunPayProcessingStatus.PENDING)
                        | (
                            (FunPayEventRow.processing_status == FunPayProcessingStatus.RETRY_PENDING)
                            & (FunPayEventRow.updated_at <= retry_after)
                        )
                        | (
                            (FunPayEventRow.processing_status == FunPayProcessingStatus.CLAIMED)
                            & (FunPayEventRow.claimed_at < now - timedelta(seconds=lease_seconds))
                        ),
                    )
                    .values(
                        processing_status=FunPayProcessingStatus.CLAIMED,
                        claim_token=token,
                        claimed_at=now,
                        attempt_count=FunPayEventRow.attempt_count + 1,
                        updated_at=now,
                    )
                )
                if (getattr(result, "rowcount", 0) or 0) == 1:
                    claims.append(FunPayEventClaim(row, token))
            return claims

    def mark_processed(self, event_id: str, token: str, now: datetime) -> bool:
        with self.db.session() as session, session.begin():
            result = session.execute(
                update(FunPayEventRow)
                .where(
                    FunPayEventRow.id == event_id,
                    FunPayEventRow.processing_status == FunPayProcessingStatus.CLAIMED,
                    FunPayEventRow.claim_token == token,
                )
                .values(processing_status=FunPayProcessingStatus.PROCESSED, updated_at=now)
            )
            return (getattr(result, "rowcount", 0) or 0) == 1

    def mark_failed_closed(self, event_id: str, token: str, now: datetime) -> None:
        with self.db.session() as session, session.begin():
            session.execute(
                update(FunPayEventRow)
                .where(FunPayEventRow.id == event_id, FunPayEventRow.claim_token == token)
                .values(processing_status=FunPayProcessingStatus.FAILED_CLOSED, updated_at=now)
            )

    def mark_retryable(self, event_id: str, token: str, now: datetime) -> bool:
        with self.db.session() as session, session.begin():
            result = session.execute(
                update(FunPayEventRow)
                .where(
                    FunPayEventRow.id == event_id,
                    FunPayEventRow.processing_status == FunPayProcessingStatus.CLAIMED,
                    FunPayEventRow.claim_token == token,
                )
                .values(
                    processing_status=FunPayProcessingStatus.RETRY_PENDING,
                    claim_token=None,
                    claimed_at=None,
                    updated_at=now,
                )
            )
            return (getattr(result, "rowcount", 0) or 0) == 1

    def add_lot(self, account_id: str, external_lot_id: str, now: datetime) -> str:
        with self.db.session() as session, session.begin():
            lot = AccountLotRow(
                account_id=account_id,
                external_lot_id=external_lot_id,
                enabled_expected=True,
                created_at=now,
                updated_at=now,
            )
            session.add(lot)
            session.flush()
            return lot.id

    def account_lot_ids(self, account_id: str) -> list[str]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(AccountLotRow.external_lot_id).where(AccountLotRow.account_id == account_id)
                )
            )

    def rental_for_order(self, funpay_order_id: str, buyer_id: str) -> RentalRow | None:
        with self.db.session() as session:
            return session.scalar(
                select(RentalRow)
                .join(OrderRow, RentalRow.order_id == OrderRow.id)
                .where(OrderRow.funpay_order_id == funpay_order_id, RentalRow.buyer_id == buyer_id)
                .limit(1)
            )

    def account_is_active(self, rental_id: str) -> bool:
        with self.db.session() as session:
            rental = session.get(RentalRow, rental_id)
            if rental is None:
                return False
            account = session.get(AccountRow, rental.account_id)
            return rental.status == RentalStatus.ACTIVE and account is not None and account.status == AccountStatus.ACTIVE

    def record_receipt(self, receipt: MessageReceipt, event_id: str | None) -> bool:
        try:
            with self.db.session() as session, session.begin():
                session.add(
                    MessageReceiptRow(
                        idempotency_key=receipt.idempotency_key,
                        funpay_event_id=event_id,
                        conversation_id=receipt.conversation_id,
                        external_message_id=receipt.external_message_id,
                        delivery_status="DELIVERED" if receipt.delivered else "FAILED",
                        verified=receipt.verified,
                        ambiguous=receipt.ambiguous,
                        occurred_at=receipt.occurred_at,
                        safe_metadata="{}",
                    )
                )
                session.flush()
                return True
        except IntegrityError:
            return False

    def create_send_otp(self, event: FunPayEventRow, rental: RentalRow, now: datetime) -> bool:
        try:
            with self.db.session() as session, session.begin():
                session.add(
                    OperationRow(
                        kind="SEND_OTP",
                        idempotency_key=f"SEND_OTP:{event.external_event_id}",
                        status="PENDING",
                        account_id=rental.account_id,
                        rental_id=rental.id,
                        order_id=rental.order_id,
                        correlation_id=event.external_event_id,
                        created_at=now,
                    )
                )
                session.flush()
                return True
        except IntegrityError:
            return False

    def receipt(self, idempotency_key: str) -> MessageReceiptRow | None:
        with self.db.session() as session:
            return session.scalar(
                select(MessageReceiptRow).where(MessageReceiptRow.idempotency_key == idempotency_key)
            )
