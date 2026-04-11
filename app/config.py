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

    # Anthropic API key for the clinical determination testing harness.
    # Used only by `tests/harness/` — not by the production engine.
    anthropic_api_key: str = ""

    # ---- Care Orchestration Engine parameters ----
    # Provider selection is certification-first: the engine does not rank
    # certified providers by a quality score or confidence interval. If a
    # provider is certified to deliver the service and can do so within
    # the convenience threshold, cost is the only tiebreaker.

    # Price drift tolerance between the routing decision's expected price
    # and the actual billed amount. Claims within this tolerance are paid
    # automatically; claims outside are flagged for fraud review.
    price_tolerance_pct: float = 0.10

    # How many days in advance to notify an employee about a scheduled
    # preventive care appointment created by the proactive scheduler.
    preventive_notification_lead_days: int = 14

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


# ---- Convenience thresholds for Layer 2 care routing ----
# Each urgency level maps to a maximum acceptable travel distance (miles)
# and maximum wait-for-appointment window (hours).
#
# travel_miles=None means no distance cap (emergent — closest available).
# appointment_hours=0 means immediate.
#
# These define the CONVENIENCE FLOOR: cost optimization can never push a
# patient below these limits. If no provider meets both the quality floor
# and these thresholds, the engine falls back to the best-available-within
# -convenience option (never sacrifice convenience for cost).
CONVENIENCE_THRESHOLDS: dict[str, dict] = {
    "emergent":       {"travel_miles": None, "appointment_hours": 0},
    "urgent":         {"travel_miles": 30,   "appointment_hours": 24},
    "time_sensitive": {"travel_miles": 45,   "appointment_hours": 24 * 7},
    "routine":        {"travel_miles": 45,   "appointment_hours": 24 * 30},
    "preventive":     {"travel_miles": 45,   "appointment_hours": 24 * 60},
    "specialty":      {"travel_miles": 60,   "appointment_hours": 24 * 14},
}


settings = Settings()
