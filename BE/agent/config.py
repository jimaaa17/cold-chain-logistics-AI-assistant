"""Typed configuration for the backend LangGraph Agent Orchestrator.

Compatible with existing ``.env`` variables used by the prototype (``Agent_llm``,
``GEMINI_API_KEY``, ``SQL_SERVER_HOST``, ``SQL_AGENT_USER``, ``SQL_AGENT_PASSWORD``, etc.)
while enforcing strict validation and secret masking.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_by_name=True,
        validate_by_alias=True,
    )

    # ---- Reasoner LLM ---------------------------------------------------------------
    agent_llm: Literal["GEMINI", "OPENAI", "OLLAMA"] | None = Field(
        default=None,
        validation_alias=AliasChoices("AGENT_LLM", "Agent_llm", "llm"),
    )
    gemini_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GEMINI_APKEY"),
    )
    gemini_model: str = "gemini-3.8-flash"
    # Gemini "thinking" before answering. 0 turns it off (measured: first token 13.4 s -> 6.4 s);
    # thresholds and checks now run in code, so the reasoner needs little of it. Unset = model default.
    gemini_thinking_budget: int | None = Field(default=0, ge=0)
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o"
    ollama_model: str = "qwen2.5:7b"
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(default=8192, gt=0)

    # ---- Telemetry SQL (Read-Only RBAC: TDD 3.1 & 3.2) ------------------------------
    sql_server_host: str = "localhost"
    sql_server_port: int = Field(default=1433, gt=0, le=65535)
    sql_server_database: str = "master"
    sql_agent_user: str = "USR_FDE_RO"
    sql_agent_password: SecretStr = SecretStr("AgentPassword2026!")
    sql_odbc_driver: str = "ODBC Driver 18 for SQL Server"
    sql_encrypt: bool = False
    sql_trust_server_certificate: bool = True
    sql_max_rows: int = Field(default=10, gt=0, le=100)
    sql_allowed_view: str = "FDE_VIEWS.VW_ACTIVE_FLEET"

    # ---- Corridor Weather REST API (TDD 2.2) ----------------------------------------
    corridor_api_url: str = "https://api.open-meteo.com/v1/forecast"
    corridor_timeout_s: float = Field(default=6.0, gt=0.0)

    # ---- In-Process SOP RAG ---------------------------------------------------------
    rag_top_k: int = Field(default=5, gt=0, le=50)

    # ---- Agentic dispatch workflow ---------------------------------------------------
    # A reading is "fresh" (and can trigger SOP actions) only if newer than this window.
    evidence_freshness_hours: float = Field(default=24.0, gt=0.0)
    # Half-width (degrees) of the box searched around a named place such as "Long Beach".
    area_half_width_deg: float = Field(default=0.35, gt=0.0, le=5.0)
    # Load the embedding model, Pinecone client and graph when the API server starts.
    agent_prewarm: bool = True

    # ---- Immutable Auditing & Langfuse Traceability (TDD 3.3) -----------------------
    audit_enabled: bool = True
    audit_table: str = "FDE_VIEWS.AgentAuditLog"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # ---- Prompt & Logging -----------------------------------------------------------
    system_prompt_path: Path = Path(__file__).resolve().parent / "prompts" / "system_prompt.txt"
    agent_log_level: str = "INFO"

    @field_validator("agent_llm", mode="before")
    @classmethod
    def _normalise_llm_provider(cls, value: object) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        return str(value).strip().upper()

    @model_validator(mode="after")
    def _resolve_default_llm(self) -> "AgentSettings":
        if self.agent_llm is None:
            if self.gemini_api_key and self.gemini_api_key.get_secret_value():
                self.agent_llm = "GEMINI"
            elif self.openai_api_key and self.openai_api_key.get_secret_value():
                self.agent_llm = "OPENAI"
            else:
                self.agent_llm = "OLLAMA"
        return self

    def odbc_connection_string(self) -> str:
        """Build a brace-escaped ODBC connection string for the read-only agent role."""
        encrypt = "yes" if self.sql_encrypt else "no"
        trust = "yes" if self.sql_trust_server_certificate else "no"
        pwd = self.sql_agent_password.get_secret_value()
        return (
            f"DRIVER={_brace_escape(self.sql_odbc_driver)};"
            f"SERVER={self.sql_server_host},{self.sql_server_port};"
            f"DATABASE={_brace_escape(self.sql_server_database)};"
            f"UID={_brace_escape(self.sql_agent_user)};"
            f"PWD={_brace_escape(pwd)};"
            f"Encrypt={encrypt};"
            f"TrustServerCertificate={trust};"
        )


def _brace_escape(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


@lru_cache(maxsize=1)
def get_agent_settings() -> AgentSettings:
    return AgentSettings()
