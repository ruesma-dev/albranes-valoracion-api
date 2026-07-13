# application/services/conciliacion_service.py
"""IA4 - servicio de conciliacion semantica (en lote).

Se invoca DESPUES del matching determinista de sv6, con el LOTE de lineas
que quedaron sin casar (o casadas sin precio) y la TABLA de lineas del
contrato. Hace UNA sola llamada al LLM (proveedor primario) con el prompt
``conciliacion_es`` y devuelve ``DocumentoConciliacion`` (por cada linea:
matched_contrato_line_id + precio_unitario_contrato_db). NO calcula
importes: eso es determinista en sv6.
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Sequence

from application.services.valuation_extraction_service import (
    ProviderClientSpec,
)
from domain.models.conciliacion_models import DocumentoConciliacion

logger = logging.getLogger(__name__)


class ConciliacionService:
    def __init__(
        self,
        *,
        providers: Sequence[ProviderClientSpec],
        prompt_repo: Any,
        schema_registry: Any,
        prompt_key: str = "conciliacion_es",
    ) -> None:
        self._providers = list(providers)
        self._prompts = prompt_repo
        self._schemas = schema_registry
        self._prompt_key = prompt_key
        if not self._providers:
            raise ValueError("ConciliacionService requiere al menos un proveedor")

    def conciliar(
        self,
        *,
        lineas_no_casadas: List[dict],
        lineas_contrato: List[dict],
    ) -> DocumentoConciliacion:
        """Concilia el lote de lineas contra las lineas de contrato."""
        spec = self._prompts.get(self._prompt_key)
        response_model = self._schemas.get(spec.schema)

        # Si no hay nada que conciliar, no llamamos al LLM.
        if not lineas_no_casadas:
            return DocumentoConciliacion(conciliaciones=[])

        user_text = self._build_user_text(
            spec_task=spec.task,
            spec_schema_hint=spec.schema_hint,
            lineas_no_casadas=lineas_no_casadas,
            lineas_contrato=lineas_contrato,
        )

        # IA4 = una sola llamada, con el proveedor primario.
        provider = self._providers[0]
        logger.info(
            "[conciliacion] IA4 proveedor=%s model=%s lote=%s lineas_contrato=%s",
            provider.provider,
            provider.model_name,
            len(lineas_no_casadas),
            len(lineas_contrato),
        )
        parsed = provider.client.extract_document(
            model=provider.model_name,
            instructions=spec.system,
            user_text=user_text,
            attachment=None,
            response_model=response_model,
        )
        if not isinstance(parsed, DocumentoConciliacion):
            # El cliente devuelve el response_model; salvaguarda de tipo.
            parsed = DocumentoConciliacion.model_validate(parsed.model_dump())
        logger.info(
            "[conciliacion] IA4 -> %s conciliaciones",
            len(parsed.conciliaciones),
        )
        return parsed

    @staticmethod
    def _build_user_text(
        *,
        spec_task: str,
        spec_schema_hint: str,
        lineas_no_casadas: List[dict],
        lineas_contrato: List[dict],
    ) -> str:
        lote = json.dumps(lineas_no_casadas, ensure_ascii=False, indent=2)
        contrato = json.dumps(lineas_contrato, ensure_ascii=False, indent=2)
        return (
            f"{spec_task}\n\n{spec_schema_hint}\n\n"
            "## LINEAS DEL ALBARAN A CONCILIAR (lote)\n\n"
            f"```json\n{lote}\n```\n\n"
            "## LINEAS DE CONTRATO DISPONIBLES (Sigrid)\n\n"
            f"```json\n{contrato}\n```\n"
        )
