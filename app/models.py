import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import column_property, relationship
from sqlalchemy.sql import func, text
from sqlalchemy_searchable import TSVectorType

from app.database import Base


# ENUMS
class Role(enum.StrEnum):
    CITIZEN = "citizen"
    MP = "mp"
    JOURNALIST = "journalist"
    ADMIN = "admin"


class NotificationType(enum.StrEnum):
    COMMENT = "COMMENT"
    VOTE = "VOTE"
    GROUP = "GROUP"
    SYSTEM = "SYSTEM"



# ASSOCIATION TABLES
group_members = Table(
    "group_members",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True, nullable=False),
    Column("group_id", Integer, ForeignKey("groups.id"), primary_key=True, nullable=False),
)

post_categories = Table(
    "post_categories",
    Base.metadata,
    Column("post_id", Integer, ForeignKey("posts.id"), primary_key=True, nullable=False),
    Column("category_id", Integer, ForeignKey("categories.id"), primary_key=True, nullable=False),
)


# FOLLOWER MODEL
class Follower(Base):
    __tablename__ = "followers"

    id = Column(Integer, primary_key=True, index=True)
    follower_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    followed_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (UniqueConstraint("follower_id", "followed_id", name="unique_follow"),)

    follower = relationship("User", foreign_keys=[follower_id], back_populates="following")
    followed = relationship("User", foreign_keys=[followed_id], back_populates="followers")

# EVENT MODELS
class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    date = Column(Date, nullable=False)
    time = Column(String(20), nullable=True)
    location = Column(String(255), nullable=False)
    category = Column(String(100), nullable=True)
    organizer_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))

    organizer = relationship("User", back_populates="organized_events")
    attendees = relationship("EventAttendee", back_populates="event", cascade="all, delete-orphan")

# EVENT ATTENDEE MODEL
class EventAttendee(Base):
    __tablename__ = "event_attendees"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)

    user = relationship("User", back_populates="attended_events")
    event = relationship("Event", back_populates="attendees")

