from typing import List
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import UserOut, Role
from .oauth2 import get_current_user  # must be async

def require_role(roles: List[Role]):
    """
    Factory function returning an async dependency that checks user roles.
    """
    if not roles:
        raise ValueError("roles list cannot be empty")

    async def dependency(
        user: UserOut = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
    ) -> UserOut:
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User account is suspended or inactive"
            )

        allowed_roles = [r.value for r in roles]
        if user.role.value not in allowed_roles and user.role.value != Role.ADMIN.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation requires one of the following roles: {', '.join(allowed_roles)}"
            )
        return user

    return dependency

async def require_admin_or_self(
    user_id: int,
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> UserOut:
    """
    Allow access if user is admin or operating on their own resource.
    """
    if user.role.value != Role.ADMIN.value and user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation requires admin role or self"
        )
    return user
