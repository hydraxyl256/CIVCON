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

# Languages & messages
LANGUAGES = {
    "1": "EN",
    "2": "LG",
    "3": "RN",
    "4": "LU",
    "5": "SW",
    "6": "RT"
}

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
        "EN": "Enter your name:",
        "LG": "Wandika erinnya lyo:",
        "RN": "Yandikaho erinya ryawe:",
        "LU": "Ket erina ni:",
        "SW": "Weka jina lako:",
        "RT": "Andika erinnya lyo:"
    },
    "register_district": {
        "EN": "Enter your district or constituency:",
        "LG": "Wandika ekitundu kyo oba disitulikiti:",
        "RN": "Yandikaho disitulikiti yawe:",
        "LU": "Ket district ni i:",
        "SW": "Weka eneo lako au wilaya:",
        "RT": "Andika district yo:"
    },
    "ask_topic": {
        "EN": "Select topic:\n",
        "LG": "Londa ekitundu:\n",
        "RN": "Hitamo ekitundu:\n",
        "LU": "Londo topic:\n",
        "SW": "Chagua mada:\n",
        "RT": "Londa ekitundu:\n"
    },
    "question": {
        "EN": "Enter your question (max 160 chars):",
        "LG": "Wandika ekibuuzo kyo (obutayinza kusukka ku 160):",
        "RN": "Yandikaho ekibuuzo kyawe (kitarenga 160):",
        "LU": "Ket penyo ni (160 ki neno):",
        "SW": "Weka swali lako (si zaidi ya herufi 160):",
        "RT": "Andika ekibuuzo kyo (obutayinza kusukka ku 160):"
    }
}

TOPICS = {
    "EN": ["Health", "Education", "Roads", "Water", "Electricity"],
    "LG": ["Obulamu", "Eby'enjigiriza", "Enzira", "Amazzi", "Masanyalaze"],
    "RN": ["Oburamu", "Eby'enjigiriza", "Enzira", "Amaizi", "Amashanyarazi"],
    "LU": ["Rwom", "Kweko", "Yo ka", "Pi", "Teko"],
    "SW": ["Afya", "Elimu", "Barabara", "Maji", "Umeme"],
    "RT": ["Oburamu", "Eby'enjigiriza", "Enzira", "Amaizi", "Amashanyarazi"]
}

def format_topics(lang):
    return "\n".join([f"{i+1}. {topic}" for i, topic in enumerate(TOPICS[lang])])

# Redis helpers
async def save_session(session_id, data, expire=600):
    redis = await get_redis()
    await redis.set(session_id, json.dumps(data), ex=expire)

async def load_session(session_id):
    redis = await get_redis()
    data = await redis.get(session_id)
    return json.loads(data) if data else None

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
    await redis.set("all_mps", json.dumps([{
        "id": m.id,
        "user_id": m.user_id,
        "district_id": m.district_id,
        "phone_number": m.phone_number
    } for m in mps]), ex=1800)
    return mps

