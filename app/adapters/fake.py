import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock

from app.domain.funpay import FunPayEvent, FunPayHealth, MessageReceipt
from app.domain.models import LotOperationResult, RawEmail
from app.domain.notifications import OwnerNotification
from app.domain.pixelstorm import (
    PixelStormAuthenticationState,
    PixelStormAuthResult,
    PixelStormCredentialResult,
    PixelStormHealth,
    PixelStormPasswordChangeResult,
    PixelStormRevocationResult,
    PixelStormSecurityCapabilities,
    PixelStormSessionState,
)


class FakeFunPayAdapter:
    sandbox_safe = True
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
    sandbox_safe = True
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


class PersistentFakePixelStormBackend:
    """File-backed remote-service simulator; application SQLite never sees its state."""

    def __init__(self, path: str) -> None:
        self._path = path
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS pixel_accounts (account_id TEXT PRIMARY KEY, login_hash TEXT, password_hash TEXT, health TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, revoke_count INTEGER NOT NULL DEFAULT 0, rotation_count INTEGER NOT NULL DEFAULT 0)")

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf8")).hexdigest()

    def set_credentials(self, account_id: str, login: str, password: str) -> None:
        with sqlite3.connect(self._path) as connection:
            connection.execute("INSERT INTO pixel_accounts(account_id, login_hash, password_hash, health) VALUES (?, ?, ?, 'READY') ON CONFLICT(account_id) DO UPDATE SET login_hash=excluded.login_hash, password_hash=excluded.password_hash", (account_id, self._digest(login), self._digest(password)))

    def set_health(self, account_id: str, health: PixelStormHealth) -> None:
        with sqlite3.connect(self._path) as connection:
            connection.execute("INSERT INTO pixel_accounts(account_id, health) VALUES (?, ?) ON CONFLICT(account_id) DO UPDATE SET health=excluded.health", (account_id, health.value))

    def health(self, account_id: str) -> PixelStormHealth:
        with sqlite3.connect(self._path) as connection:
            row = connection.execute("SELECT health FROM pixel_accounts WHERE account_id=?", (account_id,)).fetchone()
        return PixelStormHealth(row[0]) if row else PixelStormHealth.UNAVAILABLE

    def verify(self, account_id: str, login: str, password: str) -> bool:
        with sqlite3.connect(self._path) as connection:
            row = connection.execute("SELECT login_hash, password_hash, health FROM pixel_accounts WHERE account_id=?", (account_id,)).fetchone()
        return bool(row and row[2] == PixelStormHealth.READY and row[0] == self._digest(login) and row[1] == self._digest(password))

    def revoke(self, account_id: str) -> PixelStormRevocationResult:
        with sqlite3.connect(self._path) as connection:
            connection.execute("UPDATE pixel_accounts SET revoked=1, revoke_count=revoke_count+1 WHERE account_id=? AND revoked=0", (account_id,))
        return PixelStormRevocationResult.SUPPORTED_VERIFIED

    def revocation(self, account_id: str) -> PixelStormRevocationResult:
        with sqlite3.connect(self._path) as connection:
            row = connection.execute("SELECT revoked FROM pixel_accounts WHERE account_id=?", (account_id,)).fetchone()
        return PixelStormRevocationResult.SUPPORTED_VERIFIED if row and row[0] else PixelStormRevocationResult.AMBIGUOUS

    def rotate(self, account_id: str, login: str, current: str, pending: str) -> PixelStormPasswordChangeResult:
        with sqlite3.connect(self._path) as connection:
            changed = connection.execute("UPDATE pixel_accounts SET password_hash=?, rotation_count=rotation_count+1 WHERE account_id=? AND login_hash=? AND password_hash=?", (self._digest(pending), account_id, self._digest(login), self._digest(current))).rowcount
        return PixelStormPasswordChangeResult.VERIFIED if changed else PixelStormPasswordChangeResult.INVALID

    def complete_rotation(self, account_id: str, pending: str) -> PixelStormPasswordChangeResult:
        with sqlite3.connect(self._path) as connection:
            changed = connection.execute(
                "UPDATE pixel_accounts SET password_hash=?, rotation_count=rotation_count+1 WHERE account_id=?",
                (self._digest(pending), account_id),
            ).rowcount
        return PixelStormPasswordChangeResult.VERIFIED if changed else PixelStormPasswordChangeResult.INVALID

    def counter(self, account_id: str, field: str) -> int:
        if field not in {"revoke_count", "rotation_count"}:
            raise ValueError("unsupported counter")
        with sqlite3.connect(self._path) as connection:
            row = connection.execute(f"SELECT {field} FROM pixel_accounts WHERE account_id=?", (account_id,)).fetchone()
        return int(row[0]) if row else 0


