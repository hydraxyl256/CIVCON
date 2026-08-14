"""
Public re-export of role-based access dependencies.

The canonical (and only) implementations live in `app.dependencies.auth`.
This module exists for backwards compatibility: existing routers import
`require_admin`, `require_role`, and `require_admin_or_self` from here.
"""
from app.dependencies.auth import (
    require_admin,
    require_admin_or_self,
    require_role,
)

__all__ = ["require_admin", "require_admin_or_self", "require_role"]