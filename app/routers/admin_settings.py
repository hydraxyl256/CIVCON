from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models import AdminSetting
from app.schemas import AdminSettingOut, AdminSettingUpdate, Role

router = APIRouter(prefix="/admin/settings", tags=["Admin Settings"])


async def get_current_admin(current_user=Depends(get_current_user)):
    if current_user.role.value != Role.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


@router.get("/", response_model=AdminSettingOut)
async def get_admin_settings(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(AdminSetting))
    setting = result.scalar_one_or_none()

    if not setting:
        setting = AdminSetting()
        db.add(setting)
        await db.commit()
        await db.refresh(setting)

    return AdminSettingOut(
        id=setting.id,
        siteName=setting.site_name,
        rolePermissions=setting.role_permissions,
        notifications=setting.notifications,
        updated_at=str(setting.updated_at),
    )


@router.put("/", response_model=AdminSettingOut)
async def update_admin_settings(
    payload: AdminSettingUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(AdminSetting))
    setting = result.scalar_one_or_none()

    if not setting:
        setting = AdminSetting()
        db.add(setting)

    setting.site_name = payload.siteName
    setting.role_permissions = payload.rolePermissions
    setting.notifications = payload.notifications

    await db.commit()
    await db.refresh(setting)

    return AdminSettingOut(
        id=setting.id,
        siteName=setting.site_name,
        rolePermissions=setting.role_permissions,
        notifications=setting.notifications,
        updated_at=str(setting.updated_at),
    )
