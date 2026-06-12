# application/services/albaran_extraction_service.py
"""Servicio de extracción y revisión de albaranes (sv2).

Tiene DOS responsabilidades, expuestas como métodos distintos:

  - ``extract_phase_1``: ejecuta UN proveedor LLM contra la imagen
    para producir un ``DocumentoAlbaran`` (extracción inicial).

  - ``review_phase_2``: ejecuta UN proveedor LLM contra la imagen +
    el JSON de fase 1 para producir un ``RevisionAlbaranFase2``
    (patch de cambios sugeridos).

El elegir QUÉ proveedor se usa en cada fase lo gobierna la capa
superior (interface_adapters/api/app.py) leyendo ``IA_PRIMERA_FASE``
e ``IA_SEGUNDA_FASE`` del .env.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Type

from pydantic import BaseModel

from application.services.schema_registry import SchemaRegistry
from domain.models.llm_attachment import LlmAttachment
from domain.ports.llm_client import LlmVisionClient
from domain.ports.prompt_repository import PromptRepository
from infrastructure.prompts.revision_rules_repository import (
    RevisionRulesRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderClientSpec:
    """Spec de un proveedor LLM disponible en este servicio."""
    provider: str
    model_name: str
    client: LlmVisionClient
    prompt_supported: bool = True


@dataclass(frozen=True)
class ProviderExtractionResult:
    """Resultado de invocar UN proveedor LLM (fase 1 o fase 2)."""
    provider: str
    model_name: str
    schema_name: str
    prompt_key: str
    parsed: BaseModel
    debug_payload: Dict[str, Any]


class AlbaranExtractionService:
    """Servicio que orquesta la llamada a UN proveedor LLM concreto.

    Expone métodos por fase (extract_phase_1, review_phase_2). Cada
    invocación elige UN proveedor por nombre del catálogo de
    ProviderClientSpec.
    """

    def __init__(
        self,
        *,
        providers: Iterable[ProviderClientSpec],
        prompt_repo: PromptRepository,
        schema_registry: SchemaRegistry,
        revision_rules_repo: RevisionRulesRepository,
        prompt_key_phase_1: str,
    ) -> None:
        self._providers_by_name: Dict[str, ProviderClientSpec] = {
            spec.provider: spec for spec in providers
        }
        self._prompts = prompt_repo
        self._schemas = schema_registry
        self._revision_rules_repo = revision_rules_repo
        # Necesitamos saber qué prompt usó la fase 1 para poderlo
        # incrustar en las instructions de la fase 2. Lo recibe el
        # servicio en construcción (lo lee app.py de settings).
        self._prompt_key_phase_1 = prompt_key_phase_1

    # ---------------------------------------------------------- #
    # FASE 1 — extracción inicial.
    # ---------------------------------------------------------- #
    def extract_phase_1(
        self,
        *,
        attachment: LlmAttachment,
        provider: str,
        prompt_key: str,
    ) -> ProviderExtractionResult:
        spec = self._require_provider(provider)
        prompt_spec = self._prompts.get(prompt_key)
        response_model = self._schemas.get(prompt_spec.schema)

        instructions = self._build_instructions(prompt_spec)
        user_text = (
            "Documento adjunto. Extrae el albarán siguiendo las reglas "
            "del prompt. Devuelve SOLO JSON válido conforme al schema."
        )

        logger.info(
            "Extracción FASE 1 proveedor=%s prompt_key=%s schema=%s "
            "model=%s filename=%s",
            spec.provider, prompt_key, prompt_spec.schema,
            spec.model_name, attachment.filename,
        )

        return self._invoke_provider(
            spec=spec,
            instructions=instructions,
            user_text=user_text,
            attachment=attachment,
            response_model=response_model,
            schema_name=prompt_spec.schema,
            prompt_key=prompt_key,
            phase_label="phase_1",
        )

    # ---------------------------------------------------------- #
    # FASE 2 — revisión.
    # ---------------------------------------------------------- #
    def review_phase_2(
        self,
        *,
        attachment: LlmAttachment,
        provider: str,
        prompt_key: str,
        phase_1_json: dict,
        sigrid_context: dict | None = None,
    ) -> ProviderExtractionResult:
        """Ejecuta la revisión de fase 2.

        El prompt de fase 2 (config/prompts.yaml → albaran_revision_fase2_es)
        contiene 4 placeholders en su ``task``:
          - {prompt_fase_1}: el system+task del prompt de fase 1, para
            que la IA conozca las reglas que se siguieron en la
            extracción.
          - {revision_rules}: la checklist de patrones conocidos
            cargada de config/revision_rules.yaml.
          - {json_fase_1}: el JSON producido por la fase 1, para que
            la IA lo compare con el PDF y devuelva el documento
            corregido.
          - {sigrid_context}: (jun 2026) bloque de grounding contra el
            ERP. Si ``sigrid_context`` es None, el placeholder se
            sustituye por una nota de "no disponible" y la fase 2 se
            comporta exactamente como antes.

        Aquí los renderizamos antes de construir las instructions
        finales del LLM. El ``user_text`` queda mínimo (la "carne"
        de la petición ya está en las instructions).
        """
        spec = self._require_provider(provider)
        prompt_spec = self._prompts.get(prompt_key)
        response_model = self._schemas.get(prompt_spec.schema)

        # -- Cargar contenidos para los placeholders -- #
        # El prompt_key_phase_1 lo recibimos en el constructor (lo
        # lee app.py de settings.prompt_key_fase1) — así no obligamos
        # al pipeline a pasarlo en cada llamada y mantenemos la firma
        # simple.
        prompt_fase_1_spec = self._prompts.get(self._prompt_key_phase_1)
        prompt_fase_1_text = self._build_instructions(prompt_fase_1_spec)
        revision_rules_text = self._revision_rules_repo.render_for_prompt()
        json_fase_1_text = json.dumps(
            phase_1_json, ensure_ascii=False, indent=2,
        )
        sigrid_context_text = self._render_sigrid_context(sigrid_context)

        # -- Renderizar el task de fase 2 con sus placeholders -- #
        # IMPORTANTE: usamos str.replace en vez de .format() porque
        # el task contiene llaves de ejemplos JSON ("{...}") que
        # romperían el .format(). replace() es más robusto.
        task_rendered = prompt_spec.task
        task_rendered = task_rendered.replace(
            "{prompt_fase_1}", prompt_fase_1_text,
        )
        task_rendered = task_rendered.replace(
            "{revision_rules}", revision_rules_text,
        )
        task_rendered = task_rendered.replace(
            "{json_fase_1}", json_fase_1_text,
        )
        # Compatibilidad: si el prompts.yaml desplegado aún no tiene el
        # placeholder {sigrid_context}, el bloque se APPENDEA al final
        # del task (mejor inyectarlo en posición subóptima que perderlo).
        if "{sigrid_context}" in task_rendered:
            task_rendered = task_rendered.replace(
                "{sigrid_context}", sigrid_context_text,
            )
        elif sigrid_context is not None:
            task_rendered = (
                f"{task_rendered}\n\n{sigrid_context_text}"
            )

        # Reconstruimos el spec con el task renderizado (sin tocar el
        # original — Python hace inmutables nuestras instancias en
        # caliente, pero por seguridad clonamos con namedtuple-like).
        instructions = self._compose_instructions(
            system=prompt_spec.system,
            task=task_rendered,
            schema_hint=prompt_spec.schema_hint,
        )

        user_text = (
            "Documento adjunto: PDF original del albarán.\n\n"
            "El JSON de fase 1 a revisar y la checklist de reglas ya "
            "están en las instrucciones del sistema. Tu tarea: "
            "comparar el PDF con el JSON, aplicar la checklist y "
            "devolver el documento corregido completo (mismo schema "
            "que fase 1) más los razonamientos de cada cambio."
        )

        logger.info(
            "Revisión FASE 2 proveedor=%s prompt_key=%s schema=%s "
            "model=%s filename=%s json_fase1_chars=%d "
            "rules_count=%d sigrid_grounding=%s",
            spec.provider, prompt_key, prompt_spec.schema,
            spec.model_name, attachment.filename,
            len(json_fase_1_text),
            self._revision_rules_repo.count,
            "SI" if sigrid_context is not None else "NO",
        )

        return self._invoke_provider(
            spec=spec,
            instructions=instructions,
            user_text=user_text,
            attachment=attachment,
            response_model=response_model,
            schema_name=prompt_spec.schema,
            prompt_key=prompt_key,
            phase_label="phase_2",
            extra_debug={
                "phase_1_json": phase_1_json,
                "revision_rules_count": self._revision_rules_repo.count,
                "revision_rules_ids": self._revision_rules_repo.rule_ids,
                "sigrid_context": sigrid_context,
            },
        )

    @staticmethod
    def _render_sigrid_context(sigrid_context: dict | None) -> str:
        """Construye el bloque de texto del grounding para el prompt.

        Estructura esperada (generada por sv3 HeaderGroundingService):
          { "proveedor": {status, cif, nombre_canonico, ...},
            "obra": {status, codigo, nombre, direccion, ...},
            "obras_candidatas": [{codigo, nombre}, ...],
            "proveedores_candidatos": [{cif, nombre}, ...] }

        Reglas que se trasladan a la IA:
          - Bloque VALIDADO (CIF/código existen en el ERP) → NO revisar
            esos campos; copiar los valores canónicos tal cual.
          - Bloque NO validado → revisarlo usando los candidatos: si el
            texto leído casa claramente con un candidato (p.ej. mismo
            nombre con erratas de OCR), corregir nombre/código/CIF con
            los del candidato y registrar el razonamiento con
            patron_aplicado='sigrid_grounding'.
        """
        if sigrid_context is None:
            return (
                "(Grounding Sigrid no disponible en esta ejecución: "
                "revisa la cabecera solo contra el PDF, como siempre.)"
            )

        proveedor = sigrid_context.get("proveedor") or {}
        obra = sigrid_context.get("obra") or {}
        obras_cand = sigrid_context.get("obras_candidatas") or []
        provs_cand = sigrid_context.get("proveedores_candidatos") or []

        lines: list[str] = []
        lines.append(
            "Validación DETERMINISTA contra el ERP Sigrid (fuente de "
            "verdad de proveedores y obras de Construcciones Ruesma):"
        )
        lines.append("")

        # ---- Proveedor -------------------------------------------- #
        if proveedor.get("status") == "validated":
            lines.append(
                "PROVEEDOR — VALIDADO POR CIF (no lo revises): el CIF "
                f"{proveedor.get('cif')} existe en el ERP. En "
                "documento_revisado escribe EXACTAMENTE: "
                f"proveedor_cif={proveedor.get('cif')!r} y "
                f"proveedor_nombre={proveedor.get('nombre_canonico')!r}. "
                "NO añadas razonamientos sobre el proveedor aunque el "
                "PDF muestre una variante del nombre."
            )
        elif proveedor.get("status") == "not_found":
            lines.append(
                "PROVEEDOR — el CIF leído "
                f"({proveedor.get('cif_leido')!r}) NO existe en el ERP. "
                "REVÍSALO: probablemente hay un error de OCR en el CIF "
                "o en el nombre. Usa la lista de PROVEEDORES CANDIDATOS "
                "de más abajo: si el nombre leído "
                f"({proveedor.get('nombre_leido')!r}) casa claramente "
                "con un candidato, corrige proveedor_cif y "
                "proveedor_nombre con los del candidato y añade un "
                "razonamiento con patron_aplicado='sigrid_grounding'. "
                "Si ningún candidato casa con claridad, deja los "
                "valores de fase 1."
            )
        else:
            lines.append(
                "PROVEEDOR — sin CIF utilizable en fase 1. Si la lista "
                "de PROVEEDORES CANDIDATOS contiene uno que case "
                "claramente con el nombre del PDF, usa su cif y nombre "
                "(patron_aplicado='sigrid_grounding'); si no, deja lo "
                "de fase 1."
            )
        lines.append("")

        # ---- Obra -------------------------------------------------- #
        if obra.get("status") == "validated":
            lines.append(
                "OBRA — VALIDADA POR CÓDIGO (no la revises): el código "
                f"{obra.get('codigo')} existe en el ERP. En "
                "documento_revisado escribe EXACTAMENTE: "
                f"obra_codigo={obra.get('codigo')!r}, "
                f"obra_nombre={obra.get('nombre')!r} y "
                f"obra_direccion={obra.get('direccion')!r}."
            )
        elif obra.get("status") == "not_found":
            lines.append(
                "OBRA — el código leído "
                f"({obra.get('codigo_leido')!r}) NO existe en el ERP. "
                "REVÍSALA con la lista de OBRAS CANDIDATAS: si el "
                "nombre/dirección del PDF casa claramente con una "
                "candidata, corrige obra_codigo y obra_nombre con los "
                "de la candidata (patron_aplicado='sigrid_grounding'). "
                "Si no hay coincidencia clara, deja lo de fase 1."
            )
        else:
            lines.append(
                "OBRA — sin código utilizable en fase 1. Si una OBRA "
                "CANDIDATA casa claramente con el nombre/dirección del "
                "PDF, usa su código y nombre "
                "(patron_aplicado='sigrid_grounding'); si no, deja lo "
                "de fase 1."
            )
        lines.append("")

        # ---- Candidatos (solo si los hay) -------------------------- #
        if provs_cand:
            lines.append("PROVEEDORES CANDIDATOS (cif — nombre):")
            for item in provs_cand:
                lines.append(
                    f"  - {item.get('cif')} — {item.get('nombre')}"
                )
            lines.append("")
        if obras_cand:
            lines.append("OBRAS CANDIDATAS (codigo — nombre):")
            for item in obras_cand:
                lines.append(
                    f"  - {item.get('codigo')} — {item.get('nombre')}"
                )
            lines.append("")

        lines.append(
            "Recuerda: el grounding SOLO afecta a la cabecera "
            "(proveedor/obra). Las líneas del albarán se revisan con "
            "las reglas y la checklist habituales."
        )
        return "\n".join(lines)

    # ---------------------------------------------------------- #
    # Helpers internos.
    # ---------------------------------------------------------- #
    def _require_provider(self, name: str) -> ProviderClientSpec:
        spec = self._providers_by_name.get(name)
        if spec is None:
            available = ", ".join(sorted(self._providers_by_name.keys()))
            raise KeyError(
                f"Proveedor LLM '{name}' no instanciado en este servicio. "
                f"Disponibles: {available}. Revisa los flags ENABLE_* en "
                f"el .env."
            )
        return spec

    @staticmethod
    def _build_instructions(prompt_spec) -> str:
        # Concatenamos system + task + schema_hint para el system prompt
        # del proveedor (como hacía la versión anterior). Cada provider
        # client decidirá cómo lo distribuye en su API concreta.
        return AlbaranExtractionService._compose_instructions(
            system=prompt_spec.system,
            task=prompt_spec.task,
            schema_hint=prompt_spec.schema_hint,
        )

    @staticmethod
    def _compose_instructions(
        *,
        system: str | None,
        task: str | None,
        schema_hint: str | None,
    ) -> str:
        """Versión que acepta strings ya renderizados.

        Útil cuando el ``task`` lleva placeholders que se han
        sustituido externamente (como en review_phase_2).
        """
        parts = [system, task, schema_hint]
        return "\n\n".join(p for p in parts if p)

    def _invoke_provider(
        self,
        *,
        spec: ProviderClientSpec,
        instructions: str,
        user_text: str,
        attachment: LlmAttachment,
        response_model: Type[BaseModel],
        schema_name: str,
        prompt_key: str,
        phase_label: str,
        extra_debug: Optional[Dict[str, Any]] = None,
    ) -> ProviderExtractionResult:
        parsed = spec.client.extract_document(
            model=spec.model_name,
            instructions=instructions,
            user_text=user_text,
            attachment=attachment,
            response_model=response_model,
        )

        prompt_note = None
        if not spec.prompt_supported:
            prompt_note = (
                "La API de este proveedor no acepta un prompt arbitrario "
                "por petición; se conserva el mismo prompt para "
                "trazabilidad pero la extracción la gobierna el "
                "modelo/procesador configurado."
            )

        debug_payload: Dict[str, Any] = {
            f"{spec.provider}_request": {
                "provider": spec.provider,
                "model": spec.model_name,
                "prompt_key": prompt_key,
                "phase": phase_label,
                "attachment": self._attachment_debug(attachment),
                "prompt_note": prompt_note,
            },
            f"{spec.provider}_response": {
                "schema": schema_name,
                "parsed_keys": (
                    list(parsed.model_dump().keys())
                    if hasattr(parsed, "model_dump") else []
                ),
            },
        }
        if extra_debug:
            debug_payload.update(extra_debug)

        return ProviderExtractionResult(
            provider=spec.provider,
            model_name=spec.model_name,
            schema_name=schema_name,
            prompt_key=prompt_key,
            parsed=parsed,
            debug_payload=debug_payload,
        )

    @staticmethod
    def _attachment_debug(attachment: LlmAttachment) -> Dict[str, Any]:
        return {
            "kind": attachment.kind,
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "size_bytes": len(attachment.data),
            "sha256": hashlib.sha256(attachment.data).hexdigest(),
        }
