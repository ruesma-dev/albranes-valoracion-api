# application/pipelines/extract_albaran_pipeline.py
"""Pipeline que orquesta UN proveedor LLM por fase.

CAMBIO RESPECTO A LA VERSIÓN ANTERIOR:
  - Ya no se ejecutan TODOS los proveedores LLM en paralelo en una
    única llamada. Ahora hay dos métodos:
       run_phase_1(file)            -> envelope con bloque {meta,data,debug}
                                        producido por IA_PRIMERA_FASE.
       run_phase_2(file, fase1_json)-> envelope con bloque {meta,data,debug}
                                        producido por IA_SEGUNDA_FASE.
  - El "merge" entre fase 1 y fase 2 NO lo hace sv2. Lo hace sv7
    aplicando el patch de fase 2 sobre el envelope de fase 1.
  - El envelope sigue siendo un dict de la misma forma que antes —
    así sv3 (que ya consume {meta,data,debug,gemini?,claude?}) no
    necesita cambios estructurales en este punto. Solo añadirá
    metadatos de fase 2 cuando sv7 le pase el envelope mergeado.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict

from application.services.albaran_extraction_service import (
    AlbaranExtractionService,
    ProviderExtractionResult,
)
from domain.models.llm_attachment import LlmAttachment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractAlbaranRequest:
    filename: str
    mime_type: str
    file_bytes: bytes


@dataclass(frozen=True)
class ReviewAlbaranRequest:
    filename: str
    mime_type: str
    file_bytes: bytes
    phase_1_json: dict
    # Grounding determinista de cabecera contra Sigrid (jun 2026).
    # Lo genera sv3 (POST /v1/sigrid/header-grounding), lo reenvía sv7
    # y aquí se inyecta en el prompt de fase 2. None → fase 2 clásica.
    sigrid_context: dict | None = None


class ExtractAlbaranPipeline:
    def __init__(
        self,
        *,
        extraction_service: AlbaranExtractionService,
        max_file_mb: int,
        service_version: str,
        provider_phase_1: str,
        provider_phase_2: str,
        prompt_key_phase_1: str,
        prompt_key_phase_2: str,
    ) -> None:
        self._service = extraction_service
        self._max_file_mb = max_file_mb
        self._service_version = service_version
        self._provider_phase_1 = provider_phase_1
        self._provider_phase_2 = provider_phase_2
        self._prompt_key_phase_1 = prompt_key_phase_1
        self._prompt_key_phase_2 = prompt_key_phase_2

    # ----------------------------------------------------------- #
    # FASE 1 — extracción inicial.
    # ----------------------------------------------------------- #
    def run_phase_1(self, request: ExtractAlbaranRequest) -> Dict[str, Any]:
        attachment = self._build_attachment_validated(
            filename=request.filename,
            mime_type=request.mime_type,
            file_bytes=request.file_bytes,
        )
        result = self._service.extract_phase_1(
            attachment=attachment,
            provider=self._provider_phase_1,
            prompt_key=self._prompt_key_phase_1,
        )
        sha256 = hashlib.sha256(request.file_bytes).hexdigest()
        return self._envelope_block(
            provider_result=result,
            attachment=attachment,
            sha256=sha256,
            phase_label="phase_1",
        )

    # ----------------------------------------------------------- #
    # FASE 2 — revisión sobre la imagen + JSON de fase 1.
    # ----------------------------------------------------------- #
    def run_phase_2(self, request: ReviewAlbaranRequest) -> Dict[str, Any]:
        attachment = self._build_attachment_validated(
            filename=request.filename,
            mime_type=request.mime_type,
            file_bytes=request.file_bytes,
        )
        result = self._service.review_phase_2(
            attachment=attachment,
            provider=self._provider_phase_2,
            prompt_key=self._prompt_key_phase_2,
            phase_1_json=request.phase_1_json,
            sigrid_context=request.sigrid_context,
        )
        sha256 = hashlib.sha256(request.file_bytes).hexdigest()
        return self._envelope_block(
            provider_result=result,
            attachment=attachment,
            sha256=sha256,
            phase_label="phase_2",
        )

    # ----------------------------------------------------------- #
    # Helpers privados.
    # ----------------------------------------------------------- #
    def _build_attachment_validated(
        self,
        *,
        filename: str,
        mime_type: str,
        file_bytes: bytes,
    ) -> LlmAttachment:
        if not file_bytes:
            raise ValueError("Archivo vacío.")

        size_mb = len(file_bytes) / (1024 * 1024)
        if size_mb > self._max_file_mb:
            raise ValueError(
                f"Archivo demasiado grande ({size_mb:.2f} MB) > "
                f"MAX_FILE_MB={self._max_file_mb}"
            )

        return self._build_attachment(
            filename=filename,
            mime_type=mime_type,
            file_bytes=file_bytes,
        )

    @staticmethod
    def _build_attachment(
        filename: str,
        mime_type: str,
        file_bytes: bytes,
    ) -> LlmAttachment:
        filename = filename or "document.bin"
        is_pdf = (
            mime_type == "application/pdf"
            or filename.lower().endswith(".pdf")
        )
        if is_pdf:
            return LlmAttachment(
                kind="pdf",
                filename=filename,
                mime_type="application/pdf",
                data=file_bytes,
            )

        guessed_mime, _ = mimetypes.guess_type(filename)
        final_mime = mime_type or guessed_mime or "image/jpeg"
        return LlmAttachment(
            kind="image",
            filename=filename,
            mime_type=final_mime,
            data=file_bytes,
        )

    @staticmethod
    def _utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _envelope_block(
        self,
        *,
        provider_result: ProviderExtractionResult,
        attachment: LlmAttachment,
        sha256: str,
        phase_label: str,
    ) -> Dict[str, Any]:
        meta = {
            "phase": phase_label,
            "prompt_key": provider_result.prompt_key,
            "schema": provider_result.schema_name,
            "provider": provider_result.provider,
            "model": provider_result.model_name,
            "source_filename": attachment.filename,
            "source_mime_type": attachment.mime_type,
            "source_sha256": sha256,
            "processed_at_utc": self._utc_iso(),
            "service": "albaranes-extractor-api",
            "service_version": self._service_version,
        }
        return {
            "meta": meta,
            "data": provider_result.parsed.model_dump(),
            "debug": provider_result.debug_payload,
        }