class FakePixelStormAdapter:
    sandbox_safe = True
    """In-memory Pixel Storm contract double; all outcomes are explicit and typed."""

    def __init__(self, backend: PersistentFakePixelStormBackend | None = None) -> None:
        self._backend = backend
        self.business_transaction_probe = None
        self._health: dict[str, PixelStormHealth] = {}
        self._capabilities: dict[str, PixelStormSecurityCapabilities] = {}
        self._credentials: dict[str, tuple[str, str]] = {}
        self._revocation: dict[str, PixelStormRevocationResult] = {}
        self._session_valid: dict[str, bool] = {}
        self._auth_results: dict[str, list[PixelStormAuthResult]] = {}
        self._email_confirmation_required: set[str] = set()
        self.revoke_calls: list[str] = []
        self.rotation_calls: list[str] = []
        self.password_change_requests: list[str] = []
        self.authentication_calls: list[str] = []

    def set_health(self, account_id: str, value: PixelStormHealth) -> None:
        self._health[account_id] = value
        if self._backend is not None:
            self._backend.set_health(account_id, value)

    def set_credentials(self, account_id: str, login: str, password: str) -> None:
        self._credentials[account_id] = (login, password)
        if self._backend is not None:
            self._backend.set_credentials(account_id, login, password)

    def set_capabilities(self, account_id: str, value: PixelStormSecurityCapabilities) -> None:
        self._capabilities[account_id] = value

    def set_revocation_result(self, account_id: str, value: PixelStormRevocationResult) -> None:
        self._revocation[account_id] = value

    def set_session_valid(self, account_id: str, value: bool) -> None:
        self._session_valid[account_id] = value

    def set_auth_results(self, account_id: str, values: list[PixelStormAuthResult]) -> None:
        self._auth_results[account_id] = list(values)

    def require_password_email_confirmation(self, account_id: str) -> None:
        self._email_confirmation_required.add(account_id)

    def health(self, account_id: str) -> PixelStormHealth:
        if self._backend is not None:
            return self._backend.health(account_id)
        return self._health.get(account_id, PixelStormHealth.READY)

    def inspect_authentication_state(self, account_id: str) -> PixelStormAuthenticationState:
        health = self.health(account_id)
        return PixelStormAuthenticationState(health, health == PixelStormHealth.READY)

    def inspect_security_capabilities(self, account_id: str) -> PixelStormSecurityCapabilities:
        return self._capabilities.get(
            account_id,
            PixelStormSecurityCapabilities(True, True, True, True),
        )

    def inspect_session_state(self, account_id: str) -> PixelStormSessionState:
        valid = self._session_valid.get(account_id, True)
        return PixelStormSessionState(
            PixelStormHealth.READY if valid else PixelStormHealth.AUTH_REQUIRED, valid, valid
        )

    def authenticate(self, account_id: str, login: str, password: str, *, otp: str | None = None) -> PixelStormAuthResult:
        del otp
        self.authentication_calls.append(account_id)
        queued = self._auth_results.get(account_id)
        if queued:
            result = queued.pop(0)
            if result == PixelStormAuthResult.SUCCESS:
                self._health[account_id] = PixelStormHealth.READY
                if self._backend is not None:
                    self._backend.set_health(account_id, PixelStormHealth.READY)
            return result
        health = self.health(account_id)
        if health == PixelStormHealth.READY:
            return PixelStormAuthResult.SUCCESS if self._credentials.get(account_id) == (login, password) else PixelStormAuthResult.BAD_CREDENTIALS
        return PixelStormAuthResult(health) if health in PixelStormAuthResult._value2member_map_ else PixelStormAuthResult.UNKNOWN_UI

    def verify_credentials(self, account_id: str, login: str, password: str) -> PixelStormCredentialResult:
        if self.business_transaction_probe is not None:
            self.business_transaction_probe("VERIFY_CREDENTIALS")
        if self.health(account_id) != PixelStormHealth.READY:
            return PixelStormCredentialResult(self.health(account_id)) if self.health(account_id).value in PixelStormCredentialResult._value2member_map_ else PixelStormCredentialResult.AMBIGUOUS
        valid = self._backend.verify(account_id, login, password) if self._backend is not None else self._credentials.get(account_id, (login, password)) == (login, password)
        return PixelStormCredentialResult.VALID if valid else PixelStormCredentialResult.INVALID

    def revoke_sessions(self, account_id: str) -> PixelStormRevocationResult:
        if self.business_transaction_probe is not None:
            self.business_transaction_probe("REVOKE_SESSIONS")
        self.revoke_calls.append(account_id)
        if self._backend is not None:
            return self._backend.revoke(account_id)
        result = self._revocation.get(account_id, PixelStormRevocationResult.SUPPORTED_VERIFIED)
        if result == PixelStormRevocationResult.SUPPORTED_VERIFIED:
            self._revocation[account_id] = result
        return result

    def verify_revocation(self, account_id: str) -> PixelStormRevocationResult:
        if self._backend is not None:
            return self._backend.revocation(account_id)
        return self._revocation.get(account_id, PixelStormRevocationResult.SUPPORTED_VERIFIED)

    def change_password(self, account_id: str, login: str, current_password: str, pending_password: str) -> PixelStormPasswordChangeResult:
        if self.business_transaction_probe is not None:
            self.business_transaction_probe("CHANGE_PASSWORD")
        self.rotation_calls.append(account_id)
        if self._backend is not None:
            return self._backend.rotate(account_id, login, current_password, pending_password)
        if self.health(account_id) != PixelStormHealth.READY:
            return PixelStormPasswordChangeResult(self.health(account_id)) if self.health(account_id).value in PixelStormPasswordChangeResult._value2member_map_ else PixelStormPasswordChangeResult.AMBIGUOUS
        if self._credentials.get(account_id, (login, current_password)) != (login, current_password):
            return PixelStormPasswordChangeResult.INVALID
        self._credentials[account_id] = (login, pending_password)
        return PixelStormPasswordChangeResult.VERIFIED

    def request_password_change(self, account_id: str, login: str, current_password: str) -> PixelStormPasswordChangeResult:
        if self.business_transaction_probe is not None:
            self.business_transaction_probe("REQUEST_PASSWORD_CHANGE")
        self.password_change_requests.append(account_id)
        if account_id in self._email_confirmation_required:
            return PixelStormPasswordChangeResult.CONFIRMATION_REQUIRED
        if self._backend is not None:
            return PixelStormPasswordChangeResult.VERIFIED if self._backend.verify(account_id, login, current_password) else PixelStormPasswordChangeResult.INVALID
        if self._credentials.get(account_id, (login, current_password)) != (login, current_password):
            return PixelStormPasswordChangeResult.INVALID
        return PixelStormPasswordChangeResult.CONFIRMATION_REQUIRED if account_id in self._email_confirmation_required else PixelStormPasswordChangeResult.VERIFIED

    def complete_password_change(self, account_id: str, reset_url: str, pending_password: str) -> PixelStormPasswordChangeResult:
        if self.business_transaction_probe is not None:
            self.business_transaction_probe("COMPLETE_PASSWORD_CHANGE")
        del reset_url
        if self._backend is not None:
            self.rotation_calls.append(account_id)
            return self._backend.complete_rotation(account_id, pending_password)
        current = self._credentials.get(account_id)
        if current is None:
            return PixelStormPasswordChangeResult.INVALID
        self._credentials[account_id] = (current[0], pending_password)
        self.rotation_calls.append(account_id)
        return PixelStormPasswordChangeResult.VERIFIED