#USER MODEL
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=True)
    hashed_password = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    role = Column(Enum(Role), default=Role.CITIZEN)
    preferred_language = Column(String, default="EN")
    # Location & demographics
    region = Column(String, nullable=True)
    district_id = Column(String, nullable=True)
    county_id = Column(String, nullable=True)
    phone_number = Column(String, unique=True, index=True, nullable=True)

    # Profile
    occupation = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    profile_image = Column(String, nullable=True)
    political_interest = Column(String, nullable=True)
    community_role = Column(String, nullable=True)
    interests = Column(JSON, nullable=True)
    privacy_level = Column(String, default="public")
    search_vector = Column(TSVectorType("username", "first_name", "last_name", "bio"))

    # Social logins
    google_id = Column(String, unique=True, nullable=True)
    linkedin_id = Column(String, unique=True, nullable=True)
    # The OAuth provider that minted this user. Empty string for
    # email/password accounts.
    provider = Column(String(32), nullable=True, default="", server_default="")
    # The most recent OAuth subject (sub claim) we saw for this user.
    # Useful for detecting provider-side account takeovers.
    oauth_sub = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    posts = relationship("Post", back_populates="author", cascade="all, delete-orphan")
    comments = relationship("Comment", back_populates="author", cascade="all, delete-orphan")
    votes = relationship("Vote", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    groups = relationship("Group", secondary=group_members, back_populates="members")
    owned_groups = relationship("Group", back_populates="owner")
    mp = relationship("MP", back_populates="user", uselist=False)
    articles = relationship("Article", back_populates="author")

    #  Relationships with followers/following
    followers = relationship(
        "Follower",
        foreign_keys=[Follower.followed_id],
        back_populates="followed",
        cascade="all, delete-orphan"
    )

    following = relationship(
        "Follower",
        foreign_keys=[Follower.follower_id],
        back_populates="follower",
        cascade="all, delete-orphan"
    )

    #  Derived counts 
    followers_count = column_property(
        select(func.count(Follower.id))
        .where(Follower.followed_id == id)
        .correlate_except(Follower)
        .scalar_subquery()
    )

    following_count = column_property(
        select(func.count(Follower.id))
        .where(Follower.follower_id == id)
        .correlate_except(Follower)
        .scalar_subquery()
    )
    organized_events = relationship("Event", back_populates="organizer", cascade="all, delete-orphan")
    attended_events = relationship("EventAttendee", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")
    user_type_id = Column(Integer, ForeignKey("user_types.id"), nullable=True)
    user_type = relationship("UserType", back_populates="users")

    # Auth sessions — one row per login (email, Google, LinkedIn)
    auth_sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")



class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    district_id = Column(String, nullable=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    search_vector = Column(TSVectorType("title", "content"), nullable=True)
    status = Column(String, default="Approved")
    media = relationship("PostMedia", back_populates="post", cascade="all, delete-orphan")
    share_count = Column(Integer, default=0)
    # SECURITY (F-006): soft-delete column. Setting `deleted_at` hides
    # the post from the feed / detail endpoints without losing the row
    # for audit / undelete. The column is Nullable so existing rows
    # already in the database do not need a backfill.
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    deleted_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)


    # Relationships
    author = relationship("User", back_populates="posts")
    comments = relationship("Comment", back_populates="post", cascade="all, delete-orphan")
    votes = relationship("Vote", back_populates="post", cascade="all, delete-orphan")
    live_feeds = relationship("LiveFeed", back_populates="post", cascade="all, delete-orphan")
    group = relationship("Group", back_populates="posts")
    categories = relationship("Category", secondary=post_categories, back_populates="posts")

class PostMedia(Base):
    __tablename__ = "post_media"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"))
    media_url = Column(String, nullable=False)
    media_type = Column(String, nullable=False)  # e.g., image, video

    post = relationship("Post", back_populates="media")


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey("comments.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    search_vector = Column(TSVectorType("content"), nullable=True)
    media_url = Column(String, nullable=True)

    # Relationships
    author = relationship("User", back_populates="comments")
    post = relationship("Post", back_populates="comments")
    parent = relationship("Comment", remote_side=[id], back_populates="replies")
    replies = relationship("Comment", back_populates="parent", cascade="all, delete-orphan", lazy="selectin")


class Vote(Base):
    __tablename__ = "votes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    vote_type = Column(String, default="like")  # e.g., "like"
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User", back_populates="votes")
    post = relationship("Post", back_populates="votes")


class MP(Base):
    __tablename__ = "mps"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    phone_number = Column(String, nullable=True)
    email = Column(String, nullable=True)
    district_id = Column(String, nullable=True) 
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=True)
    user = relationship("User", back_populates="mp") 

    # relationship to messages sent to this MP — the chat-era
    # `messages` table was sunset; MP-side communication now flows
    # through the case-management domain. The MP profile still
    # references users via `user_id` for routing.


class District(Base):
    __tablename__ = "districts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True)

class LiveFeed(Base):
    __tablename__ = "live_feeds"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=True, index=True)
    journalist_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    district_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    is_active = Column(Boolean, default=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    post = relationship("Post", back_populates="live_feeds")
    journalist = relationship("User")


class LiveFeedMessage(Base):
    __tablename__ = "live_feed_messages"

    id = Column(Integer, primary_key=True, index=True)
    feed_id = Column(Integer, ForeignKey("live_feeds.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    feed = relationship("LiveFeed", backref="messages")
    user = relationship("User")

    
class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    is_active = Column(Boolean, default=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)  
    search_vector = Column(TSVectorType("title", "summary", "content"))
    

    # Relationships
    owner = relationship("User", back_populates="owned_groups")  
    members = relationship("User", secondary="group_members", back_populates="groups")
    posts = relationship("Post", back_populates="group", cascade="all, delete-orphan")



class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(Enum(NotificationType), nullable=False, default=NotificationType.SYSTEM)
    message = Column(String, nullable=False)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User", back_populates="notifications")


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    posts = relationship("Post", secondary=post_categories, back_populates="categories")


class UssdSession(Base):
    __tablename__ = "ussd_sessions"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, nullable=False)
    session_id = Column(String, nullable=True)
    current_step = Column(String, nullable=False)
    user_data = Column(JSON, nullable=True)
    language = Column(String, default='EN')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    
class Article(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    summary = Column(Text, nullable=True)
    content = Column(Text, nullable=True)
    category = Column(String(100), nullable=True)
    image = Column(String(255), nullable=True)
    tags = Column(JSON, default=list)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    read_time = Column(String(50), default="5 min read")
    published_at = Column(DateTime, default=datetime.utcnow)
    search_vector = Column(TSVectorType("title", "summary", "content"))

    is_featured = Column(Boolean, default=False)    
    tsv_document = Column(Text, nullable=True)
    author = relationship(
        "User",
        back_populates="articles",
        lazy="selectin",       
        passive_deletes=True,  
    )


class Topic(Base):
    __tablename__ = "topics"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=True)
    posts = Column(Integer, default=0)
    trending = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    search_vector = Column(TSVectorType("title", "summary", "content"))


class UserType(Base):
    __tablename__ = "user_types"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True)
    monthly_charge = Column(Numeric, default=0)
    is_free = Column(Boolean, default=False)
    description = Column(String, nullable=True)

    users = relationship("User", back_populates="user_type")


class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    plan = Column(String)
    status = Column(String, default="pending")
    start_date = Column(DateTime, default=datetime.utcnow)
    end_date = Column(DateTime)
    amount = Column(Numeric, default=0)
    payment_method = Column(String, nullable=True)

    user = relationship("User", back_populates="subscriptions")


class AdminSetting(Base):
    __tablename__ = "admin_settings"

    id = Column(Integer, primary_key=True, index=True)
    site_name = Column(String, default="Uganda Connects")

    # store role-based permissions using your Role enum keys
    role_permissions = Column(
        JSON,
        nullable=False,
        default=lambda: {
            Role.ADMIN.value: {"post": True, "moderate": True},
            Role.JOURNALIST.value: {"post": True, "moderate": True},
            Role.CITIZEN.value: {"post": True, "moderate": False},
            Role.MP.value: {"post": False, "moderate": True},
        },
    )

    notifications = Column(
        JSON,
        nullable=False,
        default=lambda: {"email": True, "sms": False},
    )

    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self):
        return f"<AdminSetting(site_name={self.site_name})>"


# ============================================================================
# AUTHENTICATION / SESSION TRACKING
# ============================================================================
class Session(Base):
    """
    Persistent record of an active authentication session.

    Each successful login (email or OAuth) creates a `Session` row with a
    `family_id` (UUID). When a refresh token is rotated, the JTI is updated
    on this row. If a revoked token is re-used, the entire family is revoked
    and the row is marked `revoked=True` to defend against token theft.
    """
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    family_id = Column(String(64), nullable=False, unique=True, index=True)
    current_jti = Column(String(64), nullable=False)
    user_agent = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)
    revoked_reason = Column(String, nullable=True)

    user = relationship("User", back_populates="auth_sessions")


