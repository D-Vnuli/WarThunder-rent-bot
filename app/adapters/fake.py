from datetime import datetime
from threading import Lock

from app.domain.models import LotOperationResult, RawEmail


class FakeFunPayAdapter:
    def __init__(self) -> None:
        self.lots_enabled = True
        self.sent_credentials: list[str] = []
        self.calls: list[str] = []
        self.fail_next: set[str] = set()

    def disable_lots(self, account_id: str) -> LotOperationResult:
        self.calls.append(f"disable:{account_id}")
        if "disable" in self.fail_next:
            return LotOperationResult(1, 0, False, ("fake-lot",))
        self.lots_enabled = False
        return LotOperationResult(1, 1, True)

    def send_credentials(self, rental_id: str, *, idempotency_key: str = "") -> bool:
        self.calls.append(f"credentials:{rental_id}")
        if "credentials" in self.fail_next:
            return False
        if rental_id not in self.sent_credentials:
            self.sent_credentials.append(rental_id)
        return True

    def enable_lots(self, account_id: str) -> LotOperationResult:
        self.calls.append(f"enable:{account_id}")
        if "enable" in self.fail_next:
            return LotOperationResult(1, 0, False, ("fake-lot",))
        self.lots_enabled = True
        return LotOperationResult(1, 1, True)


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

    def set_current(self, account_id: str, value: str) -> None:
        self._current[account_id] = value

    def set_pending(self, account_id: str, value: str) -> None:
        self._pending[account_id] = value

    def promote_pending(self, account_id: str) -> None:
        self._current[account_id] = self._pending.pop(account_id)

    def has_pending(self, account_id: str) -> bool:
        return account_id in self._pending


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
