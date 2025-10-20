from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List
from datetime import datetime
import enum


# User Schemas
class Role(str, enum.Enum):
    ADMIN = "admin"
    JOURNALIST = "journalist"
    CITIZEN = "citizen"
    MP = "mp"


class UserBase(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    role: Role = Role.CITIZEN
    region: Optional[str] = None
    district_id: Optional[str] = None
    county_id: Optional[str] = None
    sub_county_id: Optional[str] = None
    parish_id: Optional[str] = None
    village_id: Optional[str] = None
    occupation: Optional[str] = None
    bio: Optional[str] = None
    profile_image: Optional[str] = None
    political_interest: Optional[str] = None
    community_role: Optional[str] = None
    interests: List[str] = Field(default_factory=list)
    privacy_level: str = "public"


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


class UserOut(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: EmailStr
    profile_image: Optional[str] = None
    role: Role

    model_config = {"from_attributes": True}



# Auth Schemas
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    email: Optional[str] = None



# Post & Media Schemas
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

class CommentCreate(BaseModel):
    content: str
    post_id: int
    parent_id: Optional[int] = None  # for nested replies

# Forward declare CommentResponse for nesting
class CommentResponse(BaseModel):
    id: int
    content: str
    author: UserOut
    parent_id: Optional[int]
    created_at: datetime
    updated_at: Optional[datetime]
    replies: List["CommentResponse"] = []  # nested replies

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


class PostResponse(BaseModel):
    id: int
    title: str
    content: str
    media: List[PostMediaOut] = []
    author_id: int
    district_id: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime]
    like_count: int
    comments: List[CommentResponse] = []  

    model_config = {"from_attributes": True}


CommentResponse.model_rebuild()  # Allow recursive nesting



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

class SearchResponse(BaseModel):
    users: List[UserOut] = []
    posts: List[PostResponse] = []
    comments: List[CommentResponse] = []

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
    sub_county_id: str | None
    parish_id: str | None
    village_id: str | None
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
    sub_county_id: str | None
    parish_id: str | None
    village_id: str | None
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
