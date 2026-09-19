"""Secure credential storage with two interchangeable backends.

  KeyringStore        -- default. Passwords go into the OS secret service
                         (KWallet / GNOME Keyring). Nothing hits disk in plain
                         text; we never even see the storage file.
  EncryptedFileStore  -- fallback for headless / no-keyring-daemon boxes.
                         A single master password derives a Fernet key
                         (PBKDF2-HMAC-SHA256); per-server tokens live in an
                         0600 file.

Both satisfy the same tiny interface (set/get/delete by server name), so the
rest of the app never cares which one is active.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Protocol

from .store import config_dir

SERVICE = "davideo"


class CredentialError(Exception):
    pass


class CredentialStore(Protocol):
    backend_name: str

    def set_password(self, server_name: str, password: str) -> None: ...
    def get_password(self, server_name: str) -> str | None: ...
    def delete_password(self, server_name: str) -> None: ...


class KeyringStore:
    backend_name = "keyring"

    def __init__(self) -> None:
        import keyring  # lazy: keep core importable without the dep
        self._keyring = keyring

    def probe(self) -> None:
        """Round-trip a throwaway secret to confirm a real backend is wired up
        (the 'fail'/'null' keyring backends raise or silently drop)."""
        self._keyring.set_password(SERVICE, "__probe__", "1")
        if self._keyring.get_password(SERVICE, "__probe__") != "1":
            raise CredentialError("keyring backend is not persisting secrets")
        self._keyring.delete_password(SERVICE, "__probe__")

    def set_password(self, server_name: str, password: str) -> None:
        self._keyring.set_password(SERVICE, server_name, password)

    def get_password(self, server_name: str) -> str | None:
        return self._keyring.get_password(SERVICE, server_name)

    def delete_password(self, server_name: str) -> None:
        try:
            self._keyring.delete_password(SERVICE, server_name)
        except self._keyring.errors.PasswordDeleteError:
            pass


class EncryptedFileStore:
    backend_name = "encrypted-file"
    ITERATIONS = 390_000

    def __init__(self, master_password: str, path: Path | None = None) -> None:
        if not master_password:
            raise CredentialError(
                "encrypted backend needs a master password "
                "(set DAVIDEO_MASTER)"
            )
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        self._Fernet = Fernet
        self._hashes = hashes
        self._PBKDF2HMAC = PBKDF2HMAC
        self._path = path or (config_dir() / "secrets.enc")
        self._master = master_password.encode()
        self._load()

    def _derive(self, salt: bytes) -> bytes:
        kdf = self._PBKDF2HMAC(
            algorithm=self._hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self.ITERATIONS,
        )
        return base64.urlsafe_b64encode(kdf.derive(self._master))

    def _load(self) -> None:
        if self._path.exists():
            blob = json.loads(self._path.read_text(encoding="utf-8"))
            self._salt = base64.b64decode(blob["salt"])
            self._data: dict[str, str] = blob.get("data", {})
        else:
            self._salt = os.urandom(16)
            self._data = {}
        self._fernet = self._Fernet(self._derive(self._salt))
        # validate master password against an existing entry, if any
        for token in self._data.values():
            try:
                self._fernet.decrypt(token.encode())
            except Exception as exc:  # cryptography.fernet.InvalidToken
                raise CredentialError("wrong master password") from exc
            break

    def _save(self) -> None:
        """Write atomically, and 0600 from the moment the file exists.

        Creating the file then chmod-ing it leaves a window where the ciphertext
        sits at the umask default (usually 0644); ``os.open`` with an explicit
        mode closes that window, and ``os.replace`` preserves it.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            {"salt": base64.b64encode(self._salt).decode(), "data": self._data}
        )
        tmp = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, self._path)

    def set_password(self, server_name: str, password: str) -> None:
        self._data[server_name] = self._fernet.encrypt(password.encode()).decode()
        self._save()

    def get_password(self, server_name: str) -> str | None:
        token = self._data.get(server_name)
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except Exception as exc:  # cryptography.fernet.InvalidToken and friends
            # Never let a raw cryptography exception escape into the caller
            # (which is UI code) -- a corrupt or foreign-keyed entry is a
            # credential-store problem and should read as one.
            raise CredentialError(
                f"cannot decrypt the stored password for '{server_name}' "
                "(wrong DAVIDEO_MASTER, or the entry is corrupt)"
            ) from exc

    def delete_password(self, server_name: str) -> None:
        if self._data.pop(server_name, None) is not None:
            self._save()


def get_credential_store() -> CredentialStore:
    """Pick a backend. Honors DAVIDEO_MASTER (forces encrypted file);
    otherwise tries keyring and probes it; raises CredentialError with guidance
    if neither is usable."""
    master = os.environ.get("DAVIDEO_MASTER")
    if master:
        return EncryptedFileStore(master)
    try:
        store = KeyringStore()
        store.probe()
        return store
    except Exception as exc:
        raise CredentialError(
            "no usable keyring backend. Either start a secret-service daemon "
            "(KWallet / gnome-keyring), or set DAVIDEO_MASTER to use the "
            f"encrypted-file fallback. (underlying: {exc})"
        ) from exc