# ============================================================================
# WEB PUSH SUBSCRIPTIONS
# ============================================================================
class PushSubscription(Base):
    """
    Per-(user, endpoint) record of a Web Push subscription.

    Each device + browser a user logs in from independently creates one
    `PushSubscription`. The same user may have many subscriptions. When
    the push service returns HTTP 410 (Gone), we delete the row.

    - endpoint is the unique URL the push service gave us
      (https://fcm.googleapis.com/... or https://updates.push.services.mozilla.com/...).
    - p256dh and auth are the client's ECDH public key + shared secret,
      base64url-encoded, used by pywebpush to encrypt the payload so only
      the browser holding the matching private key can decrypt it.
    """
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint = Column(Text, nullable=False, unique=True)
    p256dh = Column(Text, nullable=False)
    auth = Column(Text, nullable=False)
    user_agent = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)


# ============================================================================
# CASE MANAGEMENT DOMAIN
# ============================================================================
# Ten ORM classes that back the new case-management surface. Zero edits
# were made to the existing 22 classes above.
#
# Anonymity invariant (spec STEP 6):
#   The DB MAY keep `reporter_user_id` for moderation + audit. The MP API
#   MUST NOT expose it. Enforcement is in three layers:
#     1. service layer  — services/cases/anonymity.py::build_reporter_view
#     2. Pydantic schema — app/schemas_case.py (next PR wires the router)
#     3. TypeScript union — deferred to the frontend PR
#
# Append-only invariant (audit + timeline):
#   case_audit_log and case_timeline are write-once tables. The
#   `case_append_only()` Postgres trigger (installed by migration
#   c3c4d5e6f7g8) raises on UPDATE or DELETE.
# ----------------------------------------------------------------------------


