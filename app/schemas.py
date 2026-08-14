import enum
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, computed_field, field_validator


# User Schemas
class Role(enum.StrEnum):
    ADMIN = "admin"
    JOURNALIST = "journalist"
    CITIZEN = "citizen"
    MP = "mp"



# USER SCHEMAS
# Naming convention for max-length caps (additive only — already-stored
# values longer than the cap still load fine; only new writes are rejected).
#
# These limits protect the API from pathological inputs (5 MB bios, etc.)
# without changing the database column widths. They do NOT cap the rows
# that the existing frontend may have already written.
#
# We size each cap at the same width as the corresponding DB column
# (SQLAlchemy `String` default = 255 unless a length is given). This keeps
# `from_attributes` reads safe even for rows that pre-date these caps.

_NAME_MAX = 255                # String default width
_USERNAME_MAX = 50             # users.username is String(50)
_REGION_MAX = 255              # users.region is String
_LOCATION_ID_MAX = 80          # district_id / county_id are stored as short codes
_OCCUPATION_MAX = 255          # String
_BIO_MAX = 10_000              # bio is Text, but cap practical API input
_PROFILE_IMAGE_MAX = 2048      # URL string
_POLITICAL_INTEREST_MAX = 255  # String
_COMMUNITY_ROLE_MAX = 255      # String
_PRIVACY_MAX = 32              # enum-shaped string
_PASSWORD_MAX = 128            # bcrypt is capped at 72 anyway


class UserBase(BaseModel):
    first_name: str = Field(..., max_length=_NAME_MAX)
    last_name: str = Field(..., max_length=_NAME_MAX)
    email: EmailStr
    username: str = Field(..., max_length=_USERNAME_MAX)
    role: Role = Role.CITIZEN
    region: str | None = Field(default=None, max_length=_REGION_MAX)
    district_id: str | None = Field(default=None, max_length=_LOCATION_ID_MAX)
    county_id: str | None = Field(default=None, max_length=_LOCATION_ID_MAX)
    occupation: str | None = Field(default=None, max_length=_OCCUPATION_MAX)
    bio: str | None = Field(default=None, max_length=_BIO_MAX)
    profile_image: str | None = Field(default=None, max_length=_PROFILE_IMAGE_MAX)
    political_interest: str | None = Field(default=None, max_length=_POLITICAL_INTEREST_MAX)
    community_role: str | None = Field(default=None, max_length=_COMMUNITY_ROLE_MAX)
    interests: list[str] = Field(default_factory=list, max_length=50)
    privacy_level: str = Field(default="public", max_length=_PRIVACY_MAX)

    # populate_by_name: accept either Python name or alias; lets us add
    # snake_case ↔ camelCase aliases in future without breaking callers.
    # str_strip_whitespace: trim leading/trailing whitespace on every
    # incoming string field so the DB never stores "  John  ".
    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }


class UserCreate(UserBase):
    password: str = Field(..., max_length=_PASSWORD_MAX)
    confirm_password: str = Field(..., max_length=_PASSWORD_MAX)

    @field_validator("confirm_password")
    def passwords_match(cls, v, info):
        password = info.data.get("password")
        if password and v != password:
            raise ValueError("Passwords do not match")
        return v


class User(UserBase):
    id: int
    is_active: bool

    model_config = {"from_attributes": True}


# 👇 Secure, public-facing version (email omitted automatically)
class UserPublic(BaseModel):
    id: int
    first_name: str
    last_name: str
    username: str
    role: str | None = None
    profile_image: str | None = None
    district_id: str | None = None
    county_id: str | None = None
    occupation: str | None = None
    bio: str | None = None
    political_interest: str | None = None
    community_role: str | None = None
    interests: list[str] = Field(default_factory=list)
    region: str | None = None
    verified: bool = False
    followers_count: int | None = 0

    model_config = {"from_attributes": True}


# You can still keep this if some internal endpoints require emails
class UserOut(UserPublic):
    email: EmailStr | None = None



