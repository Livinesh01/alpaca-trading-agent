"""Production JWT authentication for the Sentinel API.

Supports two modes:
- `development`: uses X-Dev-Role header (same as existing dev auth)
- `production`: uses JWT bearer tokens with role claims, validated with a
  signing secret from the environment. Supports RS256 (asymmetric) and
  HS256 (symmetric) algorithms.

Token format: JWT with claims { "sub": user_id, "role": "ADMIN|TRADER|VIEWER",
"exp": epoch, "iat": epoch }

Public endpoints: /health, /api/v1/health, /ready
Authenticated endpoints require a valid JWT or (in dev mode) X-Dev-Role header.
"""

from __future__ import annotations

import os
import time
from enum import StrEnum
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from errors import ConfigurationError

VALID_ROLES = {"VIEWER", "TRADER", "OPERATOR", "ADMIN"}
_DEFAULT_JWT_TTL_SECONDS = 3600
_JWT_LEEWAY_SECONDS = 30
_ALLOWED_ALGORITHMS = {"HS256", "RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}


class Role(StrEnum):
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    TRADER = "TRADER"
    VIEWER = "VIEWER"


class AuthMode(StrEnum):
    DISABLED = "disabled"
    DEVELOPMENT = "development"
    PRODUCTION = "production"


_SECURITY_SCHEME = HTTPBearer(auto_error=False)


def _env_auth_mode() -> str:
    mode = os.environ.get("API_AUTH_MODE", "").strip().lower()
    if not mode:
        app_env = os.environ.get("APP_ENV", "").strip().lower()
        if app_env == "production":
            # APP_ENV=production MUST NOT silently fall back to disabled/development auth.
            raise ConfigurationError(
                "API_AUTH_MODE is required and must be 'production' when APP_ENV=production"
            )
        return "disabled"
    if mode not in {"disabled", "development", "production"}:
        raise ConfigurationError(
            f"API_AUTH_MODE must be 'disabled', 'development', or 'production', got: {mode!r}"
        )
    return mode


def _resolve_secret() -> str:
    secret = os.environ.get("JWT_SIGNING_SECRET", "").strip()
    if not secret:
        raise ConfigurationError("JWT_SIGNING_SECRET is required in production auth mode")
    if len(secret) < 32:
        raise ConfigurationError("JWT_SIGNING_SECRET must be at least 32 characters")
    return secret


def _resolve_algorithm() -> str:
    algorithm = os.environ.get("JWT_ALGORITHM", "HS256").strip().upper()
    if algorithm not in _ALLOWED_ALGORITHMS:
        raise ConfigurationError(
            f"JWT_ALGORITHM must be one of {sorted(_ALLOWED_ALGORITHMS)}, got: {algorithm!r}"
        )
    return algorithm


def _resolve_issuer() -> str | None:
    issuer = os.environ.get("JWT_ISSUER", "").strip()
    return issuer or None


def _resolve_audience() -> str | None:
    audience = os.environ.get("JWT_AUDIENCE", "").strip()
    return audience or None


def _decode_token(token: str) -> dict[str, Any]:
    secret = _resolve_secret()
    alg = _resolve_algorithm()
    options = {"require": ["exp", "iat", "sub", "role"]}
    try:
        if alg in ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512"):
            jwt_public_key = os.environ.get("JWT_PUBLIC_KEY", "").strip()
            if not jwt_public_key:
                raise ConfigurationError("JWT_PUBLIC_KEY is required for asymmetric algorithms")
            payload = jwt.decode(
                token,
                jwt_public_key,
                algorithms=[alg],
                options=options,
                issuer=_resolve_issuer(),
                audience=_resolve_audience(),
                leeway=_JWT_LEEWAY_SECONDS,
            )
        else:
            payload = jwt.decode(
                token,
                secret,
                algorithms=[alg],
                options=options,
                issuer=_resolve_issuer(),
                audience=_resolve_audience(),
                leeway=_JWT_LEEWAY_SECONDS,
            )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}, detail="invalid token")
    role = str(payload.get("role", "")).upper()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid role in token")
    return payload


def _extract_bearer_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    if credentials is not None and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return None


def get_current_role(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_SECURITY_SCHEME),  # noqa: B008
    x_dev_role: str | None = Header(default=None),
) -> Role:
    """Resolve the caller's role from JWT (production) or X-Dev-Role (development)."""
    mode = _env_auth_mode()

    if mode == "production":
        token = _extract_bearer_token(credentials)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
                detail="authentication required",
            )
        payload = _decode_token(token)
        return Role(str(payload["role"]).upper())

    if mode == "development":
        role_str = (x_dev_role or "").strip().upper()
        try:
            return Role(role_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            )

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="authentication is not configured",
    )


def create_token(
    user_id: str,
    role: str,
    *,
    ttl_seconds: int = _DEFAULT_JWT_TTL_SECONDS,
    algorithm: str | None = None,
) -> str:
    """Create a signed JWT for a user (used by the token-issuing endpoint).

    This is for operator convenience during bootstrap / testing. In a real
    deployment, the JWT is issued by the OIDC provider, not this service.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}, got {role!r}")
    secret = _resolve_secret()
    alg = algorithm or _resolve_algorithm()
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    issuer = _resolve_issuer()
    if issuer:
        payload["iss"] = issuer
    audience = _resolve_audience()
    if audience:
        payload["aud"] = audience
    return jwt.encode(payload, secret, algorithm=alg)


def require_viewer(role: Role = Depends(get_current_role)) -> Role:  # noqa: B008
    return role


def require_trader(role: Role = Depends(get_current_role)) -> Role:  # noqa: B008
    if role not in {Role.TRADER, Role.OPERATOR, Role.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="trader role required")
    return role


def require_operator(role: Role = Depends(get_current_role)) -> Role:  # noqa: B008
    if role not in {Role.OPERATOR, Role.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="operator role required")
    return role


def require_admin(role: Role = Depends(get_current_role)) -> Role:  # noqa: B008
    if role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return role


@lru_cache(maxsize=1)
def auth_mode() -> AuthMode:
    return AuthMode(_env_auth_mode())


def is_production_auth() -> bool:
    return auth_mode() == AuthMode.PRODUCTION
