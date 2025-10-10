"""
Africa's Talking service module for CIVCON
-------------------------------------------
Handles SMS and (optionally) USSD responses with TLS 1.2 enforcement
and centralized error handling.

Usage:
    from app.services.africastalking_service import send_sms
"""

import ssl
import logging
import africastalking
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
import requests
from app.config import settings



# Logging setup
logger = logging.getLogger("africastalking")
logger.setLevel(logging.INFO)



# TLS 1.2 enforcement for all HTTPS requests
class TLS12HttpAdapter(HTTPAdapter):
    """Force TLS v1.2 for requests used by africastalking SDK."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# Patch requests globally before africastalking SDK is used
_session = requests.Session()
_session.mount("https://", TLS12HttpAdapter())
requests.sessions.Session.request = _session.request
logger.info(" Enforced TLS 1.2 for Africa's Talking requests.")


# Africa's Talking Initialization
USERNAME = settings.LIVE_USERNAME 
API_KEY = settings.LIVE_API_KEY

try:
    africastalking.initialize(USERNAME, API_KEY)
    sms = africastalking.SMS
    logger.info(f"Africa's Talking initialized successfully for user '{USERNAME}'.")
except Exception as e:
    logger.error(f" Failed to initialize Africa's Talking: {e}")
    sms = None



# SMS sending helper
def send_sms(message: str, recipients: list[str]) -> dict:
    """
    Send SMS via Africa's Talking.

    Args:
        message (str): Message text.
        recipients (list[str]): List of phone numbers (in +256... format).

    Returns:
        dict: Response from Africa's Talking or an error structure.
    """
    if not sms:
        logger.error("Africa's Talking SMS service not initialized.")
        return {"status": "error", "detail": "SMS service unavailable"}

    try:
        logger.info(f"📨 Sending SMS to {recipients} | Message: {message}")
        response = sms.send(message, recipients)
        logger.info(f" SMS sent successfully: {response}")
        return {"status": "success", "response": response}
    except Exception as e:
        logger.error(f" SMS sending failed: {e}")
        return {"status": "error", "detail": str(e)}



# mock USSD reply helper (for internal debugging)
def ussd_reply(message: str, end: bool = False) -> str:
    """
    Format USSD response text.
    Africa's Talking expects:
        - 'CON ' prefix for continuation
        - 'END ' prefix to end session
    """
    prefix = "END" if end else "CON"
    return f"{prefix} {message}"
