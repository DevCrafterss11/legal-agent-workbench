"""Envelope encryption for sensitive project-local files.

The data-encryption key (DEK) is generated per write.  A key-encryption key
(KEK) lives outside the project directory: either in macOS Keychain, an
environment-injected secret, or AWS KMS.  Files are authenticated with
AES-256-GCM and can be detected by a small versioned header.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from legalworkbench.fs import atomic_write_bytes

MAGIC = b"LAWBENCH-ENC-v1\n"
_AAD_PREFIX = b"legal-agent-workbench:v1:"


class EncryptionConfigurationError(RuntimeError):
    """Raised when encrypted storage is enabled but the KEK is unavailable."""


@dataclass(frozen=True)
class EncryptionConfig:
    provider: str = "disabled"
    keychain_service: str = ""
    keychain_account: str = ""
    aws_kms_key_id: str = ""
    aws_region: str = ""

    @property
    def enabled(self) -> bool:
        return self.provider != "disabled"


def default_keychain_service(cwd: str | Path | None = None) -> str:
    root = str(Path(cwd or Path.cwd()).resolve()).encode("utf-8")
    suffix = hashlib.sha256(root).hexdigest()[:12]
    return f"legal-agent-workbench-{suffix}"


def load_encryption_config(cwd: str | Path | None = None) -> EncryptionConfig:
    """Load non-secret provider metadata from env and plain settings.json."""

    root = Path(cwd or Path.cwd()).resolve()
    settings: dict[str, Any] = {}
    settings_path = root / ".lawbench" / "settings.json"
    if settings_path.exists():
        try:
            candidate = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                settings = candidate
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            settings = {}
    stored = settings.get("encryption", {})
    if not isinstance(stored, dict):
        stored = {}
    provider = (
        str(
            os.environ.get("LEGAL_WORKBENCH_ENCRYPTION_PROVIDER")
            or stored.get("provider")
            or "disabled"
        )
        .strip()
        .lower()
    )
    aliases = {"off": "disabled", "none": "disabled", "keychain": "macos-keychain"}
    provider = aliases.get(provider, provider)
    return EncryptionConfig(
        provider=provider,
        keychain_service=str(
            os.environ.get("LEGAL_WORKBENCH_KEYCHAIN_SERVICE")
            or stored.get("keychain_service")
            or default_keychain_service(root)
        ),
        keychain_account=str(
            os.environ.get("LEGAL_WORKBENCH_KEYCHAIN_ACCOUNT")
            or stored.get("keychain_account")
            or getpass.getuser()
        ),
        aws_kms_key_id=str(
            os.environ.get("LEGAL_WORKBENCH_AWS_KMS_KEY_ID")
            or stored.get("aws_kms_key_id")
            or ""
        ),
        aws_region=str(
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or stored.get("aws_region")
            or ""
        ),
    )


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def encrypt_bytes(
    data: bytes,
    *,
    cwd: str | Path | None = None,
    purpose: str = "sensitive-file",
) -> bytes:
    config = load_encryption_config(cwd)
    if not config.enabled:
        return data
    aad = _AAD_PREFIX + purpose.encode("utf-8")
    dek = os.urandom(32)
    header: dict[str, Any] = {
        "version": 1,
        "provider": config.provider,
        "purpose": purpose,
    }
    if config.provider in {"env", "macos-keychain"}:
        kek = _local_kek(config)
        wrap_nonce = os.urandom(12)
        wrapped_key = AESGCM(kek).encrypt(wrap_nonce, dek, aad)
        header.update(
            {
                "wrap_nonce": _b64(wrap_nonce),
                "wrapped_key": _b64(wrapped_key),
                "keychain_service": config.keychain_service
                if config.provider == "macos-keychain"
                else "",
                "keychain_account": config.keychain_account
                if config.provider == "macos-keychain"
                else "",
            }
        )
    elif config.provider == "aws-kms":
        dek, encrypted_dek = _aws_generate_data_key(config, purpose)
        header.update(
            {
                "encrypted_key": _b64(encrypted_dek),
                "aws_kms_key_id": config.aws_kms_key_id,
                "aws_region": config.aws_region,
            }
        )
    else:
        raise EncryptionConfigurationError(
            f"unsupported encryption provider: {config.provider}"
        )
    data_nonce = os.urandom(12)
    ciphertext = AESGCM(dek).encrypt(data_nonce, data, aad)
    header["data_nonce"] = _b64(data_nonce)
    return (
        MAGIC
        + json.dumps(header, separators=(",", ":")).encode("utf-8")
        + b"\n"
        + ciphertext
    )


def decrypt_bytes(data: bytes, *, cwd: str | Path | None = None) -> bytes:
    if not is_encrypted(data):
        return data
    try:
        header_line, ciphertext = data[len(MAGIC) :].split(b"\n", 1)
        header = json.loads(header_line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EncryptionConfigurationError("invalid encrypted file header") from exc
    purpose = str(header.get("purpose") or "sensitive-file")
    aad = _AAD_PREFIX + purpose.encode("utf-8")
    provider = str(header.get("provider") or "")
    configured = load_encryption_config(cwd)
    if provider == "env":
        config = EncryptionConfig(provider="env")
        kek = _local_kek(config)
        dek = AESGCM(kek).decrypt(
            _unb64(header["wrap_nonce"]), _unb64(header["wrapped_key"]), aad
        )
    elif provider == "macos-keychain":
        config = EncryptionConfig(
            provider=provider,
            keychain_service=str(
                header.get("keychain_service") or configured.keychain_service
            ),
            keychain_account=str(
                header.get("keychain_account") or configured.keychain_account
            ),
        )
        kek = _local_kek(config)
        dek = AESGCM(kek).decrypt(
            _unb64(header["wrap_nonce"]), _unb64(header["wrapped_key"]), aad
        )
    elif provider == "aws-kms":
        config = EncryptionConfig(
            provider=provider,
            aws_kms_key_id=str(
                header.get("aws_kms_key_id") or configured.aws_kms_key_id
            ),
            aws_region=str(header.get("aws_region") or configured.aws_region),
        )
        dek = _aws_decrypt_data_key(config, _unb64(header["encrypted_key"]), purpose)
    else:
        raise EncryptionConfigurationError(
            f"unsupported encrypted file provider: {provider}"
        )
    return AESGCM(dek).decrypt(_unb64(header["data_nonce"]), ciphertext, aad)


def secure_write_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    cwd: str | Path | None = None,
    purpose: str = "sensitive-file",
) -> None:
    atomic_write_bytes(path, encrypt_bytes(data, cwd=cwd, purpose=purpose))


def secure_write_text(
    path: str | os.PathLike[str],
    data: str,
    *,
    cwd: str | Path | None = None,
    purpose: str = "sensitive-file",
) -> None:
    secure_write_bytes(path, data.encode("utf-8"), cwd=cwd, purpose=purpose)


def secure_read_bytes(
    path: str | os.PathLike[str], *, cwd: str | Path | None = None
) -> bytes:
    return decrypt_bytes(Path(path).read_bytes(), cwd=cwd)


def secure_read_text(
    path: str | os.PathLike[str], *, cwd: str | Path | None = None
) -> str:
    return secure_read_bytes(path, cwd=cwd).decode("utf-8")


def _local_kek(config: EncryptionConfig) -> bytes:
    if config.provider == "env":
        encoded = os.environ.get("LEGAL_WORKBENCH_ENCRYPTION_KEY", "")
        if not encoded:
            raise EncryptionConfigurationError(
                "LEGAL_WORKBENCH_ENCRYPTION_KEY is required"
            )
    elif config.provider == "macos-keychain":
        if sys.platform != "darwin":
            raise EncryptionConfigurationError("macOS Keychain provider requires macOS")
        command = [
            "security",
            "find-generic-password",
            "-a",
            config.keychain_account,
            "-s",
            config.keychain_service,
            "-w",
        ]
        try:
            encoded = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise EncryptionConfigurationError(
                f"key not available in macOS Keychain service {config.keychain_service!r}"
            ) from exc
    else:
        raise EncryptionConfigurationError(
            f"local KEK unsupported for {config.provider}"
        )
    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise EncryptionConfigurationError(
            "encryption key must be URL-safe base64"
        ) from exc
    if len(key) != 32:
        raise EncryptionConfigurationError(
            "encryption key must decode to exactly 32 bytes"
        )
    return key


def _aws_client(config: EncryptionConfig):
    if not config.aws_kms_key_id:
        raise EncryptionConfigurationError("LEGAL_WORKBENCH_AWS_KMS_KEY_ID is required")
    try:
        import boto3  # type: ignore
    except ImportError as exc:
        raise EncryptionConfigurationError(
            "AWS KMS provider requires the optional aws-kms dependencies"
        ) from exc
    kwargs = {"region_name": config.aws_region} if config.aws_region else {}
    return boto3.client("kms", **kwargs)


def _aws_generate_data_key(
    config: EncryptionConfig, purpose: str
) -> tuple[bytes, bytes]:
    response = _aws_client(config).generate_data_key(
        KeyId=config.aws_kms_key_id,
        KeySpec="AES_256",
        EncryptionContext={"application": "legal-agent-workbench", "purpose": purpose},
    )
    return bytes(response["Plaintext"]), bytes(response["CiphertextBlob"])


def _aws_decrypt_data_key(
    config: EncryptionConfig, encrypted_key: bytes, purpose: str
) -> bytes:
    kwargs: dict[str, Any] = {
        "CiphertextBlob": encrypted_key,
        "EncryptionContext": {
            "application": "legal-agent-workbench",
            "purpose": purpose,
        },
    }
    if config.aws_kms_key_id:
        kwargs["KeyId"] = config.aws_kms_key_id
    response = _aws_client(config).decrypt(**kwargs)
    return bytes(response["Plaintext"])


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(str(value).encode("ascii"))
