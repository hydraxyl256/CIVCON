import ssl
import requests
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
import africastalking
from app.config import settings

# --- Force TLS 1.2 globally ---
class TLS12HttpAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

s = requests.Session()
s.mount("https://", TLS12HttpAdapter())
requests.sessions.Session.request = s.request
print("🔐 Forced all HTTPS to use TLS 1.2")

USERNAME = settings.AFRICASTALKING_USERNAME or "CIVCON"
API_KEY = settings.AFRICASTALKING_API_KEY
RECIPIENT = ["+256700960695"]   
MESSAGE = "Hello from CIVCON USSD Test!"

def send_sms(username, api_key):
    africastalking.initialize(username, api_key)
    sms = africastalking.SMS
    print(f"✅ Initialized SDK for {username}")
    return sms.send(MESSAGE, RECIPIENT)

try:
    print(f"🧪 Trying sandbox first...")
    print(send_sms(USERNAME, API_KEY))
except Exception as e:
    print(f"⚠️ Sandbox failed: {e}")
    print("🔁 Retrying with live API endpoint...")

    # Switch to live account 
    LIVE_USERNAME = settings.AFRICASTALKING_USERNAME
    LIVE_API_KEY = settings.AFRICASTALKING_API_KEY
    try:
        print(send_sms(LIVE_USERNAME, LIVE_API_KEY))
    except Exception as e2:
        print(f"❌ Live API also failed: {e2}")
