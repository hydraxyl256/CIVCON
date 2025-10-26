import httpx
from app.models import Post, User

async def share_to_social_media(platform: str, post: Post, user: User):
    """Stub for integrating with Facebook, X (Twitter), etc."""
    content = f"{user.first_name} shared a post: {post.title}\n{post.content}"
    print(f"Sharing to {platform}: {content}")
    # s Facebook Graph API, or Twitter/X API here

async def send_inbox_message(post: Post, sender: User, message: str | None):
    """Simulate sending a message with the post link to another user."""
    print(f"Sending inbox message: {sender.first_name} shared post {post.id}")
