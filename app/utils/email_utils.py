import os
import httpx
import logging
from typing import Optional, Dict, Any
from jinja2 import Environment, FileSystemLoader, select_autoescape
from datetime import datetime
from app.config import settings

logger = logging.getLogger("email_utils")

# Environment setup 
RESEND_API_URL = "https://api.resend.com/emails"
RESEND_API_KEY = settings.RESEND_API_KEY 
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "CIV-CON <no-reply@civcon.org>")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates")

env = Environment(
    loader=FileSystemLoader(TEMPLATE_PATH),
    autoescape=select_autoescape(["html", "xml"])
)

if not RESEND_API_KEY:
    logger.warning(" RESEND_API_KEY is not set! Emails will fail.")



#  Helper — render Jinja2 template
def render_email_template(template_name: str, context: Dict[str, Any]) -> str:
    try:
        template = env.get_template(template_name)
        return template.render(**context, year=datetime.utcnow().year)
    except Exception as e:
        logger.exception(f"Template rendering failed for {template_name}: {e}")
        raise



#  Async Resend email sender
async def send_email(
    to_email: str,
    subject: str,
    html_content: str,
    text_content: Optional[str] = None,
):
    if not RESEND_API_KEY:
        logger.error("No Resend API key found, cannot send email")
        return

    payload = {
        "from": SENDER_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html_content,
    }

    if text_content:
        payload["text"] = text_content

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                RESEND_API_URL,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json=payload,
            )
            response.raise_for_status()
        logger.info(f"✅ Email sent to {to_email}")
    except httpx.HTTPStatusError as e:
        logger.error(f" Resend API error: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        logger.exception(f" Unexpected error sending email to {to_email}: {e}")



#  Background-safe email sender
def send_email_background(to_email: str, subject: str, body: str):
    if not RESEND_API_KEY:
        logger.error("RESEND_API_KEY not configured. Cannot send background email.")
        return

    try:
        html_content = render_email_template(
            "generic_message.html",
            {"subject": subject, "body": body, "name": "User"},
        )
    except Exception:
        html_content = f"<html><body><pre>{body}</pre></body></html>"

    payload = {
        "from": SENDER_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html_content,
        "text": body,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                RESEND_API_URL,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json=payload,
            )
            response.raise_for_status()
        logger.info(f"📧 Background email sent to {to_email}")
    except httpx.HTTPStatusError as e:
        logger.error(
            f"Failed to send background email to {to_email}: "
            f"{e.response.status_code} - {e.response.text}"
        )
    except Exception as e:
        logger.exception(f"Error sending background email: {e}")



#  Password Reset Email
async def send_reset_email(to_email: str, reset_link: str, name: Optional[str] = None):
    html_content = render_email_template(
        "reset_password.html",
        {"reset_link": reset_link, "name": name or "User"},
    )
    text_content = f"Hello {name or 'User'},\n\nReset your password here: {reset_link}\nThis link expires in 30 minutes."

    await send_email(to_email, "CIV-CON Password Reset Request", html_content, text_content)
