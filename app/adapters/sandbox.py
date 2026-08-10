"""Persistent offline-only adapters for the integrated sandbox."""

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from app.domain.funpay import FunPayEvent, FunPayHealth, MessageReceipt
from app.domain.models import LotOperationResult, RawEmail


class PersistentSandboxFunPayAdapter:
    """File-backed FunPay simulator; it has no network transport."""

    sandbox_safe = True

    def __init__(self, path: str) -> None:
        self._path = path
        self.business_transaction_probe = None
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sandbox_funpay_events (
                    external_event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
                    received_at TEXT NOT NULL, funpay_order_id TEXT, buyer_id TEXT,
                    tariff_code TEXT, duration_seconds INTEGER, message_text TEXT
                );
                CREATE TABLE IF NOT EXISTS sandbox_funpay_lots (
                    lot_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sandbox_funpay_receipts (
                    idempotency_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    delivered INTEGER NOT NULL, verified INTEGER NOT NULL,
                    ambiguous INTEGER NOT NULL, occurred_at TEXT NOT NULL, send_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sandbox_funpay_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sandbox_funpay_lot_faults (
                    lot_id TEXT NOT NULL, target_enabled INTEGER NOT NULL,
                    PRIMARY KEY(lot_id, target_enabled)
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO sandbox_funpay_state(key, value) VALUES ('health', 'READY')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO sandbox_funpay_state(key, value) VALUES ('disable_calls', '0')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO sandbox_funpay_state(key, value) VALUES ('enable_calls', '0')"
            )

    def add_event(self, event: FunPayEvent) -> None:
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sandbox_funpay_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.external_event_id,
                    event.event_type,
                    event.received_at.isoformat(),
                    event.funpay_order_id,
                    event.buyer_id,
                    event.tariff_code,
                    event.duration_seconds,
                    event.message_text,
                ),
            )

    def poll_events(self, *, after: datetime) -> list[FunPayEvent]:
        with sqlite3.connect(self._path) as connection:
            rows = connection.execute(
                "SELECT external_event_id,event_type,received_at,funpay_order_id,buyer_id,tariff_code,duration_seconds,message_text FROM sandbox_funpay_events WHERE received_at >= ? ORDER BY received_at",
                (after.isoformat(),),
            ).fetchall()
        return [
            FunPayEvent(
                external_event_id=row[0], event_type=row[1], received_at=datetime.fromisoformat(row[2]),
                funpay_order_id=row[3], buyer_id=row[4], tariff_code=row[5], duration_seconds=row[6], message_text=row[7],
            )
            for row in rows
        ]

    def get_order(self, funpay_order_id: str) -> FunPayEvent | None:
        return next((event for event in self.poll_events(after=datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)) if event.funpay_order_id == funpay_order_id), None)

    def set_lot_state(self, external_lot_id: str, *, enabled: bool) -> None:
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "INSERT INTO sandbox_funpay_lots VALUES (?, ?) ON CONFLICT(lot_id) DO UPDATE SET enabled=excluded.enabled",
                (external_lot_id, int(enabled)),
            )

    def block_lot_transition(self, external_lot_id: str, *, target_enabled: bool) -> None:
        """Inject a verified partial-lot failure without a network transport."""
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sandbox_funpay_lot_faults VALUES (?, ?)",
                (external_lot_id, int(target_enabled)),
            )

    def lot_mutation_count(self, *, enabled: bool) -> int:
        key = "enable_calls" if enabled else "disable_calls"
        with sqlite3.connect(self._path) as connection:
            row = connection.execute(
                "SELECT value FROM sandbox_funpay_state WHERE key=?", (key,)
            ).fetchone()
        return int(row[0]) if row else 0

    def get_lot_state(self, external_lot_id: str) -> bool | None:
        with sqlite3.connect(self._path) as connection:
            row = connection.execute("SELECT enabled FROM sandbox_funpay_lots WHERE lot_id=?", (external_lot_id,)).fetchone()
        return bool(row[0]) if row else None

    def set_health(self, health: FunPayHealth) -> None:
        with sqlite3.connect(self._path) as connection:
            connection.execute("UPDATE sandbox_funpay_state SET value=? WHERE key='health'", (health.value,))

    def health(self) -> FunPayHealth:
        with sqlite3.connect(self._path) as connection:
            row = connection.execute("SELECT value FROM sandbox_funpay_state WHERE key='health'").fetchone()
        return FunPayHealth(row[0])

    def _verify(self, ids: Sequence[str], enabled: bool) -> LotOperationResult:
        states = tuple((lot_id, self.get_lot_state(lot_id) is True) for lot_id in ids)
        failed = tuple(lot_id for lot_id, state in states if state != enabled)
        return LotOperationResult(len(ids), len(ids) - len(failed), not failed, failed, states)

    def verify_lots_disabled(self, external_lot_ids: Sequence[str]) -> LotOperationResult:
        return self._verify(external_lot_ids, False)

    def verify_lots_enabled(self, external_lot_ids: Sequence[str]) -> LotOperationResult:
        return self._verify(external_lot_ids, True)

    def _mutate_lots(self, ids: Sequence[str], enabled: bool) -> LotOperationResult:
        if self.business_transaction_probe is not None:
            self.business_transaction_probe("ENABLE_LOTS" if enabled else "DISABLE_LOTS")
        if self.health() != FunPayHealth.READY:
            return LotOperationResult(len(ids), 0, False, tuple(ids), safe_error_category=self.health().value)
        with sqlite3.connect(self._path) as connection:
            key = "enable_calls" if enabled else "disable_calls"
            connection.execute(
                "UPDATE sandbox_funpay_state SET value=CAST(value AS INTEGER)+1 WHERE key=?", (key,)
            )
            for lot_id in ids:
                blocked = connection.execute(
                    "SELECT 1 FROM sandbox_funpay_lot_faults WHERE lot_id=? AND target_enabled=?",
                    (lot_id, int(enabled)),
                ).fetchone()
                if blocked is None:
                    connection.execute("UPDATE sandbox_funpay_lots SET enabled=? WHERE lot_id=?", (int(enabled), lot_id))
        return self._verify(ids, enabled)

    def disable_lots(self, account_id: str, external_lot_ids: Sequence[str]) -> LotOperationResult:
        del account_id
        return self._mutate_lots(external_lot_ids, False)

    def enable_lots(self, account_id: str, external_lot_ids: Sequence[str]) -> LotOperationResult:
        del account_id
        return self._mutate_lots(external_lot_ids, True)

    def send_message(self, buyer_id: str, text: str, *, idempotency_key: str, now: datetime) -> MessageReceipt:
        if self.business_transaction_probe is not None:
            self.business_transaction_probe(
                "SEND_OTP" if idempotency_key.startswith("SEND_OTP:") else "SEND_CREDENTIALS"
            )
        del text
        with sqlite3.connect(self._path) as connection:
            row = connection.execute(
                "SELECT conversation_id,delivered,verified,ambiguous,occurred_at FROM sandbox_funpay_receipts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO sandbox_funpay_receipts VALUES (?, ?, 1, 1, 0, ?, 1)",
                    (idempotency_key, buyer_id, now.isoformat()),
                )
                return MessageReceipt(idempotency_key, buyer_id, f"sandbox-{idempotency_key}", True, True, False, now)
        return MessageReceipt(idempotency_key, row[0], f"sandbox-{idempotency_key}", bool(row[1]), bool(row[2]), bool(row[3]), datetime.fromisoformat(row[4]))

    def get_message_receipt(self, idempotency_key: str) -> MessageReceipt | None:
        with sqlite3.connect(self._path) as connection:
            row = connection.execute(
                "SELECT conversation_id,delivered,verified,ambiguous,occurred_at FROM sandbox_funpay_receipts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return MessageReceipt(idempotency_key, row[0], f"sandbox-{idempotency_key}", bool(row[1]), bool(row[2]), bool(row[3]), datetime.fromisoformat(row[4]))

    def inject_ambiguous_receipt(
        self, idempotency_key: str, buyer_id: str, now: datetime
    ) -> None:
        """Persist an uncertain external outcome for fail-closed recovery tests."""
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sandbox_funpay_receipts VALUES (?, ?, 0, 0, 1, ?, 1)",
                (idempotency_key, buyer_id, now.isoformat()),
            )

    def message_count(self, idempotency_key: str) -> int:
        with sqlite3.connect(self._path) as connection:
            row = connection.execute("SELECT send_count FROM sandbox_funpay_receipts WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return 0 if row is None else int(row[0])


class PersistentSandboxGmailAdapter:
    """Independent durable mailbox used only by offline sandbox composition."""

    sandbox_safe = True

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        if not self._path.exists():
            self._write([])

    def _read(self) -> list[dict[str, str | None]]:
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, messages: list[dict[str, str | None]]) -> None:
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(messages, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self._path)

    def add_message(self, message: RawEmail) -> None:
        messages = self._read()
        if any(item["id"] == message.gmail_message_id for item in messages):
            return
        messages.append(
            {
                "id": message.gmail_message_id,
                "sender": message.sender,
                "subject": message.subject,
                "received_at": message.received_at.isoformat(),
                "text_body": message.text_body,
                "routing_account_id": message.routing_account_id,
                "html_body": message.html_body,
            }
        )
        self._write(messages)

    def get_new_messages(self, *, after: datetime) -> list[RawEmail]:
        return [
            RawEmail(
                str(row["id"]),
                str(row["sender"]),
                str(row["subject"]),
                datetime.fromisoformat(str(row["received_at"])),
                str(row["text_body"]),
                row["routing_account_id"],
                row["html_body"],
            )
            for row in sorted(self._read(), key=lambda value: str(value["received_at"]))
            if datetime.fromisoformat(str(row["received_at"])) >= after
        ]


