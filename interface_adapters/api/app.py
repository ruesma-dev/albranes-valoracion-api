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
                ),
            )
        )

    extraction_service = ValuationExtractionService(
        providers=providers,
        prompt_repo=prompt_repo,
        schema_registry=schema_registry,
        prompt_key=settings.prompt_key,
    )
    pipeline = ValueAlbaranPipeline(
        context_repository=context_repository,
        pdf_downloader=pdf_downloader,
        prefilter=UnitCategoryPrefilter(),
        extraction_service=extraction_service,
        max_pdf_mb=settings.max_pdf_mb,
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
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
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

    return app
