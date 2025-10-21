from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime
from app.database import get_db
from app.models import User, Message, MP
from app.schemas import Role as RoleEnum
from app.redis_client import get_redis
from app.config import settings
import africastalking
import asyncio
import json
import logging
from app.utils.phone_utils import normalize_phone_number

router = APIRouter(prefix="/ussd", tags=["USSD"])
logger = logging.getLogger("app.routers.ussd")

# Initialize Africa's Talking
africastalking.initialize(settings.AFRICASTALKING_USERNAME, settings.AFRICASTALKING_API_KEY)
sms = africastalking.SMS

# Supported languages
LANGUAGES = {"1": "EN", "2": "LG", "3": "RN", "4": "LU", "5": "SW", "6": "RT"}

# Welcome messages
WELCOME_MSG = {
    "EN": "Welcome to CIVCON! Raise civic issues with your MP.",
    "LG": "Tukwaniriza ku CIVCON! Wandiika obuzibu bwo eri MP wo.",
    "RN": "Okwanjwa ku CIVCON! Tegereza ebizibu byo eri MP wawe.",
    "LU": "Mabero ku CIVCON! Wek ayie gi MP mamegi.",
    "SW": "Karibu CIVCON! Toa hoja zako kwa mbunge wako.",
    "RT": "Tukwaniriza ku CIVCON! Wandiika ebizibu byo eri MP wawe.",
}

PROMPTS = {
    "register_name": {
        "EN": "Enter your name:", "LG": "Wandika erinnya lyo:", "RN": "Yandikaho erinya ryawe:",
        "LU": "Ket erina ni:", "SW": "Weka jina lako:", "RT": "Andika erinnya lyo:"
    },
    "register_district": {
        "EN": "Enter your district or constituency:", "LG": "Wandika ekitundu kyo oba disitulikiti:",
        "RN": "Yandikaho disitulikiti yawe:", "LU": "Ket district ni i:",
        "SW": "Weka eneo lako au wilaya:", "RT": "Andika district yo:"
    },
    "ask_topic": {
        "EN": "Ask question:\n", "LG": "Buuza ekibuuzo:\n", "RN": "Buuza ekibuuzo:\n",
        "LU": "Penyo kwayo:\n", "SW": "Uliza swali:\n", "RT": "Buuza ekibuuzo:\n"
    },
    "question": {
        "EN": "Enter your question (max 160 chars):", "LG": "Wandika ekibuuzo kyo (obutayinza kusukka ku 160):",
        "RN": "Yandikaho ekibuuzo kyawe (kitarenga 160):", "LU": "Ket penyo ni (160 ki neno):",
        "SW": "Weka swali lako (si zaidi ya herufi 160):", "RT": "Andika ekibuuzo kyo (obutayinza kusukka ku 160):"
    },
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
async def save_session(session_id, data, expire=180):  # 3 min timeout
    redis = await get_redis()
    await redis.set(session_id, json.dumps(data), ex=expire)

async def load_session(session_id):
    redis = await get_redis()
    data = await redis.get(session_id)
    return json.loads(data) if data else None

async def delete_session(session_id):
    redis = await get_redis()
    await redis.delete(session_id)

# Async SMS sending
async def send_sms_async(phone: str, message: str):
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: sms.send(message=message, recipients=[phone]))
        logger.info(f"SMS sent to {phone}")
    except Exception as e:
        logger.error(f"Failed to send SMS: {e}")

