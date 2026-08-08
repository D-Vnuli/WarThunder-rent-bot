from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OwnerNotification:
    """Redacted operational signal for a future owner-facing adapter."""

    category: str
    correlation_id: str
    occurred_at: datetime
    account_id: str | None = None
    rental_id: str | None = None
    order_id: str | None = None
    event_id: str | None = None
    safe_error_category: str | None = None
    safe_metadata: str = "{}"
