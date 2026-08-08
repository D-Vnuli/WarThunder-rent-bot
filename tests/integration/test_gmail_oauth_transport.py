import base64
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapters.email_classifier import EmailClassifier
from app.adapters.fake import FakeEphemeralEmailSecretStore, FakeGmailAdapter, FakeOAuthTokenStore
from app.adapters.gmail import (
    GMAIL_READONLY_SCOPE,
    GmailApiTransport,
    GmailOAuthAdapter,
    GmailOAuthClientConfig,
    GoogleGmailServiceFactory,
)
from app.application.gmail_watcher import GmailWatcher
from app.application.otp_service import OTPService
from app.domain.models import OrderInput
from app.persistence.classified_email_events import ClassifiedEmailRepository

FIXTURES = Path(__file__).parents[2] / "fixtures" / "email"
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


class _Request:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _Messages:
    def __init__(self, responses):
        self.responses = responses
        self.query = ""

    def list(self, *, userId, q):
        assert userId == "me"
        self.query = q
        return _Request({"messages": [{"id": "pixel"}, {"id": "unrelated"}]})

    def get(self, *, userId, id, format):
        assert (userId, format) == ("me", "raw")
        return _Request(self.responses[id])


class _Users:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _Service:
    def __init__(self, messages):
        self._users = _Users(messages)

    def users(self):
        return self._users


def _gmail_response(raw, message_id):
    return {
        "id": message_id,
        "raw": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "internalDate": str(int(NOW.timestamp() * 1000)),
    }


def _service():
    pixel = (FIXTURES / "pixstorm_login_otp.sanitized.eml").read_bytes()
    unrelated = b"From: ordinary@example.invalid\nSubject: ordinary\n\nnot security"
    messages = _Messages(
        {"pixel": _gmail_response(pixel, "pixel"), "unrelated": _gmail_response(unrelated, "unrelated")}
    )
    return _Service(messages), messages


def _active_rental(core):
    repository, manager, _, _ = core
    account_id = repository.add_account("WT01", NOW)
    order = manager.accept_order(OrderInput("gmail-order", "buyer", "1H", 3600), NOW)
    manager.run_operations(NOW)
    manager.run_operations(NOW)
    assert order.rental_id is not None
    return account_id, order.rental_id


def test_gmail_api_transport_scopes_sender_routes_account_and_delivers_otp(core):
    account_id, rental_id = _active_rental(core)
    service, messages = _service()
    transport = GmailApiTransport(lambda _: service, account_id)
    raw_messages = transport.fetch(str(uuid4()), after=NOW)

    assert messages.query == f"from:login@pixstorm.ru after:{int(NOW.timestamp())}"
    assert [message.gmail_message_id for message in raw_messages] == ["pixel"]
    assert raw_messages[0].routing_account_id == account_id

    events = ClassifiedEmailRepository(core[0].db)
    secrets = FakeEphemeralEmailSecretStore()
    watcher = GmailWatcher(
        FakeGmailAdapter(list(raw_messages)), EmailClassifier(), events, secrets, 300, 900
    )
    watcher.poll_once(NOW, NOW)
    assert OTPService(events, secrets, 120, 0).request_otp(rental_id, "buyer", NOW, NOW) == "654321"


def test_google_factory_and_oauth_adapter_use_only_token_store():
    captured = {}

    def credentials_factory(token, config):
        captured["token"] = token
        captured["config"] = config
        return object()

    service, _ = _service()

    def service_builder(credentials):
        captured["credentials"] = credentials
        return service

    config = GmailOAuthClientConfig("client-id", "client-secret")
    factory = GoogleGmailServiceFactory(config, credentials_factory, service_builder)
    token = str(uuid4())
    adapter = GmailOAuthAdapter(
        FakeOAuthTokenStore(token),
        config,
        "account-1",
        GmailApiTransport(factory, "account-1"),
    )
    result = adapter.get_new_messages(after=NOW)
    assert captured["token"] == token
    assert captured["config"].client_id == "client-id"
    assert captured["credentials"] is not None
    assert len(result) == 1
    assert GMAIL_READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"


def test_missing_refresh_token_fails_closed_without_service_creation():
    config = GmailOAuthClientConfig("client-id", "client-secret")
    with pytest.raises(RuntimeError, match="refresh token"):
        GmailOAuthAdapter(FakeOAuthTokenStore(), config, "account-1").get_new_messages(after=NOW)