# USSD endpoint
@router.post("/ussd_callback")
async def ussd_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        content_type = request.headers.get("content-type", "")
        data = await (request.json() if "application/json" in content_type else request.form())
        data = dict(data)

        session_id = data.get("sessionId")
        phone_number = normalize_phone_number(data.get("phoneNumber"))
        text = data.get("text", "").strip()
        user_response = text.split("*") if text else []

        logger.info(f"Incoming USSD request: {data}")

        # Load session or detect expiry
        session = await load_session(session_id)
        if not session:
            # Session expired or new user
            session = {"step": "select_language", "language": "EN", "data": {}}
            response_text = (
                "CON Please select language:\n"
                "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro"
            )
            await save_session(session_id, session)
            return PlainTextResponse(content=response_text)

        step = session.get("step", "select_language")
        language = session.get("language", "EN")
        user_data = session.get("data", {})

        # Step 1: Language selection
        if step == "select_language":
            if not user_response:
                response_text = (
                    "CON Please select language:\n"
                    "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro"
                )
            else:
                lang_choice = user_response[-1]
                if lang_choice in LANGUAGES:
                    language = LANGUAGES[lang_choice]
                    session["language"] = language
                    session["step"] = "start"
                    response_text = f"CON {WELCOME_MSG[language]}\nDo you consent?\n1. Yes\n0. No"
                else:
                    response_text = "CON Invalid choice.\nPlease select language:\n1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro"

        # Step 2: Consent
        elif step == "start":
            if not user_response:
                response_text = f"CON {WELCOME_MSG[language]}\nDo you consent?\n1. Yes\n0. No"
            elif user_response[-1] == "1":
                session["step"] = "register_name"
                response_text = f"CON {PROMPTS['register_name'][language]}"
            else:
                return PlainTextResponse(content="END You must consent to continue.")

        # Step 3: Registration
        elif step == "register_name":
            if user_response:
                user_data["name"] = user_response[-1]
                session["step"] = "register_district"
                response_text = f"CON {PROMPTS['register_district'][language]}"
            else:
                response_text = f"CON {PROMPTS['register_name'][language]}"

        elif step == "register_district":
            if user_response:
                user_data["district"] = user_response[-1].title()
                names = user_data["name"].split(" ")
                first_name = names[0]
                last_name = names[-1] if len(names) > 1 else ""
                new_user = User(
                    first_name=first_name,
                    last_name=last_name,
                    phone_number=phone_number,
                    district_id=user_data["district"],
                    is_active=True,
                    role=RoleEnum.CITIZEN,
                    preferred_language=language,
                )
                db.add(new_user)
                await db.commit()
                session["step"] = "topic_menu"
                response_text = f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}"
            else:
                response_text = f"CON {PROMPTS['register_district'][language]}"

        # Step 4: Existing User Flow
        else:
            result = await db.execute(select(User).where(User.phone_number == phone_number))
            user = result.scalars().first()
            if user:
                language = user.preferred_language or language
                session["language"] = language
                if len(user_response) == 0:
                    response_text = f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}"
                elif len(user_response) == 1:
                    choice = user_response[0]
                    topics = TOPICS[language]
                    if choice.isdigit() and 1 <= int(choice) <= len(topics):
                        user_data["topic"] = topics[int(choice) - 1]
                        session["step"] = "ask_question"
                        response_text = f"CON {PROMPTS['question'][language]}"
                    else:
                        response_text = f"CON Invalid choice.\n{PROMPTS['ask_topic'][language]}{format_topics(language)}"
                elif len(user_response) >= 2:
                    question = user_response[1][:160]
                    topic = user_data.get("topic", "General")
                    mp_result = await db.execute(select(MP))
                    mps = mp_result.scalars().all()
                    mp = next((m for m in mps if (m.district_id or "").lower() == (user.district_id or "").lower()), None)
                    recipient_id = mp.id if mp else None
                    msg = Message(
                        sender_id=user.id,
                        recipient_id=recipient_id,
                        content=question,
                        district_id=user.district_id,
                        created_at=datetime.utcnow(),
                        mp_id=recipient_id,
                    )
                    db.add(msg)
                    await db.commit()
                    if mp and mp.phone_number:
                        try:
                            recipient_phone = normalize_phone_number(mp.phone_number)
                            if not recipient_phone.startswith("+256"):
                                recipient_phone = "+256" + recipient_phone.lstrip("0")
                            await send_sms_async(
                                phone=recipient_phone,
                                message=f"New message from {user.first_name or 'a citizen'} ({user.district_id}): {question}",
                            )
                        except Exception as sms_err:
                            logger.error(f"Failed to send SMS: {sms_err}")
                    await delete_session(session_id)
                    return PlainTextResponse(content="END Thank you! Your message has been sent to your MP.")
            else:
                session["step"] = "select_language"
                response_text = (
                    "CON Please select language:\n"
                    "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro"
                )

        # Save session with timeout refresh
        session["data"] = user_data
        await save_session(session_id, session)
        return PlainTextResponse(content=response_text)

    except Exception as e:
        logger.error(f"USSD callback error: {e}", exc_info=True)
        await db.rollback()
        return PlainTextResponse(content="END Sorry, something went wrong. Please try again shortly.")
