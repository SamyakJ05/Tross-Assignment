"""HTTP request models containing caller-supplied sensitive values."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, field_validator


class SessionProfileRequest(BaseModel):
    """One uncached profile lookup using an ephemeral LinkedIn session."""

    url: str = Field(
        description="A LinkedIn profile URL or bare vanity slug.",
        examples=["https://www.linkedin.com/in/samyakj05/"],
    )
    li_at: SecretStr = Field(
        description="The caller's LinkedIn li_at cookie. Used only for this request.",
        json_schema_extra={"writeOnly": True},
        repr=False,
    )
    jsessionid: SecretStr = Field(
        description=(
            "The caller's LinkedIn JSESSIONID cookie, with or without surrounding quotes. "
            "Used only for this request."
        ),
        json_schema_extra={"writeOnly": True},
        repr=False,
    )

    @field_validator("li_at", "jsessionid")
    @classmethod
    def _must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Session values must not be blank.")
        return value
