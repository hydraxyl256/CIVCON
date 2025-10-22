from pydantic_settings import BaseSettings, SettingsConfigDict
import os

class Settings(BaseSettings):
    database_hostname: str
    database_port: str
    database_password: str
    database_name: str
    database_username: str
    secret_key: str
    algorithm: str
    access_token_expire_minutes: int
    google_client_id: str
    google_client_secret: str
    linkedin_client_id: str
    linkedin_client_secret: str
    AFRICASTALKING_USERNAME: str 
    AFRICASTALKING_API_KEY: str
    DEFAULT_CIVIC_OFFICE_NUMBER: str = os.getenv("DEFAULT_CIVIC_OFFICE_NUMBER")
    mail_username: str
    mail_password: str
    mail_from: str
    mail_port: int = 587
    mail_server: str
    mail_tls: bool = True
    mail_ssl: bool = False
    redis_url: str
    frontend_url: str
    cloudinary_cloud_name: str
    cloudinary_api_key: str
    cloudinary_api_secret: str
    session_secret_key: str
    frontend_url: str = "https://civ-con-sh2j.vercel.app/"  
    backend_url: str = "https://civcon.onrender.com/"
    FALLBACK_PHONE: str = "+256784437652"
    FALLBACK_MP_ID: int = 15

    


    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        """Build the async PostgreSQL connection URL dynamically."""
        return (
            f"postgresql+asyncpg://{self.database_username}:"
            f"{self.database_password}@{self.database_hostname}:"
            f"{self.database_port}/{self.database_name}"
        )

settings = Settings()
