from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import SQLAlchemyError
from redis.exceptions import RedisError
from datetime import datetime
from app.database import get_db
from app.models import User, Message, MP
from app.schemas import Role as RoleEnum
from app.redis_client import get_redis
from app.config import settings
from app.spam_detector import SpamDetector
import africastalking
import asyncio
import json
import logging
from app.utils.phone_utils import normalize_phone_number

router = APIRouter(prefix="/ussd", tags=["USSD"])
logger = logging.getLogger("app.routers.ussd")
detector = SpamDetector()

# Initialize Africa's Talking
africastalking.initialize(settings.AFRICASTALKING_USERNAME, settings.AFRICASTALKING_API_KEY)
sms = africastalking.SMS

# Initialize rate limiter
@router.on_event("startup")
async def startup():
    redis = await get_redis()
    await FastAPILimiter.init(redis)

# Dynamic rate limiter based on phone number and user status
async def dynamic_rate_limiter(request: Request, db: AsyncSession = Depends(get_db)):
    phone_number = normalize_phone_number(request.get("phoneNumber", ""))
    if not phone_number:
        raise HTTPException(status_code=400, detail="Phone number not provided")

    # Default limits
    times = 10  # Standard: 10 requests per minute
    seconds = 60

    # Check if user exists
    result = await db.execute(select(User).where(User.phone_number == phone_number))
    user = result.scalars().first()

    if not user:
        # New user: stricter limit
        times = 5
    else:
        # Check for flagged messages
        result = await db.execute(select(Message).where(Message.sender_id == user.id, Message.flagged == True))
        flagged_count = len(result.scalars().all())
        if flagged_count >= 1:
            times = 3  # Stricter limit for users with flagged messages

    return RateLimiter(times=times, seconds=seconds, identifier=phone_number)

# Languages & messages
LANGUAGES = {"1": "EN", "2": "LG", "3": "RN", "4": "LU", "5": "SW", "6": "RT"}

WELCOME_MSG = {
    "EN": "Welcome to CIVCON! Raise civic issues with your MP.",
    "LG": "Tukwaniriza ku CIVCON! Wandiika obuzibu bwo eri MP wo.",
    "RN": "Okwanjwa ku CIVCON! Tegereza ebizibu byo eri MP wawe.",
    "LU": "Mabero ku CIVCON! Wek ayie gi MP mamegi.",
    "SW": "Karibu CIVCON! Toa hoja zako kwa mbunge wako.",
    "RT": "Tukwaniriza ku CIVCON! Wandiika ebizibu byo eri MP wawe."
}

PROMPTS = {
    "register_name": {
        "EN": "Enter your name:", "LG": "Wandika erinnya lyo:", "RN": "Yandikaho erinya ryawe:",
        "LU": "Ket erina ni:", "SW": "Weka jina lako:", "RT": "Andika erinnya lyo:"
    },
    "register_district": {
        "EN": "Enter your district or constituency:", "LG": "Wandika ekitundu kyo oba disitulikiti:",
        "RN": "Yandikaho disitulikiti yawe:", "LU": "Ket district ni i:", "SW": "Weka eneo lako au wilaya:",
        "RT": "Andika district yo:"
    },
    "ask_topic": {
        "EN": "Select topic:\n", "LG": "Londa ekitundu:\n", "RN": "Hitamo ekitundu:\n",
        "LU": "Londo topic:\n", "SW": "Chagua mada:\n", "RT": "Londa ekitundu:\n"
    },
    "question": {
        "EN": "Enter your question (max 160 chars):", "LG": "Wandika ekibuuzo kyo (obutayinza kusukka ku 160):",
        "RN": "Yandikaho ekibuuzo kyawe (kitarenga 160):", "LU": "Ket penyo ni (160 ki neno):",
        "SW": "Weka swali lako (si zaidi ya herufi 160):", "RT": "Andika ekibuuzo kyo (obutayinza kusukka ku 160):"
    }
}

TOPICS = {
    "EN": ["Health", "Education", "Roads", "Water", "Electricity"],
    "LG": ["Obulamu", "Eby'enjigiriza", "Enzira", "Amazzi", "Masanyalaze"],
    "RN": ["Oburamu", "Eby'enjigiriza", "Enzira", "Amaizi", "Amashanyarazi"],
    "LU": ["Rwom", "Kweko", "Yo ka", "Pi", "Teko"],
    "SW": ["Afya", "Elimu", "Barabara", "Maji", "Umeme"],
    "RT": ["Oburamu", "Eby'enjigiriza", "Enzira", "Amaizi", "Amashanyarazi"],
}

def format_topics(lang):
    return "\n".join([f"{i+1}. {topic}" for i, topic in enumerate(TOPICS[lang])])

# Redis helpers
async def save_session(session_id, data, expire=900):
    redis = await get_redis()
    await redis.set(session_id, json.dumps(data), ex=expire)

