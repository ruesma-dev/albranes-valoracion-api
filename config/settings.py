# config/settings.py
from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

SharePointMode = Literal["drive_id", "folder_url", "site_path"]


class Settings(BaseSettings):
    """Configuración del servicio 5 (valuation-api).

    El servicio llama a la IA con el PDF del contrato como contexto y
    las líneas de albarán/contrato ya extraídas de BBDD. No persiste:
    devuelve el envelope con la valoración bruta al llamante.

    Proveedores LLM:
      - Por defecto SOLO Claude está habilitado (decisión del cliente).
      - Se mantienen flags de OpenAI/Gemini para poder activarlos sin
        tocar el código. Si los tres están a false, el boot falla.
    """

    # ------------------------------------------------------------
    # Flags de proveedores LLM
    # ------------------------------------------------------------
    openai_enabled: bool = Field(False, alias="ENABLE_OPENAI")
    gemini_enabled: bool = Field(False, alias="ENABLE_GEMINI")
    claude_enabled: bool = Field(True, alias="ENABLE_CLAUDE")

    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-5", alias="OPENAI_MODEL")

    gemini_api_key: str | None = Field(None, alias="GEMINI_API_KEY")
    gemini_model: str = Field("gemini-2.5-flash", alias="GEMINI_MODEL")

    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(
        "claude-sonnet-4-5",
        alias="ANTHROPIC_MODEL",
    )
    anthropic_max_tokens: int = Field(
        8192,
        alias="ANTHROPIC_MAX_TOKENS",
    )
    anthropic_timeout_s: int = Field(
        180,
        alias="ANTHROPIC_TIMEOUT_S",
    )

    # ------------------------------------------------------------
    # Política de reintentos para clientes LLM (reutilizada de svc2)
    # ------------------------------------------------------------
    llm_max_retries: int = Field(2, alias="LLM_MAX_RETRIES")
    llm_backoff_base_s: float = Field(2.0, alias="LLM_BACKOFF_BASE_S")
    llm_backoff_cap_s: float = Field(30.0, alias="LLM_BACKOFF_CAP_S")

    # ------------------------------------------------------------
    # Prompts y schema
    # ------------------------------------------------------------
    prompt_key: str = Field("valuation_es", alias="PROMPT_KEY")
    prompts_yaml_path: str = Field(
        "config/prompts.yaml",
        alias="PROMPTS_YAML_PATH",
    )

    # ------------------------------------------------------------
    # BBDD PostgreSQL (solo lectura; compartida con svc 3/6)
    # ------------------------------------------------------------
    pg_host: str = Field("localhost", alias="PG_HOST")
    pg_port: int = Field(5432, alias="PG_PORT")
    pg_db: str = Field("albaranes", alias="PG_DB")
    pg_user: str = Field(..., alias="PG_USER")
    pg_password: str = Field(..., alias="PG_PASSWORD")

    # ------------------------------------------------------------
    # SharePoint — para descargar el PDF del contrato
    # ------------------------------------------------------------
    graph_key: str = Field(..., alias="GRAPH_KEY")
    sharepoint_mode: SharePointMode = Field(
        "drive_id",
        alias="SHAREPOINT_MODE",
    )
    sharepoint_folder_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SHAREPOINT_FOLDER_URL",
            "SHAREPOINT_SHARE_URL",
        ),
    )
    sharepoint_hostname: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SHAREPOINT_HOSTNAME",
            "SHAREPOINT_HOST",
        ),
    )
    sharepoint_site_path: str | None = Field(
        default=None,
        alias="SHAREPOINT_SITE_PATH",
    )
    sharepoint_drive_name: str = Field(
        "Documentos compartidos",
        alias="SHAREPOINT_DRIVE_NAME",
    )
    sharepoint_drive_id: str | None = Field(
        default=None,
        alias="SHAREPOINT_DRIVE_ID",
    )

    # ------------------------------------------------------------
    # API
    # ------------------------------------------------------------
    api_host: str = Field("127.0.0.1", alias="API_HOST")
    api_port: int = Field(8002, alias="API_PORT")
    http_timeout_s: int = Field(60, alias="HTTP_TIMEOUT_S")
    max_pdf_mb: int = Field(40, alias="MAX_PDF_MB")

    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")
    service_version: str = Field("1.0.0", alias="SERVICE_VERSION")

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------
    # Validaciones cruzadas
    # ------------------------------------------------------------
    @model_validator(mode="after")
    def _ensure_at_least_one_provider_enabled(self) -> "Settings":
        enabled = {
            "openai": self.openai_enabled,
            "gemini": self.gemini_enabled,
            "claude": self.claude_enabled,
        }
        if not any(enabled.values()):
            raise ValueError(
                "Al menos un proveedor LLM debe estar habilitado. "
                "Revisa ENABLE_OPENAI / ENABLE_GEMINI / ENABLE_CLAUDE."
            )
        return self

    @model_validator(mode="after")
    def _ensure_api_keys_for_enabled_providers(self) -> "Settings":
        missing: list[str] = []
        if self.openai_enabled and not (self.openai_api_key or "").strip():
            missing.append("OPENAI_API_KEY (ENABLE_OPENAI=true)")
        if self.gemini_enabled and not (self.gemini_api_key or "").strip():
            missing.append("GEMINI_API_KEY (ENABLE_GEMINI=true)")
        if self.claude_enabled and not (self.anthropic_api_key or "").strip():
            missing.append("ANTHROPIC_API_KEY (ENABLE_CLAUDE=true)")
        if missing:
            raise ValueError(
                "Faltan API keys: " + ", ".join(missing)
            )
        return self

    @property
    def database_url(self) -> str:
        user = quote_plus(self.pg_user)
        password = quote_plus(self.pg_password)
        database = quote_plus(self.pg_db)
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.pg_host}:{self.pg_port}/{database}"
        )

    @property
    def enabled_llm_providers(self) -> list[str]:
        out: list[str] = []
        if self.openai_enabled:
            out.append("openai")
        if self.gemini_enabled:
            out.append("gemini")
        if self.claude_enabled:
            out.append("claude")
        return out
