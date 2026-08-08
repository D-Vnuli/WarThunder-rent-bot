from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class FunPayEventType(StrEnum):
    PAID_ORDER = "PAID_ORDER"
    BUYER_MESSAGE = "BUYER_MESSAGE"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REFUNDED = "ORDER_REFUNDED"
    SYSTEM_MESSAGE = "SYSTEM_MESSAGE"
    UNKNOWN = "UNKNOWN"


class FunPayProcessingStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RETRY_PENDING = "RETRY_PENDING"
    PROCESSED = "PROCESSED"
    FAILED_CLOSED = "FAILED_CLOSED"


class FunPayHealth(StrEnum):
    READY = "READY"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class FunPayEvent:
    external_event_id: str
    event_type: FunPayEventType
    received_at: datetime
    funpay_order_id: str | None = None
    buyer_id: str | None = None
    buyer_handle: str | None = None
    lot_id: str | None = None
    offer_id: str | None = None
    tariff_code: str | None = None
    duration_seconds: int | None = None
    message_text: str | None = None
    safe_metadata: str = "{}"


@dataclass(frozen=True)
class MessageReceipt:
    idempotency_key: str
    conversation_id: str
    external_message_id: str | None
    delivered: bool
    verified: bool
    ambiguous: bool
    occurred_at: datetime
    safe_error_category: str | None = None
