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
from app.spam_detector import SpamDetector, download_nltk_resources
import africastalking
import asyncio
import json
import logging
import re
from app.utils.phone_utils import normalize_phone_number
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
from prometheus_client import Counter

router = APIRouter(prefix="/ussd", tags=["USSD"])
logger = logging.getLogger("app.routers.ussd")

# Metrics
ussd_requests = Counter('ussd_requests_total', 'Total USSD requests')
message_flagged = Counter('message_flagged_total', 'Total flagged messages')

# Initialize Africa's Talking
africastalking.initialize(settings.AFRICASTALKING_USERNAME, settings.AFRICASTALKING_API_KEY)
sms = africastalking.SMS

# Initialize spam detector (after NLTK resources are downloaded)
spam_detector = SpamDetector()

# Languages & messages
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

def format_topics(lang):
    return "\n".join([f"{i+1}. {topic}" for i, topic in enumerate(TOPICS[lang])])

# Input validation
def validate_name(name: str) -> bool:
    return bool(name.strip() and re.match(r'^[A-Za-z\s]+$', name.strip()))

async def validate_district(db: AsyncSession, district: str) -> bool:
    result = await db.execute(select(MP.district_id).distinct())
    valid_districts = {d.lower().replace("district", "").strip() for d in result.scalars().all()}
    return district.lower().replace("district", "").strip() in valid_districts

def sanitize_input(text: str) -> str:
    return re.sub(r'[<>]', '', text.strip())[:160]

# Redis helpers
async def save_session(session_id, data, expire=900):
    redis = await get_redis()
    await redis.set(session_id, json.dumps(data), ex=expire)

async def load_session(session_id):
    redis = await get_redis()
    data = await redis.get(session_id)
    if data:
        try:
            parsed = json.loads(data)
            if not all(key in parsed for key in ["step", "language", "data"]):
                logger.warning(f"Corrupted session data for {session_id}")
                return None
            return parsed
        except json.JSONDecodeError:
            logger.error(f"Invalid session JSON for {session_id}")
            return None
    return None

async def delete_session(session_id):
    redis = await get_redis()
    await redis.delete(session_id)

# Async SMS sender
async def send_sms_async(phone: str, message: str):
    from tenacity import retry, stop_after_attempt, wait_exponential
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def send_sms_sync(phone: str, message: str):
        sms.send(message=message, recipients=[phone])

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: send_sms_sync(phone, message))
        logger.info(f"SMS sent to {phone}")
    except Exception as e:
        logger.error(f"Failed to send SMS after retries: {e}")
        raise

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

# Rate limiting setup
@router.on_event("startup")
async def startup():
    redis = await get_redis()
    await FastAPILimiter.init(redis)