# POST & MEDIA SCHEMAS
class PostMediaOut(BaseModel):
    id: int
    media_url: str
    media_type: str

    model_config = {"from_attributes": True}


_POST_TITLE_MAX = 255          # matches default String column width
_POST_CONTENT_MAX = 50_000      # well below Text column (~1GB) but bounds API inputs
_MEDIA_URL_MAX = 2048

class PostCreate(BaseModel):
    title: str = Field(..., max_length=_POST_TITLE_MAX)
    content: str = Field(..., max_length=_POST_CONTENT_MAX)
    media_url: list[str] = Field(default_factory=list, max_length=10)
    district_id: str | None = Field(default=None, max_length=_LOCATION_ID_MAX)

    @field_validator("media_url")
    @classmethod
    def _cap_each_media_url(cls, v: list[str]) -> list[str]:
        return [u[:_MEDIA_URL_MAX] for u in v]

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }



# COMMENT SCHEMAS
_COMMENT_CONTENT_MAX = 5000

class CommentCreate(BaseModel):
    content: str = Field(..., max_length=_COMMENT_CONTENT_MAX)
    parent_id: int | None = None

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }


class CommentResponse(BaseModel):
    id: int
    content: str
    author: UserPublic  
    parent_id: int | None
    created_at: datetime
    updated_at: datetime | None
    replies: list["CommentResponse"] = Field(default_factory=list)
    
    model_config = {"from_attributes": True}


class CommentUpdate(BaseModel):
    content: str | None = Field(default=None, max_length=_COMMENT_CONTENT_MAX)


class Pagination(BaseModel):
    page: int
    size: int
    total: int
    pages: int


class CommentListResponse(BaseModel):
    data: list[CommentResponse]
    pagination: Pagination


CommentResponse.model_rebuild()  # allows recursive replies



# POST RESPONSE SCHEMA
class PostResponse(BaseModel):
    id: int
    title: str
    content: str
    media: list[PostMediaOut] = []
    author: UserPublic  # 👈 use the safe, public version
    district_id: str | None
    created_at: datetime
    updated_at: datetime | None
    like_count: int
    comments: list[CommentResponse] = []
    share_count: int | None = 0

    model_config = {"from_attributes": True}



# Auth Schemas
class Token(BaseModel):
    """Standard token response shape returned by every successful authentication endpoint.

    NOTE: as of F-008, the JSON body of authentication responses NO
    LONGER carries the access or refresh token. Tokens are issued as
    HttpOnly cookies. The fields are kept on this model for backward
    compatibility with test fixtures and any code path that still
    expects the legacy shape — new code should use
    :class:`AuthSessionResponse` and read tokens from the cookies set
    on the response.
    """
    access_token: str = ""
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int = 0
    user: "UserOut"


class AuthSessionResponse(BaseModel):
    """Response body for the new cookie-based auth flow (F-008).

    Tokens are NEVER returned in the body — the browser receives them
    as HttpOnly cookies on the response. The body only contains safe
    public data: the user profile and the access-token lifetime (so
    the front-end can schedule UI hints if it wants to).
    """
    user: "UserOut"
    expires_in: int


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class RevokeSessionRequest(BaseModel):
    """Optional metadata attached when revoking a session (e.g. logging out another device)."""
    session_id: str | None = None


class OAuthBootstrapExchangeRequest(BaseModel):
    """Body of `POST /auth/oauth/exchange`."""
    code: str


class TokenData(BaseModel):
    email: str | None = None
    type: str | None = None
    family: str | None = None
    jti: str | None = None
    scope: str | None = None


class AuthError(BaseModel):
    """Standard error envelope returned by /auth/* endpoints."""
    code: str
    message: str
    hint: str | None = None



# LiveFeed Schemas
_LIVE_FEED_CONTENT_MAX = 5000

