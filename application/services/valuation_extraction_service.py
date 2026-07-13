# application/services/valuation_extraction_service.py
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Optional, Type

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


# -----------------------------------------------------------------
# Texto que se añade al user_text cuando NO hay PDF de contrato.
#
# Sustituye al hack anterior del PDF dummy de 15 bytes que Gemini
# rechazaba con "The document has no pages." Ahora el LLM recibe
# instrucciones explícitas sobre qué hacer en este caso:
#
#   - Solo fase 1a: matchear contra las líneas del contrato que
#     ya tiene en el contexto (vienen de albaran_contrato_lines_merge,
#     persistidas desde el ERP de Sigrid).
#   - precio_unitario_pdf_inferido = null (no hay PDF que leer).
#   - Para líneas que no encuentren match, marcar match_method='no_match'.
#
# El revisor en el front decidirá qué hacer con las líneas no
# matcheadas (revaluar manualmente, asignar precio, etc.).
# -----------------------------------------------------------------
_NOTA_SIN_PDF = (
    "\n\n=== AVISO: NO HAY PDF DE CONTRATO ===\n"
    "Este albarán NO tiene PDF de contrato disponible. "
    "Realiza SOLO la fase 1a de matching: busca correspondencias "
    "entre las líneas del albarán y las líneas del contrato que "
    "tienes en el bloque 'lineas_contrato' del contexto (estas "
    "líneas vienen del ERP).\n\n"
    "Reglas para este caso:\n"
    "  - precio_unitario_pdf_inferido = null en TODAS las líneas "
    "(no hay PDF que leer).\n"
    "  - pdf_inference_reasoning = null.\n"
    "  - tarifa_pdf_encontrada = null.\n"
    "  - Para líneas que SÍ encuentres en lineas_contrato: "
    "rellena matched_contrato_line_id y precio_unitario_contrato_db "
    "con datos de esa línea.\n"
    "  - Para líneas que NO encuentres en lineas_contrato: "
    "match_method='no_match', matched_contrato_line_id=null, "
    "precio_unitario_contrato_db=null. El revisor las decidirá "
    "manualmente.\n"
    "  - NO emitas líneas sintéticas (modificadores) en este caso "
    "— sin PDF no se pueden inferir tarifas de modificadores."
)


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
    recibe el contexto ya construido y, OPCIONALMENTE, el adjunto
    (PDF del contrato) y delega en cada proveedor configurado.

    -----------------------------------------------------------------
    Tanda PDF opcional (abr/may 2026):
    -----------------------------------------------------------------
    Antes, cuando el contrato no tenía PDF en SharePoint, el servicio
    inventaba un attachment dummy con un PDF vacío de 15 bytes para
    cumplir el contrato del port (que exigía attachment obligatorio).

    Ese hack:
      - Funcionaba con Anthropic (lo aceptaba silenciosamente).
      - Funcionaba con OpenAI (lo aceptaba silenciosamente).
      - **Fallaba con Gemini**: 400 INVALID_ARGUMENT
        "The document has no pages."

    Tras esta tanda, el port acepta ``attachment=None`` y los 3
    clientes LLM lo manejan correctamente (solo texto, sin bloque
    de documento). Aquí se añade al user_text la nota
    ``_NOTA_SIN_PDF`` que guía al LLM para hacer SOLO fase 1a.
    -----------------------------------------------------------------
    """

    def __init__(
        self,
        *,
        providers: Iterable[ProviderClientSpec],
        prompt_repo: PromptRepository,
        schema_registry: SchemaRegistry,
        prompt_key: str,
        ia3_provider: str | None = None,
    ) -> None:
        self._providers = list(providers)
        self._prompts = prompt_repo
        self._schemas = schema_registry
        self._prompt_key = prompt_key
        self._ia3_provider = ia3_provider

    @staticmethod
    def _attachment_debug(
        attachment: Optional[LlmAttachment],
    ) -> Optional[Dict[str, Any]]:
        if attachment is None:
            return None
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
        pdf_attachment: Optional[LlmAttachment],
        contrato_markdown: Optional[str] = None,
    ) -> Dict[str, ProviderValuationResult]:
        # Prompt de valoracion por TIPOLOGIA si existe; si no, el generico.
        prompt_key = self._prompt_key
        _tipologia = _derivar_tipologia_valoracion(context)
        _candidato = f"valuation_{_tipologia}"
        if self._prompts.has(_candidato):
            prompt_key = _candidato
        spec = self._prompts.get(prompt_key)
        response_model: Type[BaseModel] = self._schemas.get(spec.schema)
        user_text = self._build_user_text(
            spec_task=spec.task,
            spec_schema_hint=spec.schema_hint,
            context=context,
        )

        # Cómo entra el contrato al LLM (preferimos el Markdown que genera sv3):
        #   - MD disponible  -> al user_text en texto puro, SIN adjunto.
        #   - PDF disponible -> como adjunto (comportamiento anterior).
        #   - ninguno        -> nota _NOTA_SIN_PDF (solo fase 1a), sin adjunto.
        if contrato_markdown:
            user_text = (
                user_text + "\n\n## CONTRATO (markdown)\n\n" + contrato_markdown
            )
            pdf_attachment = None
            _contrato_modo = f"MD ({len(contrato_markdown)} chars)"
        elif pdf_attachment is not None:
            _contrato_modo = "PDF"
        else:
            user_text = user_text + _NOTA_SIN_PDF
            _contrato_modo = "ninguno (fase 1a)"

        logger.info("[extract] contrato -> %s", _contrato_modo)

        results: Dict[str, ProviderValuationResult] = {}
        # IA3_PROVIDER: si se configura, SOLO valora ese proveedor.
        # Sin el, se llamaba a TODOS los habilitados (coste x2 con dos
        # proveedores activos) y mandaba claude por orden fijo.
        _sel = (self._ia3_provider or "").strip().lower()
        _providers = [
            p for p in self._providers if p.provider == _sel
        ] or self._providers
        for provider_spec in _providers:
            logger.info(
                "Valoración albarán proveedor=%s prompt_key=%s model=%s "
                "document_id=%s lineas_albaran=%s lineas_contrato=%s pdf=%s",
                provider_spec.provider,
                prompt_key,
                provider_spec.model_name,
                context.document_id,
                len(context.lineas_albaran),
                len(context.lineas_contrato),
                "yes" if pdf_attachment is not None else "no",
            )
            results[provider_spec.provider] = self._extract_with_provider(
                prompt_key=prompt_key,
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
        prompt_key: str,
        spec_system: str,
        user_text: str,
        attachment: Optional[LlmAttachment],
        provider_spec: ProviderClientSpec,
        response_model: Type[BaseModel],
        schema_name: str,
    ) -> ProviderValuationResult:
        # Ya NO se inventa un PDF dummy. Si attachment es None, el
        # cliente LLM enviará solo texto al proveedor.
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
            prompt_key=prompt_key,
            parsed=parsed,
            debug_payload=debug_payload,
        )


def _derivar_tipologia_valoracion(context) -> str:
    """Tipologia del albaran para elegir el prompt de valoracion.

    Se deriva de la familia de las lineas (contexto_linea.tipo_familia):
    si hay alguna de residuos -> 'residuos'; si hay de hormigon ->
    'hormigon'; en otro caso 'generico'. Mismo criterio que el resolver
    de sv2, pero sobre las lineas ya persistidas.
    """
    fams = set()
    for l in getattr(context, "lineas_albaran", None) or []:
        ctx = getattr(l, "contexto_linea", None)
        fam = getattr(ctx, "tipo_familia", None) if ctx is not None else None
        if fam:
            fams.add(str(fam).strip().lower())
    if "residuos" in fams:
        return "residuos"
    if "hormigon" in fams:
        return "hormigon"
    return "generico"