class FakeSecureStore:
    sandbox_safe = True
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

    def get_pending_credentials(self, account_id: str) -> tuple[str, str] | None:
        return self._credentials.get(f"pending-credentials:{account_id}")

    def set_pending_credentials(self, account_id: str, login: str, password: str) -> None:
        self._credentials[f"pending-credentials:{account_id}"] = (login, password)

    def promote_pending_credentials(self, account_id: str) -> None:
        pending = self._credentials.pop(f"pending-credentials:{account_id}", None)
        if pending is not None:
            self._credentials[account_id] = pending

    def get_funpay_session(self, account_id: str) -> str | None:
        return self._current.get(f"session:{account_id}")

    def set_funpay_session(self, account_id: str, value: str) -> None:
        self._current[f"session:{account_id}"] = value

    def clear_funpay_session(self, account_id: str) -> None:
        self._current.pop(f"session:{account_id}", None)

    def get_pixelstorm_session(self, account_id: str) -> str | None:
        return self._current.get(f"pixelstorm-session:{account_id}")

    def set_pixelstorm_session(self, account_id: str, value: str) -> None:
        self._current[f"pixelstorm-session:{account_id}"] = value

    def clear_pixelstorm_session(self, account_id: str) -> None:
        self._current.pop(f"pixelstorm-session:{account_id}", None)


