"""Persistent secret boundaries for production composition.

Vault files contain authenticated protected bytes, never plaintext secrets.
Windows uses DPAPI; tests inject a deterministic protector without pretending it
is production encryption.
"""

import base64
import ctypes
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Protocol


class SecretProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...
    def unprotect(self, ciphertext: bytes) -> bytes: ...


class VaultCorruptError(RuntimeError):
    pass


class DeterministicTestProtector:
    """Injected only by offline tests; not selected by production composition."""

    def __init__(self, key: bytes = b"phase6-test-protector") -> None:
        self._key = key

    def protect(self, plaintext: bytes) -> bytes:
        return bytes(value ^ self._key[index % len(self._key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self.protect(ciphertext)


class DPAPISecretProtector:
    """Current-user Windows DPAPI implementation with UI disabled."""

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    _UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI is required for production vaults")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    def protect(self, plaintext: bytes) -> bytes:
        return self._crypt(True, plaintext)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._crypt(False, ciphertext)

    def _crypt(self, protect: bool, value: bytes) -> bytes:
        source_buffer = (ctypes.c_byte * len(value)).from_buffer_copy(value)
        source = self._Blob(len(value), source_buffer)
        target = self._Blob()
        if protect:
            ok = self._crypt32.CryptProtectData(
                ctypes.byref(source), None, None, None, None, self._UI_FORBIDDEN, ctypes.byref(target)
            )
        else:
            ok = self._crypt32.CryptUnprotectData(
                ctypes.byref(source), None, None, None, None, self._UI_FORBIDDEN, ctypes.byref(target)
            )
        if not ok:
            raise VaultCorruptError("DPAPI operation failed")
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            self._kernel32.LocalFree(target.pbData)


class ProtectedVault:
    def __init__(self, path: Path, protector: SecretProtector) -> None:
        self.path = path
        self._protector = protector
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    def validate(self) -> None:
        self._read()

    def get(self, key: str) -> str | None:
        value = self._read().get(key)
        return value if isinstance(value, str) else None

    def set(self, key: str, value: str) -> None:
        values = self._read()
        values[key] = value
        self._write(values)

    def delete(self, key: str) -> None:
        values = self._read()
        values.pop(key, None)
        self._write(values)

    def _read(self) -> dict[str, str]:
        try:
            encoded = self.path.read_bytes()
            decrypted = self._protector.unprotect(base64.b64decode(encoded, validate=True))
            value = json.loads(decrypted.decode("utf-8"))
        except Exception as error:
            raise VaultCorruptError("protected vault is corrupt") from error
        if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
            raise VaultCorruptError("protected vault has invalid structure")
        return value

    def _write(self, values: dict[str, str]) -> None:
        encoded = base64.b64encode(self._protector.protect(json.dumps(values, separators=(",", ":")).encode("utf-8")))
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(encoded)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)


class ProductionSecureStore:
    sandbox_safe = True
    production_safe = True

    def __init__(self, path: Path, protector: SecretProtector) -> None:
        self._vault = ProtectedVault(path, protector)

    def validate(self) -> None:
        self._vault.validate()

    def set_pending(self, account_id: str, value: str) -> None:
        self._vault.set(f"pending:{account_id}", value)

    def promote_pending(self, account_id: str) -> None:
        value = self._vault.get(f"pending:{account_id}")
        if value is not None:
            self._vault.set(f"current:{account_id}", value)
            self._vault.delete(f"pending:{account_id}")

    def has_pending(self, account_id: str) -> bool:
        return self._vault.get(f"pending:{account_id}") is not None

    def set_current_credentials(self, account_id: str, login: str, password: str) -> None:
        self._vault.set(f"credentials:{account_id}", json.dumps([login, password]))

    def get_current_credentials(self, account_id: str) -> tuple[str, str] | None:
        return self._credentials(f"credentials:{account_id}")

    def get_pending_credentials(self, account_id: str) -> tuple[str, str] | None:
        return self._credentials(f"pending-credentials:{account_id}")

    def set_pending_credentials(self, account_id: str, login: str, password: str) -> None:
        self._vault.set(f"pending-credentials:{account_id}", json.dumps([login, password]))

    def promote_pending_credentials(self, account_id: str) -> None:
        value = self._vault.get(f"pending-credentials:{account_id}")
        if value is not None:
            self._vault.set(f"credentials:{account_id}", value)
            self._vault.delete(f"pending-credentials:{account_id}")

    def _credentials(self, key: str) -> tuple[str, str] | None:
        value = self._vault.get(key)
        if value is None:
            return None
        try:
            login, password = json.loads(value)
        except (TypeError, ValueError) as error:
            raise VaultCorruptError("credential entry is corrupt") from error
        return (login, password) if isinstance(login, str) and isinstance(password, str) else None

    def get_gmail_refresh_token(self) -> str | None:
        return self._vault.get("gmail-refresh-token")

    def set_gmail_refresh_token(self, token: str) -> None:
        self._vault.set("gmail-refresh-token", token)


class ProductionWebSessionStore:
    sandbox_safe = True
    production_safe = True

    def __init__(self, path: Path, protector: SecretProtector) -> None:
        self._vault = ProtectedVault(path, protector)

    def validate(self) -> None:
        self._vault.validate()

    def get_funpay_session(self, account_id: str) -> str | None:
        return self._vault.get(f"funpay:{account_id}")

    def set_funpay_session(self, account_id: str, value: str) -> None:
        self._vault.set(f"funpay:{account_id}", value)

    def clear_funpay_session(self, account_id: str) -> None:
        self._vault.delete(f"funpay:{account_id}")

    def get_pixelstorm_session(self, account_id: str) -> str | None:
        return self._vault.get(f"pixelstorm:{account_id}")

    def set_pixelstorm_session(self, account_id: str, value: str) -> None:
        self._vault.set(f"pixelstorm:{account_id}", value)

    def clear_pixelstorm_session(self, account_id: str) -> None:
        self._vault.delete(f"pixelstorm:{account_id}")


class ProductionEphemeralEmailSecretStore:
    sandbox_safe = True
    production_safe = True

    def __init__(self, path: Path, protector: SecretProtector) -> None:
        self._vault = ProtectedVault(path, protector)

    def validate(self) -> None:
        self._vault.validate()

    def put(self, event_id: str, payload: str, *, expires_at: datetime) -> bool:
        if self._vault.get(f"payload:{event_id}") is not None:
            return False
        self._vault.set(f"payload:{event_id}", payload)
        self._vault.set(f"expires:{event_id}", expires_at.isoformat())
        return True

    def consume_once(self, event_id: str, *, claim_token: str, now: datetime) -> str | None:
        del claim_token
        expiry = self._vault.get(f"expires:{event_id}")
        payload = self._vault.get(f"payload:{event_id}")
        self.discard(event_id)
        if expiry is None or payload is None or datetime.fromisoformat(expiry) <= now:
            return None
        return payload

    def discard(self, event_id: str) -> None:
        self._vault.delete(f"payload:{event_id}")
        self._vault.delete(f"expires:{event_id}")

    def purge_expired(self, now: datetime) -> int:
        removed = 0
        # Structural validation is deliberate; opaque corruption fails preflight instead.
        entries = self._vault._read()
        for key, expiry in entries.items():
            if key.startswith("expires:") and datetime.fromisoformat(expiry) <= now:
                self.discard(key.removeprefix("expires:"))
                removed += 1
        return removed
