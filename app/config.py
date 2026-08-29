"""Application settings, loaded from the environment.

No credential is ever read from a file that could be committed. The only
sources are process environment variables and a gitignored local .env.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LinkedIn session -------------------------------------------------
    linkedin_li_at: str = ""
    linkedin_jsessionid: str = ""
    linkedin_profile_components_query_id: str = ""
    linkedin_profile_components_verified_on: date | None = None
    linkedin_rsc_application_version: str = "0.2.7003"

    # --- API access -------------------------------------------------------
    api_keys: str = ""

    # --- Behaviour --------------------------------------------------------
    cache_ttl_seconds: int = 900
    min_request_interval_seconds: float = 2.5
    enable_public_fallback: bool = True
    log_level: str = "INFO"

    # --- Per-caller rate limiting -----------------------------------------
    # The Pacer above throttles this process's total outbound volume to
    # LinkedIn; it does not stop one caller from consuming that entire
    # shared budget alone by requesting many distinct profiles. This caps
    # each API key independently.
    rate_limit_per_minute: int = 20
    rate_limit_window_seconds: float = 60.0

    # --- Client identity --------------------------------------------------
    # Kept in config rather than hardcoded because it is a maintenance
    # surface: it drifts with LinkedIn's real client and a stale value is a
    # detection signal in its own right.
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        )
    )
    li_client_version: str = "1.13.28361"

    @field_validator("linkedin_jsessionid")
    @classmethod
    def _keep_quotes(cls, v: str) -> str:
        """JSESSIONID carries literal double quotes in the cookie value.

        Some shells and secret managers strip them on the way in. We restore
        them here so the constructed cookie header preserves LinkedIn's expected quoting, and
        derive the unquoted csrf-token separately in the session layer.
        """
        if not v:
            return v
        v = v.strip()
        if not v.startswith('"'):
            v = f'"{v}"'
        return v

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())

    @property
    def has_session(self) -> bool:
        """Whether an authenticated tier is even possible.

        False is a supported operating mode: the service still answers from
        the public fallback tier, and says so in the response envelope.
        """
        return bool(self.linkedin_li_at and self.linkedin_jsessionid)


@lru_cache
def get_settings() -> Settings:
    return Settings()
