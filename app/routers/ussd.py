from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime
from app.database import get_db
from app.models import User, Role, Message, MP
from app.schemas import Role as RoleEnum
from app.redis_client import get_redis
import json
import logging
import africastalking
import asyncio
from app.config import settings
import re

router = APIRouter(prefix="/ussd", tags=["USSD"])
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Africa's Talking credentials
AFRICASTALKING_USERNAME = settings.AFRICASTALKING_USERNAME
AFRICASTALKING_API_KEY = settings.AFRICASTALKING_API_KEY
africastalking.initialize(AFRICASTALKING_USERNAME, AFRICASTALKING_API_KEY)
sms = africastalking.SMS

# Supported languages
LANGUAGES = {
    "1": "EN", "2": "LG", "3": "RN", "4": "LU", "5": "SW", "6": "RT"
}

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
        "RN": "Yandikaho disitulikiti yawe:", "LU": "Ket district ni i:", "SW": "Weka eneo lako au wilaya:",
        "RT": "Andika district yo:"
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

def format_topics(language):
    return "\n".join([f"{i+1}. {topic}" for i, topic in enumerate(TOPICS[language])])

# Redis session helpers
async def save_session(session_id: str, session_data: dict, expire_seconds: int = 3600):
    redis = await get_redis()
    await redis.set(session_id, json.dumps(session_data), ex=expire_seconds)

async def load_session(session_id: str):
    redis = await get_redis()
    data = await redis.get(session_id)
    return json.loads(data) if data else None

async def delete_session(session_id: str):
    redis = await get_redis()
    await redis.delete(session_id)

# Normalize phone number to E.164 format (Uganda example)
def normalize_phone(phone: str) -> str:
    phone = re.sub(r"[^\d]", "", phone)
    if phone.startswith("0"):
        phone = "+256" + phone[1:]
    elif phone.startswith("256"):
        phone = "+" + phone
    elif not phone.startswith("+256"):
        phone = "+256" + phone
    return phone

# Async-safe SMS sender
async def send_sms_to_mp(message: Message, phone_number: str):
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: sms.send(message=message.content, recipients=[phone_number])
        )
        logger.info(f"Message sent to {phone_number}")
    except Exception as e:
        logger.error(f"Failed to send SMS: {e}")

# USSD callback endpoint
@router.post("/ussd_callback")
async def ussd_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        data = await request.json()
        session_id = data.get("sessionId")
        phone = normalize_phone(data.get("phoneNumber"))
        text = data.get("text", "").strip()
        levels = text.split("*") if text else []

        # Load or initialize session
        session = await load_session(session_id) or {"step": "start", "language": "EN", "data": {}, "level_index": 0}
        step = session["step"]
        language = session.get("language", "EN")
        user_data = session["data"]
        level_index = session.get("level_index", 0)

        # Check if user exists (returning user)
        result = await db.execute(select(User).where(User.phone_number == phone))
        user = result.scalar_one_or_none()

        # START or CONSENT step for new users
        if not user:
            if step in ["start", "consent"]:
                if not text:
                    session["step"] = "consent"
                    response_text = f"CON {WELCOME_MSG[language]}\n1. Consent to terms\n0. End"
                elif text == "1":
                    session["step"] = "language"
                    response_text = "CON Choose language:\n1. English\n2. Luganda\n3. Runyankole\n4. Luo/Acholi\n5. Swahili\n6. Rutoro"
                else:
                    response_text = "END Consent required to continue."
            elif step == "language":
                lang_choice = levels[level_index] if len(levels) > level_index else "1"
                chosen_lang = LANGUAGES.get(lang_choice, "EN")
                session.update({"language": chosen_lang, "step": "register_name"})
                response_text = f"CON {PROMPTS['register_name'][chosen_lang]}"
                session["level_index"] += 1
            elif step == "register_name":
                name_input = levels[level_index] if len(levels) > level_index else None
                if not name_input:
                    response_text = f"CON {PROMPTS['register_name'][language]}"
                else:
                    user_data["name"] = name_input
                    session["step"] = "register_district"
                    response_text = f"CON {PROMPTS['register_district'][language]}"
                    session["level_index"] += 1
            elif step == "register_district":
                district_input = levels[level_index] if len(levels) > level_index else None
                if not district_input:
                    response_text = f"CON {PROMPTS['register_district'][language]}"
                else:
                    user_data["district"] = district_input
                    session["step"] = "topic_menu"
                    response_text = f"CON {PROMPTS['ask_topic'][language]}{format_topics(language)}"
                    session["level_index"] += 1
        else:
            # Returning user skips consent and registration
            session["step"] = "topic_menu"
            if "district" not in user_data:
                user_data["district"] = user.district_id

        # TOPIC MENU
        if session["step"] == "topic_menu":
            topic_choice = levels[level_index] if len(levels) > level_index else None
            topics_list = TOPICS[language]
            if topic_choice and topic_choice.isdigit() and 1 <= int(topic_choice) <= len(topics_list):
                user_data["topic"] = topics_list[int(topic_choice) - 1]
                session["step"] = "ask_question"
                response_text = f"CON {PROMPTS['question'][language]}"
                session["level_index"] += 1
            else:
                response_text = f"CON Invalid choice. {PROMPTS['ask_topic'][language]}{format_topics(language)}"

        # ASK QUESTION
        elif session["step"] == "ask_question":
            question_input = levels[level_index] if len(levels) > level_index else None
            if not question_input:
                response_text = f"CON {PROMPTS['question'][language]}"
            else:
                user_data["question"] = question_input[:160]  # Limit to 160 chars

                # Create user if not exists
                if not user:
                    names = user_data["name"].split(" ")
                    first_name = names[0]
                    last_name = names[-1] if len(names) > 1 else ""
                    user = User(
                        first_name=first_name,
                        last_name=last_name,
                        phone_number=phone,
                        district_id=user_data["district"],
                        role=RoleEnum.CITIZEN,
                        preferred_language=language
                    )
                    db.add(user)
                    await db.commit()
                    await db.refresh(user)

                # Find MP for this district
                mp_result = await db.execute(select(MP).where(MP.district_id == user_data["district"]))
                mp = mp_result.scalar_one_or_none()

                # Determine recipient
                recipient_number = mp.phone_number if mp else settings.DEFAULT_CIVIC_OFFICE_NUMBER

                # Save message in DB
                message = Message(
                    sender_id=user.id,
                    recipient_id=None,  # can be updated if MP user exists
                    content=user_data["question"],
                    district_id=user_data["district"],
                    created_at=datetime.utcnow()
                )
                db.add(message)
                await db.commit()
                await db.refresh(message)

                # Send SMS
                await send_sms_to_mp(message, recipient_number)

                response_text = "END Thank you! Your issue has been submitted to your MP."
                await delete_session(session_id)

        # Save session progress
        await save_session(session_id, session)
        return {"response": response_text}

    except Exception as e:
        logger.error(f"USSD callback error: {e}")
        return {"response": "END An error occurred. Please try again later."}