class CaseCategory(Base):
    """Civic-issue taxonomy (e.g. Health, Education, Roads).

    `name` is the unique natural key; admins CRUD via the future
    `/admin/case-categories` endpoint. Default seeding is OUT of
    scope for this PR — handled by a separate admin command.
    """

    __tablename__ = "case_categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Backrefs populated by Case.category
    cases = relationship("Case", back_populates="category")


class MPRegion(Base):
    """A geographic region that an MP represents.

    The existing `users.district_id` is a free-form string, so MPRegion
    is a NEW canonical region table. Old chat-era `mps` rows are NOT
    backfilled here — that's a separate migration to write once we
    know the canonical Uganda region list from the production front-end.
    """

    __tablename__ = "mp_regions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), unique=True, nullable=False, index=True)
    code = Column(String(32), unique=True, nullable=False)
    # Mirror the free-form district_id convention used elsewhere.
    district_id = Column(String(80), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Backrefs
    profiles = relationship("MPProfile", back_populates="region")


class MPProfile(Base):
    """The richer MP record (spec STEP 1 — replaces nothing).

    Parallel to — NOT a replacement for — the existing chat-era `mps`
    table. A user who is an MP gets at most one MPProfile row. The chat
    path (`mps.user_id`) keeps working unchanged.

    `user_id` is UNIQUE so we enforce the 1:1 constraint at the DB level
    (a user cannot have two MPProfile rows). CASCADE on delete so a
    User delete removes its MPProfile row.
    """

    __tablename__ = "mp_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    # Region the MP represents. Nullable to allow profiles that are
    # not yet assigned a region (admin onboarding flow).
    region_id = Column(
        Integer,
        ForeignKey("mp_regions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    office = Column(String(255), nullable=True)  # e.g. "Kira Municipality"
    photo_url = Column(String(2048), nullable=True)
    bio = Column(Text, nullable=True)
    # Constituency the MP represents. Distinct from `region_id` (the
    # larger administrative region) and from `office` (a free-text
    # description of where the MP holds office). Used by the MP
    # routing engine (services/cases/routing.py) as a tiebreaker when
    # `district_id` matches but multiple MPs share the same district
    # (rare in production but supported in the data model from day 1).
    constituency = Column(String(120), nullable=True, index=True)
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")
    is_accepting_cases = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    user = relationship("User")
    region = relationship("MPRegion", back_populates="profiles")
    cases_assigned = relationship("Case", back_populates="assigned_mp_profile")
    assignments = relationship(
        "CaseAssignment",
        back_populates="mp_profile",
        cascade="all, delete-orphan",
    )


class Case(Base):
    """The case itself — the central entity of the new domain.

    `case_number` is the human-facing identifier (`CIV-2026-000001`).
    Generated atomically by Postgres SEQUENCE `case_number_seq` in
    `services/cases/numbers.py::next_case_number`; concurrent inserts
    cannot collide.

    `reporter_user_id` is NULLABLE on purpose: even fully-anonymous
    cases (where `is_anonymous=True` AND `reporter_user_id IS NULL`)
    are valid. The case row exists, gets a number, gets assigned,
    but no one can ever know who filed it — the API contract for MP
    viewers hides it; admin tooling can decrypt a future encrypted FK
    column if we add one. Spec STEP 6.
    """

    __tablename__ = "cases"

    id = Column(Integer, primary_key=True, index=True)
    # Unique sequential identifier — formatted by the service layer.
    case_number = Column(String(32), unique=True, nullable=False, index=True)
    # NULL allowed for fully-anonymous cases. SET NULL on user delete
    # so a User delete does not cascade-delete the case (cases are
    # retained for the audit window).
    reporter_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Display label the MP / public see. Defaults to "Anonymous Citizen"
    # server-side; citizen may override with a pseudonym at filing time.
    display_handle = Column(String(120), nullable=False, server_default="Anonymous Citizen")
    category_id = Column(
        Integer,
        ForeignKey("case_categories.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    # Mirror users.district_id convention (free-form string).
    district_id = Column(String(80), nullable=True, index=True)
    # Constituency the case is filed under. Optional — populated by
    # the routing engine from the citizen's selected address. Stored
    # so re-routing (e.g. after MP reassignment) is deterministic.
    constituency = Column(String(120), nullable=True, index=True)
    priority = Column(
        Enum("low", "normal", "high", "critical", name="case_priority_enum"),
        nullable=False,
        server_default="normal",
        index=True,
    )
    status = Column(
        Enum(
            "submitted", "received", "assigned", "under_review",
            "information_requested", "citizen_responded",
            "in_progress", "resolved", "closed",
            "withdrawn", "rejected",
            name="case_status_enum",
        ),
        nullable=False,
        server_default="submitted",
        index=True,
    )
    assigned_mp_profile_id = Column(
        Integer,
        ForeignKey("mp_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_anonymous = Column(Boolean, nullable=False, server_default="false")
    language = Column(String(8), nullable=False, server_default="EN")
    # Full-text search vector. Matches the existing
    # `sqlalchemy-searchable` pattern used by Post and Article.
    search_vector = Column(TSVectorType("title", "description"), nullable=True)

    submitted_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    # Composite indexes expressed via __table_args__. Pattern matches
    # the existing `b1c2d3e4f5a6_perf_index_constraints_and_fks.py`
    # migration; kept inline here for clarity.
    __table_args__ = (
        # Core perf index used by the MP inbox query.
        # EXPLAIN PLAN: WHERE status = ? AND priority = ? ORDER BY submitted_at DESC
        UniqueConstraint("case_number", name="uq_cases_case_number"),
    )

    # Relationships
    reporter = relationship("User", foreign_keys=[reporter_user_id])
    category = relationship("CaseCategory", back_populates="cases")
    assigned_mp_profile = relationship(
        "MPProfile",
        foreign_keys=[assigned_mp_profile_id],
        back_populates="cases_assigned",
    )
    attachments = relationship(
        "CaseAttachment",
        back_populates="case",
        cascade="all, delete-orphan",
    )
    responses = relationship(
        "CaseResponse",
        back_populates="case",
        cascade="all, delete-orphan",
    )
    timeline = relationship(
        "CaseTimeline",
        back_populates="case",
        cascade="all, delete-orphan",
    )
    audit_log = relationship(
        "CaseAuditLog",
        back_populates="case",
        cascade="all, delete-orphan",
    )
    assignments = relationship(
        "CaseAssignment",
        back_populates="case",
        cascade="all, delete-orphan",
    )
    # Self-referential through-table for duplicates (CaseSupport).
    supports_received = relationship(
        "CaseSupport",
        foreign_keys="CaseSupport.duplicate_case_id",
        back_populates="duplicate_case",
        cascade="all, delete-orphan",
    )
    supports_given = relationship(
        "CaseSupport",
        foreign_keys="CaseSupport.original_case_id",
        back_populates="original_case",
        cascade="all, delete-orphan",
    )


class CaseAttachment(Base):
    """Evidence attached to a case (image, PDF, voice note).

    Mirrors PostMedia but scoped to a Case. `uploaded_by_id` SET NULL
    on user delete so the evidence survives a user being removed.

    `file_name` is the original filename the user uploaded; `byte_size`
    is the byte length of the decoded binary (the `media_url` column
    stores the binary as a base64 data URL, ~33% larger). `mime_type`
    is the wire MIME header from the upload.
    """

    __tablename__ = "case_attachments"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(
        Integer,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_name = Column(String(255), nullable=False)
    media_url = Column(Text, nullable=False)
    media_type = Column(String(64), nullable=False, server_default="image")
    mime_type = Column(String(128), nullable=False, server_default="application/octet-stream")
    byte_size = Column(Integer, nullable=False, server_default="0")
    sha256 = Column(String(64), nullable=True)
    uploaded_by_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    case = relationship("Case", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])


class CaseResponse(Base):
    """A message in a case's conversation thread.

    `author_role` distinguishes who wrote this: `citizen`, `mp`, `admin`.
    `is_internal=True` marks MP-private notes that the citizen must not
    see. The future case-response API filters `is_internal=False` for
    the citizen view.
    """

    __tablename__ = "case_responses"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(
        Integer,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # "citizen" | "mp" | "admin" — kept as a string column rather than
    # an Enum so future roles (e.g. "observer") don't require a
    # migration.
    author_role = Column(String(32), nullable=False)
    body = Column(Text, nullable=False)
    is_internal = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Composite index supports the case-detail "load timeline" query,
    # which orders responses by created_at within a single case.
    __table_args__ = (
        UniqueConstraint("id", name="uq_case_responses_id"),
    )

    case = relationship("Case", back_populates="responses")
    author = relationship("User", foreign_keys=[author_user_id])


class CaseTimeline(Base):
    """Customer-visible timeline of case events.

    Append-only. Updates/deletes blocked by a Postgres trigger installed
    in migration c3c4d5e6f7g8.
    """

    __tablename__ = "case_timeline"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(
        Integer,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = Column(String(64), nullable=False)
    # The status the case moved FROM. NULL for events that don't change
    # status (e.g. response_added, attachment_added).
    from_status = Column(String(32), nullable=True)
    # The status the case moved TO. NULL for non-status events.
    to_status = Column(String(32), nullable=True)
    # "citizen" | "mp" | "admin" | "system" — matches the audit-log
    # vocabulary so a single query can join both tables by this column.
    actor_role = Column(String(32), nullable=False)
    actor_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    case = relationship("Case", back_populates="timeline")
    actor = relationship("User", foreign_keys=[actor_user_id])


class CaseAuditLog(Base):
    """Append-only security audit trail for case actions.

    Captures everything CaseTimeline captures PLUS a longer list of
    security-relevant events (CASE_VIEWED, EVIDENCE_EXPORTED,
    REPORTER_DECRYPTED). `request_id` is the X-Request-Id from the
    originating HTTP request, set by RequestIdMiddleware in
    `app/core/middleware.py`. `payload` is a JSON column that captures
    event-specific detail (the diff for STATUS_CHANGED, the action for
    admin tooling, etc.).

    Append-only. Updates/deletes blocked by a Postgres trigger installed
    in migration c3c4d5e6f7g8.
    """

    __tablename__ = "case_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(
        Integer,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action = Column(String(64), nullable=False)
    actor_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_role = Column(String(32), nullable=False)
    request_id = Column(String(64), nullable=True, index=True)
    ip_address = Column(String(64), nullable=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    case = relationship("Case", back_populates="audit_log")
    actor = relationship("User", foreign_keys=[actor_user_id])


class CaseAssignment(Base):
    """Which MP is currently assigned to a case.

    A case can have MANY CaseAssignment rows over its lifetime
    (reassignment history). The partial unique index at the DB level
    enforces "at most one active row per case" — see migration
    c3c4d5e6f7g8.
    """

    __tablename__ = "case_assignments"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(
        Integer,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    mp_profile_id = Column(
        Integer,
        ForeignKey("mp_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assigned_at = Column(DateTime(timezone=True), server_default=func.now())
    assigned_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    unassigned_at = Column(DateTime(timezone=True), nullable=True)
    # Partial unique index declared in the migration (c3c4d5e6f7g8):
    # CREATE UNIQUE INDEX uq_case_assignments_active
    #   ON case_assignments (case_id) WHERE unassigned_at IS NULL;

    # Relationships
    case = relationship("Case", back_populates="assignments")
    mp_profile = relationship("MPProfile", back_populates="assignments")
    assigned_by = relationship("User", foreign_keys=[assigned_by_user_id])


class CaseSupport(Base):
    """Duplicate case support record.

    When a citizen filing a new case is shown "Similar cases", they can
    choose to support one of the existing cases instead of creating a
    new one. A CaseSupport row encodes that choice.

    Both `original_case_id` (the existing case) and `duplicate_case_id`
    (the new case the citizen was about to file) are stored even
    though `duplicate_case_id` is typically NULL — when the citizen
    supports an existing case we don't actually persist their would-be
    case, but if they did start one and then backed out, we'd record
    the link. For now, we accept `duplicate_case_id` is nullable to
    keep the row useful in both flows.

    `supporter_user_id` is SET NULL on user delete so that if a user
    later leaves the platform, the duplicate count remains accurate.
    """

    __tablename__ = "case_support"

    id = Column(Integer, primary_key=True, index=True)
    original_case_id = Column(
        Integer,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    duplicate_case_id = Column(
        Integer,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    supporter_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Partial unique index declared in the migration:
    # CREATE UNIQUE INDEX uq_case_support_pair
    #   ON case_support (original_case_id, duplicate_case_id)
    #   WHERE supporter_user_id IS NOT NULL;

    # Relationships — see Case model for the corresponding back_populates.
    original_case = relationship(
        "Case",
        foreign_keys=[original_case_id],
        back_populates="supports_given",
    )
    duplicate_case = relationship(
        "Case",
        foreign_keys=[duplicate_case_id],
        back_populates="supports_received",
    )
    supporter = relationship("User", foreign_keys=[supporter_user_id])