class LiveFeedCreate(BaseModel):
    content: str = Field(..., max_length=_LIVE_FEED_CONTENT_MAX)
    district_id: str | None = Field(default=None, max_length=_LOCATION_ID_MAX)

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }


class LiveFeedResponse(BaseModel):
    id: int
    content: str
    journalist: UserOut
    post: PostResponse | None
    district_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}

class LiveFeedMessageUser(BaseModel):
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    profile_image: str | None = None

    class Config:
        model_config = {"from_attributes": True}  # noqa: RUF012 — Pydantic v1 style

class LiveFeedMessageResponse(BaseModel):
    id: int
    feed_id: int
    user: LiveFeedMessageUser | None = None
    message: str
    created_at: datetime

    class Config:
        model_config = {"from_attributes": True}  # noqa: RUF012 — Pydantic v1 style



class LiveFeedMessagesList(BaseModel):
    data: list[LiveFeedMessageResponse]
    total: int
    skip: int
    limit: int


# Category & Group Schemas
_CATEGORY_NAME_MAX = 80
_GROUP_NAME_MAX = 120
_GROUP_DESCRIPTION_MAX = 2000

class CategoryBase(BaseModel):
    name: str = Field(..., max_length=_CATEGORY_NAME_MAX)

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }


class CategoryCreate(CategoryBase):
    pass


class CategoryResponse(CategoryBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class GroupBase(BaseModel):
    name: str = Field(..., max_length=_GROUP_NAME_MAX)
    description: str | None = Field(default=None, max_length=_GROUP_DESCRIPTION_MAX)

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }


class GroupCreate(GroupBase):
    pass


class GroupResponse(GroupBase):
    id: int
    created_at: datetime
    owner: UserOut
    member_count: int
    # Optional nested members if you want:
    # members: List[UserOut] = []

    model_config = {"from_attributes": True}



# Notification Schemas

class NotificationType(enum.StrEnum):
    COMMENT = "COMMENT"
    VOTE = "VOTE"
    GROUP = "GROUP"
    SYSTEM = "SYSTEM"


class NotificationBase(BaseModel):
    type: NotificationType
    message: str
    post_id: int | None = None
    group_id: int | None = None


class NotificationResponse(NotificationBase):
    id: int
    user: UserOut
    is_read: bool
    created_at: datetime
    # Optional nested objects
# (Moved above)




class NotificationListResponse(BaseModel):
    data: list[NotificationResponse]
    pagination: Pagination



# Sharing Schemas
class ShareRequest(BaseModel):
    recipient_ids: list[int] | None = None  # In-app sharing
    group_id: int | None = None
    platform: str | None = None  # e.g., "twitter", "whatsapp"



# Vote Schemas
class Vote(BaseModel):
    post_id: int
    vote_type: str  # e.g., "upvote" or "downvote"

class VoteResponse(BaseModel):
    id: int
    post_id: int
    user_id: int
    vote_type: str

    model_config = {"from_attributes": True}




class USSDRequest(BaseModel):
    sessionId: str
    serviceCode: str
    phoneNumber: str
    text: str

class USSDResponse(BaseModel):
    response: str


class UserUpdate(BaseModel):
    first_name: str | None = Field(default=None, max_length=_NAME_MAX)
    last_name: str | None = Field(default=None, max_length=_NAME_MAX)
    email: EmailStr | None = None
    region: str | None = Field(default=None, max_length=_REGION_MAX)
    district_id: str | None = Field(default=None, max_length=_LOCATION_ID_MAX)
    county_id: str | None = Field(default=None, max_length=_LOCATION_ID_MAX)
    occupation: str | None = Field(default=None, max_length=_OCCUPATION_MAX)
    bio: str | None = Field(default=None, max_length=_BIO_MAX)
    political_interest: str | None = Field(default=None, max_length=_POLITICAL_INTEREST_MAX)
    community_role: str | None = Field(default=None, max_length=_COMMUNITY_ROLE_MAX)
    interests: list[str] | None = None
    notifications: dict | None = None
    privacy_level: str | None = Field(default=None, max_length=_PRIVACY_MAX)

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }


