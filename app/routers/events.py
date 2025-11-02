from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, and_, or_
from typing import List, Optional
from app.database import get_db
from app.models import Event, EventAttendee, User
from app.schemas import EventCreate, EventPublic, AttendeePublic
from app.routers.oauth2 import get_current_user
from app.utils.notifications import notify_user  


router = APIRouter(prefix="/events", tags=["events"])


# --- Helper: build filters for list endpoint
def _build_event_filters(
    upcoming: Optional[bool],
    date_from: Optional[str],
    date_to: Optional[str],
    category: Optional[str],
    location: Optional[str],
    search: Optional[str],
    organizer_id: Optional[int],
):
    filters = []
    if upcoming is True:
        filters.append(Event.date >= func.current_date())
    if upcoming is False:
        filters.append(Event.date < func.current_date())

    if date_from:
        filters.append(Event.date >= date_from)
    if date_to:
        filters.append(Event.date <= date_to)

    if category:
        filters.append(Event.category.ilike(f"%{category}%"))
    if location:
        filters.append(Event.location.ilike(f"%{location}%"))
    if organizer_id:
        filters.append(Event.organizer_id == organizer_id)

    if search:
        s = f"%{search}%"
        filters.append(or_(Event.title.ilike(s), Event.description.ilike(s)))

    return filters



# GET /events/  - list with filters, pagination, sort
@router.get("/", response_model=List[EventPublic])
async def list_events(
    db: AsyncSession = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    upcoming: Optional[bool] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    organizer_id: Optional[int] = Query(None),
    sort: Optional[str] = Query("date_desc", regex="^(date_desc|date_asc|popular)$"),
):
    """
    List events with pagination and filters.
    - `upcoming`: True to show future events only, False for past only.
    - `date_from`, `date_to`: ISO date strings.
    - `category`, `location`, `search`, `organizer_id`: filters.
    - `sort`: date_desc (default), date_asc, popular (by attendee count).
    """
    filters = _build_event_filters(upcoming, date_from, date_to, category, location, search, organizer_id)

    # build base query
    stmt = select(Event)

    if filters:
        stmt = stmt.where(and_(*filters))

    # sorting
    if sort == "date_asc":
        stmt = stmt.order_by(Event.date.asc(), Event.created_at.desc())
    elif sort == "popular":
        # join subquery for attendee counts to sort by popularity
        subq = select(EventAttendee.event_id, func.count(EventAttendee.id).label("c")).group_by(EventAttendee.event_id).subquery()
        stmt = stmt.outerjoin(subq, Event.id == subq.c.event_id).order_by(func.coalesce(subq.c.c, 0).desc(), Event.date.desc())
    else:
        stmt = stmt.order_by(Event.date.desc(), Event.created_at.desc())

    stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(stmt)
    events = result.scalars().all()

    # fetch attendees_count efficiently in batch
    if events:
        event_ids = [e.id for e in events]
        counts_q = await db.execute(
            select(EventAttendee.event_id, func.count(EventAttendee.id)).where(EventAttendee.event_id.in_(event_ids)).group_by(EventAttendee.event_id)
        )
        counts = {row[0]: row[1] for row in counts.all()}
    else:
        counts = {}

    # attach attendees_count for response_model
    for e in events:
        setattr(e, "attendees_count", counts.get(e.id, 0))

    return events



# POST /events/ - create event
@router.post("/", response_model=EventPublic, status_code=status.HTTP_201_CREATED)
async def create_event(
    event_in: EventCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create an event. Authenticated users become the organizer.
    """
    try:
        event = Event(
            title=event_in.title.strip(),
            description=(event_in.description or "").strip(),
            date=event_in.date,
            time=(event_in.time or "").strip(),
            location=event_in.location.strip(),
            category=(event_in.category or "").strip(),
            organizer_id=current_user.id,
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)

        # ensure attendees_count attribute is present
        setattr(event, "attendees_count", 0)
        return event
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create event") from exc



# GET /events/{event_id} - single event
@router.get("/{event_id}", response_model=EventPublic)
async def get_event(event_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(Event).where(Event.id == event_id)
    result = await db.execute(stmt)
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    attendees_count = await db.scalar(select(func.count()).where(EventAttendee.event_id == event_id))
    setattr(event, "attendees_count", attendees_count or 0)
    return event



# POST /events/{event_id}/join - join (RSVP)
@router.post("/{event_id}/join", status_code=status.HTTP_200_OK)
async def join_event(
    event_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Join (RSVP) an event and notify the organizer."""
    # Ensure event exists
    result = await db.execute(select(Event).where(Event.id == event_id))
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Prevent duplicates
    existing = await db.execute(
        select(EventAttendee).where(
            EventAttendee.event_id == event_id,
            EventAttendee.user_id == current_user.id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Already joined")

    attendee = EventAttendee(user_id=current_user.id, event_id=event_id)
    db.add(attendee)
    await db.commit()

    #  Send notification (async safe)
    try:
        message = f"{current_user.first_name} {current_user.last_name} joined your event: {event.title}"
        await notify_user(event.organizer_id, message, link=f"/events/{event.id}")
    except Exception as e:
        print(f" Notification send failed: {e}")

    return {"message": "Joined event"}




# POST /events/{event_id}/leave - leave event
@router.post("/{event_id}/leave", status_code=status.HTTP_200_OK)
async def leave_event(event_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    stmt = select(EventAttendee).where(EventAttendee.event_id == event_id, EventAttendee.user_id == current_user.id)
    result = await db.execute(stmt)
    attendee = result.scalar_one_or_none()
    if not attendee:
        raise HTTPException(status_code=404, detail="Not attending")

    try:
        await db.delete(attendee)
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to leave event")

    return {"message": "Left event"}



# GET /events/{event_id}/attendees - list attendees (paginated)
@router.get("/{event_id}/attendees", response_model=List[AttendeePublic])
async def get_attendees(event_id: int, db: AsyncSession = Depends(get_db), skip: int = 0, limit: int = 50):
    # check exists
    evt = await db.execute(select(Event).where(Event.id == event_id))
    if not evt.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Event not found")

    stmt = (
        select(User)
        .join(EventAttendee, EventAttendee.user_id == User.id)
        .where(EventAttendee.event_id == event_id)
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(stmt)
    users = result.scalars().all()

    # return Pydantic models directly
    return users
