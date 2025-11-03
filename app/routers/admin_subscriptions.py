from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, update
from datetime import datetime, timedelta
from app.database import get_db
from app.models import Subscription, UserType, User
from app.schemas import UserTypeCreate, UserTypeUpdate
from typing import Optional

router = APIRouter(prefix="/admin/subscriptions", tags=["Admin Subscriptions"])



#  USER TYPES MANAGEMENT
@router.get("/user-types")
async def get_user_types(db: AsyncSession = Depends(get_db)):
    """Return all user types for admin subscription panel."""
    result = await db.execute(select(UserType))
    types = result.scalars().all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "monthlyCharge": float(t.monthly_charge or 0),
            "isFree": t.is_free,
            "description": t.description or "",
        }
        for t in types
    ]


@router.post("/user-types")
async def create_user_type(payload: UserTypeCreate, db: AsyncSession = Depends(get_db)):
    new_type = UserType(
        name=payload.name,
        monthly_charge=payload.monthlyCharge,
        is_free=payload.isFree,
        description=payload.description,
    )
    db.add(new_type)
    await db.commit()
    await db.refresh(new_type)
    return {
        "id": str(new_type.id),
        "name": new_type.name,
        "monthlyCharge": float(new_type.monthly_charge or 0),
        "isFree": new_type.is_free,
        "description": new_type.description or "",
    }


@router.put("/user-types/{type_id}")
async def update_user_type(type_id: int, payload: UserTypeUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(UserType).filter(UserType.id == type_id))
    user_type = result.scalar_one_or_none()
    if not user_type:
        raise HTTPException(status_code=404, detail="User type not found")

    user_type.name = payload.name
    user_type.monthly_charge = payload.monthlyCharge
    user_type.is_free = payload.isFree
    user_type.description = payload.description

    await db.commit()
    await db.refresh(user_type)
    return {"success": True, "message": "User type updated successfully"}


@router.delete("/user-types/{type_id}")
async def delete_user_type(type_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(UserType).filter(UserType.id == type_id))
    user_type = result.scalar_one_or_none()
    if not user_type:
        raise HTTPException(status_code=404, detail="User type not found")

    await db.delete(user_type)
    await db.commit()
    return {"success": True, "message": "User type deleted"}



#  SUBSCRIPTION MANAGEMENT
@router.get("/all")
async def get_all_subscriptions(db: AsyncSession = Depends(get_db)):
    """Return all subscriptions joined with user and type."""
    result = await db.execute(
        select(
            Subscription.id,
            User.full_name,
            Subscription.plan,
            Subscription.status,
            Subscription.start_date,
            Subscription.end_date,
            Subscription.amount,
            Subscription.payment_method,
            UserType.name.label("user_type"),
        )
        .join(User, Subscription.user_id == User.id)
        .join(UserType, User.user_type_id == UserType.id)
    )

    rows = result.all()
    return [
        {
            "id": str(r.id),
            "userName": r.full_name,
            "userType": r.user_type,
            "plan": r.plan,
            "status": r.status,
            "startDate": r.start_date.strftime("%Y-%m-%d") if r.start_date else None,
            "endDate": r.end_date.strftime("%Y-%m-%d") if r.end_date else None,
            "amount": float(r.amount or 0),
            "paymentMethod": r.payment_method or "N/A",
        }
        for r in rows
    ]


@router.post("/create")
async def create_subscription(
    user_id: int,
    plan: str,
    amount: float,
    payment_method: str,
    db: AsyncSession = Depends(get_db),
):
    """Create a new subscription manually (admin action)."""
    new_sub = Subscription(
        user_id=user_id,
        plan=plan,
        status="active",
        start_date=datetime.utcnow(),
        end_date=datetime.utcnow() + timedelta(days=30),
        amount=amount,
        payment_method=payment_method,
    )
    db.add(new_sub)
    await db.commit()
    await db.refresh(new_sub)
    return {"success": True, "message": "Subscription created", "id": str(new_sub.id)}


@router.put("/edit/{subscription_id}")
async def edit_subscription(
    subscription_id: int,
    plan: Optional[str] = None,
    status: Optional[str] = None,
    end_date: Optional[str] = None,
    amount: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
):
    """Edit subscription details (plan, status, amount, etc.)."""
    result = await db.execute(select(Subscription).filter(Subscription.id == subscription_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if plan:
        sub.plan = plan
    if status:
        sub.status = status
    if amount is not None:
        sub.amount = amount
    if end_date:
        sub.end_date = datetime.fromisoformat(end_date)

    await db.commit()
    await db.refresh(sub)
    return {"success": True, "message": "Subscription updated successfully"}


@router.post("/renew/{subscription_id}")
async def renew_subscription(subscription_id: int, db: AsyncSession = Depends(get_db)):
    """Extend subscription by 30 days."""
    result = await db.execute(select(Subscription).filter(Subscription.id == subscription_id))
    sub = result.scalar_one_or_none()

    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sub.end_date = (sub.end_date or datetime.utcnow()) + timedelta(days=30)
    sub.status = "active"

    await db.commit()
    await db.refresh(sub)
    return {"success": True, "message": "Subscription renewed successfully"}


@router.post("/bulk-renew")
async def bulk_renew_expired(db: AsyncSession = Depends(get_db)):
    """Renew all expired subscriptions."""
    result = await db.execute(select(Subscription).filter(Subscription.status == "expired"))
    subs = result.scalars().all()
    count = 0
    for sub in subs:
        sub.status = "active"
        sub.end_date = (sub.end_date or datetime.utcnow()) + timedelta(days=30)
        count += 1

    await db.commit()
    return {"success": True, "renewed": count}


@router.post("/cancel/{subscription_id}")
async def cancel_subscription(subscription_id: int, db: AsyncSession = Depends(get_db)):
    """Cancel a user's subscription."""
    result = await db.execute(select(Subscription).filter(Subscription.id == subscription_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sub.status = "expired"
    sub.end_date = datetime.utcnow()

    await db.commit()
    await db.refresh(sub)
    return {"success": True, "message": "Subscription cancelled successfully"}



#  REVENUE ANALYTICS
@router.get("/revenue-summary")
async def get_revenue_summary(db: AsyncSession = Depends(get_db)):
    """Return total and monthly revenue summary."""
    total_result = await db.execute(
        select(func.sum(Subscription.amount)).where(Subscription.status == "active")
    )
    total_revenue = total_result.scalar() or 0

    monthly_result = await db.execute(
        select(
            func.date_trunc("month", Subscription.start_date).label("month"),
            func.sum(Subscription.amount).label("revenue"),
        )
        .where(Subscription.status == "active")
        .group_by("month")
        .order_by("month")
    )

    trend = [
        {"month": row.month.strftime("%b %Y"), "revenue": float(row.revenue)}
        for row in monthly_result.all()
    ]

    return {"totalRevenue": float(total_revenue), "trend": trend}
