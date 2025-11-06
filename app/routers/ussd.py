
from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import SQLAlchemyError
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
import re
from app.utils.phone_utils import normalize_phone_number
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
from prometheus_client import Counter

# Router & logger
router = APIRouter(prefix="/ussd", tags=["USSD"])
logger = logging.getLogger("app.routers.ussd")
logger.setLevel(logging.INFO)

# Metrics
ussd_requests = Counter('ussd_requests_total', 'Total USSD requests')
message_flagged = Counter('message_flagged_total', 'Total flagged messages')

# Africa's Talking init (must be set in env)
try:
    africastalking.initialize(settings.AFRICASTALKING_USERNAME, settings.AFRICASTALKING_API_KEY)
    sms = africastalking.SMS
except Exception:
    sms = None
    logger.warning("Africa's Talking not initialized - SMS sending will fail until keys are configured.")

# Static texts
LANGUAGES = {"1": "EN", "2": "LG", "3": "RN", "4": "LU", "5": "SW", "6": "RT"}

WELCOME_MSG = {
    "EN": "Welcome to CIVCON! Raise civic issues with your MP.\n1. Consent to continue",
    "LG": "Tukwaniriza ku CIVCON! Wandiika obuzibu bwo eri MP wo.\n1. Okukkiriza okweyongerayo",
    "RN": "Okwanjwa ku CIVCON! Tegereza ebizibu byo eri MP wawe.\n1. Okwemera okugumya",
    "LU": "Mabero ku CIVCON! Wek ayie gi MP mamegi.\n1. Yie me medo",
    "SW": "Karibu CIVCON! Toa hoja zako kwa mbunge wako.\n1. Idhini ya kuendelea",
    "RT": "Tukwaniriza ku CIVCON! Wandiika ebizibu byo eri MP wawe.\n1. Okukkiriza okweyongerayo"
}

