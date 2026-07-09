# domain/models/valuation_models.py
from __future__ import annotations

from typing import Any, List, Literal, Optional

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
    "carga_incompleta",
    "otro",
]


class LineValuation(StrictSchemaModel):
    """Output de la IA para una línea de valoración (V3).

    V3 añade el concepto de LÍNEA SINTÉTICA: el LLM puede emitir líneas
    adicionales a las del albarán para representar modificadores
    implícitos (año, consistencia, árido, aditivo, gestión de residuos,
    exceso de tiempo, carga incompleta). Estas líneas sintéticas llevan:
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
            "aditivo, gestión de residuos, exceso de tiempo, carga "
            "incompleta)."
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
            "vs contrato 2024', 'retraso 33 min sobre límite 10:47', "
            "'vertido 4 m3 sobre mínimo 6 m3 del contrato')."
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

    cantidad_override: Optional[float] = Field(
        default=None,
        description=(
            "Solo para sintéticas con cantidad calculada por el LLM:\n"
            "  * modifier_source='tiempo_exceso' → minutos de exceso "
            "calculados a partir de contexto_linea.notas_tiempo (M6).\n"
            "  * modifier_source='carga_incompleta' → m³ de diferencia "
            "hasta el mínimo del contrato (M7).\n"
            "Puede ser 0. Null para todas las demás sintéticas (heredan "
            "cantidad del parent) y para líneas 'from_albaran' (tienen "
            "cantidad propia en el albarán)."
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
            "'incremento_tiempo' | 'incremento_carga_incompleta' | "
            "'incremento_otro'."
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
    contenedor_m3: Optional[float] = Field(
        default=None,
        description=(
            "SOLO residuos (contexto_linea.tipo_familia='residuos'): m³ "
            "por contenedor del contrato para la línea de CONTENEDOR que "
            "ELIGES. Regla: si el contrato tiene un contenedor cuyo tamaño "
            "coincide con los m³ del albarán, usa ese; si no, el contenedor "
            "por defecto del contrato. Otro servicio calcula "
            "num_contenedores = ceil(volumen_m3 / contenedor_m3). Null si "
            "no es residuos o no hay contenedor en el contrato."
        ),
    )
    razon_corta: str = Field(
        description=(
            "Explicación corta del matching hecho, en español, para "
            "auditoría. Para líneas sintéticas: si el LLM la omite, "
            "se rellena automáticamente con modifier_reason o "
            "descripcion_linea (validador defensivo, ver "
            "_backfill_razon_corta_for_synthetic)."
        ),
    )

    # ----------------------------------------------------------------- #
    # Fix defensivo (Tanda razon_corta — abr 2026):
    #
    # El prompt V3.x lista los campos obligatorios de cada sintética
    # (paso 7) pero olvidaba 'razon_corta'. Como consecuencia el LLM
    # devolvía sintéticas sin ese campo y Pydantic rompía con 400:
    #     "lineas.N.razon_corta - Field required".
    #
    # En lugar de hacer 'razon_corta' Optional (que degradaría la
    # auditoría de las líneas reales 'from_albaran'), interceptamos
    # en mode='before' y SOLO para sintéticas copiamos
    # modifier_reason o descripcion_linea como fallback.
    #
    # Las líneas 'from_albaran' no se tocan: si no traen razon_corta,
    # siguen rompiendo (es un bug real del LLM, no un desalineamiento
    # prompt/schema).
    #
    # Esto es defensa en profundidad: aunque el prompt YAML se haya
    # corregido en paralelo (cuádruple verificación), si en el futuro
    # algún proveedor de LLM se desvía, el sistema sigue funcionando.
    # ----------------------------------------------------------------- #

    @model_validator(mode="before")
    @classmethod
    def _backfill_razon_corta_for_synthetic(cls, data: Any) -> Any:
        if isinstance(data, dict):
            line_kind = data.get("line_kind")
            razon_corta = data.get("razon_corta")
            if line_kind == "synthetic_modifier" and not razon_corta:
                fallback = (
                    data.get("modifier_reason")
                    or data.get("descripcion_linea")
                    or "línea sintética sin razón explícita"
                )
                data["razon_corta"] = fallback
        return data

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
