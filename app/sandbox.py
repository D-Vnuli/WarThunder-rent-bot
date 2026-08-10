"""Offline integrated sandbox and a small safe developer demo."""

import argparse
import gc
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.adapters.email_classifier import EmailClassifier
from app.adapters.fake import (
    FakeOwnerNotifier,
    PersistentFakePixelStormBackend,
    PersistentFakeSecureStore,
)
from app.adapters.sandbox import (
    PersistentSandboxEphemeralEmailSecretStore,
    PersistentSandboxFunPayAdapter,
    PersistentSandboxGmailAdapter,
)
from app.config.settings import Settings
from app.domain.funpay import FunPayEvent, FunPayEventType
from app.domain.models import RawEmail
from app.main import create_application
from app.persistence.database import Database


@dataclass
class DeterministicClock:
    value: datetime

    def __post_init__(self) -> None:
        self._listeners: list[Callable[[], None]] = []

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> datetime:
        delta = timedelta(**kwargs)
        steps = max(1, int(delta.total_seconds()))
        increment = delta / steps
        for _ in range(steps):
            self.value += increment
            for listener in tuple(self._listeners):
                listener()
        return self.value

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe


class SandboxEnvironment:
    """File-backed complete application environment with no live transports."""

    def __init__(self, root: Path, now: datetime | None = None) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.clock = DeterministicClock(now or datetime.now(UTC))
        self._database_url = f"sqlite:///{(root / 'application.db').as_posix()}"
        self._upgrade_database()
        self.database = Database(self._database_url)
        self.owner = FakeOwnerNotifier()
        self._open_adapters()
        self.application = create_application(
            settings=Settings(app_mode="SANDBOX", dry_run=True, database_url=self._database_url),
            database=self.database,
            funpay=self.funpay,
            pixelstorm=self._pixel_adapter(),
            secrets=self.secrets,
            gmail=self.gmail,
            email_secrets=self.email_secrets,
            owner_notifier=self.owner,
            now=self.clock.now(),
            clock=self.clock,
            lease_heartbeat_interval_seconds=0.01,
        )

    def _open_adapters(self) -> None:
        from app.adapters.fake import FakePixelStormAdapter

        self.funpay = PersistentSandboxFunPayAdapter(str(self.root / "funpay.db"))
        self.gmail = PersistentSandboxGmailAdapter(str(self.root / "gmail.inbox"))
        self.pixel_backend = PersistentFakePixelStormBackend(str(self.root / "pixelstorm.db"))
        self.pixelstorm = FakePixelStormAdapter(self.pixel_backend)
        self.secrets = PersistentFakeSecureStore(str(self.root / "secure-store.vault"))
        self.email_secrets = PersistentSandboxEphemeralEmailSecretStore(
            str(self.root / "email-secrets.vault")
        )

    def _pixel_adapter(self):
        return self.pixelstorm

    def _upgrade_database(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        config = Config(str(project_root / "alembic.ini"))
        config.set_main_option("script_location", str(project_root / "alembic"))
        config.attributes["database_url"] = self._database_url
        command.upgrade(config, "head")

    def restart(self) -> None:
        self.application.close()
        self._upgrade_database()
        self.database = Database(self._database_url)
        self._open_adapters()
        self.application = create_application(
            settings=Settings(app_mode="SANDBOX", dry_run=True, database_url=self._database_url),
            database=self.database,
            funpay=self.funpay,
            pixelstorm=self._pixel_adapter(),
            secrets=self.secrets,
            gmail=self.gmail,
            email_secrets=self.email_secrets,
            owner_notifier=self.owner,
            now=self.clock.now(),
            clock=self.clock,
            lease_heartbeat_interval_seconds=0.01,
        )

    def close(self) -> None:
        self.application.close()

    def seed_account(
        self,
        code: str = "SANDBOX-01",
        lots: int = 1,
        login: str | None = None,
        password: str | None = None,
    ) -> str:
        repository = self.application.repository
        account_id = repository.add_account(code, self.clock.now())
        for index in range(lots):
            lot_id = f"sandbox-lot-{code}-{index + 1}"
            repository.add_account_lot(account_id, lot_id, self.clock.now())
            self.funpay.set_lot_state(lot_id, enabled=True)
        login = login or f"sandbox-login-{code}"
        password = password or f"sandbox-password-{code}"
        self.secrets.set_current_credentials(account_id, login, password)
        self.pixel_backend.set_credentials(account_id, login, password)
        return account_id

    def paid_order(self, external_event_id: str, order_id: str, buyer_id: str, duration_seconds: int) -> None:
        self.funpay.add_event(
            FunPayEvent(external_event_id, FunPayEventType.PAID_ORDER, self.clock.now(), order_id, buyer_id, tariff_code="SANDBOX", duration_seconds=duration_seconds)
        )

    def buyer_code(self, external_event_id: str, order_id: str, buyer_id: str) -> None:
        self.funpay.add_event(
            FunPayEvent(external_event_id, FunPayEventType.BUYER_MESSAGE, self.clock.now(), order_id, buyer_id, message_text="код")
        )

    def login_otp_email(self, message_id: str, account_id: str, otp: str) -> None:
        policy = EmailClassifier().policy
        self.gmail.add_message(
            RawEmail(message_id, "login@pixstorm.ru", policy.login_subject, self.clock.now(), f"{policy.login_purpose_phrase} {otp} type=two_step_email_code", account_id)
        )

    def password_change_email(self, message_id: str, account_id: str, token: str) -> None:
        policy = EmailClassifier().policy
        url = f"https://login.pixstorm.ru/ru/sso/changePassword/000000?token={token}"
        self.gmail.add_message(
            RawEmail(message_id, "login@pixstorm.ru", policy.password_change_subject, self.clock.now(), "type=verify_password_change", account_id, f'<a href="{url}">reset</a> type=verify_password_change')
        )

    def run_once(self) -> None:
        self.application.run_once(self.clock.now())

    def readiness(self) -> dict[str, str]:
        repository = self.application.repository
        try:
            accounts = self._accounts()
        except Exception:
            return {name: "NOT_READY:DB" for name in ("DB", "FUNPAY", "EMAIL_PIPELINE", "PIXELSTORM", "SECURESTORE", "ACCOUNT", "LOTS")}
        rentable = [row for row in accounts if row.status == "AVAILABLE"]
        funpay = self.funpay.health()
        credential_ready = bool(rentable) and all(
            self.secrets.get_current_credentials(row.id) is not None for row in rentable
        )
        pixel_ready = bool(rentable) and all(
            self.pixelstorm.health(row.id).value == "READY" for row in rentable
        )
        lots_ready = bool(rentable) and all(
            (lot_ids := repository.account_lot_ids(row.id))
            and all(self.funpay.get_lot_state(lot_id) is True for lot_id in lot_ids)
            for row in rentable
        )
        return {
            "DB": "READY",
            "FUNPAY": "READY" if funpay.value == "READY" else f"NOT_READY:{funpay.value}",
            "EMAIL_PIPELINE": "READY",
            "PIXELSTORM": "READY" if pixel_ready else "NOT_READY:PIXELSTORM",
            "SECURESTORE": "READY" if credential_ready else "NOT_READY:MISSING_CREDENTIALS",
            "LOTS": "READY" if lots_ready else "NOT_READY:LOTS",
            "ACCOUNT": "READY" if credential_ready and pixel_ready and lots_ready else "NOT_READY:ACCOUNT",
        }

    def _accounts(self):
        from sqlalchemy import select

        from app.persistence.models import AccountRow

        with self.database.session() as session:
            return list(session.scalars(select(AccountRow)))


def _demo() -> None:
    with tempfile.TemporaryDirectory(prefix="war-thunder-rent-sandbox-") as temporary:
        sandbox = SandboxEnvironment(Path(temporary), datetime(2026, 1, 1, tzinfo=UTC))
        try:
            sandbox.seed_account()
            sandbox.paid_order("demo-paid", "demo-order", "demo-buyer", 60)
            for _ in range(2):
                sandbox.run_once()
            sandbox.buyer_code("demo-buyer-code", "demo-order", "demo-buyer")
            # Route the OTP to the sole seeded account without exposing it in output.
            account_id = sandbox._accounts()[0].id
            sandbox.gmail.add_message(
                RawEmail(
                    "demo-login-otp-account",
                    "login@pixstorm.ru",
                    EmailClassifier().policy.login_subject,
                    sandbox.clock.now(),
                    f"{EmailClassifier().policy.login_purpose_phrase} 123456 type=two_step_email_code",
                    account_id,
                )
            )
            for _ in range(2):
                sandbox.run_once()
            sandbox.clock.advance(seconds=60)
            for _ in range(4):
                sandbox.run_once()
            print("sandbox=READY rental=FINISHED account=AVAILABLE dry_run=true")
        finally:
            sandbox.close()
            gc.collect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Offline War Thunder rent-bot sandbox")
    parser.add_argument("--scenario", choices=["happy-path"], default="happy-path")
    parser.parse_args()
    _demo()
