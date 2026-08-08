from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OrderInput:
    funpay_order_id: str
    buyer_id: str
    tariff_code: str
    duration_seconds: int


@dataclass(frozen=True)
class LotOperationResult:
    requested: int
    changed: int
    verified: bool
    failed_lot_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StartResult:
    accepted: bool
    fulfillment_status: str
    rental_id: str | None = None


@dataclass(frozen=True)
class ActiveRental:
    rental_id: str
    account_id: str
    expires_at: datetime


@dataclass(frozen=True)
class RawEmail:
    gmail_message_id: str
    sender: str
    subject: str
    received_at: datetime
    text_body: str
    routing_account_id: str | None = None
    html_body: str | None = None


@dataclass(frozen=True)
class ClassifiedEmailEvent:
    id: str
    gmail_message_id: str
    message_type: str
    received_at: datetime
    routing_account_id: str | None
    correlation_operation_id: str | None
    payload_state: str


@dataclass(frozen=True)
class EmailClassification:
    message_type: str
    sensitive_payload: str | None = None
