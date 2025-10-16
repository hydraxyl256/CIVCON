import asyncio
import json
import redis.asyncio as redis
from .config import settings

# Use environment/config URL
REDIS_URL = settings.redis_url
r = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

# Existing helpers
REDIS_EXPIRE = 3600  # 1 hour session expiry

async def save_session(session_id: str, session_data: dict):
    await r.set(session_id, json.dumps(session_data), ex=REDIS_EXPIRE)

async def get_session(session_id: str) -> dict:
    data = await r.get(session_id)
    return json.loads(data) if data else None

async def delete_session(session_id: str):
    await r.delete(session_id)

async def get_redis():
    return r
