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


# Helper Functions
async def get_mps_by_district(db: AsyncSession, district_id: str):
    result = await db.execute(
        select(User).where(User.role == Role.MP, User.district_id == district_id)
    )
    return result.scalars().all()


def get_topics_menu() -> str:
    topics = [
        "1. Human Capital Development",
        "2. Health",
        "3. Infrastructure",
        "4. Education",
        "5. Politics",
        "0. Back"
    ]
    return "\n".join(topics)


# USSD Handler
@router.post("/", response_model=USSDResponse)
async def ussd_callback(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    session_id = body["sessionId"]
    phone_number = body["phoneNumber"]
    text = body["text"]

    logger.info(f"USSD request from {phone_number}, session {session_id}, text: {text}")

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
    response_text = ""

    # Flow Logic
    if text == "":
        response_text = "CON Welcome to CIVCON! Raise civic issues with your MP.\n1. Consent to terms\n0. End"
    else:
        levels = text.split("*")

        if step == "consent":
            if levels[-1] == "1":
                response_text = "CON Consent accepted. Choose language:\n1. English (EN)\n2. Luganda (LG)"
                session["step"] = "language"
            else:
                response_text = "END Thank you for using CIVCON. Consent required to continue."

        elif step == "language":
            if levels[-1] == "1":
                session.update({"language": "EN", "step": "register_name"})
                response_text = "CON Language: English. Enter your name:"
            elif levels[-1] == "2":
                session.update({"language": "LG", "step": "register_name"})
                response_text = "CON Olulimi: Luganda. Yita erinnya lyo:"
            else:
                response_text = "CON Invalid choice. Choose language:\n1. English\n2. Luganda"

        elif step == "register_name":
            data["name"] = levels[-1]
            session["step"] = "register_district"
            response_text = (
                "CON Name saved. Select district:\n1. Central\n2. Eastern\n3. Northern\n4. Western\n0. Back"
            )

        elif step == "register_district":
            district_map = {"1": "Central", "2": "Eastern", "3": "Northern", "4": "Western"}
            district = district_map.get(levels[-1])

            if district:
                data["district"] = district
                data["district_id"] = district

                # Check or create user
                result = await db.execute(
                    select(User).where(User.email == f"ussd_{phone_number}@civcon.ug")
                )
                existing_user = result.scalars().first()

                if not existing_user:
                    user = User(
                        first_name=data["name"],
                        last_name="Citizen",
                        email=f"ussd_{phone_number}@civcon.ug",
                        hashed_password=None,
                        role=Role.CITIZEN,
                        region=district,
                        district_id=phone_number,
                        privacy_level="public"
                    )
                    db.add(user)
                    await db.commit()
                    await db.refresh(user)
                    session["user_id"] = user.id
                else:
                    session["user_id"] = existing_user.id

                session["step"] = "topic_menu"
                response_text = f"CON Registration complete for {data['name']} in {district}. Ask question:\n{get_topics_menu()}"
            else:
                response_text = "CON Invalid district. Select:\n1. Central\n2. Eastern\n3. Northern\n4. Western\n0. Back"

        elif step == "topic_menu":
            topic_map = {
                "1": "Human Capital Development",
                "2": "Health",
                "3": "Infrastructure",
                "4": "Education",
                "5": "Politics"
            }
            topic = topic_map.get(levels[-1])
            if topic:
                data["topic"] = topic
                session["step"] = "question"
                response_text = f"CON Topic: {topic}. Enter your question (max 160 chars):"
            elif levels[-1] == "0":
                session["step"] = "consent"
                response_text = "CON Main menu:\n1. Consent (skip if done)\n0. End"
            else:
                response_text = f"CON Invalid. {get_topics_menu()}"

        elif step == "question":
            question = levels[-1]
            if len(question) > 160:
                response_text = "CON Question too long. Try again:"
            else:
                data["question"] = question
                is_spam, score = detector.predict_spam(question)

                if is_spam or score > 0.7:
                    response_text = "END Your message contains inappropriate content. Please try again."
                else:
                    mps = await get_mps_by_district(db, data.get("district_id", phone_number))
                    if not mps:
                        response_text = "END No MP found for your district. Contact support."
                    else:
                        mp = mps[0]
                        message = Message(
                            sender_id=session["user_id"],
                            recipient_id=mp.id,
                            content=f"Topic: {data['topic']}\nQuestion: {question}\nFrom: {data['name']} in {data['district']}",
                            district_id=data.get("district_id", phone_number)
                        )
                        db.add(message)
                        await db.commit()

                        response_text = f"END Question sent to MP {mp.first_name} {mp.last_name} about {data['topic']}. Thank you!"

                session["step"] = "end"

        else:
            response_text = "END An error occurred. Please try again."

    # Save or Delete session
    await create_or_update_session(db, session_id, phone_number, session["step"], session["data"], session.get("language", "EN"))
    if session["step"] == "end":
        await delete_session(db, session_id)

    # Optional SMS Notification
    try:
        sms.send(message=f"CIVCON update: {response_text}", recipients=[phone_number])
    except Exception as e:
        logger.error(f"Africa’s Talking SMS error: {e}")

    return USSDResponse(response=response_text)
