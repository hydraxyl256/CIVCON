from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Post, Comment
from app.crud import get_user_by_email
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import logging
from app.config import settings


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm

router = APIRouter(prefix="/admin", tags=["admin"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

async def get_current_admin(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = get_user_by_email(db, email=email)
    if user is None or user.role != "admin":
        raise credentials_exception
    return user

@router.delete("/posts/{post_id}")
async def delete_post(post_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_admin)):
    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    db.delete(post)
    db.commit()
    logger.info(f"Post {post_id} deleted by admin {current_user.email}")
    return {"message": "Post deleted"}

@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_admin)):
    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    db.delete(comment)
    db.commit()
    logger.info(f"Comment {comment_id} deleted by admin {current_user.email}")
    return {"message": "Comment deleted"}