@router.post("/ussd_callback", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def ussd_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        ussd_requests.inc()
        content_type = request.headers.get("content-type", "")
        data = await (request.json() if "application/json" in content_type else request.form())
        data = dict(data)

        # Redact sensitive data for logging
        def redact_sensitive(data: dict) -> dict:
            safe_data = data.copy()
            if "phoneNumber" in safe_data:
                safe_data["phoneNumber"] = "REDACTED"
            return safe_data

        logger.info(f"USSD request: {redact_sensitive(data)}")

        session_id = data.get("sessionId")
        phone_number = normalize_phone_number(data.get("phoneNumber"))
        text = data.get("text", "").strip()
        user_response = text.split("*") if text else []
        current_input = user_response[-1] if user_response else None

        # Load user
        result = await db.execute(select(User).where(User.phone_number == phone_number))
        user = result.scalars().first()

        # Load or initialize session
        session = await load_session(session_id) or {"step": "consent", "language": "EN", "data": {}, "user_id": user.id if user else None}
        if not session:
            logger.error(f"Failed to load or initialize session for {session_id}")
            return PlainTextResponse(content="END Session expired. Please start over.")

        language = session.get("language", "EN")
        user_data = session.get("data", {})

        # BACK navigation (exclude consent and returning_language_option)
        if current_input == "0" and session.get("step") not in ["consent", "returning_language_option"]:
            back_map = {
                "select_language": "consent",
                "register_name": "select_language",
                "register_district": "register_name",
                "topic_menu": "register_district" if not user else "returning_language_option",
                "ask_question": "topic_menu"
            }
            session["step"] = back_map.get(session["step"], session["step"])
            step = session["step"]
            response_text = f"CON {PROMPTS.get(step, WELCOME_MSG).get(language, '')}"
            if step == "topic_menu":
                response_text += format_topics(language)
            elif step == "returning_language_option":
                response_text = PROMPTS["returning_language_option"][language].format(lang=language)
            await save_session(session_id, session)
            return PlainTextResponse(content=response_text)

        step = session.get("step", "consent")

        # RETURNING USER
        if user and step == "consent":
            session["step"] = "returning_language_option"
            language = user.preferred_language or "EN"
            session["language"] = language
            session["user_id"] = user.id
            response_text = (
                f"CON Welcome back {user.first_name}!\n" +
                PROMPTS["returning_language_option"][language].format(lang=language)
            )
            await save_session(session_id, session)
            return PlainTextResponse(content=response_text)

        if step == "returning_language_option":
            if current_input == "1":
                session["step"] = "select_language"
                response_text = (
                    "CON Please select language:\n"
                    "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro\n0. Back"
                )
            elif current_input == "2":
                session["step"] = "topic_menu"
                response_text = f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}"
            else:
                response_text = (
                    f"CON Invalid choice. Please select 1 or 2.\n" +
                    PROMPTS["returning_language_option"][language].format(lang=language)
                )
            await save_session(session_id, session)
            return PlainTextResponse(content=response_text)

        # NEW USER FLOW
        if step == "consent":
            if not user_response or current_input != "1":
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
                    "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro\n0. Back"
                )
            else:
                language = LANGUAGES[current_input]
                session["language"] = language
                if user:  # Returning user changing language
                    user.preferred_language = language
                    await db.commit()
                    session["step"] = "topic_menu"
                    response_text = f"CON Language updated to {language}.\n{PROMPTS['ask_topic'][language]}{format_topics(language)}"
                else:
                    session["step"] = "register_name"
                    response_text = f"CON {PROMPTS['register_name'][language]}"
                await save_session(session_id, session)
                return PlainTextResponse(content=response_text)

        elif step == "register_name":
            if not current_input:
                response_text = f"CON {PROMPTS['register_name'][language]}"
            elif not validate_name(current_input):
                response_text = f"CON Invalid name. Use letters and spaces only.\n{PROMPTS['register_name'][language]}"
            else:
                user_data["name"] = current_input
                session["data"] = user_data
                session["step"] = "register_district"
                response_text = f"CON {PROMPTS['register_district'][language]}"

        elif step == "register_district":
            if not current_input:
                response_text = f"CON {PROMPTS['register_district'][language]}"
            elif not await validate_district(db, current_input):
                response_text = f"CON Invalid district. Enter a valid district like 'Kampala'.\n{PROMPTS['register_district'][language]}"
            else:
                user_data["district"] = current_input.title()
                session["data"] = user_data
                if not user:
                    # Check for existing user
                    result = await db.execute(select(User).where(User.phone_number == phone_number))
                    if result.scalars().first():
                        response_text = "END This phone number is already registered. Please use a different number."
                        return PlainTextResponse(content=response_text)
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
                    session["user_id"] = user.id
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
            question = sanitize_input(current_input.strip()) if current_input else ""
            if not question:
                response_text = f"CON {PROMPTS['question'][language]}"
            else:
                if not user:
                    logger.error(f"No user found for session {session_id}")
                    return PlainTextResponse(content="END Session error. Please start over.")

                # Check for spam or offensive content
                try:
                    is_spam, spam_prob = spam_detector.predict_spam(question, language.lower())
                    is_offensive = spam_detector.check_offensive(question, language.lower())
                except Exception as e:
                    logger.error(f"Spam detection failed: {e}")
                    is_spam, is_offensive = False, False  # Fallback to allow message

                if is_spam or is_offensive:
                    message_flagged.inc()
                    logger.warning(f"Flagged message from {phone_number}: spam={is_spam}, offensive={is_offensive}, text={question}")
                    # Save flagged message
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
                    except SQLAlchemyError as e:
                        logger.error(f"Failed to save flagged message: {e}")
                        await db.rollback()
                        return PlainTextResponse(content="END Something went wrong saving your message.")
                    response_text = "END Your message was flagged as inappropriate and will be reviewed."
                    await delete_session(session_id)
                    return PlainTextResponse(content=response_text)

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
                        content=question,
                        district_id=user.district_id,
                        created_at=datetime.utcnow(),
                        mp_id=recipient_id,
                        is_flagged=False
                    )
                    db.add(msg)
                    await db.commit()
                except SQLAlchemyError as e:
                    logger.error(f"Failed to save message: {e}")
                    await db.rollback()
                    return PlainTextResponse(content="END Something went wrong saving your message.")

                # Send SMS
                try:
                    normalized_recipient = normalize_phone_number(recipient_phone)
                    if not normalized_recipient.startswith("+256"):
                        normalized_recipient = "+256" + normalized_recipient.lstrip("0")
                    sms_message = f"CIVCON ALERT:\nNew issue from {user.first_name} ({phone_number}).\n\nMessage: {question}\nDistrict: {user_district.capitalize()}"
                    await send_sms_async(phone=normalized_recipient, message=sms_message)
                    response_text = "END Thank you! Your message has been sent successfully to your MP."
                except Exception as e:
                    logger.error(f"SMS send failed: {e}")
                    response_text = "END Message saved but SMS failed to send."
                    # Queue for retry
                    redis = await get_redis()
                    await redis.lpush("failed_sms", json.dumps({"phone": normalized_recipient, "message": sms_message}))

                await delete_session(session_id)
                return PlainTextResponse(content=response_text)

        session["data"] = user_data
        await save_session(session_id, session)
        return PlainTextResponse(content=response_text)

    except SQLAlchemyError as e:
        logger.error(f"Database error: {e}", exc_info=True)
        await db.rollback()
        return PlainTextResponse(content="END Database error. Please try again later.")
    except Exception as e:
        logger.error(f"USSD callback error: {e}", exc_info=True)
        await delete_session(session_id)
        return PlainTextResponse(content="END Something went wrong. Please try again shortly.")