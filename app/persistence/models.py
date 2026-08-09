from datetime import datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.persistence.types import UTCDateTime


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class AccountRow(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    credential_version: Mapped[int] = mapped_column(Integer, default=1)
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    rotation_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    funpay_order_id: Mapped[str] = mapped_column(String(128), unique=True)
    buyer_id: Mapped[str] = mapped_column(String(128))
    tariff_code: Mapped[str] = mapped_column(String(64))
    duration_seconds: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    fulfillment_status: Mapped[str] = mapped_column(String(32), index=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    safe_metadata: Mapped[str] = mapped_column(Text, default="{}")


class RentalRow(Base):
    __tablename__ = "rentals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    buyer_id: Mapped[str] = mapped_column(String(128))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    tariff_code: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    credential_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class OperationRow(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    rental_id: Mapped[str | None] = mapped_column(ForeignKey("rentals.id"), nullable=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(180))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    maintenance_login_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    password_change_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    security_state: Mapped[str] = mapped_column(String(48), default="INIT")
    recovery_claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    safe_metadata: Mapped[str] = mapped_column(Text, default="{}")


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("event_type", "correlation_id", name="uq_audit_correlation"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())
    event_type: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rental_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(180))
    safe_metadata: Mapped[str] = mapped_column(Text, default="{}")


class ProcessedMessageRow(Base):
    __tablename__ = "processed_messages"
    __table_args__ = (UniqueConstraint("source", "external_message_id", name="uq_processed_message"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32))
    external_message_id: Mapped[str] = mapped_column(String(255))
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ClassifiedEmailEventRow(Base):
    __tablename__ = "classified_email_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gmail_message_id: Mapped[str] = mapped_column(String(255), unique=True)
    message_type: Mapped[str] = mapped_column(String(32), index=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())
    routing_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    correlation_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations.id"), nullable=True, index=True
    )
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    payload_state: Mapped[str] = mapped_column(String(32))
    security_processing_state: Mapped[str] = mapped_column(String(32), default="PENDING")
    security_claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    security_claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    safe_metadata: Mapped[str] = mapped_column(Text, default="{}")


class OtpRequestRow(Base):
    __tablename__ = "otp_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rental_id: Mapped[str] = mapped_column(ForeignKey("rentals.id"), index=True)
    buyer_id: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime())
    outcome: Mapped[str] = mapped_column(String(32))
    gmail_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("classified_email_events.gmail_message_id"), unique=True, nullable=True
    )


class SecurityEventRow(Base):
    __tablename__ = "security_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    rental_id: Mapped[str | None] = mapped_column(ForeignKey("rentals.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())
    safe_metadata: Mapped[str] = mapped_column(Text, default="{}")


class AccountLotRow(Base):
    __tablename__ = "account_lots"
    __table_args__ = (UniqueConstraint("external_lot_id", name="uq_account_lot_external"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    external_lot_id: Mapped[str] = mapped_column(String(128))
    enabled_expected: Mapped[bool] = mapped_column(default=True)
    safe_metadata: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class FunPayEventRow(Base):
    __tablename__ = "funpay_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_event_id: Mapped[str] = mapped_column(String(180), unique=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    funpay_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    buyer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    buyer_handle: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    offer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tariff_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())
    processing_status: Mapped[str] = mapped_column(String(32), index=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str] = mapped_column(String(180))
    safe_metadata: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MessageReceiptRow(Base):
    __tablename__ = "message_receipts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    funpay_event_id: Mapped[str | None] = mapped_column(ForeignKey("funpay_events.id"), nullable=True)
    conversation_id: Mapped[str] = mapped_column(String(128))
    external_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(32))
    verified: Mapped[bool] = mapped_column(default=False)
    ambiguous: Mapped[bool] = mapped_column(default=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())
    safe_metadata: Mapped[str] = mapped_column(Text, default="{}")