# USSD callback
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

        logger.info(f"USSD request: {data}")

        session = await load_session(session_id)
        if not session:
            session = {"step": "consent", "language": "EN", "data": {}}
            await save_session(session_id, session)
            return PlainTextResponse(content=f"CON {WELCOME_MSG['EN']}\nDo you consent?\n1. Yes\n0. No")

        step = session.get("step", "consent")
        language = session.get("language", "EN")
        user_data = session.get("data", {})

        # CONSENT
        if step == "consent":
            if not user_response or user_response[-1] != "1":
                return PlainTextResponse(content="END You must consent to continue.")
            session["step"] = "select_language"
            response_text = (
                "CON Please select language:\n"
                "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro"
            )

        # LANGUAGE
        elif step == "select_language":
            choice = user_response[-1] if user_response else None
            if not choice or choice not in LANGUAGES:
                response_text = (
                    "CON Invalid choice. Please select a valid language:\n"
                    "1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro"
                )
            else:
                language = LANGUAGES[choice]
                session["language"] = language
                session["step"] = "register_name"
                response_text = f"CON {PROMPTS['register_name'][language]}"

        # REGISTER NAME
        elif step == "register_name":
            current_input = user_response[-1] if user_response else None
            if not current_input:
                response_text = f"CON {PROMPTS['register_name'][language]}"
            else:
                user_data["name"] = current_input
                session["data"] = user_data
                session["step"] = "register_district"
                response_text = f"CON {PROMPTS['register_district'][language]}"

        # REGISTER DISTRICT
        elif step == "register_district":
            current_input = user_response[-1] if user_response else None
            if not current_input:
                response_text = f"CON {PROMPTS['register_district'][language]}"
            else:
                user_data["district"] = current_input.title()
                session["data"] = user_data

                # Check if user exists
                result = await db.execute(select(User).where(User.phone_number == phone_number))
                user = result.scalars().first()
                if not user:
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

        # TOPIC
        elif step == "topic_menu":
            current_input = user_response[-1] if user_response else None
            result = await db.execute(select(User).where(User.phone_number == phone_number))
            user = result.scalars().first()
            if not user:
                session["step"] = "consent"
                response_text = f"CON {WELCOME_MSG['EN']}\nDo you consent?\n1. Yes\n0. No"
            else:
                language = user.preferred_language or language
                session["language"] = language
                if not current_input:
                    response_text = f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}"
                elif current_input.isdigit() and 1 <= int(current_input) <= len(TOPICS[language]):
                    user_data["topic"] = TOPICS[language][int(current_input) - 1]
                    session["data"] = user_data
                    session["step"] = "ask_question"
                    response_text = f"CON {PROMPTS['question'][language]}"
                else:
                    response_text = f"CON Invalid choice.\n{PROMPTS['ask_topic'][language]}{format_topics(language)}"

        # ASK QUESTION
        elif step == "ask_question":
            question = user_response[-1].strip() if user_response else ""
            if not question:
                response_text = f"CON {PROMPTS['question'][language]}"
            else:
                # Fetch user
                result = await db.execute(select(User).where(User.phone_number == phone_number))
                user = result.scalars().first()
                if not user:
                    response_text = "END Session expired. Please try again."
                    await delete_session(session_id)
                    return PlainTextResponse(content=response_text)

                # Get MPs
                mps = await get_mps(db)
                user_district = (user.district_id or "").lower().replace("district", "").strip()
                mp = next(
                    (
                        m for m in mps
                        if user_district in (m.district_id or "").lower().replace("district", "").strip()
                        or (m.district_id or "").lower().replace("district", "").strip() in user_district
                    ),
                    None
                )

                # CIVCON fallback
                fallback_phone = "+256784437652"
                fallback_mp_id = 15  # ADMIN fallback MP ID

                # Determine recipient
                recipient_id = mp.user_id if mp else fallback_mp_id
                recipient_phone = mp.phone_number if mp else fallback_phone

                # Save message safely
                try:
                    msg = Message(
                        sender_id=user.id,
                        recipient_id=recipient_id,
                        content=question[:160],
                        district_id=user.district_id,
                        created_at=datetime.utcnow(),
                        mp_id=recipient_id
                    )
                    db.add(msg)
                    await db.commit()
                except Exception as e:
                    logger.error(f"Failed to save message: {e}")
                    await db.rollback()
                    response_text = "END Sorry, something went wrong saving your message. Please try again later."
                    return PlainTextResponse(content=response_text)

                # Send SMS with phone normalization
                try:
                    normalized_recipient = normalize_phone_number(recipient_phone)
                    if not normalized_recipient.startswith("+256"):
                        normalized_recipient = "+256" + normalized_recipient.lstrip("0")

                    sms_message = (
                        f"CIVCON ALERT:\n"
                        f"New issue from {user.first_name or 'Citizen'} ({user.phone_number}).\n\n"
                        f"Message: {question}\n"
                        f"District: {user_district.capitalize()}"
                    )
                    await send_sms_async(phone=normalized_recipient, message=sms_message)
                    response_text = "END Thank you! Your message has been sent successfully to your MP."
                except Exception as e:
                    logger.error(f"SMS send failed: {e}")
                    response_text = (
                        "END Message saved but SMS failed to send. "
                        "Our team will ensure it reaches your MP."
                    )

                # Clear session
                await delete_session(session_id)
                return PlainTextResponse(content=response_text)

        else:
            response_text = "END Invalid session state. Please try again."
            await delete_session(session_id)

        # Save session if not ending
        session["data"] = user_data
        await save_session(session_id, session)
        return PlainTextResponse(content=response_text)

    except Exception as e:
        logger.error(f"USSD callback error: {e}", exc_info=True)
        await db.rollback()
        await delete_session(session_id)
        return PlainTextResponse(content="END Sorry, something went wrong. Please try again shortly.")