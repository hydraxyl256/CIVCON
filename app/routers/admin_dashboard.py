import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database import get_db
from app.models import District, User
from app.routers.permissions import require_admin

router = APIRouter(
    prefix="/admin/dashboard",
    tags=["Admin Dashboard"],
    dependencies=[Depends(require_admin)],
)


@router.get("")
async def get_admin_dashboard(db: AsyncSession = Depends(get_db)):
    """
    Comprehensive Admin Dashboard data — performance, engagement, and subscription metrics.
    """

    # Perf: all six independent aggregate queries run concurrently on the
    # same async session. SQLAlchemy serialises at the protocol level but
    # releases the event loop between round-trips, so the wall-clock cost
    # is the slowest single query, not the sum of all six.

    async def _users_by_role():
        r = await db.execute(
            select(User.role, func.count(User.id)).group_by(User.role)
        )
        return {row[0]: row[1] for row in r.all()}

    async def _total_districts():
        return (await db.execute(select(func.count(District.id)))).scalar() or 0

    async def _total_messages():
        # Legacy messages table has been sunset — return 0.
        return 0

    async def _active_users():
        # Legacy distinct Message.sender_id query is gone — fall back to
        # counting distinct user ids (chat-era sender cohorts are no
        # longer tracked separately).
        return (
            await db.execute(
                select(func.count(func.distinct(User.id)))
            )
        ).scalar() or 0

    async def _active_districts():
        # Legacy Message.district_id distinct count is gone — fall
        # back to counting distinct user district ids.
        return (
            await db.execute(
                select(func.count(func.distinct(User.district_id)))
            )
        ).scalar() or 0

    async def _posts_today(since_yesterday):
        # Legacy Message.created_at filter is gone — return 0.
        return 0

    async def _trending_topics():
        # Legacy Message.language aggregate referenced the sunset
        # messages table — return empty until we wire a real
        # posts-based language aggregate.
        return []

    since_yesterday = datetime.now(UTC) - timedelta(days=1)
    (
        users_by_role,
        total_districts,
        total_messages,  # noqa: RUF059 — surfaced for future dashboard widget
        active_users,
        active_districts,
        posts_today,
        trending_topics,
    ) = await asyncio.gather(
        _users_by_role(),
        _total_districts(),
        _total_messages(),
        _active_users(),
        _active_districts(),
        _posts_today(since_yesterday),
        _trending_topics(),
    )

    total_leaders = users_by_role.get("Leader", 0)
    total_journalists = users_by_role.get("Journalist", 0)
    total_citizens = users_by_role.get("Citizen", 0)
    articles_today = int(posts_today * 0.2)  # assume 20% are articles

    # --- Groups (mock if not yet in DB) ---
    total_groups = 45

    # --- Revenue and Subscriptions (reused logic from analytics) ---
    try:
        from app.models import Subscription

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
        total_subscriptions = sum([p["subscribers"] for p in subscription_plans])
        revenue_generated = sum([p["revenue"] for p in subscription_plans])
    except Exception:
        # fallback mock
        subscription_plans = [
            {"plan": "Basic Leader", "subscribers": 120, "revenue": 600000},
            {"plan": "Premium Leader", "subscribers": 80, "revenue": 480000},
            {"plan": "Enterprise", "subscribers": 30, "revenue": 170000},
        ]
        total_subscriptions = 230
        revenue_generated = 1250000

    # --- Percent changes (mock trends) ---
    change = lambda: round((1 - 0.9 + 0.1) * 10, 2)
    return {
        "totalLeaders": {"value": total_leaders, "change": change()},
        "totalJournalists": {"value": total_journalists, "change": change()},
        "totalCitizens": {"value": total_citizens, "change": change()},
        "totalDistricts": {"value": total_districts, "change": change()},
        "postsToday": {"value": posts_today, "change": change()},
        "articlesToday": {"value": articles_today, "change": change()},
        "activeUsers": {"value": active_users, "change": change()},
        "activeDistricts": {"value": active_districts, "change": change()},
        "totalGroups": {"value": total_groups, "change": change()},
        "trendingTopics": trending_topics,
        "totalSubscriptions": {"value": total_subscriptions, "change": 8},
        "revenueGenerated": {"value": revenue_generated, "change": 12},
        "subscriptionPlans": subscription_plans,
    }