async def load_session(session_id):
    redis = await get_redis()
    data = await redis.get(session_id)
    if data:
        parsed = json.loads(data)
        if not all(key in parsed for key in ["step", "language", "data"]):
            logger.warning(f"Corrupted session data for {session_id}")
            return None
        return parsed
    return None

async def delete_session(session_id):
    redis = await get_redis()
    await redis.delete(session_id)

# Async SMS sender
async def send_sms_async(phone: str, message: str):
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: sms.send(message=message, recipients=[phone]))
        logger.info(f"SMS sent to {phone}")
    except Exception as e:
        logger.error(f"Failed to send SMS: {e}")

# Fetch MPs (cached)
async def get_mps(db: AsyncSession):
    redis = await get_redis()
    cached = await redis.get("all_mps")
    if cached:
        return [MP(**m) for m in json.loads(cached)]
    result = await db.execute(select(MP))
    mps = result.scalars().all()
    await redis.set(
        "all_mps",
        json.dumps([{"id": m.id, "user_id": m.user_id, "district_id": m.district_id, "phone_number": m.phone_number} for m in mps]),
        ex=1800
    )
    return mps

# Check for excessive flagged messages
async def check_user_flags(db: AsyncSession, user_id: int) -> bool:
    result = await db.execute(select(Message).where(Message.sender_id == user_id, Message.flagged == True))
    flagged_count = len(result.scalars().all())
    return flagged_count >= 3  # Block after 3 flagged messages

