import asyncio
import json
import redis.asyncio as redis

REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0
REDIS_EXPIRE = 3600  # 1 hour session expiry

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

# Existing helpers
async def save_session(session_id: str, session_data: dict):
    await r.set(session_id, json.dumps(session_data), ex=REDIS_EXPIRE)

async def get_session(session_id: str) -> dict:
    data = await r.get(session_id)
    return json.loads(data) if data else None

async def delete_session(session_id: str):
    await r.delete(session_id)

async def get_redis():
    return r
