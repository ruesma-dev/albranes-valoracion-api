# config/settings.py
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Tipo Literal para los proveedores válidos en el routing de fases.
# Si algún día se añade un nuevo proveedor (p.ej. azure_openai), basta
# con añadirlo aquí y todo el routing fase 1 / fase 2 lo aceptará sin
# tocar más sitios.
ProviderName = Literal["openai", "gemini", "claude"]


class Settings(BaseSettings):
    # ------------------------------------------------------------
    # Flags de habilitación por proveedor LLM.
    #
    # El wiring del FastAPI (interface_adapters/api/app.py) lee
    # ``openai_enabled`` / ``gemini_enabled`` / ``claude_enabled`` y
    # construye SOLO los clientes de los proveedores habilitados.
    # Los no habilitados quedan fuera del envelope de extracción — el
    # merge del servicio 3 ya trata Gemini/Claude como opcionales
    # (``if envelope.gemini is not None``) así que no rompe nada.
    #
    # Las API keys asociadas pasan a ser OPCIONALES: solo se exige la
    # clave del proveedor si está habilitado. Así puedes tener el .env
    # sin ``ANTHROPIC_API_KEY`` si ``ENABLE_CLAUDE=false``.
    #
    # Validación global: al menos un proveedor debe estar habilitado
    # (ver ``_ensure_at_least_one_provider_enabled`` más abajo).
    # ------------------------------------------------------------
    openai_enabled: bool = Field(True, alias="ENABLE_OPENAI")
    gemini_enabled: bool = Field(True, alias="ENABLE_GEMINI")
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
        120,
        alias="ANTHROPIC_TIMEOUT_S",
    )

    google_document_ai_enabled: bool = Field(
        False,
        alias="GOOGLE_DOCUMENT_AI_ENABLED",
    )
    google_document_ai_project_id: str | None = Field(
        None,
        alias="GOOGLE_DOCUMENT_AI_PROJECT_ID",
    )
    google_document_ai_location: str = Field(
        "eu",
        alias="GOOGLE_DOCUMENT_AI_LOCATION",
    )
    google_document_ai_processor_id: str | None = Field(
        None,
        alias="GOOGLE_DOCUMENT_AI_PROCESSOR_ID",
    )
    google_document_ai_processor_version: str | None = Field(
        None,
        alias="GOOGLE_DOCUMENT_AI_PROCESSOR_VERSION",
    )
    google_application_credentials: str | None = Field(
        None,
        alias="GOOGLE_APPLICATION_CREDENTIALS",
    )

    azure_document_intelligence_enabled: bool = Field(
        False,
        alias="AZURE_DOCUMENT_INTELLIGENCE_ENABLED",
    )
    azure_document_intelligence_endpoint: str | None = Field(
        None,
        alias="AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
    )
    azure_document_intelligence_key: str | None = Field(
        None,
        alias="AZURE_DOCUMENT_INTELLIGENCE_KEY",
    )
    azure_document_intelligence_model_id: str = Field(
        "prebuilt-invoice",
        alias="AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID",
    )
    azure_document_intelligence_api_version: str = Field(
        "2024-11-30",
        alias="AZURE_DOCUMENT_INTELLIGENCE_API_VERSION",
    )
    azure_document_intelligence_timeout_s: int = Field(
        120,
        alias="AZURE_DOCUMENT_INTELLIGENCE_TIMEOUT_S",
    )

    # --------------------------------------------------------------
    # Política de reintentos común para los clientes LLM
    # (openai / gemini / claude). Ver infrastructure/llm/retry_policy.py
    # --------------------------------------------------------------
    llm_max_retries: int = Field(2, alias="LLM_MAX_RETRIES")
    llm_backoff_base_s: float = Field(2.0, alias="LLM_BACKOFF_BASE_S")
    llm_backoff_cap_s: float = Field(30.0, alias="LLM_BACKOFF_CAP_S")

    # --------------------------------------------------------------
    # Routing FASE 1 / FASE 2 — refactor de extracción + revisión.
    #
    # FASE 1 = extracción "ciega" del PDF.
    # FASE 2 = revisión del JSON de fase 1 buscando inconsistencias
    #          (importes negativos, fechas raras, ahorros aritméticos…)
    #          y proponiendo cambios puntuales.
    #
    # Cada fase usa UN proveedor (no es multi-LLM como antes). Lo
    # decides aquí. El proveedor que elijas para cada fase debe estar
    # habilitado por el flag correspondiente (ENABLE_OPENAI/...).
    #
    # Razones para tener dos proveedores distintos:
    #   - Diversidad: lo que no ve uno, lo ve el otro.
    #   - Coste: fase 1 va a un modelo barato (gemini-flash), fase 2
    #     a uno caro especializado en razonamiento (gpt-5 / claude).
    # --------------------------------------------------------------
    ia_primera_fase: ProviderName = Field(
        "gemini",
        alias="IA_PRIMERA_FASE",
    )
    ia_segunda_fase: ProviderName = Field(
        "openai",
        alias="IA_SEGUNDA_FASE",
    )

    prompt_key_fase1: str = Field(
        "albaran_factura_es",
        alias="PROMPT_KEY_FASE1",
    )
    prompt_key_fase2: str = Field(
        "albaran_revision_fase2_es",
        alias="PROMPT_KEY_FASE2",
    )

    # Compatibilidad con código antiguo que aún lea "prompt_key" suelto.
    # Apunta al de fase 1.
    @property
    def prompt_key(self) -> str:
        return self.prompt_key_fase1

    prompts_yaml_path: str = Field(
        "config/prompts.yaml",
        alias="PROMPTS_YAML_PATH",
    )
    revision_rules_yaml_path: str = Field(
        "config/revision_rules.yaml",
        alias="REVISION_RULES_YAML_PATH",
    )
    api_host: str = Field("127.0.0.1", alias="API_HOST")
    api_port: int = Field(8000, alias="API_PORT")
    max_file_mb: int = Field(25, alias="MAX_FILE_MB")
    cors_allow_origins: str | None = Field(None, alias="CORS_ALLOW_ORIGINS")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")
    service_version: str = Field("1.0.0", alias="SERVICE_VERSION")

    # ------------------------------------------------------------
    # IA call logging — para tuning de prompts.
    #
    # Si IA_LOGGING_ENABLED=true, cada llamada a Gemini/OpenAI/Claude
    # genera un par de archivos JSON (request + response) en
    # ``IA_LOGGING_DIR`` particionado por día:
    #
    #   <IA_LOGGING_DIR>/<YYYYMMDD>/<HHMMSS_milis>_<provider>_<id>.json
    #
    # No incluye bytes binarios del PDF (solo metadatos + sha256).
    # No incluye claves API.
    #
    # Sin retención automática: se conserva todo el histórico.
    # ------------------------------------------------------------
    ia_logging_enabled: bool = Field(
        False,
        alias="IA_LOGGING_ENABLED",
    )
    ia_logging_dir: str = Field(
        "logs/ia",
        alias="IA_LOGGING_DIR",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --------------------------------------------------------------
    # Validaciones cruzadas.
    #
    # 1) Al menos un proveedor LLM debe estar habilitado — si los tres
    #    están a false, el servicio no puede extraer nada y el boot
    #    falla con un mensaje explícito en lugar de descubrirlo en
    #    runtime tras procesar un PDF.
    # 2) Si un proveedor está habilitado, su API key debe existir.
    #    Evita arrancar con ENABLE_OPENAI=true y OPENAI_API_KEY vacío,
    #    que causaría un 401 en la primera extracción.
    # 3) Los proveedores escogidos para fase 1 / fase 2 deben estar
    #    habilitados. Si no, salta error en boot en lugar de en
    #    runtime con un KeyError críptico.
    # --------------------------------------------------------------
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
                "Revisa ENABLE_OPENAI / ENABLE_GEMINI / ENABLE_CLAUDE "
                "en el .env — actualmente los tres están a false."
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
                "Faltan API keys para los proveedores habilitados: "
                + ", ".join(missing)
                + ". Si no quieres usar un proveedor, pon su flag a "
                "false (p.e. ENABLE_CLAUDE=false) en lugar de dejar "
                "la clave vacía."
            )
        return self

    @model_validator(mode="after")
    def _ensure_phase_providers_enabled(self) -> "Settings":
        flag_by_provider = {
            "openai": self.openai_enabled,
            "gemini": self.gemini_enabled,
            "claude": self.claude_enabled,
        }
        problems: list[str] = []
        if not flag_by_provider.get(self.ia_primera_fase, False):
            problems.append(
                f"IA_PRIMERA_FASE='{self.ia_primera_fase}' pero "
                f"ENABLE_{self.ia_primera_fase.upper()} no está a true"
            )
        if not flag_by_provider.get(self.ia_segunda_fase, False):
            problems.append(
                f"IA_SEGUNDA_FASE='{self.ia_segunda_fase}' pero "
                f"ENABLE_{self.ia_segunda_fase.upper()} no está a true"
            )
        if problems:
            raise ValueError(
                "Hay proveedores asignados a fases que no están "
                "habilitados: " + "; ".join(problems) + ". "
                "Habilita el proveedor o cambia la fase a otro."
            )
        return self

    # Helpers de conveniencia para el wiring.
    @property
    def enabled_llm_providers(self) -> list[str]:
        """Lista ordenada de proveedores LLM habilitados.

        Útil para logs y para el endpoint /health. El orden es el
        canónico del sistema: openai → gemini → claude.
        """
        out: list[str] = []
        if self.openai_enabled:
            out.append("openai")
        if self.gemini_enabled:
            out.append("gemini")
        if self.claude_enabled:
            out.append("claude")
        return out
