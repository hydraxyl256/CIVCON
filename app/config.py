from pydantic_settings import BaseSettings, SettingsConfigDict

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