class UserResponse(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: str
    role: str
    region: str | None
    district_id: str | None
    county_id: str | None
    occupation: str | None
    bio: str | None
    profile_image: str | None
    political_interest: str | None
    community_role: str | None
    interests: list | None
    notifications: dict | None
    privacy_level: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class ResetPasswordSchema(BaseModel):
    token: str
    new_password: str

class Location(BaseModel):
    id: str
    name: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

# Author Schemas 
class AuthorOut(BaseModel):
    id: int
    username: str
    first_name: str | None = None
    last_name: str | None = None
    profile_image: str | None = None

    @computed_field
    @property
    def name(self) -> str:
        if self.first_name or self.last_name:
            return f"{self.first_name or ''} {self.last_name or ''}".strip()
        return self.username

    class Config:
        from_attributes = True


#  Article Schemas
_ARTICLE_TITLE_MAX = 200
_ARTICLE_SUMMARY_MAX = 1000
_ARTICLE_CONTENT_MAX = 50_000
_ARTICLE_CATEGORY_MAX = 80
_ARTICLE_IMAGE_MAX = 2048
_ARTICLE_TAG_MAX = 50
_ARTICLE_TAGS_MAX_ITEMS = 20
_ARTICLE_READ_TIME_MAX = 32

class ArticleBase(BaseModel):
    title: str = Field(..., max_length=_ARTICLE_TITLE_MAX)
    summary: str | None = Field(default=None, max_length=_ARTICLE_SUMMARY_MAX)
    content: str | None = Field(default=None, max_length=_ARTICLE_CONTENT_MAX)
    category: str | None = Field(default=None, max_length=_ARTICLE_CATEGORY_MAX)
    image: str | None = Field(default=None, max_length=_ARTICLE_IMAGE_MAX)
    tags: list[str] = Field(default_factory=list, max_length=_ARTICLE_TAGS_MAX_ITEMS)
    read_time: str | None = Field(default="2 min read", max_length=_ARTICLE_READ_TIME_MAX)
    is_featured: bool | None = False

    @field_validator("tags")
    @classmethod
    def _cap_each_tag(cls, v: list[str]) -> list[str]:
        return [t[:_ARTICLE_TAG_MAX] for t in v]

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }


class ArticleCreate(ArticleBase):
    author_id: int


class ArticleUpdate(ArticleBase):
    pass


class ArticleOut(ArticleBase):
    id: int
    author: AuthorOut | None
    published_at: datetime

    class Config:
        from_attributes = True


class SearchResponse(BaseModel):
    users: list[UserOut] = []
    posts: list[PostResponse] = []
    comments: list[CommentResponse] = []
    articles: list[ArticleOut] = []

    model_config = {"from_attributes": True}


class SearchItem(BaseModel):
    id: int
    type: str                  
    title: str | None = None  
    name: str | None = None   
    snippet: str | None = None  
    image: str | None = None
    category: str | None = None

    model_config = {"from_attributes": True}

# Topic Schemas
_TOPIC_TITLE_MAX = 200
_TOPIC_DESCRIPTION_MAX = 1000
_TOPIC_CATEGORY_MAX = 80

class TopicBase(BaseModel):
    title: str = Field(..., max_length=_TOPIC_TITLE_MAX)
    description: str | None = Field(default=None, max_length=_TOPIC_DESCRIPTION_MAX)
    category: str | None = Field(default=None, max_length=_TOPIC_CATEGORY_MAX)

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }

class TopicCreate(TopicBase):
    pass

class TopicUpdate(TopicBase):
    pass

class TopicOut(TopicBase):
    id: int
    posts: int | None = 0
    trending: bool | None = False
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class MutualInterestsResponse(BaseModel):
    user_id: int
    username: str
    mutual_interests: list[str]