class PersistentFakeSecureStore(FakeSecureStore):
    """Test secure-store boundary whose state survives adapter/worker recreation.

    It intentionally keeps values in an isolated in-process vault, never in
    the application database, audit records, logs or browser artifacts.
    """

    def __init__(self, vault_id: str) -> None:
        super().__init__()
        self._vault_id = Path(vault_id)
        if not self._vault_id.exists():
            self._write({})

    def _read(self) -> dict[str, dict[str, str | int | None]]:
        return json.loads(self._vault_id.read_text(encoding="utf-8"))

    def _write(self, values: dict[str, dict[str, str | int | None]]) -> None:
        temporary = self._vault_id.with_suffix(self._vault_id.suffix + ".tmp")
        temporary.write_text(json.dumps(values, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self._vault_id)

    def set_current_credentials(self, account_id: str, login: str, password: str) -> None:
        values = self._read()
        entry = values.setdefault(account_id, {})
        entry.update(login=login, current_password=password)
        self._write(values)

    def get_current_credentials(self, account_id: str) -> tuple[str, str] | None:
        entry = self._read().get(account_id)
        if entry is None or not isinstance(entry.get("login"), str) or not isinstance(entry.get("current_password"), str):
            return None
        return str(entry["login"]), str(entry["current_password"])

    def get_pending_credentials(self, account_id: str) -> tuple[str, str] | None:
        entry = self._read().get(account_id)
        if entry is None or not isinstance(entry.get("login"), str) or not isinstance(entry.get("pending_password"), str):
            return None
        return str(entry["login"]), str(entry["pending_password"])

    def set_pending_credentials(self, account_id: str, login: str, password: str) -> None:
        values = self._read()
        entry = values.setdefault(account_id, {})
        if entry.get("pending_password") is None:
            entry["pending_password"] = password
            entry["pending_created"] = int(entry.get("pending_created") or 0) + 1
        entry["login"] = login
        self._write(values)

    def promote_pending_credentials(self, account_id: str) -> None:
        values = self._read()
        entry = values.get(account_id)
        if entry is not None and isinstance(entry.get("pending_password"), str):
            entry["current_password"] = entry["pending_password"]
            entry["pending_password"] = None
            self._write(values)

    def pending_created_count(self, account_id: str) -> int:
        entry = self._read().get(account_id)
        return int(entry.get("pending_created") or 0) if entry else 0


class FakeGmailAdapter:
    sandbox_safe = True
    def __init__(self, messages: list[RawEmail] | None = None) -> None:
        self.messages = messages or []

    def get_new_messages(self, *, after: datetime) -> list[RawEmail]:
        return [message for message in self.messages if message.received_at >= after]


class FakeEphemeralEmailSecretStore:
    """In-memory TTL/one-time store: no payload is ever persisted to SQLite."""

    sandbox_safe = True

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
