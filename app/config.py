import os
from pathlib import Path

from pydantic_settings import BaseSettings

_APP_DIR = Path(__file__).resolve().parent.parent  # beneflex/


class Settings(BaseSettings):
    # Database
    database_url: str = f"sqlite:///{_APP_DIR / 'beneflex.db'}"

    # Auth0
    auth0_domain: str = ""
    auth0_api_audience: str = ""
    auth0_client_id: str = ""
    auth0_client_secret: str = ""

    # Application
    app_env: str = "development"
    app_secret_key: str = "change-me"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # Encryption key for PII/PHI fields (maps to AWS KMS in Phase 4)
    encryption_key: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


settings = Settings()