_EVENT_TITLE_MAX = 200
_EVENT_DESCRIPTION_MAX = 5000
_EVENT_LOCATION_MAX = 300
_EVENT_CATEGORY_MAX = 80
_EVENT_TIME_MAX = 32

class EventBase(BaseModel):
    title: str = Field(..., max_length=_EVENT_TITLE_MAX)
    description: str | None = Field(default=None, max_length=_EVENT_DESCRIPTION_MAX)
    date: date
    time: str | None = Field(default=None, max_length=_EVENT_TIME_MAX)
    location: str = Field(..., max_length=_EVENT_LOCATION_MAX)
    category: str | None = Field(default=None, max_length=_EVENT_CATEGORY_MAX)

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }


class EventCreate(EventBase):
    pass


class OrganizerPublic(BaseModel):
    id: int
    first_name: str
    last_name: str
    username: str
    profile_image: str | None = None

    model_config = {"from_attributes": True}

class AttendeePublic(OrganizerPublic):
    pass

class EventPublic(EventBase):
    id: int
    attendees: int = 0
    organizer: OrganizerPublic | None

    model_config = {"from_attributes": True}


_USER_TYPE_NAME_MAX = 80
_USER_TYPE_DESCRIPTION_MAX = 500

class UserTypeBase(BaseModel):
    name: str = Field(..., max_length=_USER_TYPE_NAME_MAX)
    monthlyCharge: float
    isFree: bool
    description: str | None = Field(default=None, max_length=_USER_TYPE_DESCRIPTION_MAX)

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }

class UserTypeCreate(UserTypeBase):
    pass

class UserTypeUpdate(UserTypeBase):
    pass


class RolePermission(BaseModel):
    post: bool
    moderate: bool

class NotificationSettings(BaseModel):
    email: bool
    sms: bool

class AdminSettingBase(BaseModel):
    siteName: str = Field(..., alias="site_name")
    rolePermissions: dict[Role, RolePermission]
    notifications: NotificationSettings

    class Config:
        populate_by_name = True
        use_enum_values = True
        model_config = {"from_attributes": True}  # noqa: RUF012 — Pydantic v1 Config style


class AdminSettingOut(AdminSettingBase):
    id: int
    updated_at: str | None


class AdminSettingUpdate(AdminSettingBase):
    pass


def default_admin_setting() -> AdminSettingOut:
    return AdminSettingOut(
        id=1,
        siteName="Uganda Connects",
        rolePermissions={
            Role.ADMIN: {"post": True, "moderate": True},
            Role.JOURNALIST: {"post": True, "moderate": True},
            Role.CITIZEN: {"post": True, "moderate": False},
            Role.MP: {"post": False, "moderate": True},
        },
        notifications={"email": True, "sms": False},
    )


# Web Push Schemas
# -----------------
# These models describe the JSON payloads exchanged with the PWA's
# `PushManager.subscribe()` flow. Kept narrow — we only accept the
# fields we actually persist, and the frontend is the only caller.


class PushSubscriptionKeys(BaseModel):
    """The `keys` block of a `PushSubscriptionJSON`."""
    p256dh: str = Field(..., min_length=1)
    auth: str = Field(..., min_length=1)


class PushSubscriptionIn(BaseModel):
    """Request body for POST /push/subscribe and DELETE /push/subscribe."""
    endpoint: str = Field(..., min_length=1, max_length=2048)
    keys: PushSubscriptionKeys


class PushSubscriptionOut(BaseModel):
    """Response confirming a successful (un)subscription."""
    id: int
    endpoint: str
    created_at: datetime

    model_config = {"from_attributes": True}


class VapidPublicKey(BaseModel):
    """Response for GET /push/vapid-public-key. The frontend uses this
    to encrypt push payloads to the backend's VAPID identifier."""
    key: str


class PushTestResult(BaseModel):
    """Response for POST /push/test. Reports what happened on the fan-out."""
    sent: int
    failed: int
    pruned: int
    total: int