from __future__ import annotations

import os
from enum import StrEnum

from fastapi import Depends, Header, HTTPException, status


class Role(StrEnum):
    ADMIN = "ADMIN"
    TRADER = "TRADER"
    VIEWER = "VIEWER"


def current_role(x_dev_role: str | None = Header(default=None)) -> Role:
    """Development-only identity boundary; never pretends to be production auth."""
    if os.environ.get("API_AUTH_MODE", "disabled").strip().lower() != "development":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="authentication is not configured")
    try:
        return Role((x_dev_role or "").strip().upper())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required") from exc


ROLE_DEP = Depends(current_role)


def require_viewer(role: Role = ROLE_DEP) -> Role:
    return role


def require_trader(role: Role = ROLE_DEP) -> Role:
    if role not in {Role.TRADER, Role.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="trader role required")
    return role


def require_admin(role: Role = ROLE_DEP) -> Role:
    if role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return role
