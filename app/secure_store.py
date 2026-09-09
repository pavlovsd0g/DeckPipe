from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from ctypes import wintypes


CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4
STORE_VERSION = 1
STORE_FILE_PURPOSE = "deckpipe.secure-store.dpapi.current-user.v1"
STORE_PAYLOAD_PURPOSE = "deckpipe.credentials.v1"
DEEZER_ARL_RECORD = "deezer_arl"
SOUNDCLOUD_OAUTH_RECORD = "soundcloud_oauth"
TELEGRAM_BOT_TOKEN_RECORD = "telegram_bot_token"
LEGACY_SECRET_FIELDS = ("arl", "sc_oauth", "telegram_bot_token")
_FIELD_TO_RECORD = {
    "arl": DEEZER_ARL_RECORD,
    "sc_oauth": SOUNDCLOUD_OAUTH_RECORD,
    "telegram_bot_token": TELEGRAM_BOT_TOKEN_RECORD,
}
_ALLOWED_RECORDS = frozenset(_FIELD_TO_RECORD.values())
_WRITE_LOCK = threading.RLock()
_AUTH_TRANSACTION_LOCAL = threading.local()


class SecureStoreError(RuntimeError):
    """Generic non-secret secure-store failure."""


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _require_windows() -> None:
    if os.name != "nt":
        raise SecureStoreError("Windows current-user DPAPI is required")


_CRYPT32 = None
_KERNEL32 = None


def _crypt32():
    global _CRYPT32
    _require_windows()
    if _CRYPT32 is None:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(DATA_BLOB),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(DATA_BLOB),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DATA_BLOB),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(DATA_BLOB),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        _CRYPT32 = crypt32
    return _CRYPT32


def _kernel32():
    global _KERNEL32
    _require_windows()
    if _KERNEL32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        _KERNEL32 = kernel32
    return _KERNEL32


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception:
        raise SecureStoreError("secure store is unreadable") from None


def _make_blob(buffer) -> DATA_BLOB:
    return DATA_BLOB(len(buffer), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))


def _local_free_ptr(ptr) -> None:
    if ptr:
        handle = wintypes.HLOCAL(ctypes.addressof(ptr.contents))
        _kernel32().LocalFree(handle)


class WindowsDpapi:
    def protect(self, plaintext: bytes, *, description: str | None, optional_entropy, flags: int) -> bytes:
        _require_windows()
        in_buffer = ctypes.create_string_buffer(plaintext, len(plaintext))
        in_blob = _make_blob(in_buffer)
        out_blob = DATA_BLOB()
        try:
            ok = _crypt32().CryptProtectData(
                ctypes.byref(in_blob),
                description,
                optional_entropy,
                None,
                None,
                flags,
                ctypes.byref(out_blob),
            )
            if not ok:
                raise SecureStoreError("DPAPI protect failed")
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.memset(ctypes.addressof(in_buffer), 0, len(in_buffer))
            _local_free_ptr(out_blob.pbData)

    def unprotect(self, ciphertext: bytes, *, optional_entropy, flags: int) -> bytes:
        _require_windows()
        in_buffer = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        in_blob = _make_blob(in_buffer)
        out_blob = DATA_BLOB()
        try:
            ok = _crypt32().CryptUnprotectData(
                ctypes.byref(in_blob),
                None,
                optional_entropy,
                None,
                None,
                flags,
                ctypes.byref(out_blob),
            )
            if not ok:
                raise SecureStoreError("DPAPI unprotect failed")
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.memset(ctypes.addressof(in_buffer), 0, len(in_buffer))
            if out_blob.pbData:
                ctypes.memset(out_blob.pbData, 0, out_blob.cbData)
            _local_free_ptr(out_blob.pbData)


def _default_dpapi():
    return WindowsDpapi()


def _validate_file_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        raise SecureStoreError("secure store is unreadable")
    if set(payload) != {"version", "purpose", "ciphertext"}:
        raise SecureStoreError("secure store schema is unsupported")
    if payload.get("version") != STORE_VERSION or payload.get("purpose") != STORE_FILE_PURPOSE:
        raise SecureStoreError("secure store schema is unsupported")
    ciphertext = payload.get("ciphertext")
    if not isinstance(ciphertext, str) or not ciphertext:
        raise SecureStoreError("secure store schema is unsupported")
    return ciphertext


