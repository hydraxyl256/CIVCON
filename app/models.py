from sqlalchemy import (
    Column,
    Integer,
    Float,
    String,
    Boolean,
    DateTime,
    JSON,
    Text,
    ForeignKey,
    Enum,
    TIMESTAMP,
    Table,
    UniqueConstraint,
    select,
    Date,
)
from sqlalchemy.sql import func, text
from sqlalchemy.orm import relationship, column_property
from sqlalchemy_searchable import TSVectorType
from app.database import Base
import enum
from datetime import datetime




# ENUMS
class Role(str, enum.Enum):
    CITIZEN = "citizen"
    MP = "mp"
    JOURNALIST = "journalist"
    ADMIN = "admin"


class NotificationType(str, enum.Enum):
    COMMENT = "COMMENT"
    VOTE = "VOTE"
    MESSAGE = "MESSAGE"
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

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    posts = relationship("Post", back_populates="author", cascade="all, delete-orphan")
    comments = relationship("Comment", back_populates="author", cascade="all, delete-orphan")
    votes = relationship("Vote", back_populates="user", cascade="all, delete-orphan")
    messages_sent = relationship("Message", foreign_keys="Message.sender_id", back_populates="sender")
    messages_received = relationship("Message", foreign_keys="Message.recipient_id", back_populates="recipient")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    groups = relationship("Group", secondary=group_members, back_populates="members")
    owned_groups = relationship("Group", back_populates="owner")
    mp = relationship("MP", back_populates="user", uselist=False)
    articles = relationship("Article", back_populates="author")

    # 👥 Relationships with followers/following
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
    media = relationship("PostMedia", back_populates="post", cascade="all, delete-orphan")
    share_count = Column(Integer, default=0)


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

    # relationship to messages sent to this MP
    messages = relationship("Message", back_populates="mp")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    recipient_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    district_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    response = Column(String, nullable=True)  # MP reply
    responded_at = Column(DateTime, nullable=True)
    language = Column(String, nullable=True)
    mp_id = Column(Integer, ForeignKey("mps.id"), nullable=False)
    mp = relationship("MP", back_populates="messages")

    # Relationships
    sender = relationship("User", foreign_keys=[sender_id], back_populates="messages_sent")
    recipient = relationship("User", foreign_keys=[recipient_id], back_populates="messages_received")


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

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    plan_name = Column(String, nullable=False)
    amount = Column(Float, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)