from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
import africastalking
import logging
from dotenv import load_dotenv

from app.database import get_db
from app.models import User, Message, Role
from app.schemas import USSDResponse
from app.spam_detector import detector
from app.config import settings
from app.session.ussd_session import get_session, create_or_update_session, delete_session

# Load environment & logging
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Router
router = APIRouter(prefix="/ussd", tags=["ussd"])

# Africa’s Talking Init
username = settings.AFRICASTALKING_USERNAME
api_key = settings.AFRICASTALKING_API_KEY
africastalking.initialize(username, api_key)
sms = africastalking.SMS

# Supported languages
LANGUAGES = {
    "1": "EN",  # English
    "2": "LG",  # Luganda
    "3": "RN",  # Runyankole
    "4": "LU",  # Luo/Acholi
    "5": "SW",  # Swahili
    "6": "RT",  # Rutoro
}

# Welcome messages per language
WELCOME_MSG = {
    "EN": "CON Welcome to CIVCON! Raise civic issues with your MP.\n1. Consent to terms\n0. End",
    "LG": "CON Tukusanyize mu CIVCON! Tuma eby'obulamuzi ku MP wo.\n1. Kutegeera eby'okukola\n0. Sawa",
    "RN": "CON Tukusanyize mu CIVCON! Tuma ebibuuzo ku MP.\n1. Eby'okwemanya\n0. Okurangiza",
    "LU": "CON Wacwiny mu CIVCON! Tuma eby'obulamuzi ku MP.\n1. Kwecoba\n0. Kweno",
    "SW": "CON Karibu CIVCON! Tuma maswali kwa MP wako.\n1. Ridha masharti\n0. Mwisho",
    "RT": "CON Nkwatire mu CIVCON! Tuma eby'okubaza ku MP.\n1. Okutegereza\n0. Kureka",
}

# Topics menu per language
TOPICS_MENU = {
    "EN": ["1. Human Capital Development", "2. Health", "3. Infrastructure", "4. Education", "5. Politics", "0. Back"],
    "LG": ["1. Obukyala", "2. Obulamu", "3. Eby'obutale", "4. Obuyigirize", "5. Pulitiki", "0. Okudda"],
    "RN": ["1. Obukyala", "2. Obulamu", "3. Enkora", "4. Obuyigirize", "5. Pulitiki", "0. Okudda"],
    "LU": ["1. Human Capital", "2. Health", "3. Infrastructure", "4. Education", "5. Politics", "0. Back"],
    "SW": ["1. Elimu", "2. Afya", "3. Miundombinu", "4. Elimu ya Juu", "5. Siasa", "0. Nyuma"],
    "RT": ["1. Ebyobukyala", "2. Obulamu", "3. Enzibagiriro", "4. Obuyigirize", "5. Pulitiki", "0. Okudda"],
}

def format_topics(language):
    return "\n".join(TOPICS_MENU.get(language, TOPICS_MENU["EN"]))

# Helper functions
async def get_mps_by_district(db: AsyncSession, district_id: str):
    result = await db.execute(
        select(User).where(User.role == Role.MP, User.district_id == district_id)
    )
    return result.scalars().all()

# USSD handler
@router.post("/", response_model=USSDResponse)
async def ussd_callback(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    session_id = body["sessionId"]
    phone_number = body["phoneNumber"]
    text = body["text"]

    # Load or initialize session
    db_session = await get_session(db, session_id, phone_number)
    if db_session:
        session = {
            "step": db_session.current_step,
            "data": db_session.user_data or {},
            "language": db_session.language or "EN"
        }
    else:
        session = {"step": "consent", "data": {}, "language": "EN"}

    step = session["step"]
    data = session["data"]
    language = session.get("language", "EN")
    response_text = ""

    levels = text.split("*") if text else []

    # Flow Logic 
    if step == "consent":
        if levels and levels[-1] == "1":
            response_text = (
                "CON Consent accepted. Choose language:\n"
                "1. English\n2. Luganda\n3. Runyankole\n4. Luo/Acholi\n5. Swahili\n6. Rutoro"
            )
            session["step"] = "language"
        else:
            response_text = "END Thank you for using CIVCON. Consent required to continue."

    elif step == "language":
        lang_choice = levels[-1] if levels else "1"
        chosen_lang = LANGUAGES.get(lang_choice, "EN")
        session.update({"language": chosen_lang, "step": "register_name"})
        response_text = f"CON Language selected: {chosen_lang}. Enter your name:"

    elif step == "register_name":
        data["name"] = levels[-1] if levels else ""
        session["step"] = "register_district"
        response_text = "CON Enter your district or constituency:"

    elif step == "register_district":
        district = levels[-1].strip() if levels else ""
        if district:
            data["district"] = district
            data["district_id"] = district

            # Check or create user
            result = await db.execute(select(User).where(User.email == f"ussd_{phone_number}@civcon.ug"))
            existing_user = result.scalars().first()

            if not existing_user:
                user = User(
                    first_name=data["name"],
                    last_name="Citizen",
                    email=f"ussd_{phone_number}@civcon.ug",
                    hashed_password=None,
                    role=Role.CITIZEN,
                    district_id=district,
                    privacy_level="public"
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)
                session["user_id"] = user.id
            else:
                session["user_id"] = existing_user.id

            session["step"] = "topic_menu"
            response_text = f"CON Registration complete. Ask question:\n{format_topics(language)}"
        else:
            response_text = "CON Invalid district. Please enter your district or constituency:"

    elif step == "topic_menu":
        # Localized topic map
        localized_topics = TOPICS_MENU.get(language, TOPICS_MENU["EN"])
        topic_map = {str(i + 1): t.split(". ")[1] for i, t in enumerate(localized_topics[:-1])}

        topic_choice = levels[-1] if levels else ""
        topic = topic_map.get(topic_choice)
        if topic:
            data["topic"] = topic
            session["step"] = "question"
            response_text = f"CON Topic: {topic}. Enter your question (max 160 chars):"
        else:
            response_text = f"CON Invalid choice. {format_topics(language)}"

    elif step == "question":
        question = levels[-1] if levels else ""
        if len(question) > 160:
            response_text = "CON Question too long. Try again:"
        elif detector(question, language):
            response_text = "END Your question contains inappropriate content. Please try again."
        else:
            data["question"] = question
            mps = await get_mps_by_district(db, data.get("district_id", phone_number))
            if not mps:
                response_text = "END No MP found for your district. Contact support."
            else:
                mp = mps[0]
                message = Message(
                    sender_id=session["user_id"],
                    recipient_id=mp.id,
                    content=f"Topic: {data['topic']}\nQuestion: {question}\nFrom: {data['name']} in {data['district']}",
                    district_id=data.get("district_id")
                )
                db.add(message)
                await db.commit()
                response_text = f"END Question sent to MP {mp.first_name} {mp.last_name} about {data['topic']}. Thank you!"
            session["step"] = "end"

    else:
        response_text = "END An error occurred. Please try again."

    # Save or delete session
    await create_or_update_session(db, session_id, phone_number, session["step"], session["data"], language)
    if session["step"] == "end":
        await delete_session(db, session_id)

    # Optional SMS notification
    try:
        sms.send(message=f"CIVCON update: {response_text}", recipients=[phone_number])
    except Exception as e:
        logger.error(f"Africa’s Talking SMS error: {e}")

    return USSDResponse(response=response_text)