class PersistentSandboxEphemeralEmailSecretStore:
    """Sandbox-only vault for restart-safe one-time email payloads.

    This deliberately lives outside business SQLite and is never used by a live runtime.
    """

    sandbox_safe = True

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        if not self._path.exists():
            self._write({})

    def _read(self) -> dict[str, dict[str, str | bool]]:
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, values: dict[str, dict[str, str | bool]]) -> None:
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(values, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self._path)

    def put(self, event_id: str, payload: str, *, expires_at: datetime) -> bool:
        values = self._read()
        if event_id in values:
            return False
        values[event_id] = {"payload": payload, "expires_at": expires_at.isoformat(), "consumed": False}
        self._write(values)
        return True

    def consume_once(self, event_id: str, *, claim_token: str, now: datetime) -> str | None:
        del claim_token
        values = self._read()
        entry = values.get(event_id)
        if entry is None or bool(entry["consumed"]) or datetime.fromisoformat(str(entry["expires_at"])) <= now:
            return None
        entry["consumed"] = True
        self._write(values)
        return str(entry["payload"])

    def discard(self, event_id: str) -> None:
        values = self._read()
        if event_id in values:
            del values[event_id]
            self._write(values)

    def purge_expired(self, now: datetime) -> int:
        values = self._read()
        expired = [
            event_id
            for event_id, entry in values.items()
            if datetime.fromisoformat(str(entry["expires_at"])) <= now
        ]
        for event_id in expired:
            del values[event_id]
        if expired:
            self._write(values)
        return len(expired)
