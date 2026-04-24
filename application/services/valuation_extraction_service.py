# application/services/valuation_extraction_service.py
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Type

from pydantic import BaseModel

from application.services.schema_registry import SchemaRegistry
from domain.models.llm_attachment import LlmAttachment
from domain.models.valuation_context import (
    AlbaranLineForValuation,
    ContextoValoracion,
)
from domain.ports.llm_client import LlmVisionClient
from domain.ports.prompt_repository import PromptRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderClientSpec:
    provider: str
    model_name: str
    client: LlmVisionClient


@dataclass(frozen=True)
class ProviderValuationResult:
    provider: str
    model_name: str
    schema_name: str
    prompt_key: str
    parsed: BaseModel
    debug_payload: Dict[str, Any]


def _albaran_line_to_dict(line: AlbaranLineForValuation) -> Dict[str, Any]:
    """Serializa AlbaranLineForValuation a dict serializable en JSON.

    ``asdict`` de ``dataclasses`` no recurse dentro de BaseModel de
    Pydantic (``contexto_linea``): lo deja como objeto. Luego
    ``json.dumps`` reventaría. Este helper convierte el bloque
    ``contexto_linea`` a dict usando ``model_dump(exclude_none=True)``
    — ver prompts V2 para qué debe hacer el LLM con este bloque.
    """
    data = asdict(line)
    if line.contexto_linea is not None:
        data["contexto_linea"] = line.contexto_linea.model_dump(
            exclude_none=True,
        )
    else:
        data["contexto_linea"] = None
    return data


class ValuationExtractionService:
    """Orquesta la llamada a los proveedores LLM habilitados.

    Mismo patrón que ``AlbaranExtractionService`` del servicio 2:
    recibe el contexto ya construido y el adjunto (PDF del contrato)
    y delega en cada proveedor configurado.
    """

    def __init__(
        self,
        *,
        providers: Iterable[ProviderClientSpec],
        prompt_repo: PromptRepository,
        schema_registry: SchemaRegistry,
        prompt_key: str,
    ) -> None:
        self._providers = list(providers)
        self._prompts = prompt_repo
        self._schemas = schema_registry
        self._prompt_key = prompt_key

    @staticmethod
    def _attachment_debug(attachment: LlmAttachment) -> Dict[str, Any]:
        return {
            "kind": attachment.kind,
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "size_bytes": len(attachment.data),
            "sha256": hashlib.sha256(attachment.data).hexdigest(),
        }

    @staticmethod
    def _serialize_context(context: ContextoValoracion) -> Dict[str, Any]:
        """Serializa el contexto para el prompt y para el debug payload."""
        return {
            "document_id": context.document_id,
            "contrato": {
                "codigo_contrato": context.codigo_contrato,
                "nombre_contrato": context.nombre_contrato,
                "cif_proveedor": context.cif_proveedor,
                "nombre_proveedor": context.nombre_proveedor,
                "codigo_obra": context.codigo_obra,
                "nombre_obra": context.nombre_obra,
            },
            "lineas_albaran": [
                _albaran_line_to_dict(line)
                for line in context.lineas_albaran
            ],
            "lineas_contrato": [
                asdict(line) for line in context.lineas_contrato
            ],
        }

    def _build_user_text(
        self,
        *,
        spec_task: str,
        spec_schema_hint: str,
        context: ContextoValoracion,
    ) -> str:
        serialized = self._serialize_context(context)
        parts = [spec_task, spec_schema_hint]
        parts.append("\n--- DATOS DEL CONTRATO Y LÍNEAS ---\n")
        parts.append(json.dumps(serialized, ensure_ascii=False, indent=2))
        return "\n\n".join(part for part in parts if part).strip()

    def extract(
        self,
        *,
        context: ContextoValoracion,
        pdf_attachment: LlmAttachment | None,
    ) -> Dict[str, ProviderValuationResult]:
        spec = self._prompts.get(self._prompt_key)
        response_model: Type[BaseModel] = self._schemas.get(spec.schema)
        user_text = self._build_user_text(
            spec_task=spec.task,
            spec_schema_hint=spec.schema_hint,
            context=context,
        )

        # Si no hay PDF, creamos un attachment dummy con un PDF vacío
        # NO: mejor mandamos el texto sin PDF. Los clientes LLM están
        # preparados para aceptar attachments opcionales solo si se
        # pasan. Como nuestra interfaz actual exige attachment, creamos
        # un attachment TEXT embebido indicando la ausencia.
        if pdf_attachment is None:
            # Lo tratamos como texto plano adjunto: un PDF vacío
            # con un placeholder. En la práctica: si no hay PDF, la
            # IA solo podrá hacer fase 1a (match por líneas de BD).
            user_text = (
                user_text
                + "\n\nNOTA IMPORTANTE: no se ha podido adjuntar el PDF "
                "del contrato. Realiza SOLO la fase 1a (matching por "
                "descripción contra la tabla de contrato). Deja "
                "precio_unitario_pdf_inferido=null en todas las líneas."
            )

        results: Dict[str, ProviderValuationResult] = {}
        for provider_spec in self._providers:
            logger.info(
                "Valoración albarán proveedor=%s prompt_key=%s model=%s "
                "document_id=%s lineas_albaran=%s lineas_contrato=%s pdf=%s",
                provider_spec.provider,
                self._prompt_key,
                provider_spec.model_name,
                context.document_id,
                len(context.lineas_albaran),
                len(context.lineas_contrato),
                "yes" if pdf_attachment is not None else "no",
            )
            results[provider_spec.provider] = self._extract_with_provider(
                spec_system=spec.system,
                user_text=user_text,
                attachment=pdf_attachment,
                provider_spec=provider_spec,
                response_model=response_model,
                schema_name=spec.schema,
            )
        return results

    def _extract_with_provider(
        self,
        *,
        spec_system: str,
        user_text: str,
        attachment: LlmAttachment | None,
        provider_spec: ProviderClientSpec,
        response_model: Type[BaseModel],
        schema_name: str,
    ) -> ProviderValuationResult:
        # Si no hay attachment, creamos un PDF placeholder de 1 byte.
        # (Es un hack pero mantiene la interfaz del puerto igual que
        # en el servicio 2. Los clientes LLM aceptan PDFs pequeños.)
        if attachment is None:
            attachment = LlmAttachment(
                kind="pdf",
                filename="empty.pdf",
                mime_type="application/pdf",
                data=b"%PDF-1.4\n%%EOF\n",
            )

        parsed = provider_spec.client.extract_document(
            model=provider_spec.model_name,
            instructions=spec_system,
            user_text=user_text,
            attachment=attachment,
            response_model=response_model,
        )
        debug_payload: Dict[str, Any] = {
            f"{provider_spec.provider}_request": {
                "provider": provider_spec.provider,
                "model": provider_spec.model_name,
                "prompt_key": self._prompt_key,
                "instructions": spec_system,
                "user_text": user_text,
                "response_schema_name": schema_name,
                "response_model_name": response_model.__name__,
                "response_schema_json": response_model.model_json_schema(),
                "attachment": self._attachment_debug(attachment),
            },
            f"{provider_spec.provider}_response": {
                "provider": provider_spec.provider,
                "model": provider_spec.model_name,
                "parsed": parsed.model_dump(),
            },
        }
        return ProviderValuationResult(
            provider=provider_spec.provider,
            model_name=provider_spec.model_name,
            schema_name=schema_name,
            prompt_key=self._prompt_key,
            parsed=parsed,
            debug_payload=debug_payload,
        )
