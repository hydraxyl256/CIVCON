from pydantic import BaseModel, EmailStr, Field, field_validator, computed_field
from typing import Optional, List
from datetime import datetime
import enum


# User Schemas
class Role(str, enum.Enum):
    ADMIN = "admin"
    JOURNALIST = "journalist"
    CITIZEN = "citizen"
    MP = "mp"



# USER SCHEMAS
class UserBase(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    username: str
    role: Role = Role.CITIZEN
    region: Optional[str] = None
    district_id: Optional[str] = None
    county_id: Optional[str] = None
    occupation: Optional[str] = None
    bio: Optional[str] = None
    profile_image: Optional[str] = None
    political_interest: Optional[str] = None
    community_role: Optional[str] = None
    interests: List[str] = Field(default_factory=list)
    privacy_level: str = "public"

    model_config = {"from_attributes": True}


class UserCreate(UserBase):
    password: str
    confirm_password: str

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
    role: Optional[str] = None
    profile_image: Optional[str] = None
    district_id: Optional[str] = None
    county_id: Optional[str] = None
    occupation: Optional[str] = None
    bio: Optional[str] = None
    political_interest: Optional[str] = None
    community_role: Optional[str] = None
    interests: List[str] = []
    region: Optional[str] = None

    model_config = {"from_attributes": True}


# You can still keep this if some internal endpoints require emails
class UserOut(UserPublic):
    email: Optional[EmailStr] = None



# POST & MEDIA SCHEMAS
class PostMediaOut(BaseModel):
    id: int
    media_url: str
    media_type: str

    model_config = {"from_attributes": True}


class PostCreate(BaseModel):
    title: str
    content: str
    media_url: List[str] = Field(default_factory=list)
    district_id: Optional[str] = None



# COMMENT SCHEMAS
class CommentCreate(BaseModel):
    content: str
    parent_id: Optional[int] = None


class CommentResponse(BaseModel):
    id: int
    content: str
    author: UserPublic  
    parent_id: Optional[int]
    created_at: datetime
    updated_at: Optional[datetime]
    replies: List["CommentResponse"] = Field(default_factory=list)
    
    model_config = {"from_attributes": True}


class CommentUpdate(BaseModel):
    content: Optional[str] = None


class Pagination(BaseModel):
    page: int
    size: int
    total: int
    pages: int


class CommentListResponse(BaseModel):
    data: List[CommentResponse]
    pagination: Pagination


CommentResponse.model_rebuild()  # allows recursive replies



# POST RESPONSE SCHEMA
class PostResponse(BaseModel):
    id: int
    title: str
    content: str
    media: List[PostMediaOut] = []
    author: UserPublic  # 👈 use the safe, public version
    district_id: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime]
    like_count: int
    comments: List[CommentResponse] = []
    share_count: Optional[int] = 0

    model_config = {"from_attributes": True}



# Auth Schemas
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    email: Optional[str] = None



# LiveFeed Schemas
class LiveFeedCreate(BaseModel):
    content: str
    district_id: Optional[str] = None


class LiveFeedResponse(BaseModel):
    id: int
    content: str
    journalist: UserOut
    post: Optional[PostResponse]
    district_id: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}

class LiveFeedMessageUser(BaseModel):
    id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    profile_image: Optional[str] = None

    class Config:
        model_config = {"from_attributes": True}

class LiveFeedMessageResponse(BaseModel):
    id: int
    feed_id: int
    user: Optional[LiveFeedMessageUser] = None
    message: str
    created_at: datetime

    class Config:
        model_config = {"from_attributes": True}



class LiveFeedMessagesList(BaseModel):
    data: list[LiveFeedMessageResponse]
    total: int
    skip: int
    limit: int


# Category & Group Schemas
class CategoryBase(BaseModel):
    name: str


class CategoryCreate(CategoryBase):
    pass


class CategoryResponse(CategoryBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class GroupBase(BaseModel):
    name: str
    description: Optional[str] = None


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

class NotificationType(str, enum.Enum):
    COMMENT = "COMMENT"
    VOTE = "VOTE"
    MESSAGE = "MESSAGE"
    GROUP = "GROUP"
    SYSTEM = "SYSTEM"


class NotificationBase(BaseModel):
    type: NotificationType
    message: str
    post_id: Optional[int] = None
    group_id: Optional[int] = None


class NotificationResponse(NotificationBase):
    id: int
    user: UserOut
    is_read: bool
    created_at: datetime
    # Optional nested objects
# (Moved above)




class NotificationListResponse(BaseModel):
    data: List[NotificationResponse]
    pagination: Pagination



# Sharing Schemas
class ShareRequest(BaseModel):
    recipient_ids: Optional[List[int]] = None  # In-app sharing
    group_id: Optional[int] = None
    platform: Optional[str] = None  # e.g., "twitter", "whatsapp"



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



class MessageBase(BaseModel):
    content: str


class MessageCreate(BaseModel):
    recipient_id: Optional[int] = None  
    content: str

class MessageResponse(MessageBase):
    id: int
    sender_id: int
    recipient_id: int
    district_id: Optional[str] = None
    created_at: datetime
    is_read: bool  
    sender: UserOut
    recipient: UserOut


    class Config:
        from_attributes = True


class USSDRequest(BaseModel):
    sessionId: str
    serviceCode: str
    phoneNumber: str
    text: str

class USSDResponse(BaseModel):
    response: str


class UserUpdate(BaseModel):
    first_name: str | None
    last_name: str | None
    email: EmailStr | None
    region: str | None
    district_id: str | None
    county_id: str | None
    occupation: str | None
    bio: str | None
    political_interest: str | None
    community_role: str | None
    interests: list[str] | None
    notifications: dict | None
    privacy_level: str | None


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
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    profile_image: Optional[str] = None

    @computed_field
    @property
    def name(self) -> str:
        if self.first_name or self.last_name:
            return f"{self.first_name or ''} {self.last_name or ''}".strip()
        return self.username

    class Config:
        from_attributes = True


#  Article Schemas 
class ArticleBase(BaseModel):
    title: str
    summary: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    image: Optional[str] = None
    tags: List[str] = []
    read_time: Optional[str] = "2 min read"
    is_featured: Optional[bool] = False


class ArticleCreate(ArticleBase):
    author_id: int


class ArticleUpdate(ArticleBase):
    pass


class ArticleOut(ArticleBase):
    id: int
    author: Optional[AuthorOut]
    published_at: datetime

    class Config:
        from_attributes = True


class SearchResponse(BaseModel):
    users: List[UserOut] = []
    posts: List[PostResponse] = []
    comments: List[CommentResponse] = []
    articles: List[ArticleOut] = []

    model_config = {"from_attributes": True}