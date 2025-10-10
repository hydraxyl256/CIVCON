from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, insert
from sqlalchemy.orm import selectinload
from typing import List
from fastapi.responses import JSONResponse
from .. import models, schemas
from ..routers import oauth2
from ..database import get_db
from ..services.notifications import create_and_send_notification

router = APIRouter(
    prefix="/groups",
    tags=["Groups"]
)


async def get_db_user(db: AsyncSession, user_id: int):
    query = select(models.User).where(models.User.id == user_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.post("/", response_model=schemas.GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    group: schemas.GroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: schemas.UserOut = Depends(oauth2.get_current_user),
):
    # Ensure unique group name
    result = await db.execute(select(models.Group).where(models.Group.name == group.name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group name already exists")

    db_group = models.Group(name=group.name, description=group.description, owner_id=current_user.id)
    db.add(db_group)
    await db.commit()
    await db.refresh(db_group)

    # Add creator as member
    await db.execute(insert(models.group_members).values(group_id=db_group.id, user_id=current_user.id))
    await db.commit()

    # Reload group with members
    group_result = await db.execute(
        select(models.Group)
        .where(models.Group.id == db_group.id)
        .options(selectinload(models.Group.owner), selectinload(models.Group.members))
    )
    db_group = group_result.scalar_one_or_none()
    return {**db_group.__dict__, "member_count": len(db_group.members)}


@router.get("/", response_model=List[schemas.GroupResponse])
async def list_groups(db: AsyncSession = Depends(get_db)):
    query = select(models.Group).options(selectinload(models.Group.owner), selectinload(models.Group.members))
    result = await db.execute(query)
    groups = result.scalars().all()
    return [{**g.__dict__, "member_count": len(g.members)} for g in groups]


@router.post("/{group_id}/join", response_model=schemas.GroupResponse)
async def join_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: schemas.UserOut = Depends(oauth2.get_current_user),
):
    group_query = select(models.Group).where(models.Group.id == group_id).options(selectinload(models.Group.members))
    group_result = await db.execute(group_query)
    group = group_result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    # Check membership
    member_check = await db.execute(
        select(models.group_members).where(models.group_members.c.group_id == group_id,
                                           models.group_members.c.user_id == current_user.id)
    )
    if member_check.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Already a member")

    await db.execute(insert(models.group_members).values(group_id=group_id, user_id=current_user.id))
    await db.commit()

    # Reload group with updated members
    group_result = await db.execute(
        select(models.Group).where(models.Group.id == group_id).options(selectinload(models.Group.members))
    )
    group = group_result.scalar_one_or_none()
    return {**group.__dict__, "member_count": len(group.members)}


@router.get("/{group_id}/posts")
async def get_group_posts(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 10
):
    group_result = await db.execute(select(models.Group).where(models.Group.id == group_id))
    if not group_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    total_query = select(func.count()).select_from(models.Post).where(models.Post.group_id == group_id)
    total_result = await db.execute(total_query)
    total_count = total_result.scalar()

    posts_query = (
        select(models.Post)
        .where(models.Post.group_id == group_id)
        .offset(skip)
        .limit(limit)
        .options(selectinload(models.Post.author))
    )
    posts_result = await db.execute(posts_query)
    posts = posts_result.scalars().all()

    data = []
    for post in posts:
        like_count = (await db.execute(select(func.count()).select_from(models.Vote).where(models.Vote.post_id == post.id))).scalar()
        comment_count = (await db.execute(select(func.count()).select_from(models.Comment).where(models.Comment.post_id == post.id))).scalar()
        data.append({
            "post": post,
            "like_count": like_count,
            "comment_count": comment_count
        })

    next_url = f"/groups/{group_id}/posts?limit={limit}&skip={skip + limit}" if skip + limit < total_count else None
    prev_url = f"/groups/{group_id}/posts?limit={limit}&skip={skip - limit}" if skip > 0 else None

    return JSONResponse({
        "data": data,
        "pagination": {
            "total_count": total_count,
            "limit": limit,
            "skip": skip,
            "next": next_url,
            "previous": prev_url
        }
    })
