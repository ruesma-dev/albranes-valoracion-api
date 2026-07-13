# config/settings.py
from __future__ import annotations

from urllib.parse import quote_plus

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del servicio 5 (``albaranes-valuation-api``).

    NOTA SOBRE LA FRONTERA CON EL SV2
    ---------------------------------
    Históricamente este ``settings.py`` era un copy-paste del settings
    del sv2 e incluía variables como ``IA_PRIMERA_FASE`` /
    ``IA_SEGUNDA_FASE``, ``PROMPT_KEY_FASE2`` y un validator que exigía
    que el proveedor de cada fase estuviera habilitado. Eso era un
    **acoplamiento incorrecto**: la noción de "fase 1 / fase 2" es del
    sv2 (extracción ciega + revisión interna del JSON). El sv5 es un
    servicio distinto, con un único pipeline IA de valoración, y NUNCA
    usaba esas variables en su código — pero el validator de boot
    crasheaba con::

        ValidationError: IA_SEGUNDA_FASE='openai' pero ENABLE_OPENAI
        no está a true.

    cuando, legítimamente, el sv5 tiene un solo proveedor habilitado
    (o ninguno OpenAI). Por eso este archivo se ha **purgado** de todo
    lo que era del sv2.

    Contrato entre sv2 ↔ sv5: SOLO el envelope JSON. El sv5 NO mira ni
    valida campos del envelope que indiquen qué proveedor usó el sv2
    para tal o cual fase — eso es asunto interno del sv2.

    QUÉ HAY EN ESTE SETTINGS
    ------------------------
    Solo lo que el sv5 consume realmente en su wiring
    (``interface_adapters/api/app.py``):

      * Flags ``ENABLE_*`` y claves API por proveedor.
      * BBDD (mismo PostgreSQL que sv3 y sv6, para el contexto de
        valoración).
      * Graph + SharePoint (descarga del PDF de contrato).
      * Política de reintentos LLM.
      * Prompt único de valoración.
      * IA call logging.
      * API host/port, logging, versión.
    """

    # ------------------------------------------------------------
    # Flags de habilitación por proveedor LLM.
    #
    # El wiring lee ``openai_enabled`` / ``gemini_enabled`` /
    # ``claude_enabled`` y construye SOLO los clientes habilitados.
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
        120,
        alias="ANTHROPIC_TIMEOUT_S",
    )

    # ------------------------------------------------------------
    # OCR puros (off por defecto). Hoy no se usan en sv5; los dejo
    # declarados para coherencia con sv2/sv6 y por si se cablean en
    # el futuro.
    # ------------------------------------------------------------
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

    # ------------------------------------------------------------
    # Política de reintentos LLM (compartida por OpenAI/Gemini/Claude).
    # ------------------------------------------------------------
    llm_max_retries: int = Field(2, alias="LLM_MAX_RETRIES")
    llm_backoff_base_s: float = Field(2.0, alias="LLM_BACKOFF_BASE_S")
    llm_backoff_cap_s: float = Field(30.0, alias="LLM_BACKOFF_CAP_S")

    # ------------------------------------------------------------
    # BBDD PostgreSQL (misma que sv3 y sv6).
    #
    # El sv5 lee el contexto de valoración (líneas merge, líneas de
    # contrato, cabecera). Si se prefiere modo read-only se gestiona
    # a nivel de PostgreSQL con GRANT SELECT only, no aquí.
    # ------------------------------------------------------------
    pg_host: str = Field("localhost", alias="PG_HOST")
    pg_port: int = Field(5432, alias="PG_PORT")
    pg_db: str = Field("albaranes", alias="PG_DB")
    pg_user: str = Field(..., alias="PG_USER")
    pg_password: str = Field(..., alias="PG_PASSWORD")

    @property
    def database_url(self) -> str:
        """URL completa para SQLAlchemy con encoding de credenciales."""
        user = quote_plus(self.pg_user)
        password = quote_plus(self.pg_password)
        database = quote_plus(self.pg_db)
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.pg_host}:{self.pg_port}/{database}"
        )

    # ------------------------------------------------------------
    # Microsoft Graph + SharePoint (descarga del PDF de contrato).
    #
    # ``graph_key`` es un JSON o JSON-base64 con:
    #     {"tenant_id": "...", "client_id": "...", "client_secret": "..."}
    #
    # ``sharepoint_mode`` decide cómo localizar el drive:
    #     * ``drive_id``   → usa ``sharepoint_drive_id`` directamente.
    #     * ``folder_url`` → resuelve desde ``sharepoint_folder_url``.
    #     * ``site_path``  → usa ``sharepoint_hostname`` +
    #                        ``sharepoint_site_path`` +
    #                        ``sharepoint_drive_name``.
    # ------------------------------------------------------------
    graph_key: str = Field(..., alias="GRAPH_KEY")
    http_timeout_s: int = Field(60, alias="HTTP_TIMEOUT_S")
    sharepoint_mode: str = Field("drive_id", alias="SHAREPOINT_MODE")
    sharepoint_hostname: str | None = Field(
        None,
        alias="SHAREPOINT_HOSTNAME",
    )
    sharepoint_site_path: str | None = Field(
        None,
        alias="SHAREPOINT_SITE_PATH",
    )
    sharepoint_drive_name: str = Field(
        "Documentos compartidos",
        alias="SHAREPOINT_DRIVE_NAME",
    )
    sharepoint_drive_id: str | None = Field(
        None,
        alias="SHAREPOINT_DRIVE_ID",
    )
    sharepoint_folder_url: str | None = Field(
        None,
        alias="SHAREPOINT_FOLDER_URL",
    )

    # ------------------------------------------------------------
    # Prompt único del sv5 (valoración).
    #
    # A diferencia del sv2, aquí NO hay fase 1 / fase 2: la valoración
    # es un único paso IA por documento.
    # ------------------------------------------------------------
    prompt_key: str = Field(
        "valuation_es",
        alias="PROMPT_KEY",
    )
    prompts_yaml_path: str = Field(
        "config/prompts.yaml",
        alias="PROMPTS_YAML_PATH",
    )

    # ------------------------------------------------------------
    # IA4 (conciliacion semantica en lote). Se lanza en sv6 solo
    # para lineas que el determinista NO caso. Aqui se elige QUE
    # proveedor la ejecuta y con que prompt.
    #   - IA4_PROVIDER: 'openai' | 'gemini' | 'claude'. Si vacio, usa
    #     el primer proveedor habilitado (mismo que la valoracion).
    #   - IA4_PROMPT_KEY: prompt de conciliacion (por defecto
    #     'conciliacion_es').
    # ------------------------------------------------------------
    # IA3 (valoracion). Si se fija, SOLO ese proveedor valora (una llamada).
    # Si esta vacio, se mantiene el comportamiento historico: se llama a
    # TODOS los proveedores habilitados y manda claude > gemini > openai.
    ia3_provider: str | None = Field(None, alias="IA3_PROVIDER")
    ia4_provider: str | None = Field(None, alias="IA4_PROVIDER")
    ia4_prompt_key: str = Field(
        "conciliacion_es",
        alias="IA4_PROMPT_KEY",
    )

    # ------------------------------------------------------------
    # API y logging.
    # ------------------------------------------------------------
    api_host: str = Field("127.0.0.1", alias="API_HOST")
    api_port: int = Field(8002, alias="API_PORT")
    max_pdf_mb: int = Field(40, alias="MAX_PDF_MB")
    cors_allow_origins: str | None = Field(
        None,
        alias="CORS_ALLOW_ORIGINS",
    )
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")
    service_version: str = Field("1.0.0", alias="SERVICE_VERSION")

    # ------------------------------------------------------------
    # IA call logging — útil para tuning de prompts.
    #
    # Si IA_LOGGING_ENABLED=true, cada llamada al LLM genera un par
    # de archivos JSON (request + response) en ``IA_LOGGING_DIR``
    # particionado por día:
    #
    #   <IA_LOGGING_DIR>/<YYYYMMDD>/<HHMMSS_milis>_<provider>_<id>.json
    #
    # No incluye bytes binarios del PDF (solo metadatos + sha256).
    # No incluye claves API.
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
    # Validaciones cruzadas — SOLO las que tienen sentido en el sv5.
    #
    # 1) Al menos un proveedor LLM debe estar habilitado.
    # 2) Si un proveedor está habilitado, su API key debe existir.
    #
    # Se ELIMINARON intencionalmente:
    #   - ``_ensure_phase_providers_enabled`` (era el causante del
    #     crash del sv5: validaba que el proveedor de
    #     ``IA_SEGUNDA_FASE`` estuviera habilitado, pero esa variable
    #     era ruido copiado del sv2).
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
                "Al menos un proveedor LLM debe estar habilitado en el "
                "sv5. Revisa ENABLE_OPENAI / ENABLE_GEMINI / "
                "ENABLE_CLAUDE en el .env — actualmente los tres están "
                "a false."
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
                "Faltan API keys para los proveedores habilitados en "
                "el sv5: " + ", ".join(missing) + ". Si no quieres "
                "usar un proveedor, pon su flag a false (p.e. "
                "ENABLE_OPENAI=false) en lugar de dejar la clave vacía."
            )
        return self

    # Helpers de conveniencia para el wiring.
    @property
    def enabled_llm_providers(self) -> list[str]:
        """Lista ordenada de proveedores LLM habilitados.

        Útil para logs y para el endpoint /health. Orden canónico:
        openai → gemini → claude.
        """
        out: list[str] = []
        if self.openai_enabled:
            out.append("openai")
        if self.gemini_enabled:
            out.append("gemini")
        if self.claude_enabled:
            out.append("claude")
        return out