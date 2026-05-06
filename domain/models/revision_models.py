# domain/models/revision_models.py
"""Schema de respuesta de la fase 2 (revisión).

NUEVO ENFOQUE — DICIEMBRE 2026:

La fase 2 ya NO devuelve una lista de cambios estructurados con valores
heterogéneos (que obligaba a `Any` y rompía OpenAI Structured Outputs,
o a `ScalarValue` y rompía la inserción de líneas nuevas).

Ahora la fase 2 devuelve directamente el documento revisado COMPLETO
con el mismo schema que fase 1 (`DocumentoAlbaran`), más una lista
de razonamientos textuales (uno por cada cambio que ha hecho).

Ventajas del enfoque:

  - Schema idéntico a fase 1 → compatibilidad total con las 3 IAs
    (Gemini, OpenAI, Claude). Ya está probado en fase 1.
  - Cero ambigüedad de tipos. Cada campo es lo que es.
  - Sin código de "patching": el output de fase 2 es el JSON final
    que se manda a sv3 (sv7 solo hace un diff opcional para marcar
    `source_phase` en líneas modificadas).
  - Si la fase 2 quiere añadir una línea, devuelve un objeto
    LineaAlbaran completo, válido contra el schema. Imposible
    confundirse.

Trade-off: la trazabilidad explícita de "qué cambió" se pierde a
nivel estructurado. Se conserva como texto libre en `razonamientos`,
y sv7 puede recalcularla por diff si la necesita.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import Field

from domain.models.albaran_models import DocumentoAlbaran
from domain.models.schema_base import StrictSchemaModel


ReviewStatus = Literal["ok", "ok_with_changes", "inconsistent"]


class Razonamiento(StrictSchemaModel):
    """Justificación textual de un cambio aplicado por la fase 2.

    No contiene valores: solo describe en lenguaje natural qué se
    cambió y por qué. Los valores reales viven en
    ``documento_revisado``.
    """

    campo: str = Field(
        ...,
        description=(
            "Ruta del campo cambiado, en notación dot/bracket. "
            "Ejemplos: 'cabecera.fecha', 'lineas[0].precio', "
            "'lineas[3]' (línea entera nueva o eliminada)."
        ),
    )
    descripcion: str = Field(
        ...,
        description=(
            "Explicación breve del cambio en lenguaje natural. "
            "Ejemplo: 'Cambiado el precio de 0.0085 a 0.85 porque "
            "en el albarán se lee claramente 0,85 €/kg en la "
            "columna PVP de la línea 4.'"
        ),
    )
    patron_aplicado: Optional[str] = Field(
        default=None,
        description=(
            "Identificador de la regla de revision_rules.yaml que "
            "disparó el cambio. Ejemplo: 'importe_minimo'. "
            "Opcional: si el cambio no encaja en ninguna regla, "
            "déjalo vacío."
        ),
    )


class RevisionAlbaranFase2(StrictSchemaModel):
    """Respuesta completa de la fase 2.

    review_status:
      - 'ok': no se ha cambiado nada. ``documento_revisado`` igual al
        de fase 1.
      - 'ok_with_changes': se ha cambiado al menos un campo. Mira
        ``razonamientos`` para ver qué.
      - 'inconsistent': el documento parece ilegible o contradictorio.
        ``documento_revisado`` puede contener el mejor intento de
        corrección, pero el revisor humano debe verificar.

    documento_revisado:
      JSON completo con el mismo schema que fase 1
      (DocumentoAlbaran). Si no había nada que cambiar, es idéntico
      al input.

    razonamientos:
      Lista (puede estar vacía si review_status == 'ok'). Una entrada
      por cada cambio aplicado.
    """

    review_status: ReviewStatus = Field(
        ...,
        description="Resultado global de la revisión.",
    )
    explicacion_global: Optional[str] = Field(
        default=None,
        description=(
            "Resumen 1-3 frases del diagnóstico. Si 'ok', describe "
            "brevemente qué se validó. Si 'ok_with_changes', resume "
            "el tipo de errores. Si 'inconsistent', explica por qué."
        ),
    )
    documento_revisado: DocumentoAlbaran = Field(
        ...,
        description=(
            "Documento corregido completo, con el mismo schema que "
            "fase 1. Si no hubo cambios, idéntico al de fase 1."
        ),
    )
    razonamientos: List[Razonamiento] = Field(
        default_factory=list,
        description=(
            "Lista de cambios realizados. Vacía si review_status="
            "'ok'. Una entrada por cada campo modificado."
        ),
    )
