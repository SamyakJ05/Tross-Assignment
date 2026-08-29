"""Request-only models. Credential fields are never part of a response model."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.config import Settings


class ProfileRequestCredentials(BaseModel):
    """One caller-owned LinkedIn session, used for exactly one API request."""

    model_config = ConfigDict(populate_by_name=True)

    linkedin_li_at: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LINKEDIN_LI_AT", "linkedin_li_at"),
        serialization_alias="LINKEDIN_LI_AT",
        description=(
            "LinkedIn li_at cookie value. Required with LINKEDIN_JSESSIONID without a header."
        ),
        json_schema_extra={"writeOnly": True},
    )
    linkedin_jsessionid: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LINKEDIN_JSESSIONID", "linkedin_jsessionid"),
        serialization_alias="LINKEDIN_JSESSIONID",
        description="LinkedIn JSESSIONID value. Required with LINKEDIN_LI_AT without a header.",
        json_schema_extra={"writeOnly": True},
    )
    linkedin_cookie_header: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LINKEDIN_COOKIE_HEADER", "linkedin_cookie_header"),
        serialization_alias="LINKEDIN_COOKIE_HEADER",
        description="Full Cookie request-header value. Recommended for current RSC profile cards.",
        json_schema_extra={"writeOnly": True},
    )

    @model_validator(mode="after")
    def _has_complete_session(self) -> ProfileRequestCredentials:
        if self.linkedin_cookie_header or (self.linkedin_li_at and self.linkedin_jsessionid):
            return self
        raise ValueError(
            "Provide LINKEDIN_COOKIE_HEADER, or provide both LINKEDIN_LI_AT and "
            "LINKEDIN_JSESSIONID."
        )

    def apply_to(self, settings: Settings) -> Settings:
        """Create ephemeral settings without mutating process-wide configuration."""
        li_at = self.linkedin_li_at.get_secret_value() if self.linkedin_li_at else ""
        jsessionid = self.linkedin_jsessionid.get_secret_value() if self.linkedin_jsessionid else ""
        if jsessionid and not jsessionid.strip().startswith('"'):
            jsessionid = f'"{jsessionid.strip()}"'
        cookie_header = (
            self.linkedin_cookie_header.get_secret_value() if self.linkedin_cookie_header else ""
        )
        return settings.model_copy(
            update={
                "linkedin_li_at": li_at,
                "linkedin_jsessionid": jsessionid,
                "linkedin_cookie_header": cookie_header,
            }
        )


class ProfileRequestWithCredentials(BaseModel):
    """POST payload for a profile lookup with a caller-provided session."""

    url: str = Field(
        description="Full LinkedIn profile URL or a public identifier.",
        examples=["https://www.linkedin.com/in/example/"],
    )
    credentials: ProfileRequestCredentials
