# interface_adapters/api/app.py
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from application.pipelines.value_albaran_pipeline import (
    ValueAlbaranPipeline,
    ValueAlbaranRequest,
)
from application.services.conciliacion_service import ConciliacionService
from application.services.schema_registry import SchemaRegistry
from application.services.unit_category_prefilter import UnitCategoryPrefilter
from application.services.valuation_extraction_service import (
    ProviderClientSpec,
    ValuationExtractionService,
)
from config.settings import Settings
from infrastructure.database.session_factory import SessionFactory
from infrastructure.database.sqlalchemy_valuation_context_repository import (
    SqlAlchemyValuationContextRepository,
)
from infrastructure.llm.claude_messages_client import (
    ClaudeMessagesVisionClient,
)
from infrastructure.llm.gemini_genai_client import GeminiGenAiVisionClient
from infrastructure.llm.llm_call_logger import LlmCallLogger
from infrastructure.llm.openai_responses_client import (
    OpenAIResponsesVisionClient,
)
from infrastructure.llm.retry_policy import RetryPolicy
from infrastructure.prompts.yaml_prompt_repository import YamlPromptRepository
from infrastructure.storage.sharepoint_contrato_pdf_downloader import (
    SharePointContratoPdfDownloader,
)

logger = logging.getLogger(__name__)


class ValueRequestBody(BaseModel):
    document_id: str
    codigo_contrato: str | None = None


class ConciliarLineaEntrada(BaseModel):
    line_ref: int
    descripcion: str
    unidad_medida: str | None = None
    cantidad: float | None = None
    codigo_partida: str | None = None
    precio_albaran: float | None = None


class ConciliarLineaContrato(BaseModel):
    id: int
    descripcion: str
    unidad_medida: str | None = None
    precio_unitario: float | None = None
    codigo_partida: str | None = None


class ConciliarRequestBody(BaseModel):
    document_id: str
    lineas_no_casadas: list[ConciliarLineaEntrada] = []
    lineas_contrato: list[ConciliarLineaContrato] = []



