import sqlite3
from datetime import datetime
from threading import Lock

from app.domain.funpay import FunPayEvent, FunPayHealth, MessageReceipt
from app.domain.models import LotOperationResult, RawEmail
from app.domain.notifications import OwnerNotification


class FakeFunPayAdapter:
    def __init__(self, backend=None) -> None:
        self.lots_enabled = True
        self.calls: list[str] = []
        self.fail_next: set[str] = set()
        self._events: list[FunPayEvent] = []
        self._lots: dict[str, bool] = {}
        self.lot_operations: list[tuple[str, tuple[str, ...]]] = []
        self._receipts: dict[str, MessageReceipt] = {}
        self.message_send_count = 0
        self._health = FunPayHealth.READY
        self._backend = backend

    def add_event(self, event: FunPayEvent) -> None:
        self._events.append(event)

    def poll_events(self, *, after: datetime) -> list[FunPayEvent]:
        return [event for event in self._events if event.received_at >= after]

    def get_order(self, funpay_order_id: str) -> FunPayEvent | None:
        return next((event for event in self._events if event.funpay_order_id == funpay_order_id), None)

    def set_lot_state(self, external_lot_id: str, *, enabled: bool) -> None:
        self._lots[external_lot_id] = enabled

    def set_health(self, health: FunPayHealth) -> None:
        self._health = health

    def health(self) -> FunPayHealth:
        return self._health

    def get_lot_state(self, external_lot_id: str) -> bool | None:
        return self._lots.get(external_lot_id)

    def verify_lots_disabled(self, external_lot_ids: list[str]) -> LotOperationResult:
        states = tuple((lot_id, self._lots.get(lot_id, True)) for lot_id in external_lot_ids)
        failed = tuple(lot_id for lot_id, enabled in states if enabled)
        return LotOperationResult(
            len(external_lot_ids),
            len(external_lot_ids) - len(failed),
            not failed,
            failed,
            states,
        )

    def verify_lots_enabled(self, external_lot_ids: list[str]) -> LotOperationResult:
        states = tuple((lot_id, self._lots.get(lot_id, False)) for lot_id in external_lot_ids)
        failed = tuple(lot_id for lot_id, enabled in states if not enabled)
        return LotOperationResult(
            len(external_lot_ids),
            len(external_lot_ids) - len(failed),
            not failed,
            failed,
            states,
        )

    def send_message(
        self, buyer_id: str, text: str, *, idempotency_key: str, now: datetime
    ) -> MessageReceipt:
        existing = self._receipts.get(idempotency_key)
        if self._backend is not None:
            existing = self._backend.get(idempotency_key)
        if existing is not None:
            return existing
        self.message_send_count += 1
        ambiguous = "message_ambiguous" in self.fail_next
        delivered = not ambiguous and "message" not in self.fail_next
        receipt = MessageReceipt(
            idempotency_key,
            buyer_id,
            f"fake-message-{len(self._receipts) + 1}" if delivered else None,
            delivered,
            delivered,
            ambiguous,
            now,
            "AMBIGUOUS" if ambiguous else ("FAILED" if not delivered else None),
        )
        self._receipts[idempotency_key] = receipt
        if self._backend is not None:
            self._backend.put(receipt)
        return receipt

    def get_message_receipt(self, idempotency_key: str) -> MessageReceipt | None:
        return self._backend.get(idempotency_key) if self._backend is not None else self._receipts.get(idempotency_key)

    def disable_lots(self, account_id: str, external_lot_ids: list[str]) -> LotOperationResult:
        self.calls.append(f"disable:{account_id}")
        lot_ids = list(external_lot_ids)
        self.lot_operations.append(("disable", tuple(lot_ids)))
        if "disable" in self.fail_next or self._health != FunPayHealth.READY:
            return LotOperationResult(len(lot_ids), 0, False, tuple(lot_ids))
        changed_lot_ids = lot_ids[:-1] if "disable_partial" in self.fail_next else lot_ids
        for lot_id in changed_lot_ids:
            if lot_id in self._lots:
                self._lots[lot_id] = False
        self.lots_enabled = False
        return self.verify_lots_disabled(lot_ids)

    def enable_lots(self, account_id: str, external_lot_ids: list[str]) -> LotOperationResult:
        self.calls.append(f"enable:{account_id}")
        lot_ids = list(external_lot_ids)
        self.lot_operations.append(("enable", tuple(lot_ids)))
        if "enable" in self.fail_next or self._health != FunPayHealth.READY:
            return LotOperationResult(len(lot_ids), 0, False, tuple(lot_ids))
        changed_lot_ids = lot_ids[:-1] if "enable_partial" in self.fail_next else lot_ids
        for lot_id in changed_lot_ids:
            if lot_id in self._lots:
                self._lots[lot_id] = True
        self.lots_enabled = True
        return self.verify_lots_enabled(lot_ids)


