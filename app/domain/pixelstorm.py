"""Typed, fail-closed Pixel Storm account-security contract."""

from dataclasses import dataclass
from enum import StrEnum


class PixelStormHealth(StrEnum):
    READY = "READY"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PIXEL_PASS_REQUIRED = "PIXEL_PASS_REQUIRED"
    CHALLENGE = "CHALLENGE"
    WRONG_REGION = "WRONG_REGION"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN_UI = "UNKNOWN_UI"


class PixelStormAuthResult(StrEnum):
    SUCCESS = "SUCCESS"
    BAD_CREDENTIALS = "BAD_CREDENTIALS"
    EMAIL_OTP_REQUIRED = "EMAIL_OTP_REQUIRED"
    PIXEL_PASS_REQUIRED = "PIXEL_PASS_REQUIRED"
    CHALLENGE = "CHALLENGE"
    WRONG_REGION = "WRONG_REGION"
    UNKNOWN_UI = "UNKNOWN_UI"
    UNAVAILABLE = "UNAVAILABLE"


class PixelStormRevocationResult(StrEnum):
    SUPPORTED_VERIFIED = "SUPPORTED_VERIFIED"
    UNSUPPORTED = "UNSUPPORTED"
    PARTIAL = "PARTIAL"
    AMBIGUOUS = "AMBIGUOUS"
    CHALLENGE = "CHALLENGE"
    UNKNOWN_UI = "UNKNOWN_UI"


class PixelStormCredentialResult(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    PIXEL_PASS_REQUIRED = "PIXEL_PASS_REQUIRED"
    CHALLENGE = "CHALLENGE"
    WRONG_REGION = "WRONG_REGION"
    UNKNOWN_UI = "UNKNOWN_UI"
    AMBIGUOUS = "AMBIGUOUS"
    UNAVAILABLE = "UNAVAILABLE"


class PixelStormPasswordChangeResult(StrEnum):
    VERIFIED = "VERIFIED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    INVALID = "INVALID"
    PIXEL_PASS_REQUIRED = "PIXEL_PASS_REQUIRED"
    CHALLENGE = "CHALLENGE"
    WRONG_REGION = "WRONG_REGION"
    UNKNOWN_UI = "UNKNOWN_UI"
    AMBIGUOUS = "AMBIGUOUS"
    UNAVAILABLE = "UNAVAILABLE"


class SecurityOperationOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    FAILED_CLOSED = "FAILED_CLOSED"


@dataclass(frozen=True)
class PixelStormSecurityCapabilities:
    session_history_available: bool
    session_revocation_available: bool
    revoke_all_available: bool
    password_change_available: bool
    pixel_pass_detected: bool = False
    unknown: bool = False


@dataclass(frozen=True)
class PixelStormAuthenticationState:
    health: PixelStormHealth
    authenticated: bool


@dataclass(frozen=True)
class PixelStormSessionState:
    health: PixelStormHealth
    session_present: bool
    session_valid: bool
