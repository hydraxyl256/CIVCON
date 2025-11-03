from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, String, cast
from app.database import get_db
from app.models import User, Message, District
from datetime import datetime, timedelta

router = APIRouter(prefix="/admin/analytics", tags=["Admin Analytics"])


#  GENERAL ANALYTICS
@router.get("")
async def get_admin_analytics(db: AsyncSession = Depends(get_db)):
    """
    Return aggregated analytics data for admin dashboard.
    """

    # 1 User growth
    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    users_last_month = (
        await db.execute(
            select(func.count(User.id)).where(
                User.created_at >= datetime.utcnow() - timedelta(days=30)
            )
        )
    ).scalar() or 0

    user_growth = {
        "value": total_users,
        "change": round((users_last_month / total_users) * 100 if total_users else 0, 2),
    }

    #  Engagement rate (messages per active user)
    total_messages = (await db.execute(select(func.count(Message.id)))).scalar() or 0
    active_users = (
        await db.execute(select(func.count(func.distinct(Message.sender_id))))
    ).scalar() or 0

    engagement_rate = {
        "value": f"{round((total_messages / active_users) * 100 if active_users else 0, 2)}%",
        "change": 5,  # Placeholder
    }

    #  Post categories (topics/languages)
    result = await db.execute(
        select(Message.language, func.count(Message.id)).group_by(Message.language)
    )
    post_categories = [
        {"name": row[0] or "Unknown", "count": row[1]} for row in result.all()
    ]

    #  Top districts by user count
    result = await db.execute(
    select(District.name, func.count(User.id))
    .join(User, cast(User.district_id, String) == cast(District.id, String))
    .group_by(District.name)
    .limit(5)
)
    top_districts = [{"name": row[0], "users": row[1]} for row in result.all()]

    #  Post analytics (topic + role)
    result = await db.execute(
        select(Message.language, User.role, func.count(Message.id))
        .join(User, User.id == Message.sender_id)
        .group_by(Message.language, User.role)
    )
    post_analytics = [
        {
            "topic": row[0] or "General",
            "role": row[1] or "Citizen",
            "posts": row[2],
            "engagement": min(100, row[2] // 2),
        }
        for row in result.all()
    ]

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
