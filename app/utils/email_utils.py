from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
from pydantic import EmailStr
from app.config import settings

#  FastMail SMTP configuration
conf = ConnectionConfig(
    MAIL_USERNAME=settings.mail_username,       
    MAIL_PASSWORD=settings.mail_password,       
    MAIL_FROM=settings.mail_from,               
    MAIL_PORT=settings.mail_port,               
    MAIL_SERVER=settings.mail_server,           
    MAIL_STARTTLS=settings.mail_tls,            
    MAIL_SSL_TLS=settings.mail_ssl,             
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=True,
)



async def send_reset_email(email: EmailStr, reset_link: str):
    """
    Sends a password reset email to the specified user email.
    """
    message = MessageSchema(
        subject="Password Reset Request - CIVCON",
        recipients=[email],
        body=f"""
        <h3>Hello,</h3>
        <p>We received a password reset request for your account.</p>
        <p>Click below to reset your password:</p>
        <a href="{reset_link}" style="padding:10px 20px; background-color:#2563eb; color:white; text-decoration:none;">Reset Password</a>
        <br><br>
        <p>If you did not request this, please ignore this email.</p>
        <p>-- CIVCON Team</p>
        """,
        subtype="html",
    )

    fm = FastMail(conf)
    await fm.send_message(message)
