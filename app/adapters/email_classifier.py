import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit

from app.domain.models import EmailClassification, RawEmail
from app.domain.states import EmailMessageType

_OTP_PATTERN = re.compile(r"^\d{6}$")
_RESET_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_PASSWORD_RESET_PATH = re.compile(r"^/ru/sso/changePassword/[^/?#]+$")
_LOGIN_MARKER = "type=two_step_email_code"
_PASSWORD_MARKER = "type=verify_password_change"


@dataclass
class _HtmlNode:
    tag: str
    attrs: dict[str, str]
    text: list[str]
    children: list["_HtmlNode"]


class _EmailHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("root", {}, [], [])
        self._stack = [self.root]
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        node = _HtmlNode(tag.lower(), values, [], [])
        self._stack[-1].children.append(node)
        self._stack.append(node)
        if node.tag == "a" and values.get("href"):
            self.hrefs.append(values["href"])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if len(self._stack) > 1:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._stack[-1].tag not in {"style", "script"}:
            self._stack[-1].text.append(data)


def _visible_text(node: _HtmlNode) -> str:
    return " ".join(node.text + [_visible_text(child) for child in node.children])


def _html_login_otp(html: str, purpose: str) -> str | None:
    parser = _EmailHtmlParser()
    parser.feed(html)
    parser.close()

    def walk(node: _HtmlNode) -> str | None:
        text = _visible_text(node)
        if purpose.casefold() in text.casefold():
            codes = [part.strip() for part in text.split() if _OTP_PATTERN.fullmatch(part.strip())]
            if len(codes) == 1:
                return codes[0]
        for child in node.children:
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(parser.root)


def _html_hrefs(html: str) -> list[str]:
    parser = _EmailHtmlParser()
    parser.feed(html)
    parser.close()
    return parser.hrefs


@dataclass(frozen=True)
class EmailClassifierPolicy:
    allowed_sender: str = "login@pixstorm.ru"
    login_subject: str = "Подтверждение входа"
    login_purpose_phrase: str = "Код подтверждения для входа"
    password_change_subject: str = "Подтверждение смены пароля для учетной записи"


class EmailClassifier:
    """Strictly classify only fixture-confirmed message formats."""

    def __init__(self, policy: EmailClassifierPolicy | None = None) -> None:
        self.policy = policy or EmailClassifierPolicy()

    def classify(self, message: RawEmail) -> EmailClassification:
        if self._sender(message.sender) != self.policy.allowed_sender.lower():
            return EmailClassification(EmailMessageType.UNKNOWN)
        if self._is_password_change(message):
            reset_url = self._reset_url(message)
            if reset_url is not None:
                return EmailClassification(EmailMessageType.PASSWORD_CHANGE, reset_url)
        if self._is_login_otp(message):
            otp = self._login_otp(message)
            if otp is not None:
                return EmailClassification(EmailMessageType.LOGIN_OTP, otp)
        return EmailClassification(EmailMessageType.UNKNOWN)

    def _is_login_otp(self, message: RawEmail) -> bool:
        return (
            message.subject == self.policy.login_subject
            and self.policy.login_purpose_phrase.casefold() in message.text_body.casefold()
            and _LOGIN_MARKER in (message.html_body or message.text_body)
        )

    def _is_password_change(self, message: RawEmail) -> bool:
        return (
            message.subject == self.policy.password_change_subject
            and _PASSWORD_MARKER in (message.html_body or message.text_body)
        )

    def _login_otp(self, message: RawEmail) -> str | None:
        if message.html_body is not None:
            return _html_login_otp(message.html_body, self.policy.login_purpose_phrase)
        pattern = re.compile(
            rf"{re.escape(self.policy.login_purpose_phrase)}\D{{0,80}}(?<!\d)(\d{{6}})(?!\d)",
            re.IGNORECASE,
        )
        match = pattern.search(message.text_body)
        return match.group(1) if match else None

    def _reset_url(self, message: RawEmail) -> str | None:
        candidates = (
            _html_hrefs(message.html_body)
            if message.html_body is not None
            else _RESET_URL_PATTERN.findall(message.text_body)
        )
        for candidate in candidates:
            try:
                parsed = urlsplit(candidate)
                port = parsed.port
            except ValueError:
                continue
            if (
                parsed.scheme == "https"
                and parsed.hostname == "login.pixstorm.ru"
                and port is None
                and parsed.username is None
                and not parsed.fragment
                and _PASSWORD_RESET_PATH.fullmatch(parsed.path)
                and self._valid_reset_query(parsed.query)
            ):
                return candidate
        return None

    @staticmethod
    def _valid_reset_query(query: str) -> bool:
        try:
            parameters = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return False
        return len(parameters) == 1 and parameters[0][0] == "token" and bool(parameters[0][1])

    @staticmethod
    def _sender(value: str) -> str:
        return parseaddr(value)[1].lower()


def parse_sanitized_eml(raw: bytes, *, gmail_message_id: str, received_at, routing_account_id=None) -> RawEmail:
    """Parse a sanitized fixture; callers must not persist the raw body."""
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    body = parsed.get_body(preferencelist=("plain", "html"))
    content = body.get_content() if body is not None else ""
    html = content if body is not None and body.get_content_type() == "text/html" else None
    if html is not None:
        parser = _EmailHtmlParser()
        parser.feed(html)
        parser.close()
        text = _visible_text(parser.root)
    else:
        text = content
    return RawEmail(
        gmail_message_id=gmail_message_id,
        sender=parsed.get("From", ""),
        subject=parsed.get("Subject", ""),
        received_at=received_at,
        text_body=text,
        routing_account_id=routing_account_id,
        html_body=html,
    )
