"""Typed configuration for the legacy loader.

Values come from environment variables (or the project ``.env``), matched case-insensitively,
e.g. ``sql_server_host`` <- ``SQL_SERVER_HOST``. Env names used by the original script
(``SQL_SERVER_HOST``, ``SQL_SERVER_PORT``, ``SQL_ADMIN_USER``, ``SQL_ADMIN_PASSWORD``,
``RAW_DATA_PATH``) are unchanged.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rag.config import PROJECT_ROOT

DEFAULT_DATA_FILE = PROJECT_ROOT / "data" / "raw" / "dynamic_supply_chain_logistics_dataset.csv"


def _odbc_value(value: str) -> str:
    """Brace-quote an ODBC attribute so ``;`` / ``}`` in passwords can't break the string."""
    return "{" + value.replace("}", "}}") + "}"


class LegacySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Connection -----------------------------------------------------------------
    sql_server_host: str = "localhost"
    sql_server_port: int = Field(default=1433, gt=0, lt=65536)
    sql_server_database: str = "master"
    sql_admin_user: str | None = None  # checked at connect time so --dry-run needs no creds
    sql_admin_password: SecretStr | None = None
    sql_odbc_driver: str | None = None  # None -> newest installed "ODBC Driver NN for SQL Server"
    sql_encrypt: bool = False
    sql_trust_server_certificate: bool = True
    sql_connect_timeout: int = Field(default=15, gt=0)
    sql_connect_attempts: int = Field(default=5, gt=0)

    # ---- Source / target ------------------------------------------------------------
    raw_data_path: Path = DEFAULT_DATA_FILE
    target_schema: str = "dbo"
    target_table: str = "TBL_SC_FLEET_HIST_RAW"

    # ---- Throughput / quality -------------------------------------------------------
    csv_chunk_rows: int = Field(default=10_000, gt=0)
    insert_batch_rows: int = Field(default=5_000, gt=0)
    max_reject_ratio: float = Field(default=0.01, ge=0.0, le=1.0)
    reject_dir: Path = PROJECT_ROOT / "data" / "rejects"

    legacy_log_level: str = "INFO"

    @field_validator("raw_data_path", "reject_dir", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    def odbc_connection_string(self, driver: str) -> str:
        password = self.sql_admin_password.get_secret_value() if self.sql_admin_password else ""
        parts = {
            "DRIVER": _odbc_value(driver),
            "SERVER": f"{self.sql_server_host},{self.sql_server_port}",
            "DATABASE": _odbc_value(self.sql_server_database),
            "UID": _odbc_value(self.sql_admin_user or ""),
            "PWD": _odbc_value(password),
            "Encrypt": "yes" if self.sql_encrypt else "no",
            "TrustServerCertificate": "yes" if self.sql_trust_server_certificate else "no",
            "APP": "cold-chain-legacy-ingest",
        }
        return ";".join(f"{key}={value}" for key, value in parts.items()) + ";"

    def describe_target(self) -> str:
        """Log-safe connection summary (never includes the password)."""
        return (
            f"{self.sql_admin_user}@{self.sql_server_host}:{self.sql_server_port}/"
            f"{self.sql_server_database} -> {self.target_schema}.{self.target_table}"
        )


@lru_cache(maxsize=1)
def get_legacy_settings() -> LegacySettings:
    return LegacySettings()
