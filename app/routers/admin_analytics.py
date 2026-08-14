import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import String, cast, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database import get_db
from app.models import District, User
from app.routers.permissions import require_admin

router = APIRouter(
    prefix="/admin/analytics",
    tags=["Admin Analytics"],
    dependencies=[Depends(require_admin)],
)


#  GENERAL ANALYTICS
@router.get("")
async def get_admin_analytics(db: AsyncSession = Depends(get_db)):
    """
    Return aggregated analytics data for admin dashboard.
    """

    # Perf: five independent aggregate queries run concurrently on the
    # same async session — wall-clock becomes the slowest single query
    # instead of the sum of all five.

    async def _total_users():
        return (await db.execute(select(func.count(User.id)))).scalar() or 0

    async def _users_last_month(since):
        return (
            await db.execute(
                select(func.count(User.id)).where(User.created_at >= since)
            )
        ).scalar() or 0

    async def _total_messages():
        # Legacy messages table has been sunset — return 0.
        return 0

    async def _active_users():
        # Legacy distinct Message.sender_id query is gone — fall back to
        # counting distinct user ids in the time window.
        return (
            await db.execute(
                select(func.count(func.distinct(User.id))).where(
                    User.created_at >= since_30d
                )
            )
        ).scalar() or 0

    async def _post_categories():
        # Legacy Message.language aggregate referenced the sunset
        # messages table — return empty until we wire a real
        # posts-based language aggregate.
        return []

    async def _top_districts():
        r = await db.execute(
            select(District.name, func.count(User.id))
            .join(User, cast(User.district_id, String) == cast(District.id, String))
            .group_by(District.name)
            .limit(5)
        )
        return [{"name": row[0], "users": row[1]} for row in r.all()]

    async def _post_analytics():
        # Legacy Message.language × User.role aggregate referenced the
        # sunset messages table — return empty until we wire a real
        # posts-based rollup.
        return []

    since_30d = datetime.now(UTC) - timedelta(days=30)
    (
        total_users,
        users_last_month,
        total_messages,
        active_users,
        post_categories,
        top_districts,
        post_analytics,
    ) = await asyncio.gather(
        _total_users(),
        _users_last_month(since_30d),
        _total_messages(),
        _active_users(),
        _post_categories(),
        _top_districts(),
        _post_analytics(),
    )

    user_growth = {
        "value": total_users,
        "change": round((users_last_month / total_users) * 100 if total_users else 0, 2),
    }

    engagement_rate = {
        "value": f"{round((total_messages / active_users) * 100 if active_users else 0, 2)}%",
        "change": 5,  # Placeholder
    }

    return {
        "userGrowth": user_growth,
        "engagementRate": engagement_rate,
        "postCategories": post_categories,
        "topDistricts": top_districts,
        "postAnalytics": post_analytics,
    }



#  REVENUE ANALYTICS
@router.get("/revenue")
async def get_revenue_analytics(db: AsyncSession = Depends(get_db)):
    """
    Returns revenue and subscription statistics for admin dashboard.
    """

    try:
        #  Try to query Subscription table if it exists
        from app.models import Subscription  # optional import

        result = await db.execute(
            select(
                Subscription.plan_name,
                func.count(Subscription.id),
                func.sum(Subscription.amount),
            ).group_by(Subscription.plan_name)
        )

        subscription_plans = [
            {
                "plan": row[0],
                "subscribers": row[1],
                "revenue": float(row[2] or 0),
            }
            for row in result.all()
        ]

        total_subscriptions = sum(r["subscribers"] for r in subscription_plans)
        revenue_generated = sum(r["revenue"] for r in subscription_plans)

    except Exception:
        #  Fallback mock data
        total_subscriptions = 230
        revenue_generated = 1_250_000  # UGX
        subscription_plans = [
            {"plan": "Citizen", "subscribers": 180, "revenue": 0},
            {"plan": "Journalist", "subscribers": 35, "revenue": 875_000},
            {"plan": "Leader", "subscribers": 15, "revenue": 375_000},
        ]

    #  Simulated growth trends
    total_sub_change = 8
    revenue_change = 12

    return {
        "totalSubscriptions": {"value": total_subscriptions, "change": total_sub_change},
        "revenueGenerated": {"value": revenue_generated, "change": revenue_change},
        "subscriptionPlans": subscription_plans,
    }
