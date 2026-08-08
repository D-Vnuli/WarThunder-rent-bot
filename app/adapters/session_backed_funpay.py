from collections.abc import Sequence
from datetime import datetime

from app.domain.funpay import FunPayEvent, FunPayHealth, MessageReceipt
from app.domain.models import LotOperationResult
from app.domain.ports import FunPayTransport, WebSessionStore


class SessionBackedFunPayAdapter:
    """Runtime gate that obtains a FunPay web session only from WebSessionStore."""

    def __init__(self, account_id: str, sessions: WebSessionStore, transport: FunPayTransport) -> None:
        self._account_id = account_id
        self._sessions = sessions
        self._transport = transport

    def _session(self) -> str | None:
        session = self._sessions.get_funpay_session(self._account_id)
        if session is None:
            return None
        health = self._transport.health(session)
        if health == FunPayHealth.AUTH_REQUIRED:
            self._sessions.clear_funpay_session(self._account_id)
            return None
        return session

    def health(self) -> FunPayHealth:
        session = self._sessions.get_funpay_session(self._account_id)
        if session is None:
            return FunPayHealth.AUTH_REQUIRED
        health = self._transport.health(session)
        if health == FunPayHealth.AUTH_REQUIRED:
            self._sessions.clear_funpay_session(self._account_id)
        return health

    def poll_events(self, *, after: datetime) -> Sequence[FunPayEvent]:
        session = self._ready_session()
        return () if session is None else self._transport.poll_events(session, after=after)

    def get_order(self, funpay_order_id: str) -> FunPayEvent | None:
        session = self._ready_session()
        return None if session is None else self._transport.get_order(session, funpay_order_id)

    def send_message(self, buyer_id: str, text: str, *, idempotency_key: str, now: datetime) -> MessageReceipt:
        session = self._ready_session()
        if session is None:
            return MessageReceipt(idempotency_key, buyer_id, None, False, False, False, now, "AUTH_REQUIRED")
        return self._transport.send_message(session, buyer_id, text, idempotency_key=idempotency_key, now=now)

    def get_message_receipt(self, idempotency_key: str) -> MessageReceipt | None:
        session = self._ready_session()
        return None if session is None else self._transport.get_message_receipt(session, idempotency_key)

    def get_lot_state(self, external_lot_id: str) -> bool | None:
        session = self._ready_session()
        return None if session is None else self._transport.get_lot_state(session, external_lot_id)

    def verify_lots_disabled(self, external_lot_ids: Sequence[str]) -> LotOperationResult:
        session = self._ready_session()
        return self._not_ready(external_lot_ids) if session is None else self._transport.verify_lots_disabled(session, external_lot_ids)

    def verify_lots_enabled(self, external_lot_ids: Sequence[str]) -> LotOperationResult:
        session = self._ready_session()
        return self._not_ready(external_lot_ids) if session is None else self._transport.verify_lots_enabled(session, external_lot_ids)

    def disable_lots(self, account_id: str, external_lot_ids: Sequence[str]) -> LotOperationResult:
        session = self._ready_session()
        return self._not_ready(external_lot_ids) if session is None else self._transport.disable_lots(session, account_id, external_lot_ids)

    def enable_lots(self, account_id: str, external_lot_ids: Sequence[str]) -> LotOperationResult:
        session = self._ready_session()
        return self._not_ready(external_lot_ids) if session is None else self._transport.enable_lots(session, account_id, external_lot_ids)

    def _ready_session(self) -> str | None:
        return self._session() if self.health() == FunPayHealth.READY else None

    @staticmethod
    def _not_ready(lot_ids: Sequence[str]) -> LotOperationResult:
        return LotOperationResult(len(lot_ids), 0, False, tuple(lot_ids), safe_error_category="AUTH_REQUIRED")