def build_app(settings: Settings) -> FastAPI:
    prompt_repo = YamlPromptRepository(settings.prompts_yaml_path)
    schema_registry = SchemaRegistry()

    session_factory = SessionFactory(database_url=settings.database_url)
    context_repository = SqlAlchemyValuationContextRepository(session_factory)

    pdf_downloader = SharePointContratoPdfDownloader(
        graph_key=settings.graph_key,
        timeout_s=settings.http_timeout_s,
        mode=settings.sharepoint_mode,
        hostname=settings.sharepoint_hostname,
        site_path=settings.sharepoint_site_path,
        drive_name=settings.sharepoint_drive_name,
        drive_id=settings.sharepoint_drive_id,
        folder_url=settings.sharepoint_folder_url,
    )

    retry_policy = RetryPolicy(
        max_retries=settings.llm_max_retries,
        backoff_base_s=settings.llm_backoff_base_s,
        backoff_cap_s=settings.llm_backoff_cap_s,
    )

    # ----------------------------------------------------------- #
    # IA call logger — para tuning de prompts.
    # Si IA_LOGGING_ENABLED=false, base_dir=None y todas las llamadas
    # a log_call() son no-op (cero coste, cero I/O).
    # ----------------------------------------------------------- #
    ia_log_dir = (
        settings.ia_logging_dir
        if settings.ia_logging_enabled else None
    )
    call_logger = LlmCallLogger(base_dir=ia_log_dir)
    logger.info(
        "[svc5][wiring] IA call logger %s (dir=%s)",
        "ACTIVO" if call_logger.enabled else "INACTIVO",
        settings.ia_logging_dir if call_logger.enabled else "n/a",
    )

    providers: list[ProviderClientSpec] = []
    if settings.claude_enabled:
        providers.append(
            ProviderClientSpec(
                provider="claude",
                model_name=settings.anthropic_model,
                client=ClaudeMessagesVisionClient(
                    api_key=settings.anthropic_api_key or "",
                    max_tokens=settings.anthropic_max_tokens,
                    timeout_s=settings.anthropic_timeout_s,
                    retry_policy=retry_policy,
                    call_logger=call_logger,
                    # El cliente canónico (ruesma-albaranes-comun) es
                    # compartido con sv2: cada servicio fija su tool.
                    tool_name="emit_valuation_result",
                    tool_description=(
                        "Devuelve la valoración estructurada de las líneas del albarán "
                        "conforme al esquema exigido. Llama SIEMPRE y SOLO a esta herramienta."
                    ),
                ),
            )
        )
    if settings.gemini_enabled:
        providers.append(
            ProviderClientSpec(
                provider="gemini",
                model_name=settings.gemini_model,
                client=GeminiGenAiVisionClient(
                    api_key=settings.gemini_api_key or "",
                    retry_policy=retry_policy,
                    call_logger=call_logger,
                ),
            )
        )
    if settings.openai_enabled:
        providers.append(
            ProviderClientSpec(
                provider="openai",
                model_name=settings.openai_model,
                client=OpenAIResponsesVisionClient(
                    settings.openai_api_key or "",
                    retry_policy=retry_policy,
                    call_logger=call_logger,
                ),
            )
        )

    extraction_service = ValuationExtractionService(
        providers=providers,
        prompt_repo=prompt_repo,
        schema_registry=schema_registry,
        prompt_key=settings.prompt_key,
        ia3_provider=settings.ia3_provider,
    )
    # IA4: proveedor dedicado si se configura (IA4_PROVIDER); si no, el
    # primero habilitado. ConciliacionService usa providers[0] del que
    # le pasemos, asi que filtramos la lista al elegido.
    _ia4_sel = (settings.ia4_provider or "").strip().lower()
    _ia4_providers = (
        [p for p in providers if p.provider == _ia4_sel] or providers
    )
    conciliacion_service = ConciliacionService(
        providers=_ia4_providers,
        prompt_repo=prompt_repo,
        schema_registry=schema_registry,
        prompt_key=settings.ia4_prompt_key,
    )

    pipeline = ValueAlbaranPipeline(
        context_repository=context_repository,
        pdf_downloader=pdf_downloader,
        prefilter=UnitCategoryPrefilter(),
        extraction_service=extraction_service,
        max_pdf_mb=settings.max_pdf_mb,
        ia3_provider=settings.ia3_provider,
        service_version=settings.service_version,
    )

    app = FastAPI(
        title="Albaranes Valuation API",
        version=settings.service_version,
    )

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "service": "albaranes-valuation-api",
            "version": settings.service_version,
            "enabled_providers": settings.enabled_llm_providers,
            "prompt_key": settings.prompt_key,
            "ia_logging": {
                "enabled": call_logger.enabled,
                "dir": settings.ia_logging_dir if call_logger.enabled else None,
            },
        }

    @app.post("/v1/albaranes/value")
    def value(body: ValueRequestBody) -> Dict[str, Any]:
        try:
            return pipeline.run(
                ValueAlbaranRequest(
                    document_id=body.document_id,
                    codigo_contrato_override=body.codigo_contrato,
                )
            )
        except KeyError as exc:
            logger.warning(
                "[value] 404 document_id=%s: %s", body.document_id, exc
            )
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            logger.warning(
                "[value] 400 document_id=%s (posible ValidationError del "
                "LLM contra DocumentoValoracion): %s",
                body.document_id,
                exc,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "Error valorando albarán document_id=%s",
                body.document_id,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error valorando albarán: {exc}",
            ) from exc

    @app.post("/v1/albaranes/conciliar")
    def conciliar(body: ConciliarRequestBody) -> Dict[str, Any]:
        try:
            result = conciliacion_service.conciliar(
                lineas_no_casadas=[
                    l.model_dump() for l in body.lineas_no_casadas
                ],
                lineas_contrato=[
                    l.model_dump() for l in body.lineas_contrato
                ],
            )
            return result.model_dump()
        except Exception as exc:
            logger.exception(
                "Error conciliando document_id=%s", body.document_id
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error conciliando: {exc}",
            ) from exc

    # ----------------------------------------------------------- #
    # GET /v1/debug/ia-logs[?date=YYYYMMDD]
    # Lista los archivos JSON guardados por LlmCallLogger.
    # Útil para tuning de prompts: ves qué se mandó y qué respondió
    # cada modelo sin tener que abrir el explorador de archivos.
    # ----------------------------------------------------------- #
    @app.get("/v1/debug/ia-logs")
    def list_ia_logs(date: str | None = None) -> Dict[str, Any]:
        if not call_logger.enabled:
            return {
                "enabled": False,
                "message": (
                    "IA logging desactivado. Pon IA_LOGGING_ENABLED=true "
                    "en el .env para activarlo."
                ),
                "files": [],
            }
        from pathlib import Path as _Path
        base = _Path(settings.ia_logging_dir)
        if not base.exists():
            return {
                "enabled": True,
                "base_dir": str(base),
                "message": "Aún no hay logs (carpeta no creada).",
                "files": [],
            }

        files: list[dict[str, Any]] = []
        days_iter = (
            [base / date] if date
            else sorted(
                [p for p in base.iterdir() if p.is_dir()],
                reverse=True,
            )
        )
        for day_dir in days_iter:
            if not day_dir.exists() or not day_dir.is_dir():
                continue
            for f in sorted(day_dir.glob("*.json")):
                stat = f.stat()
                files.append({
                    "filename": f.name,
                    "day": day_dir.name,
                    "size_bytes": stat.st_size,
                    "modified_utc": f"{stat.st_mtime:.0f}",
                    "relative_path": str(f.relative_to(base)),
                })
        return {
            "enabled": True,
            "base_dir": str(base),
            "filter_date": date,
            "count": len(files),
            "files": files,
        }

    @app.get("/v1/debug/ia-logs/{day}/{filename}")
    def read_ia_log(day: str, filename: str) -> Dict[str, Any]:
        """Devuelve el contenido de un archivo de log concreto."""
        if not call_logger.enabled:
            raise HTTPException(
                status_code=404,
                detail="IA logging desactivado.",
            )
        from pathlib import Path as _Path
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(status_code=400, detail="filename inválido")
        if "/" in day or "\\" in day or ".." in day:
            raise HTTPException(status_code=400, detail="day inválido")
        target = _Path(settings.ia_logging_dir) / day / filename
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="no encontrado")
        try:
            import json as _json
            with target.open("r", encoding="utf-8") as fp:
                return _json.load(fp)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"error leyendo log: {exc}",
            ) from exc

    return app
