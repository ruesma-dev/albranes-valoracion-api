# domain/models/valuation_models.py
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import Field, model_validator

from domain.models.schema_base import StrictSchemaModel


MatchMethod = Literal["exact_concept", "semantic", "price_only", "no_match"]

# -----------------------------------------------------------------------------
# LineKind (V3):
#   - 'from_albaran': la línea procede de una línea real del albarán
#                     (merge_line_id no nulo).
#   - 'synthetic_modifier': línea generada por el valorador para
#                           representar un modificador implícito que no
#                           aparece como línea en el albarán pero sí está
#                           tarifado en el contrato (ver prompt V3 del svc5).
#                           merge_line_id es null; parent_merge_line_id
#                           apunta a la línea base de la que cuelga.
# -----------------------------------------------------------------------------
LineKind = Literal["from_albaran", "synthetic_modifier"]

ModifierSource = Literal[
    "codigo_producto",
    "observaciones",
    "year_contract",
    "year_albaran",
    "tiempo_exceso",
    "gestion_residuos",
    "otro",
]


class LineValuation(StrictSchemaModel):
    """Output de la IA para una línea de valoración (V3).

    V3 añade el concepto de LÍNEA SINTÉTICA: el LLM puede emitir líneas
    adicionales a las del albarán para representar modificadores
    implícitos (año, consistencia, árido, aditivo, gestión de residuos,
    exceso de tiempo). Estas líneas sintéticas llevan:
      - line_kind = 'synthetic_modifier'
      - merge_line_id = null
      - parent_merge_line_id = merge_line_id de la línea base que las origina
      - modifier_source / modifier_reason / descripcion_linea rellenos
      - rol_linea = 'incremento_year' | 'incremento_consistencia' | ...

    La IA SOLO rellena los resultados de las fases 1a y 1b:
      - matched_contrato_line_id + precio_unitario_contrato_db (fase 1a)
      - precio_unitario_pdf_inferido + pdf_inference_reasoning (fase 1b)
      - categoría de unidad confirmada + match_confidence + razón

    NO rellena: partida final, cantidad convertida, importe calculado.
    Eso lo hace el servicio 6.
    """

    merge_line_id: Optional[int] = Field(
        default=None,
        description=(
            "Id de la línea del albarán (albaran_lines_merge.id) que se "
            "recibió en el input. OBLIGATORIO si line_kind='from_albaran'. "
            "NULL si line_kind='synthetic_modifier' (líneas generadas por "
            "el valorador que no existen en el albarán)."
        ),
    )

    line_kind: LineKind = Field(
        default="from_albaran",
        description=(
            "'from_albaran' si corresponde a una línea real del albarán. "
            "'synthetic_modifier' si es un modificador sintético generado "
            "por el valorador (incremento por año, consistencia, árido, "
            "aditivo, gestión de residuos, exceso de tiempo)."
        ),
    )

    parent_merge_line_id: Optional[int] = Field(
        default=None,
        description=(
            "Solo para line_kind='synthetic_modifier': el merge_line_id de "
            "la línea base del albarán de la que cuelga este modificador. "
            "Null para líneas 'from_albaran'."
        ),
    )

    modifier_source: Optional[ModifierSource] = Field(
        default=None,
        description=(
            "Solo para sintéticas: fuente del modificador. Null para "
            "líneas 'from_albaran'."
        ),
    )

    modifier_reason: Optional[str] = Field(
        default=None,
        description=(
            "Solo para sintéticas: razón corta en lenguaje humano "
            "(ej. 'consistencia F en código HA-25/F/20', 'año 2026 "
            "vs contrato 2024', 'retraso 33 min sobre límite 10:47')."
        ),
    )

    descripcion_linea: Optional[str] = Field(
        default=None,
        description=(
            "Solo para sintéticas: descripción corta que verá el revisor "
            "en la UI (ej. 'INCREMENTO POR CONSISTENCIA FLUIDA'). Null "
            "para líneas 'from_albaran' (la descripción viene del "
            "albarán)."
        ),
    )

    rol_linea: Optional[str] = Field(
        default=None,
        description=(
            "Rol de la línea dentro de su familia. Para from_albaran "
            "copia el rol del contexto_linea de entrada ('base' "
            "normalmente). Para sintéticas: 'incremento_year' | "
            "'incremento_consistencia' | 'incremento_arido' | "
            "'incremento_aditivo' | 'incremento_residuos' | "
            "'incremento_tiempo' | 'incremento_otro'."
        ),
    )

    match_method: MatchMethod = Field(
        description="exact_concept | semantic | price_only | no_match",
    )
    matched_contrato_line_id: Optional[int] = Field(
        default=None,
        description=(
            "Id de la línea del contrato (albaran_contrato_lines_merge.id) "
            "que la IA considera match. Null si no hay. Para sintéticas "
            "casi siempre será null (la tarifa viene del PDF, no de una "
            "línea de tabla)."
        ),
    )
    match_confidence_pct: float = Field(
        ge=0, le=100,
        description="Confianza 0-100 del matching hecho.",
    )
    unidad_categoria_albaran: str = Field(
        description=(
            "Categoría confirmada de la unidad: mass|volume|length|area|"
            "count|time|lump_sum|unknown."
        ),
    )
    unidad_category_match: bool = Field(
        description=(
            "True solo si hay una línea de contrato con categoría "
            "compatible Y fue casable. Si no, false."
        ),
    )
    precio_unitario_contrato_db: Optional[float] = Field(
        default=None,
        description=(
            "Precio unitario de la línea de contrato casada (fase 1a). "
            "Si matched_contrato_line_id es null, este también lo es."
        ),
    )
    precio_unitario_pdf_inferido: Optional[float] = Field(
        default=None,
        description=(
            "Precio unitario leído del PDF del contrato (fase 1b). Null "
            "si no se ha podido deducir (Forma C: modificador no tarifado)."
        ),
    )
    pdf_inference_reasoning: Optional[str] = Field(
        default=None,
        description=(
            "Explicación corta del lugar del PDF donde se ha encontrado "
            "el precio inferido."
        ),
    )
    razon_corta: str = Field(
        description=(
            "Explicación corta del matching hecho, en español, para "
            "auditoría."
        ),
    )

    # ----------------------------------------------------------------- #
    # Validación cruzada: coherencia de campos según line_kind.
    # ----------------------------------------------------------------- #

    @model_validator(mode="after")
    def _validate_kind_coherence(self) -> "LineValuation":
        if self.line_kind == "from_albaran":
            if self.merge_line_id is None:
                raise ValueError(
                    "line_kind='from_albaran' requiere merge_line_id no nulo."
                )
            if self.parent_merge_line_id is not None:
                raise ValueError(
                    "line_kind='from_albaran' no puede tener "
                    "parent_merge_line_id."
                )
        elif self.line_kind == "synthetic_modifier":
            if self.merge_line_id is not None:
                raise ValueError(
                    "line_kind='synthetic_modifier' requiere merge_line_id "
                    "nulo."
                )
            if self.parent_merge_line_id is None:
                raise ValueError(
                    "line_kind='synthetic_modifier' requiere "
                    "parent_merge_line_id no nulo."
                )
            if self.descripcion_linea is None:
                raise ValueError(
                    "line_kind='synthetic_modifier' requiere descripcion_linea."
                )
        return self


class DocumentoValoracion(StrictSchemaModel):
    lineas: List[LineValuation]
