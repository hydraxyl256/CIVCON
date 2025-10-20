import os
import httpx
import logging
from typing import Optional

logger = logging.getLogger("email_utils")

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "CIV-CON <no-reply@civcon.org>")

if not RESEND_API_KEY:
    logger.warning("RESEND_API_KEY is not set! Emails will fail.")


async def send_email(
    to_email: str,
    subject: str,
    html_content: str,
    text_content: Optional[str] = None,
):
    """
    Send an email via Resend API.

    Args:
        to_email: Recipient email address
        subject: Email subject
        html_content: HTML content of the email
        text_content: Optional plain text fallback
    """
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
        logger.info(f"Email sent to {to_email} successfully.")
    except httpx.HTTPStatusError as e:
        logger.error(
            f"Failed to send email to {to_email}. Status: {e.response.status_code}, Body: {e.response.text}"
        )
        raise
    except Exception as e:
        logger.exception(f"Unexpected error sending email to {to_email}: {e}")
        raise


async def send_reset_email(to_email: str, reset_link: str):
    """
    Sends a password reset email to the user.

    Args:
        to_email: Recipient email
        reset_link: URL for resetting password
    """
    subject = "CIV-CON Password Reset Request"
    html_content = f"""
    <html>
    <body>
        <p>Hello,</p>
        <p>You requested a password reset for your CIV-CON account. Click the link below to reset your password:</p>
        <p><a href="{reset_link}" target="_blank">{reset_link}</a></p>
        <p>This link will expire in 30 minutes.</p>
        <p>If you did not request this, please ignore this email.</p>
        <br/>
        <p>— CIV-CON Team</p>
    </body>
    </html>
    """
    text_content = f"Reset your password using this link: {reset_link}"

    await send_email(to_email, subject, html_content, text_content)
