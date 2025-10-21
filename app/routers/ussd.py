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

WELCOME_MSG = {
    "EN": "Welcome to CIVCON! Raise civic issues with your MP.",
    "LG": "Tukwaniriza ku CIVCON! Wandiika obuzibu bwo eri MP wo.",
    "RN": "Okwanjwa ku CIVCON! Tegereza ebizibu byo eri MP wawe.",
    "LU": "Mabero ku CIVCON! Wek ayie gi MP mamegi.",
    "SW": "Karibu CIVCON! Toa hoja zako kwa mbunge wako.",
    "RT": "Tukwaniriza kuCIVCON! Wandiika ebizibu byo eri MP wawe.",
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

# Cache MPs in Redis for performance
async def get_mps(db: AsyncSession):
    redis = await get_redis()
    cached_mps = await redis.get("all_mps")
    if cached_mps:
        return [MP(**m) for m in json.loads(cached_mps)]
    result = await db.execute(select(MP))
    mps = result.scalars().all()
    await redis.set("all_mps", json.dumps([{"id": m.id, "user_id": m.user_id, "district_id": m.district_id, "phone_number": m.phone_number} for m in mps]), ex=1800)
    return mps

#  MAIN USSD CALLBACK 
@router.post("/ussd_callback")
async def ussd_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        # Load request data
        content_type = request.headers.get("content-type", "")
        data = await (request.json() if "application/json" in content_type else request.form())
        data = dict(data)

        session_id = data.get("sessionId")
        phone_number = normalize_phone_number(data.get("phoneNumber"))
        text = data.get("text", "").strip()
        user_response = text.split("*") if text else []

        logger.info(f"USSD request: {data}")

        # Load or create session
        session = await load_session(session_id) or {"step": "consent", "language": "EN", "data": {}}
        step = session.get("step")
        language = session.get("language", "EN")
        user_data = session.get("data", {})

        #  CONSENT 
        if step == "consent":
            if not user_response or user_response[-1] != "1":
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON {WELCOME_MSG['EN']}\nDo you consent?\n1. Yes\n0. No")
            session["step"] = "select_language"

        #  LANGUAGE SELECTION 
        if step == "select_language":
            lang_choice = user_response[-1] if user_response else None
            if lang_choice in LANGUAGES:
                language = LANGUAGES[lang_choice]
                session["language"] = language
                session["step"] = "register_name"
            else:
                await save_session(session_id, session)
                return PlainTextResponse(content="CON Invalid choice.\nPlease select language:\n1. English\n2. Luganda\n3. Runyankore\n4. Lango\n5. Swahili\n6. Rutooro")

        #  REGISTER NAME 
        if step == "register_name":
            if not user_response:
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON {PROMPTS['register_name'][language]}")
            user_data["name"] = user_response[-1]
            session["data"] = user_data
            session["step"] = "register_district"

        #  REGISTER DISTRICT 
        if step == "register_district":
            if not user_response:
                await save_session(session_id, session)
                return PlainTextResponse(content=f"CON {PROMPTS['register_district'][language]}")
            user_data["district"] = user_response[-1].title()
            session["data"] = user_data
            # Save new user
            names = user_data["name"].split(" ")
            new_user = User(
                first_name=names[0],
                last_name=names[-1] if len(names) > 1 else "",
                phone_number=phone_number,
                district_id=user_data["district"],
                is_active=True,
                role=RoleEnum.CITIZEN,
                preferred_language=language,
            )
            db.add(new_user)
            await db.commit()
            await db.refresh(new_user)
            session["step"] = "topic_menu"

        #  TOPIC SELECTION & QUESTION 
        if session["step"] in ["topic_menu", "ask_question"]:
            # Ensure user exists
            result = await db.execute(select(User).where(User.phone_number == phone_number))
            user = result.scalars().first()
            if not user:
                return PlainTextResponse(content="END User not found. Please register again.")

            language = user.preferred_language or language
            session["language"] = language
            topics_list = TOPICS[language]

            if session["step"] == "topic_menu":
                if not user_response:
                    await save_session(session_id, session)
                    return PlainTextResponse(content=f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}")
                choice = user_response[-1]
                if choice.isdigit() and 1 <= int(choice) <= len(topics_list):
                    user_data["topic"] = topics_list[int(choice)-1]
                    session["data"] = user_data
                    session["step"] = "ask_question"
                    await save_session(session_id, session)
                    return PlainTextResponse(content=f"CON {PROMPTS['question'][language]}")
                else:
                    await save_session(session_id, session)
                    return PlainTextResponse(content=f"CON Invalid choice.\n{PROMPTS['ask_topic'][language]}{format_topics(language)}")

            elif session["step"] == "ask_question":
                if not user_response:
                    await save_session(session_id, session)
                    return PlainTextResponse(content=f"CON {PROMPTS['question'][language]}")
                question = user_response[-1][:160]

                # Determine MP recipient
                mps = await get_mps(db)
                user_district = (user.district_id or "").lower().replace("district","").strip()
                mp = next((m for m in mps if user_district in (m.district_id or "").lower().replace("district","").strip()), None)
                recipient_id = mp.id if mp else None
                recipient_phone = mp.phone_number if mp else "+256784437652"

                # Save message
                msg = Message(
                    sender_id=user.id,
                    recipient_id=recipient_id,
                    content=question,
                    district_id=user.district_id,
                    created_at=datetime.utcnow(),
                    mp_id=recipient_id
                )
                db.add(msg)
                await db.commit()

                # Send SMS
                normalized_recipient = normalize_phone_number(recipient_phone)
                if not normalized_recipient.startswith("+256"):
                    normalized_recipient = "+256" + normalized_recipient.lstrip("0")
                await send_sms_async(
                    phone=normalized_recipient,
                    message=f"New message from {user.first_name or 'a citizen'} ({user.district_id}): {question}"
                )

                # End session
                await delete_session(session_id)
                return PlainTextResponse(content="END Thank you! Your message has been sent to your MP or civic office.")

        # Save session at the end
        await save_session(session_id, session)
        return PlainTextResponse(content=f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}")

    except Exception as e:
        logger.error(f"USSD callback error: {e}", exc_info=True)
        return PlainTextResponse(content="END Sorry, an error occurred. Please try again.")