class FakeOwnerNotifier:
    """Test-only sink; deduplication prevents repeated retry notifications."""

    def __init__(self) -> None:
        self.notifications: list[OwnerNotification] = []
        self._seen: set[tuple[str, str]] = set()

    def notify(self, notification: OwnerNotification) -> None:
        key = (notification.category, notification.correlation_id)
        if key not in self._seen:
            self._seen.add(key)
            self.notifications.append(notification)


class FakeFunPayTransport:
    """Session-aware fake transport; it never knows how sessions are persisted."""

    def __init__(self, adapter: FakeFunPayAdapter, *, valid_sessions: set[str]) -> None:
        self._adapter = adapter
        self._valid_sessions = valid_sessions
        self._health = FunPayHealth.READY
        self._challenge = False
        self.poll_calls = 0

    def set_health(self, health: FunPayHealth) -> None:
        self._health = health

    def set_challenge(self, value: bool) -> None:
        self._challenge = value

    def health(self, session: str) -> FunPayHealth:
        if self._challenge or session not in self._valid_sessions:
            return FunPayHealth.AUTH_REQUIRED
        return self._health

    def poll_events(self, session: str, *, after: datetime):
        self.poll_calls += 1
        return () if self.health(session) != FunPayHealth.READY else self._adapter.poll_events(after=after)

    def get_order(self, session: str, funpay_order_id: str):
        return None if self.health(session) != FunPayHealth.READY else self._adapter.get_order(funpay_order_id)

    def send_message(self, session: str, buyer_id: str, text: str, *, idempotency_key: str, now: datetime) -> MessageReceipt:
        if self.health(session) != FunPayHealth.READY:
            return MessageReceipt(idempotency_key, buyer_id, None, False, False, False, now, "AUTH_REQUIRED")
        return self._adapter.send_message(buyer_id, text, idempotency_key=idempotency_key, now=now)

    def get_message_receipt(self, session: str, idempotency_key: str) -> MessageReceipt | None:
        return None if self.health(session) != FunPayHealth.READY else self._adapter.get_message_receipt(idempotency_key)

    def get_lot_state(self, session: str, external_lot_id: str) -> bool | None:
        return None if self.health(session) != FunPayHealth.READY else self._adapter.get_lot_state(external_lot_id)

    def verify_lots_disabled(self, session: str, external_lot_ids):
        return self._not_ready(external_lot_ids) if self.health(session) != FunPayHealth.READY else self._adapter.verify_lots_disabled(list(external_lot_ids))

    def verify_lots_enabled(self, session: str, external_lot_ids):
        return self._not_ready(external_lot_ids) if self.health(session) != FunPayHealth.READY else self._adapter.verify_lots_enabled(list(external_lot_ids))

    def disable_lots(self, session: str, account_id: str, external_lot_ids):
        return self._not_ready(external_lot_ids) if self.health(session) != FunPayHealth.READY else self._adapter.disable_lots(account_id, list(external_lot_ids))

    def enable_lots(self, session: str, account_id: str, external_lot_ids):
        return self._not_ready(external_lot_ids) if self.health(session) != FunPayHealth.READY else self._adapter.enable_lots(account_id, list(external_lot_ids))

    @staticmethod
    def _not_ready(lot_ids) -> LotOperationResult:
        ids = tuple(lot_ids)
        return LotOperationResult(len(ids), 0, False, ids, safe_error_category="AUTH_REQUIRED")


