# application/pipelines/value_albaran_pipeline.py
from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict

from application.services.unit_category_prefilter import UnitCategoryPrefilter
from application.services.valuation_extraction_service import (
    ProviderValuationResult,
    ValuationExtractionService,
)
from domain.models.llm_attachment import LlmAttachment
from domain.models.valuation_context import (
    AlbaranLineForValuation,
    ContextoValoracion,
)
from domain.ports.contrato_pdf_downloader import ContratoPdfDownloader
from domain.ports.valuation_context_repository import (
    RawAlbaranLine,
    ValuationContextRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValueAlbaranRequest:
    document_id: str
    codigo_contrato_override: str | None = None


def _albaran_line_to_dict(line: AlbaranLineForValuation) -> Dict[str, Any]:
    """Serializa AlbaranLineForValuation a dict serializable en JSON.

    ``asdict`` de ``dataclasses`` no recurse dentro de BaseModel de
    Pydantic (``contexto_linea``): lo deja como objeto y revienta al
    pasarlo por ``json.dumps``. Este helper hace el volcado a mano.
    """
    data = asdict(line)
    if line.contexto_linea is not None:
        data["contexto_linea"] = line.contexto_linea.model_dump(
            exclude_none=True,
        )
    else:
        # asdict ya lo deja como None, pero lo dejamos explícito para
        # que el LLM sepa que no aplica.
        data["contexto_linea"] = None
    return data


def _raw_albaran_line_to_dict(line: RawAlbaranLine) -> Dict[str, Any]:
    """Serializa una RawAlbaranLine (antes del prefilter) a dict.

    Usado solo en el envelope de status='no_contract', donde no hemos
    pasado aún por el prefilter y no tenemos ``contexto_linea``
    deserializado — ponemos el JSON crudo como está (puede ser None).
    """
    return {
        "merge_line_id": line.merge_line_id,
        "line_index": line.line_index,
        "codigo": line.codigo,
        "descripcion": line.descripcion,
        "unidad_medida": line.unidad_medida,
        "cantidad": line.cantidad,
        "precio_unitario_albaran": line.precio_unitario_albaran,
        "importe_albaran": line.importe_albaran,
        "codigo_partida_albaran": line.codigo_partida_albaran,
        "contexto_linea_json": line.contexto_linea_json,
    }


class ValueAlbaranPipeline:
    """Pipeline principal del servicio 5.

    Pasos:
      1. Cargar contexto (líneas albarán + líneas contrato + cabecera)
         desde la BBDD compartida.
      2. Clasificar la categoría de unidad de cada línea y
         deserializar el contexto_linea (determinista).
      3. Descargar el PDF del contrato desde SharePoint si hay path.
      4. Llamar a los LLMs habilitados.
      5. Devolver el envelope con meta + data por proveedor + debug.

    El envelope es consumido por el servicio 6 que hace la persistencia.
    """

    def __init__(
        self,
        *,
        context_repository: ValuationContextRepository,
        pdf_downloader: ContratoPdfDownloader,
        prefilter: UnitCategoryPrefilter,
        extraction_service: ValuationExtractionService,
        max_pdf_mb: int,
        service_version: str,
    ) -> None:
        self._context_repo = context_repository
        self._pdf_downloader = pdf_downloader
        self._prefilter = prefilter
        self._service = extraction_service
        self._max_pdf_mb = max_pdf_mb
        self._service_version = service_version

    @staticmethod
    def _utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def run(self, request: ValueAlbaranRequest) -> Dict[str, Any]:
        raw_ctx = self._context_repo.load_context(
            document_id=request.document_id,
            codigo_contrato_override=request.codigo_contrato_override,
        )

        if not raw_ctx.lineas_albaran:
            raise ValueError(
                f"El documento {request.document_id} no tiene líneas de "
                "albarán en el merge. No se puede valorar."
            )

        if raw_ctx.contrato is None:
            # Devolvemos envelope con status "no_contract" para que el
            # servicio 6 lo persista como tal. No hay IA.
            return self._envelope_no_contract(
                request=request,
                raw_ctx=raw_ctx,
            )

        contrato_header = raw_ctx.contrato
        lineas_albaran = self._prefilter.build_albaran_lines(
            raw_ctx.lineas_albaran
        )
        lineas_contrato = self._prefilter.build_contrato_lines(
            raw_ctx.lineas_contrato
        )

        context = ContextoValoracion(
            document_id=raw_ctx.document_id,
            codigo_contrato=contrato_header.codigo_contrato,
            nombre_contrato=contrato_header.nombre_contrato,
            cif_proveedor=contrato_header.cif_proveedor,
            nombre_proveedor=contrato_header.nombre_proveedor,
            codigo_obra=contrato_header.codigo_obra,
            nombre_obra=contrato_header.nombre_obra,
            pdf_relative_path=contrato_header.pdf_relative_path,
            pdf_filename=None,
            lineas_albaran=lineas_albaran,
            lineas_contrato=lineas_contrato,
        )

        pdf_attachment = self._try_download_pdf(contrato_header.pdf_relative_path)
        if pdf_attachment is not None:
            context = ContextoValoracion(
                document_id=context.document_id,
                codigo_contrato=context.codigo_contrato,
                nombre_contrato=context.nombre_contrato,
                cif_proveedor=context.cif_proveedor,
                nombre_proveedor=context.nombre_proveedor,
                codigo_obra=context.codigo_obra,
                nombre_obra=context.nombre_obra,
                pdf_relative_path=context.pdf_relative_path,
                pdf_filename=pdf_attachment.filename,
                lineas_albaran=context.lineas_albaran,
                lineas_contrato=context.lineas_contrato,
            )

        results = self._service.extract(
            context=context,
            pdf_attachment=pdf_attachment,
        )

        return self._build_envelope(
            context=context,
            results=results,
            pdf_attachment=pdf_attachment,
        )

    def _try_download_pdf(
        self,
        relative_path: str | None,
    ) -> LlmAttachment | None:
        if not relative_path:
            logger.info(
                "[pipeline] contrato sin pdf_sharepoint_relative_path; "
                "la IA solo hará fase 1a."
            )
            return None
        try:
            downloaded = self._pdf_downloader.download_by_relative_path(
                relative_path=relative_path,
            )
        except Exception:
            logger.exception(
                "[pipeline] fallo descargando PDF contrato path=%s; "
                "se continúa sin PDF.",
                relative_path,
            )
            return None

        size_mb = len(downloaded.data) / (1024 * 1024)
        if size_mb > self._max_pdf_mb:
            logger.warning(
                "[pipeline] PDF contrato demasiado grande (%.2f MB > %s MB). "
                "Se omite.",
                size_mb,
                self._max_pdf_mb,
            )
            return None

        return LlmAttachment(
            kind="pdf",
            filename=downloaded.filename or "contrato.pdf",
            mime_type=downloaded.mime_type or "application/pdf",
            data=downloaded.data,
        )

    def _envelope_no_contract(
        self,
        *,
        request: ValueAlbaranRequest,
        raw_ctx: Any,
    ) -> Dict[str, Any]:
        logger.warning(
            "[pipeline] document_id=%s no tiene contrato seleccionado; "
            "devolviendo envelope no_contract.",
            request.document_id,
        )
        return {
            "status": "no_contract",
            "meta": {
                "document_id": request.document_id,
                "codigo_contrato": None,
                "processed_at_utc": self._utc_iso(),
                "service": "albaranes-valuation-api",
                "service_version": self._service_version,
                "prompt_key": None,
                "providers_used": [],
            },
            "data": {"lineas": []},
            "context": {
                "lineas_albaran": [
                    _raw_albaran_line_to_dict(l)
                    for l in raw_ctx.lineas_albaran
                ],
                "lineas_contrato": [],
            },
            "debug": {},
        }

    def _build_envelope(
        self,
        *,
        context: ContextoValoracion,
        results: Dict[str, ProviderValuationResult],
        pdf_attachment: LlmAttachment | None,
    ) -> Dict[str, Any]:
        # Convención: si Claude está habilitado, su salida es la
        # principal (campo "data"). Si no, cae a Gemini, y si no a
        # OpenAI. El resto se anexa como proveedores secundarios.
        primary_order = ("claude", "gemini", "openai")
        primary_result: ProviderValuationResult | None = None
        primary_name: str | None = None
        for provider_name in primary_order:
            if provider_name in results:
                primary_result = results[provider_name]
                primary_name = provider_name
                break
        if primary_result is None:
            # Salvavidas: si ningún canónico está, coge el primero.
            primary_name, primary_result = next(iter(results.items()))

        pdf_sha = (
            hashlib.sha256(pdf_attachment.data).hexdigest()
            if pdf_attachment is not None
            else None
        )

        envelope: Dict[str, Any] = {
            "status": "ok",
            "meta": {
                "document_id": context.document_id,
                "codigo_contrato": context.codigo_contrato,
                "pdf_relative_path": context.pdf_relative_path,
                "pdf_filename": context.pdf_filename,
                "pdf_sha256": pdf_sha,
                "prompt_key": primary_result.prompt_key,
                "schema": primary_result.schema_name,
                "primary_provider": primary_name,
                "model": primary_result.model_name,
                "processed_at_utc": self._utc_iso(),
                "service": "albaranes-valuation-api",
                "service_version": self._service_version,
                "providers_used": sorted(results.keys()),
            },
            "data": primary_result.parsed.model_dump(),
            "context": {
                "lineas_albaran": [
                    _albaran_line_to_dict(l) for l in context.lineas_albaran
                ],
                "lineas_contrato": [asdict(l) for l in context.lineas_contrato],
            },
            "debug": {primary_name: primary_result.debug_payload},
        }

        for provider_name, provider_result in results.items():
            if provider_name == primary_name:
                continue
            envelope[provider_name] = {
                "meta": {
                    "provider": provider_name,
                    "model": provider_result.model_name,
                    "prompt_key": provider_result.prompt_key,
                    "schema": provider_result.schema_name,
                },
                "data": provider_result.parsed.model_dump(),
                "debug": provider_result.debug_payload,
            }

        return envelope