def _validate_records(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise SecureStoreError("secure store payload is unsupported")
    if set(payload) != {"version", "purpose", "records"}:
        raise SecureStoreError("secure store payload is unsupported")
    if payload.get("version") != STORE_VERSION or payload.get("purpose") != STORE_PAYLOAD_PURPOSE:
        raise SecureStoreError("secure store payload is unsupported")
    records = payload.get("records")
    if not isinstance(records, dict):
        raise SecureStoreError("secure store payload is unsupported")
    if not set(records).issubset(_ALLOWED_RECORDS):
        raise SecureStoreError("secure store payload is unsupported")
    for key, value in records.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SecureStoreError("secure store payload is unsupported")
    return dict(records)


def _record_name(field: str) -> str:
    try:
        return _FIELD_TO_RECORD[field]
    except KeyError:
        raise SecureStoreError("unknown secure credential field") from None


def _read_json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise SecureStoreError("secure store is unreadable") from None


def _atomic_write_json(path: Path, payload: object, replace: Callable[[Path, Path], None] = os.replace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace(tmp, path)
    except Exception:
        raise SecureStoreError("atomic secure write failed") from None
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class SecureCredentialStore:
    def __init__(self, path: Path, *, dpapi=None, replace: Callable[[Path, Path], None] = os.replace):
        self.path = Path(path)
        self._dpapi = dpapi if dpapi is not None else _default_dpapi()
        self._replace = replace

    @property
    def auth_journal_path(self):
        return self.path.with_name(self.path.name + '.auth-rollback.json')

    def _require_recovered(self):
        if self.auth_journal_path.exists() and not getattr(_AUTH_TRANSACTION_LOCAL, 'active', False):
            raise SecureStoreError('auth transaction recovery required')

    def recover_auth_transaction(self, restore_metadata):
        """Recover an interrupted commit before any credential can be consumed.

        The journal contains the previous encrypted DPAPI envelope and public
        account metadata only. All credential readers fail closed until recovery.
        """
        with _WRITE_LOCK:
            if not self.auth_journal_path.exists():
                return
            journal = _read_json_file(self.auth_journal_path)
            if not isinstance(journal, dict) or set(journal) != {'version', 'encrypted_store', 'metadata'} or journal['version'] != 1:
                raise SecureStoreError('auth recovery journal is unreadable')
            original = journal['encrypted_store']
            if original is None:
                self.path.unlink(missing_ok=True)
            else:
                _validate_file_payload(original)
                _atomic_write_json(self.path, original, self._replace)
            restore_metadata(journal['metadata'])
            self.auth_journal_path.unlink()

    @contextmanager
    def auth_transaction(self, snapshot_metadata, restore_metadata):
        with _WRITE_LOCK:
            self.recover_auth_transaction(restore_metadata)
            original = _read_json_file(self.path) if self.path.exists() else None
            if original is not None:
                _validate_file_payload(original)
            journal = {'version': 1, 'encrypted_store': original, 'metadata': snapshot_metadata()}
            _atomic_write_json(self.auth_journal_path, journal)
            _AUTH_TRANSACTION_LOCAL.active = True
            try:
                yield
                self.auth_journal_path.unlink()
            except BaseException:
                # If recovery itself fails, keep the durable journal and block
                # all future credential reads/writes until a retry succeeds.
                self.recover_auth_transaction(restore_metadata)
                raise
            finally:
                _AUTH_TRANSACTION_LOCAL.active = False

    def _read_records(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        ciphertext = _validate_file_payload(_read_json_file(self.path))
        plaintext = bytearray()
        try:
            plaintext.extend(
                self._dpapi.unprotect(
                    _b64decode(ciphertext),
                    optional_entropy=None,
                    flags=CRYPTPROTECT_UI_FORBIDDEN,
                )
            )
            decoded = json.loads(bytes(plaintext).decode("utf-8"))
            return _validate_records(decoded)
        except Exception:
            raise SecureStoreError("secure store is unreadable") from None
        finally:
            for index in range(len(plaintext)):
                plaintext[index] = 0

    def _write_records(self, records: dict[str, str]) -> None:
        payload = {"version": STORE_VERSION, "purpose": STORE_PAYLOAD_PURPOSE, "records": dict(records)}
        plaintext = bytearray(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        try:
            ciphertext = self._dpapi.protect(
                bytes(plaintext),
                description=None,
                optional_entropy=None,
                flags=CRYPTPROTECT_UI_FORBIDDEN,
            )
            file_payload = {
                "version": STORE_VERSION,
                "purpose": STORE_FILE_PURPOSE,
                "ciphertext": _b64encode(ciphertext),
            }
            _atomic_write_json(self.path, file_payload, self._replace)
        except Exception:
            raise SecureStoreError("secure store write failed") from None
        finally:
            for index in range(len(plaintext)):
                plaintext[index] = 0

    def set_secret(self, field: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise SecureStoreError("secure credential value must be a non-empty string")
        record = _record_name(field)
        with _WRITE_LOCK:
            self._require_recovered()
            records = self._read_records()
            records[record] = value
            self._write_records(records)

    def get_secret(self, field: str) -> str | None:
        record = _record_name(field)
        with _WRITE_LOCK:
            self._require_recovered()
            return self._read_records().get(record)

    def delete_secret(self, field: str) -> None:
        """Remove only the selected credential; retain unrelated provider records."""
        record = _record_name(field)
        with _WRITE_LOCK:
            self._require_recovered()
            records = self._read_records()
            if record in records:
                del records[record]
                self._write_records(records)

    def has_secret(self, field: str) -> bool:
        record = _record_name(field)
        with _WRITE_LOCK:
            self._require_recovered()
            return record in self._read_records()

    def set_deezer_arl(self, value: str) -> None:
        self.set_secret("arl", value)

    def get_deezer_arl(self) -> str | None:
        return self.get_secret("arl")

    def set_soundcloud_oauth(self, value: str) -> None:
        self.set_secret("sc_oauth", value)

    def get_soundcloud_oauth(self) -> str | None:
        return self.get_secret("sc_oauth")

    def set_telegram_bot_token(self, value: str) -> None:
        self.set_secret("telegram_bot_token", value)

    def get_telegram_bot_token(self) -> str | None:
        return self.get_secret("telegram_bot_token")


SecureStore = SecureCredentialStore


@dataclass(frozen=True)
class MigrationResult:
    migrated_fields: tuple[str, ...]
    already_present_fields: tuple[str, ...]


def _load_legacy_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return json.loads(config_path.read_text(encoding="cp1251"))
    except Exception:
        raise SecureStoreError("legacy preference file is unreadable") from None


def _default_readback_validator(store: SecureCredentialStore, records: dict[str, str]) -> None:
    for field, value in records.items():
        if store.get_secret(field) != value:
            raise SecureStoreError("secure migration readback failed")


def _legacy_secret_records(config: dict) -> dict[str, str]:
    records: dict[str, str] = {}
    for field in ("arl", "sc_oauth"):
        value = config.get(field)
        if isinstance(value, str) and value:
            records[field] = value
    telegram = config.get("telegram")
    if isinstance(telegram, dict):
        value = telegram.get("bot_token")
        if isinstance(value, str) and value:
            records["telegram_bot_token"] = value
    return records


def _sanitize_legacy_config(config: dict, records: dict[str, str]) -> dict:
    sanitized = dict(config)
    for field in ("arl", "sc_oauth"):
        if field in records:
            sanitized.pop(field, None)
    if "telegram_bot_token" in records:
        telegram = sanitized.get("telegram")
        if isinstance(telegram, dict):
            telegram = dict(telegram)
            telegram.pop("bot_token", None)
            if telegram:
                sanitized["telegram"] = telegram
            else:
                sanitized.pop("telegram", None)
    return sanitized


def migrate_legacy_config(
    *,
    config_path: Path,
    store_path: Path,
    dpapi=None,
    replace: Callable[[Path, Path], None] = os.replace,
    readback_validator: Callable[[SecureCredentialStore, dict[str, str]], None] = _default_readback_validator,
) -> MigrationResult:
    config_path = Path(config_path)
    store_path = Path(store_path)
    config = _load_legacy_config(config_path)
    if not isinstance(config, dict):
        raise SecureStoreError("legacy preference file is unsupported")

    records = _legacy_secret_records(config)

    store = SecureCredentialStore(store_path, dpapi=dpapi, replace=replace)
    try:
        already_present = tuple(field for field in LEGACY_SECRET_FIELDS if field not in records and store.has_secret(field))
    except Exception:
        raise SecureStoreError("secure migration failed") from None
    if not records:
        return MigrationResult(migrated_fields=(), already_present_fields=already_present)

    try:
        for field, value in records.items():
            store.set_secret(field, value)
        readback_validator(store, records)
    except Exception:
        raise SecureStoreError("secure migration failed") from None

    sanitized = _sanitize_legacy_config(config, records)
    try:
        _atomic_write_json(config_path, sanitized, replace)
    except Exception:
        raise SecureStoreError("legacy preference rewrite failed") from None
    return MigrationResult(migrated_fields=tuple(records), already_present_fields=already_present)