@router.post("/ussd_callback", dependencies=[Depends(dynamic_rate_limiter)])
async def ussd_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        content_type = request.headers.get("content-type", "")
        data = await (request.json() if "application/json" in content_type else request.form())
        data = dict(data)

        session_id = data.get("sessionId")
        phone_number = normalize_phone_number(data.get("phoneNumber"))
        text = data.get("text", "").strip()
        user_response = text.split("*") if text else []
        current_input = user_response[-1] if user_response else None

        # Redact sensitive data for logging
        logger.info(f"USSD request: {redact_sensitive(data)}")

        # Load user
        result = await db.execute(select(User).where(User.phone_number == phone_number))
        user = result.scalars().first()

        # Check if user is blocked due to flagged messages
        if user and await check_user_flags(db, user.id):
            response_text = "END Your account is blocked due to repeated inappropriate messages."
            await delete_session(session_id)
            return PlainTextResponse(content=response_text)

        # Load or initialize session
        session = await load_session(session_id) or {"step": "consent", "language": "EN", "data": {}}
        language = session.get("language", "EN")
        user_data = session.get("data", {})

        # BACK navigation
        if current_input == "0" and session.get("step") != "consent":
            back_map = {
                "select_language": "consent",
                "register_name": "select_language",
                "register_district": "register_name",
                "topic_menu": "register_district",
                "ask_question": "topic_menu",
                "returning_language_option": "topic_menu"
            }
            session["step"] = back_map.get(session["step"], "consent")
            step = session["step"]
            response_text = f"CON {PROMPTS.get(step, WELCOME_MSG).get(language, '')}"
            if step == "topic_menu":
                response_text += format_topics(language)
            await save_session(session_id, session)
            return PlainTextResponse(content=response_text)

        step = session.get("step", "consent")

        # RETURNING USER
        if user and step == "consent":
            session["step"] = "returning_language_option"
            language = user.preferred_language or "EN"
            session["language"] = language
            response_text = (
                f"CON Welcome back {user.first_name}!\n"
                f"Your current language is {language}.\n"
                "Do you want to change language?\n1. Yes\n2. No"
            )
            await save_session(session_id, session)
            return PlainTextResponse(content=response_text)

        if step == "returning_language_option":
            if current_input == "1":
                session["step"] = "select_language"
                response_text = (
                    "CON Please select language:\n"
                    "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro"
                )
            elif current_input == "2":
                session["step"] = "topic_menu"
                response_text = f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}"
            else:
                response_text = (
                    "CON Invalid choice. Do you want to change language?\n1. Yes\n2. No"
                )
            await save_session(session_id, session)
            return PlainTextResponse(content=response_text)

        # NEW USER FLOW
        if step == "consent":
            if not user_response or user_response[-1] != "1":
                return PlainTextResponse(content="END You must consent to continue.")
            session["step"] = "select_language"
            response_text = (
                "CON Please select language:\n"
                "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro"
            )

        elif step == "select_language":
            if not current_input or current_input not in LANGUAGES:
                response_text = (
                    "CON Invalid choice. Please select a valid language:\n"
                    "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro"
                )
            else:
                language = LANGUAGES[current_input]
                session["language"] = language
                session["step"] = "register_name"
                response_text = f"CON {PROMPTS['register_name'][language]}"

        elif step == "register_name":
            if not current_input:
                response_text = f"CON {PROMPTS['register_name'][language]}"
            else:
                user_data["name"] = current_input
                session["data"] = user_data
                session["step"] = "register_district"
                response_text = f"CON {PROMPTS['register_district'][language]}"

        elif step == "register_district":
            if not current_input:
                response_text = f"CON {PROMPTS['register_district'][language]}"
            else:
                user_data["district"] = current_input.title()
                session["data"] = user_data
                if not user:
                    # Create new user
                    names = user_data["name"].split()
                    first_name = names[0]
                    last_name = names[-1] if len(names) > 1 else ""
                    new_user = User(
                        first_name=first_name,
                        last_name=last_name,
                        phone_number=phone_number,
                        district_id=user_data["district"],
                        is_active=True,
                        role=RoleEnum.CITIZEN,
                        preferred_language=language
                    )
                    db.add(new_user)
                    await db.commit()
                    await db.refresh(new_user)
                    user = new_user
                session["step"] = "topic_menu"
                response_text = f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}"

        elif step == "topic_menu":
            if not current_input:
                response_text = f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}"
            elif current_input.isdigit() and 1 <= int(current_input) <= len(TOPICS[language]):
                user_data["topic"] = TOPICS[language][int(current_input) - 1]
                session["data"] = user_data
                session["step"] = "ask_question"
                response_text = f"CON {PROMPTS['question'][language]}"
            else:
                response_text = f"CON Invalid choice.\n{PROMPTS['ask_topic'][language]}{format_topics(language)}"

        elif step == "ask_question":
            question = current_input.strip() if current_input else ""
            if not question:
                response_text = f"CON {PROMPTS['question'][language]}"
            else:
                # Check for spam or offensive content
                is_spam, spam_prob = detector.predict_spam(question, language.lower())
                is_offensive = detector.check_offensive(question, language.lower())
                if is_spam or is_offensive:
                    try:
                        msg = Message(
                            sender_id=user.id,
                            recipient_id=None,
                            content=question[:160],
                            district_id=user.district_id,
                            created_at=datetime.utcnow(),
                            mp_id=None,
                            flagged=True
                        )
                        db.add(msg)
                        await db.commit()
                        response_text = "END Your message contains inappropriate content and has been flagged. Please rephrase."
                        await delete_session(session_id)
                        return PlainTextResponse(content=response_text)
                    except Exception as e:
                        logger.error(f"Failed to save flagged message: {e}")
                        await db.rollback()
                        return PlainTextResponse(content="END Something went wrong saving your message.")

                mps = await get_mps(db)
                user_district = (user.district_id or "").lower().replace("district", "").strip()
                mp = next(
                    (m for m in mps if user_district in (m.district_id or "").lower().replace("district", "").strip() or
                     (m.district_id or "").lower().replace("district", "").strip() in user_district),
                    None
                )
                fallback_phone = settings.FALLBACK_PHONE
                fallback_mp_id = settings.FALLBACK_MP_ID
                recipient_id = mp.user_id if mp else fallback_mp_id
                recipient_phone = mp.phone_number if mp else fallback_phone

                # Save message
                try:
                    msg = Message(
                        sender_id=user.id,
                        recipient_id=recipient_id,
                        content=question[:160],
                        district_id=user.district_id,
                        created_at=datetime.utcnow(),
                        mp_id=recipient_id,
                        flagged=False
                    )
                    db.add(msg)
                    await db.commit()
                except Exception as e:
                    logger.error(f"Failed to save message: {e}")
                    await db.rollback()
                    return PlainTextResponse(content="END Something went wrong saving your message.")

                # Send SMS
                try:
                    normalized_recipient = normalize_phone_number(recipient_phone)
                    if not normalized_recipient.startswith("+256"):
                        normalized_recipient = "+256" + normalized_recipient.lstrip("0")
                    sms_message = f"CIVCON ALERT:\nNew issue from {user.first_name} ({user.phone_number}).\n\nMessage: {question}\nDistrict: {user_district.capitalize()}"
                    await send_sms_async(phone=normalized_recipient, message=sms_message)
                    response_text = "END Thank you! Your message has been sent successfully to your MP."
                except Exception as e:
                    logger.error(f"SMS send failed: {e}")
                    response_text = "END Message saved but SMS failed to send."

                await delete_session(session_id)
                return PlainTextResponse(content=response_text)

        session["data"] = user_data
        await save_session(session_id, session)
        return PlainTextResponse(content=response_text)

    except HTTPException as e:
        logger.error(f"Rate limit exceeded for {phone_number}: {e.detail}")
        return PlainTextResponse(content="END Too many requests. Please try again later.")
    except SQLAlchemyError as e:
        logger.error(f"Database error: {e}", exc_info=True)
        await db.rollback()
        return PlainTextResponse(content="END Database error. Please try again later.")
    except RedisError as e:
        logger.error(f"Redis error: {e}", exc_info=True)
        return PlainTextResponse(content="END Session error. Please try again.")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        await delete_session(session_id)
        return PlainTextResponse(content="END Something went wrong. Please try again shortly.")

def redact_sensitive(data: dict) -> dict:
    safe_data = data.copy()
    if "phoneNumber" in safe_data:
        safe_data["phoneNumber"] = "REDACTED"
    return safe_data