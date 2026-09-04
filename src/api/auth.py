"""RBAC role definitions and authorization helpers.

Re-exports the production JWT authentication from `auth.py` while keeping
backward compatibility with the existing API layer imports.
"""

from __future__ import annotations

from auth import (
    Role,
    get_current_role,
    require_admin,
    require_operator,
    require_trader,
    require_viewer,
)

__all__ = ["Role", "get_current_role", "require_admin", "require_operator", "require_trader", "require_viewer"]
