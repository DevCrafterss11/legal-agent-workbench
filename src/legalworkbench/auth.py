"""JWT authentication and role-based authorization for the workbench API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from legalworkbench.paths import settings_path
from legalworkbench.secrets import load_secrets


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"workbench:read"}),
    "reviewer": frozenset(
        {"workbench:read", "document:write", "review:create"}
    ),
    "operator": frozenset(
        {"workbench:read", "document:write", "review:create", "task:manage"}
    ),
    "admin": frozenset({"*"}),
}


class AuthError(ValueError):
    """Raised when an access token is missing, invalid, or expired."""


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    roles: frozenset[str] = field(default_factory=lambda: frozenset({"viewer"}))

    def can(self, permission: str) -> bool:
        granted: set[str] = set()
        for role in self.roles:
            granted.update(ROLE_PERMISSIONS.get(role, ()))
        return "*" in granted or permission in granted

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "roles": sorted(self.roles),
        }


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "local"
    issuer: str = "legal-agent-workbench"
    audience: str = "legal-agent-api"
    local_tenant_id: str = "local"
    local_user_id: str = "local-admin"
    local_roles: tuple[str, ...] = ("admin",)
    clock_skew_seconds: int = 30

    @staticmethod
    def load(cwd: str | Path | None = None) -> "AuthConfig":
        raw: dict[str, Any] = {}
        path = settings_path(cwd)
        if path.exists():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                raw = parsed if isinstance(parsed, dict) else {}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raw = {}
        auth = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}
        roles = auth.get("local_roles", ["admin"])
        if not isinstance(roles, list):
            roles = ["admin"]
        return AuthConfig(
            mode=str(os.environ.get("LEGAL_WORKBENCH_AUTH_MODE") or auth.get("mode") or "local").lower(),
            issuer=str(auth.get("issuer") or "legal-agent-workbench"),
            audience=str(auth.get("audience") or "legal-agent-api"),
            local_tenant_id=str(auth.get("local_tenant_id") or "local"),
            local_user_id=str(auth.get("local_user_id") or "local-admin"),
            local_roles=tuple(str(item) for item in roles if str(item) in ROLE_PERMISSIONS) or ("admin",),
            clock_skew_seconds=max(
                0, int(auth.get("clock_skew_seconds", 30))
            ),
        )


class AuthManager:
    def __init__(self, cwd: str | Path | None = None, *, config: AuthConfig | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.config = config or AuthConfig.load(self.cwd)

    def authenticate(
        self,
        *,
        authorization: str = "",
        cookie_token: str = "",
    ) -> Principal:
        if self.config.mode == "local":
            return Principal(
                tenant_id=self.config.local_tenant_id,
                user_id=self.config.local_user_id,
                roles=frozenset(self.config.local_roles),
            )
        if self.config.mode != "jwt":
            raise AuthError(f"unsupported auth mode: {self.config.mode}")
        token = _bearer_token(authorization) or cookie_token.strip()
        if not token:
            raise AuthError("bearer token required")
        return self.verify_token(token)

    def issue_token(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: list[str] | tuple[str, ...],
        ttl_seconds: int = 3600,
    ) -> str:
        if not tenant_id.strip() or not user_id.strip():
            raise AuthError("tenant_id and user_id are required")
        normalized_roles = sorted({role for role in roles if role in ROLE_PERMISSIONS})
        if not normalized_roles:
            raise AuthError("at least one valid role is required")
        now = int(time.time())
        payload = {
            "iss": self.config.issuer,
            "aud": self.config.audience,
            "sub": user_id.strip(),
            "tenant_id": tenant_id.strip(),
            "roles": normalized_roles,
            "iat": now,
            "exp": now + max(60, int(ttl_seconds)),
        }
        header = {"alg": "HS256", "typ": "JWT"}
        signing_input = f"{_encode_json(header)}.{_encode_json(payload)}"
        signature = hmac.new(
            self._signing_key(), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{signing_input}.{_b64encode(signature)}"

    def verify_token(self, token: str) -> Principal:
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            header = _decode_json(encoded_header)
            payload = _decode_json(encoded_payload)
            signature = _b64decode(encoded_signature)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError("invalid JWT format") from exc
        if header.get("alg") != "HS256" or header.get("typ") != "JWT":
            raise AuthError("unsupported JWT header")
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        expected = hmac.new(self._signing_key(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise AuthError("invalid JWT signature")
        now = int(time.time())
        skew = self.config.clock_skew_seconds
        if payload.get("iss") != self.config.issuer:
            raise AuthError("invalid JWT issuer")
        if payload.get("aud") != self.config.audience:
            raise AuthError("invalid JWT audience")
        try:
            expires_at = int(payload["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthError("JWT exp claim required") from exc
        if now >= expires_at + skew:
            raise AuthError("JWT expired")
        try:
            issued_at = int(payload["iat"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthError("JWT iat claim required") from exc
        if issued_at > now + skew:
            raise AuthError("JWT issued in the future")
        tenant_id = str(payload.get("tenant_id") or "").strip()
        user_id = str(payload.get("sub") or "").strip()
        roles = payload.get("roles")
        if not tenant_id or not user_id or not isinstance(roles, list):
            raise AuthError("JWT identity claims required")
        normalized_roles = frozenset(
            str(role) for role in roles if str(role) in ROLE_PERMISSIONS
        )
        if not normalized_roles:
            raise AuthError("JWT contains no valid roles")
        return Principal(tenant_id=tenant_id, user_id=user_id, roles=normalized_roles)

    def _signing_key(self) -> bytes:
        value = str(
            os.environ.get("LEGAL_WORKBENCH_JWT_SECRET")
            or load_secrets(self.cwd).get("jwt_signing_key")
            or ""
        )
        if len(value.encode("utf-8")) < 32:
            raise AuthError("JWT signing key must contain at least 32 bytes")
        return value.encode("utf-8")


def _bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.strip().partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _encode_json(value: dict[str, Any]) -> str:
    return _b64encode(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )


def _decode_json(value: str) -> dict[str, Any]:
    parsed = json.loads(_b64decode(value).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("JWT segment must be an object")
    return parsed


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


__all__ = [
    "AuthConfig",
    "AuthError",
    "AuthManager",
    "Principal",
    "ROLE_PERMISSIONS",
]