PROMPTS = {
    "register_name": {
        "EN": "Enter your name (letters only):",
        "LG": "Wandika erinnya lyo (obukuumi bupya):",
        "RN": "Yandikaho erinya ryawe (obukuumi bupya):",
        "LU": "Ket erina ni (litere kende):",
        "SW": "Weka jina lako (herufi pekee):",
        "RT": "Andika erinnya lyo (obukuumi bupya):"
    },
    "register_district": {
        "EN": "Enter your district (e.g., Kampala):",
        "LG": "Wandika ekitundu kyo (oku nkola, Kampala):",
        "RN": "Yandikaho disitulikiti yawe (oku nkola, Kampala):",
        "LU": "Ket district ni i (ngeo, Kampala):",
        "SW": "Weka eneo lako (k.m., Kampala):",
        "RT": "Andika district yo (oku nkola, Kampala):"
    },
    "ask_topic": {
        "EN": "Select topic:\n0. Back",
        "LG": "Londa ekitundu:\n0. Emabega",
        "RN": "Hitamo ekitundu:\n0. Inyuma",
        "LU": "Londo topic:\n0. Cen",
        "SW": "Chagua mada:\n0. Rudi",
        "RT": "Londa ekitundu:\n0. Emabega"
    },
    "question": {
        "EN": "Enter your question (max 160 chars, no offensive words):",
        "LG": "Wandika ekibuuzo kyo (obutayinza kusukka ku 160, tewali bigambo by'okuzirira):",
        "RN": "Yandikaho ekibuuzo kyawe (kitarenga 160, nta bigambo by'okuzirira):",
        "LU": "Ket penyo ni (160 ki neno, peki lok marac):",
        "SW": "Weka swali lako (si zaidi ya herufi 160, hakuna maneno ya matusi):",
        "RT": "Andika ekibuuzo kyo (obutayinza kusukka ku 160, tewali bigambo by'okuzirira):"
    },
    "returning_language_option": {
        "EN": "Your current language is {lang}. Change language?\n1. Yes\n2. No",
        "LG": "Lugambo lwo lwa {lang}. Okukyusa lugambo?\n1. Ye\n2. Nedda",
        "RN": "Ururimi rwawe ni {lang}. Okurihindura?\n1. Yego\n2. Oya",
        "LU": "Lok ma itiyo kede ni {lang}. Bedo adwong?\n1. Eyo\n2. Pe",
        "SW": "Lugha yako ya sasa ni {lang}. Badilisha lugha?\n1. Ndio\n2. Hapana",
        "RT": "Orurimi rwawe ni {lang}. Okukyusa orurimi?\n1. Eyo\n2. Nedda"
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

def format_topics(lang: str) -> str:
    return "\n".join([f"{i+1}. {topic}" for i, topic in enumerate(TOPICS.get(lang, []))])

# Validation
def validate_name(name: str) -> bool:
    return bool(name and re.match(r'^[A-Za-z\s]+$', name.strip()))

async def validate_district(db: AsyncSession, district: str) -> bool:
    try:
        result = await db.execute(select(MP.district_id).distinct())
        districts = [str(d).lower().replace("district", "").strip() for d in result.scalars().all() if d]
        user_input = str(district).lower().replace("district", "").strip()
        valid = user_input in districts
        logger.info(f"District validation: input='{user_input}' valid={valid}")
        return valid
    except Exception as e:
        logger.error(f"District validation error: {e}", exc_info=True)
        return False

def sanitize_input(text: str) -> str:
    return re.sub(r'[<>]', '', (text or "").strip())[:160]

# Redis helpers
async def save_session(session_id, data, expire=900):
    redis = await get_redis()
    try:
        await redis.set(session_id, json.dumps(data), ex=expire)
        logger.info(f"Session saved: {session_id} -> {data.get('step')}")
    except Exception as e:
        logger.error(f"Failed to save session {session_id}: {e}", exc_info=True)

async def load_session(session_id):
    redis = await get_redis()
    try:
        data = await redis.get(session_id)
        if data:
            parsed = json.loads(data)
            logger.info(f"Session loaded for {session_id}: {parsed.get('step')}")
            return parsed
    except Exception as e:
        logger.error(f"Failed to load session {session_id}: {e}", exc_info=True)
    return None

async def delete_session(session_id):
    redis = await get_redis()
    try:
        await redis.delete(session_id)
        logger.info(f"Session deleted: {session_id}")
    except Exception as e:
        logger.error(f"Failed to delete session {session_id}: {e}", exc_info=True)

# Async SMS sender
async def send_sms_async(phone: str, message: str):
    from tenacity import retry, stop_after_attempt, wait_exponential
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def send_sms_sync(phone: str, message: str):
        if not sms:
            raise RuntimeError("Africa's Talking SMS client not initialized")
        sms.send(message=message, recipients=[phone])
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: send_sms_sync(phone, message))
        logger.info(f"SMS dispatched to {phone}")
    except Exception as e:
        logger.error(f"Failed to send SMS to {phone}: {e}", exc_info=True)
        raise

# Fetch MPs (cached)
async def get_mps(db: AsyncSession):
    redis = await get_redis()
    try:
        cached = await redis.get("all_mps")
        if cached:
            logger.info("Loaded MPs from cache")
            return [MP(**m) for m in json.loads(cached)]
        result = await db.execute(select(MP))
        mps = result.scalars().all()
        await redis.set("all_mps", json.dumps([
            {"id": m.id, "user_id": m.user_id, "district_id": m.district_id, "phone_number": m.phone_number}
            for m in mps
        ]), ex=1800)
        logger.info(f"Cached {len(mps)} MPs")
        return mps
    except Exception as e:
        logger.error(f"Error fetching MPs: {e}", exc_info=True)
        return []

# Rate limiter setup
@router.on_event("startup")
async def startup():
    redis = await get_redis()
    await FastAPILimiter.init(redis)
    logger.info("FastAPILimiter initialized for USSD")

@router.post("/ussd_callback", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def ussd_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        ussd_requests.inc()
        data = await (request.json() if "application/json" in request.headers.get("content-type", "") else request.form())
        data = dict(data)
        logger.info(f"Incoming USSD request raw: {data}")

        # prepare detector (instantiated per-request to avoid worker-global NLTK issues)
        spam_detector = SpamDetector()

        session_id = data.get("sessionId")
        phone_number = normalize_phone_number(data.get("phoneNumber"))
        text = (data.get("text") or "").strip()
        user_response = text.split("*") if text else []
        current_input = user_response[-1] if user_response else None

        logger.info(f"Parsed USSD -> session_id={session_id}, phone={phone_number}, text='{text}', current_input='{current_input}'")

        # load user
        result = await db.execute(select(User).where(User.phone_number == phone_number))
        user = result.scalars().first()

        # load or initialize session
        session = await load_session(session_id)
        if not session:
            session = {"step": "consent", "language": "EN", "data": {}, "user_id": user.id if user else None}
            await save_session(session_id, session)

        # normalize step
        step_raw = session.get("step", "consent")
        step = (step_raw or "consent").strip().lower()
        language = session.get("language", "EN")
        user_data = session.get("data", {})

        logger.info(f"Session {session_id} loaded: step='{step_raw}' normalized='{step}' language='{language}' user_id='{session.get('user_id')}'")

        # Consent step
        if step == "consent":
            if not user_response or text == "":
                response = f"CON {WELCOME_MSG.get(language, WELCOME_MSG['EN'])}"
                await save_session(session_id, session)
                return PlainTextResponse(content=response)
            if current_input != "1":
                await delete_session(session_id)
                return PlainTextResponse(content="END You must consent to continue.")
            session["step"] = "select_language"
            await save_session(session_id, session)
            return PlainTextResponse(content="CON Please select language:\n1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro")

        # Returning user path (handled only if user exists and we were at consent)
        if user and step == "consent":
            session["step"] = "returning_language_option"
            language = user.preferred_language or "EN"
            session["language"] = language
            await save_session(session_id, session)
            return PlainTextResponse(content=f"CON Welcome back {user.first_name}!\n" + PROMPTS["returning_language_option"][language].format(lang=language))

        # Returning language option
        if step == "returning_language_option":
            if current_input == "1":
                session["step"] = "select_language"
                await save_session(session_id, session)
                return PlainTextResponse(content="CON Please select language:\n1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro\n0. Back")
            if current_input == "2":
                session["step"] = "topic_menu"
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}")
            await save_session(session_id, session)
            return PlainTextResponse(content="CON Invalid choice. " + PROMPTS["returning_language_option"][language].format(lang=language))

        # Language selection
        if step == "select_language":
            if not current_input or current_input not in LANGUAGES:
                return PlainTextResponse(content="CON Invalid choice. Please select a valid language:\n1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro\n0. Back")
            language = LANGUAGES[current_input]
            session["language"] = language
            if user:
                user.preferred_language = language
                await db.commit()
                session["step"] = "topic_menu"
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON Language updated to {language}.\n{PROMPTS['ask_topic'][language]}{format_topics(language)}")
            session["step"] = "register_name"
            await save_session(session_id, session)
            return PlainTextResponse(content=f"CON {PROMPTS['register_name'][language]}")

        # Register name
        if step == "register_name":
            if not current_input:
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON {PROMPTS['register_name'][language]}")
            if not validate_name(current_input):
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON Invalid name. Use letters and spaces only.\n{PROMPTS['register_name'][language]}")
            user_data["name"] = current_input.strip()
            session["data"] = user_data
            session["step"] = "register_district"
            await save_session(session_id, session)
            return PlainTextResponse(content=f"CON {PROMPTS['register_district'][language]}")

        # Register district
        if step == "register_district":
            logger.info(f"Register district step: input='{current_input}' session={session_id} phone={phone_number}")
            if not current_input:
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON {PROMPTS['register_district'][language]}")
            valid = await validate_district(db, current_input)
            if not valid:
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON Invalid district. Enter a valid district like 'Kampala'.\n{PROMPTS['register_district'][language]}")
            # valid district -> create or update user
            user_data["district"] = current_input.title()
            session["data"] = user_data
            if not user:
                # Prevent duplicates (race-safe)
                result = await db.execute(select(User).where(User.phone_number == phone_number))
                if result.scalars().first():
                    await delete_session(session_id)
                    return PlainTextResponse(content="END This phone number is already registered.")
                names = user_data.get("name", "").split()
                first_name = names[0] if names else ""
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
                session["user_id"] = user.id
                logger.info(f"New user registered {user.phone_number} -> id={user.id}")
            else:
                # update user's district if different
                if (user.district_id or "").lower() != user_data["district"].lower():
                    user.district_id = user_data["district"]
                    await db.commit()
                    logger.info(f"Updated user {user.phone_number} district -> {user.district_id}")
            session["step"] = "topic_menu"
            await save_session(session_id, session)
            return PlainTextResponse(content=f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}")

        # Topic selection
        if step == "topic_menu":
            if not current_input:
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}")
            if current_input.isdigit() and 1 <= int(current_input) <= len(TOPICS[language]):
                user_data["topic"] = TOPICS[language][int(current_input) - 1]
                session["data"] = user_data
                session["step"] = "ask_question"
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON {PROMPTS['question'][language]}")
            await save_session(session_id, session)
            return PlainTextResponse(content=f"CON Invalid choice.\n{PROMPTS['ask_topic'][language]}{format_topics(language)}")

        # Ask question
        if step == "ask_question":
            logger.info(f"Ask question step: input='{current_input}' session={session_id} phone={phone_number}")
            question = sanitize_input(current_input or "")
            if not question:
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON {PROMPTS['question'][language]}")
            if not user:
                logger.error(f"No user found for session {session_id}")
                await delete_session(session_id)
                return PlainTextResponse(content="END Session error. Please start over.")

            # Spam/offensive detection (non-blocking)
            try:
                is_spam, spam_prob = spam_detector.predict_spam(question, language.lower())
                is_offensive = spam_detector.check_offensive(question, language.lower())
                logger.info(f"Spam detection -> spam={is_spam} prob={spam_prob:.2f} offensive={is_offensive}")
            except Exception as e:
                logger.warning(f"Spam detection failed: {e}", exc_info=True)
                is_spam = False
                is_offensive = False

            # Flag messages when necessary
            if (is_spam and spam_prob >= 0.8) or is_offensive:
                message_flagged.inc()
                try:
                    msg = Message(
                        sender_id=user.id,
                        recipient_id=None,
                        content=question,
                        district_id=user.district_id,
                        created_at=datetime.utcnow(),
                        mp_id=None,
                        is_flagged=True
                    )
                    db.add(msg)
                    await db.commit()
                except Exception as e:
                    logger.error(f"Failed to save flagged message: {e}", exc_info=True)
                    await db.rollback()
                    await delete_session(session_id)
                    return PlainTextResponse(content="END Something went wrong saving your message.")
                await delete_session(session_id)
                return PlainTextResponse(content="END Your message was flagged as inappropriate and will be reviewed.")

            # Normal message flow: find MP, save message, send SMS
            try:
                mps = await get_mps(db)
                user_district = (user.district_id or "").lower().replace("district", "").strip()
                mp = next((m for m in mps if user_district in (m.district_id or "").lower().replace("district", "").strip()), None)
                recipient_id = mp.user_id if mp else settings.FALLBACK_MP_ID
                recipient_phone = mp.phone_number if mp else settings.FALLBACK_PHONE

                msg = Message(
                    sender_id=user.id,
                    recipient_id=recipient_id,
                    content=question,
                    district_id=user.district_id,
                    created_at=datetime.utcnow(),
                    mp_id=recipient_id,
                    is_flagged=False
                )
                db.add(msg)
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to save message: {e}", exc_info=True)
                await db.rollback()
                await delete_session(session_id)
                return PlainTextResponse(content="END Something went wrong saving your message.")

            # Send SMS
            try:
                normalized_recipient = normalize_phone_number(recipient_phone)
                if not normalized_recipient.startswith("+256"):
                    normalized_recipient = "+256" + normalized_recipient.lstrip("0")
                sms_message = f"CIVCON ALERT:\nNew issue from {user.first_name} ({phone_number}).\n\nMessage: {question}\nDistrict: {user_district.capitalize()}"
                await send_sms_async(phone=normalized_recipient, message=sms_message)
                response = "END Thank you! Your message has been sent successfully to your MP."
            except Exception as e:
                logger.error(f"SMS send failed: {e}", exc_info=True)
                # queue for retry
                try:
                    redis = await get_redis()
                    await redis.lpush("failed_sms", json.dumps({"phone": recipient_phone, "message": question}))
                except Exception as re:
                    logger.error(f"Failed to queue failed SMS: {re}", exc_info=True)
                response = "END Message saved but SMS failed to send."

            await delete_session(session_id)
            return PlainTextResponse(content=response)

        # If step is unknown, give a clear message and dump session for debugging
        logger.warning(f"Unexpected flow. session={session}")
        # provide helpful message rather than generic unexpected flow
        return PlainTextResponse(content="END Session error or expired. Please start over.")

    except SQLAlchemyError as e:
        logger.error(f"Database error: {e}", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            logger.exception("Rollback failed")
        return PlainTextResponse(content="END Database error. Please try again later.")
    except Exception as e:
        logger.error(f"USSD callback error: {e}", exc_info=True)
        # if we have session id, try to clean it
        try:
            if 'session_id' in locals() and session_id:
                await delete_session(session_id)
        except Exception:
            logger.exception("Failed to delete session after exception")
        return PlainTextResponse(content="END Something went wrong. Please try again shortly.")