class PersistentFakeFunPayBackend:
    def __init__(self, path: str) -> None:
        self._path = path
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS receipts (k TEXT PRIMARY KEY, msg TEXT, delivered INTEGER, verified INTEGER, ambiguous INTEGER, occurred TEXT, count INTEGER)")

    def get(self, key: str) -> MessageReceipt | None:
        with sqlite3.connect(self._path) as connection:
            row = connection.execute("SELECT msg, delivered, verified, ambiguous, occurred FROM receipts WHERE k=?", (key,)).fetchone()
        if row is None:
            return None
        return MessageReceipt(key, "safe", row[0], bool(row[1]), bool(row[2]), bool(row[3]), datetime.fromisoformat(row[4]))

    def put(self, receipt: MessageReceipt) -> None:
        with sqlite3.connect(self._path) as connection:
            connection.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?, ?, ?, ?, ?, 1)", (receipt.idempotency_key, receipt.external_message_id, int(receipt.delivered), int(receipt.verified), int(receipt.ambiguous), receipt.occurred_at.isoformat()))

    def send_count(self, key: str) -> int:
        with sqlite3.connect(self._path) as connection:
            row = connection.execute("SELECT count FROM receipts WHERE k=?", (key,)).fetchone()
        return 0 if row is None else row[0]



class FakeGaijinController:
    def __init__(self) -> None:
        self.revoked: list[str] = []
        self.rotated: list[str] = []

    def revoke_sessions(self, account_id: str) -> bool:
        self.revoked.append(account_id)
        return True

    def rotate_password(self, account_id: str) -> bool:
        self.rotated.append(account_id)
        return True

    def verify_access(self, account_id: str) -> bool:
        return account_id in self.rotated


class FakeSecureStore:
    """In-memory test double; it deliberately never persists secrets."""

    def __init__(self) -> None:
        self._current: dict[str, str] = {}
        self._pending: dict[str, str] = {}
        self._credentials: dict[str, tuple[str, str]] = {}

    def set_current(self, account_id: str, value: str) -> None:
        self._current[account_id] = value

    def set_pending(self, account_id: str, value: str) -> None:
        self._pending[account_id] = value

    def promote_pending(self, account_id: str) -> None:
        self._current[account_id] = self._pending.pop(account_id)

    def has_pending(self, account_id: str) -> bool:
        return account_id in self._pending

    def set_current_credentials(self, account_id: str, login: str, password: str) -> None:
        self._credentials[account_id] = (login, password)

    def get_current_credentials(self, account_id: str) -> tuple[str, str] | None:
        return self._credentials.get(account_id)

    def get_funpay_session(self, account_id: str) -> str | None:
        return self._current.get(f"session:{account_id}")

    def set_funpay_session(self, account_id: str, value: str) -> None:
        self._current[f"session:{account_id}"] = value

    def clear_funpay_session(self, account_id: str) -> None:
        self._current.pop(f"session:{account_id}", None)


class FakeGmailAdapter:
    def __init__(self, messages: list[RawEmail] | None = None) -> None:
        self.messages = messages or []

    def get_new_messages(self, *, after: datetime) -> list[RawEmail]:
        return [message for message in self.messages if message.received_at >= after]


class FakeEphemeralEmailSecretStore:
    """In-memory TTL/one-time store: no payload is ever persisted to SQLite."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, datetime]] = {}
        self._lock = Lock()

    def put(self, event_id: str, payload: str, *, expires_at: datetime) -> bool:
        with self._lock:
            if event_id in self._entries:
                return False
            self._entries[event_id] = (payload, expires_at)
            return True

    def consume_once(self, event_id: str, *, claim_token: str, now: datetime) -> str | None:
        del claim_token
        with self._lock:
            entry = self._entries.pop(event_id, None)
            if entry is None or entry[1] <= now:
                return None
            return entry[0]

    def discard(self, event_id: str) -> None:
        with self._lock:
            self._entries.pop(event_id, None)

    def purge_expired(self, now: datetime) -> int:
        with self._lock:
            expired = [event_id for event_id, (_, deadline) in self._entries.items() if deadline <= now]
            for event_id in expired:
                del self._entries[event_id]
            return len(expired)


class FakeOAuthTokenStore:
    def __init__(self, refresh_token: str | None = None) -> None:
        self._refresh_token = refresh_token

    def get_gmail_refresh_token(self) -> str | None:
        return self._refresh_token

    def set_gmail_refresh_token(self, token: str) -> None:
        self._refresh_token = token
