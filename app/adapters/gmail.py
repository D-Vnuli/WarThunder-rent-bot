import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Protocol

from app.adapters.email_classifier import parse_sanitized_eml
from app.domain.models import RawEmail
from app.domain.ports import OAuthTokenStore


class GmailOAuthTransport(Protocol):
    def fetch(self, refresh_token: str, *, after: datetime) -> Sequence[RawEmail]: ...


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
PIXSTORM_SECURITY_SENDER = "login@pixstorm.ru"


@dataclass(frozen=True)
class GmailOAuthClientConfig:
    client_id: str
    client_secret: str
    token_uri: str = "https://oauth2.googleapis.com/token"


class GmailApiRequest(Protocol):
    def execute(self) -> dict: ...


class GmailApiMessages(Protocol):
    def list(self, *, userId: str, q: str) -> GmailApiRequest: ...
    def get(self, *, userId: str, id: str, format: str) -> GmailApiRequest: ...


class GmailApiUsers(Protocol):
    def messages(self) -> GmailApiMessages: ...


class GmailApiService(Protocol):
    def users(self) -> GmailApiUsers: ...


class GoogleCredentialsFactory(Protocol):
    def __call__(self, refresh_token: str, config: GmailOAuthClientConfig) -> object: ...


class GoogleServiceBuilder(Protocol):
    def __call__(self, credentials: object) -> GmailApiService: ...


class GoogleGmailServiceFactory:
    """Builds the official Gmail API service only from runtime OAuth credentials."""

    def __init__(
        self,
        config: GmailOAuthClientConfig,
        credentials_factory: GoogleCredentialsFactory | None = None,
        service_builder: GoogleServiceBuilder | None = None,
    ) -> None:
        self._config = config
        self._credentials_factory = credentials_factory or self._create_google_credentials
        self._service_builder = service_builder or self._build_google_service

    def __call__(self, refresh_token: str) -> GmailApiService:
        credentials = self._credentials_factory(refresh_token, self._config)
        return self._service_builder(credentials)

    @staticmethod
    def _create_google_credentials(refresh_token: str, config: GmailOAuthClientConfig) -> object:
        from google.oauth2.credentials import Credentials

        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=config.token_uri,
            client_id=config.client_id,
            client_secret=config.client_secret,
            scopes=[GMAIL_READONLY_SCOPE],
        )

    @staticmethod
    def _build_google_service(credentials: object) -> GmailApiService:
        from googleapiclient.discovery import build

        return build("gmail", "v1", credentials=credentials, cache_discovery=False)


class GoogleOAuthConsentBootstrap:
    """Owner-invoked consent boundary; it stores the resulting token only via OAuthTokenStore."""

    def __init__(self, config: GmailOAuthClientConfig) -> None:
        self._config = config

    def create_flow(self):
        from google_auth_oauthlib.flow import InstalledAppFlow

        return InstalledAppFlow.from_client_config(
            {
                "installed": {
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "token_uri": self._config.token_uri,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "redirect_uris": ["http://localhost"],
                }
            },
            scopes=[GMAIL_READONLY_SCOPE],
        )

    @staticmethod
    def store_refresh_token(token_store: OAuthTokenStore, refresh_token: str | None) -> None:
        if not refresh_token:
            raise RuntimeError("OAuth consent did not produce a refresh token")
        token_store.set_gmail_refresh_token(refresh_token)


class GmailApiTransport:
    """Concrete Gmail API boundary; service creation stays outside secret persistence."""

    def __init__(
        self,
        service_factory: Callable[[str], GmailApiService],
        routing_account_id: str,
        sender_scope: str = PIXSTORM_SECURITY_SENDER,
    ) -> None:
        if not routing_account_id:
            raise ValueError("GmailApiTransport requires an explicit routing_account_id")
        self._service_factory = service_factory
        self._routing_account_id = routing_account_id
        self._sender_scope = sender_scope

    def fetch(self, refresh_token: str, *, after: datetime) -> Sequence[RawEmail]:
        service = self._service_factory(refresh_token)
        listing = service.users().messages().list(
            userId="me", q=f"from:{self._sender_scope} after:{int(after.timestamp())}"
        ).execute()
        messages: list[RawEmail] = []
        for summary in listing.get("messages", []):
            response = service.users().messages().get(
                userId="me", id=summary["id"], format="raw"
            ).execute()
            raw = base64.urlsafe_b64decode(response["raw"] + "===")
            received_at = datetime.fromtimestamp(int(response["internalDate"]) / 1000, tz=UTC)
            message = parse_sanitized_eml(
                raw,
                gmail_message_id=response["id"],
                received_at=received_at,
                routing_account_id=self._routing_account_id,
            )
            if parseaddr(message.sender)[1].lower() == self._sender_scope:
                messages.append(message)
        return messages


class GmailOAuthAdapter:
    """OAuth boundary; credentials remain solely in OAuthTokenStore."""

    def __init__(
        self,
        token_store: OAuthTokenStore,
        client_config: GmailOAuthClientConfig,
        routing_account_id: str,
        transport: GmailOAuthTransport | None = None,
    ) -> None:
        self._token_store = token_store
        self._transport = transport or GmailApiTransport(
            GoogleGmailServiceFactory(client_config), routing_account_id
        )

    def get_new_messages(self, *, after: datetime) -> Sequence[RawEmail]:
        token = self._token_store.get_gmail_refresh_token()
        if token is None:
            raise RuntimeError("Gmail OAuth refresh token is unavailable in SecureStore")
        return self._transport.fetch(token, after=after)
